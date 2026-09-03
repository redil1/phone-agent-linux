package com.phoneagent.gateway;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.IOException;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Persistent authenticated control channel, independent from media backpressure. */
public final class ProtocolControlServer {
    public static final int PORT = 8768;
    private static final String TAG = "PhoneAgentProtocolControl";
    private static final int COMMAND_CACHE_SIZE = 256;
    private static final String REPLAY_PREFS = "authenticated_command_replay";
    private static final String REPLAY_JOURNAL = "journal";

    private static final Map<String, CachedResponse> commandCache =
            new LinkedHashMap<String, CachedResponse>(COMMAND_CACHE_SIZE + 1, 0.75f, true) {
                @Override
                protected boolean removeEldestEntry(Map.Entry<String, CachedResponse> eldest) {
                    return size() > COMMAND_CACHE_SIZE;
                }
            };

    private static volatile boolean running;
    private static Context appContext;
    private static ServerSocket serverSocket;
    private static Thread acceptThread;
    private static ExecutorService workers;

    private ProtocolControlServer() {}

    public static synchronized void start(Context context) {
        if (running) return;
        running = true;
        appContext = context.getApplicationContext();
        loadReplayJournal();
        workers = Executors.newFixedThreadPool(2);
        acceptThread = new Thread(ProtocolControlServer::acceptLoop, "gateway-protocol-control");
        acceptThread.setDaemon(true);
        acceptThread.start();
    }

    private static void acceptLoop() {
        try {
            ServerSocket server = new ServerSocket();
            server.setReuseAddress(true);
            server.bind(new InetSocketAddress(InetAddress.getLoopbackAddress(), PORT), 4);
            serverSocket = server;
            Log.i(TAG, "Authenticated control server listening on 127.0.0.1:" + PORT);
            while (running) {
                try {
                    Socket client = server.accept();
                    client.setTcpNoDelay(true);
                    client.setSoTimeout(15000);
                    ExecutorService pool = workers;
                    if (pool != null) pool.execute(() -> handleClient(client));
                } catch (IOException error) {
                    if (running) Log.e(TAG, "Control accept failed", error);
                }
            }
        } catch (IOException error) {
            if (running) Log.e(TAG, "Control server failed", error);
        } finally {
            running = false;
            closeServer();
        }
    }

    private static void handleClient(Socket client) {
        try (Socket ignored = client;
             BufferedInputStream input = new BufferedInputStream(client.getInputStream());
             BufferedOutputStream output = new BufferedOutputStream(client.getOutputStream())) {
            byte[] key = LinkKeyStore.requireKey(appContext);
            LinkSessionRegistry.Binding binding = LinkSessionRegistry.handshake(
                    input, output, key, "control"
            );
            client.setSoTimeout(0);
            while (running && LinkSessionRegistry.isCurrent(binding)) {
                ProtocolCodec.Frame request = ProtocolCodec.read(input, key);
                if (request == null) return;
                CachedResponse response = process(request, binding);
                ProtocolCodec.write(
                        output,
                        ProtocolCodec.jsonFrame(
                                response.kind,
                                ProtocolCodec.DIRECTION_PHONE_TO_MAC,
                                (request.flags & ProtocolCodec.FLAG_URGENT),
                                request.callId,
                                DigitalAudioBridge.getGeneration(),
                                request.sequence,
                                response.body
                        ),
                        key
                );
            }
        } catch (Exception error) {
            if (running) Log.w(TAG, "Authenticated control client ended: " + error.getMessage());
        }
    }

    private static CachedResponse process(ProtocolCodec.Frame request,
                                          LinkSessionRegistry.Binding binding) {
        try {
            if (request.kind != ProtocolCodec.KIND_CONTROL
                    || request.direction != ProtocolCodec.DIRECTION_MAC_TO_PHONE
                    || !request.callId.equals(binding.callId)) {
                return error("invalid control frame identity", "");
            }
            JSONObject envelope = request.json();
            String commandId = envelope.optString("command_id");
            String epoch = envelope.optString("link_epoch");
            if (commandId.isEmpty() || !LinkSessionRegistry.matches(request.callId, epoch)) {
                return error("missing command_id or stale link epoch", commandId);
            }
            try {
                UUID.fromString(commandId);
            } catch (IllegalArgumentException invalid) {
                return error("command_id must be a UUID", commandId);
            }

            synchronized (commandCache) {
                String requestSignature = typeAndPayloadSignature(envelope);
                CachedResponse cached = commandCache.get(commandId);
                if (cached != null) {
                    if (!cached.requestSignature.equals(requestSignature)) {
                        return error("command_id was reused for a different operation", commandId);
                    }
                    return cached;
                }

                // Commit a conservative marker before performing any telephony
                // mutation. If the process dies after the side effect but before
                // its final response is persisted, a retry returns this marker
                // instead of executing the operation a second time.
                CachedResponse pending = uncertain(commandId, requestSignature);
                commandCache.put(commandId, pending);
                if (!persistReplayJournal()) {
                    commandCache.remove(commandId);
                    return error("could not persist command replay marker", commandId);
                }

                CachedResponse executed = execute(
                        envelope.optString("type"),
                        commandId,
                        envelope.optJSONObject("payload") == null
                                ? new JSONObject() : envelope.optJSONObject("payload")
                );
                CachedResponse response = executed.withRequestSignature(requestSignature);
                commandCache.put(commandId, response);
                if (!persistReplayJournal()) {
                    Log.e(TAG, "Command completed but its final replay result was not persisted");
                }
                return response;
            }
        } catch (Exception error) {
            return error(error.getClass().getSimpleName() + ": " + error.getMessage(), "");
        }
    }

    private static CachedResponse execute(String type, String commandId, JSONObject payload)
            throws Exception {
        if ("gateway.health".equals(type)) {
            JSONObject result = base(commandId);
            result.put("gateway", "ready");
            result.put("dialer_role", GatewayService.isDialerRoleHeld(appContext));
            result.put("apk_source_sha256", BuildProvenance.ANDROID_SOURCE_SHA256);
            result.put(
                    "remote_link_protocol_version",
                    BuildProvenance.REMOTE_LINK_PROTOCOL_VERSION
            );
            result.put(
                    "remote_link_negotiated_version",
                    GatewayService.remoteLinkProtocolVersion()
            );
            result.put("audio", DigitalAudioBridge.getStatusJson());
            return ok(result);
        }
        if ("call.status".equals(type)) return ok(callStatus(commandId));
        if ("audio.status".equals(type)) {
            JSONObject result = base(commandId);
            result.put("audio", DigitalAudioBridge.getStatusJson());
            return ok(result);
        }
        if ("audio.flush".equals(type)) {
            long requested = payload.optLong("next_generation", 0);
            if (requested < 1) return error("next_generation must be >= 1", commandId);
            long generation = DigitalAudioBridge.flushOutput(requested);
            JSONObject result = base(commandId);
            result.put("action", "audio_flushed");
            result.put("generation", generation);
            result.put("last_accepted_sequence", DigitalAudioBridge.getLastAcceptedSequence());
            result.put("last_rendered_sequence", DigitalAudioBridge.getLastRenderedSequence());
            return ok(result);
        }
        if ("audio.route".equals(type)) {
            // Which audio route to bridge: the modem, or an app's VoIP call.
            // Cellular is the service default, so a client that never sends this
            // behaves exactly as it did before the route existed.
            String route = payload.optString("route");
            if (!"voip".equals(route) && !"cellular".equals(route)) {
                return error("route must be 'cellular' or 'voip'", commandId);
            }
            DigitalAudioBridge.setVoipMode("voip".equals(route));
            JSONObject result = base(commandId);
            result.put("action", "audio_route_set");
            result.put("route", route);
            return ok(result);
        }
        if ("call.dial".equals(type)) {
            String number = payload.optString("number");
            return action(CallManager.placeCall(appContext, number), commandId, "dialing");
        }
        if ("call.answer".equals(type)) {
            return action(CallManager.answerCall(), commandId, "answered");
        }
        if ("call.reject".equals(type)) {
            return action(CallManager.rejectCall(), commandId, "rejected");
        }
        if ("call.hangup".equals(type)) {
            return action(CallManager.hangupCall(), commandId, "hung_up");
        }
        if ("dtmf.send".equals(type)) {
            String digit = payload.optString("digit");
            if (digit.length() != 1 || "0123456789*#".indexOf(digit.charAt(0)) < 0) {
                return error("DTMF digit must be one of 0-9, *, #", commandId);
            }
            return action(CallManager.sendDtmf(digit.charAt(0)), commandId, "dtmf_sent");
        }
        return error("unsupported command type: " + type, commandId);
    }

    private static CachedResponse action(boolean success, String commandId, String action)
            throws Exception {
        JSONObject result = base(commandId);
        result.put("action", action);
        if (!success) {
            result.put("status", "error");
            result.put("message", CallManager.getLastError());
            return new CachedResponse(ProtocolCodec.KIND_ERROR, result);
        }
        return ok(result);
    }

    private static JSONObject callStatus(String commandId) throws Exception {
        JSONObject result = base(commandId);
        result.put("state", CallManager.getCallState());
        result.put("state_code", CallManager.getCallStateCode());
        result.put("incoming_number", CallManager.getActiveCallerNumber());
        return result;
    }

    private static JSONObject base(String commandId) throws Exception {
        JSONObject result = new JSONObject();
        result.put("type", "command.ack");
        result.put("status", "ok");
        result.put("command_id", commandId);
        result.put("generation", DigitalAudioBridge.getGeneration());
        result.put("link_epoch", LinkSessionRegistry.activeEpoch());
        return result;
    }

    private static CachedResponse ok(JSONObject body) {
        return new CachedResponse(ProtocolCodec.KIND_ACK, body);
    }

    private static CachedResponse error(String message, String commandId) {
        try {
            JSONObject body = base(commandId);
            body.put("type", "error");
            body.put("status", "error");
            body.put("message", message == null ? "unknown error" : message);
            return new CachedResponse(ProtocolCodec.KIND_ERROR, body);
        } catch (Exception impossible) {
            throw new IllegalStateException(impossible);
        }
    }

    private static CachedResponse uncertain(String commandId, String requestSignature) {
        CachedResponse response = error(
                "command outcome is uncertain after process restart; refusing replay",
                commandId
        );
        return response.withRequestSignature(requestSignature);
    }

    private static String typeAndPayloadSignature(JSONObject envelope) {
        JSONObject payload = envelope.optJSONObject("payload");
        return envelope.optString("type") + "\n"
                + (payload == null ? "{}" : payload.toString());
    }

    private static void loadReplayJournal() {
        synchronized (commandCache) {
            commandCache.clear();
            String encoded = appContext.getSharedPreferences(REPLAY_PREFS, Context.MODE_PRIVATE)
                    .getString(REPLAY_JOURNAL, "");
            if (encoded == null || encoded.isEmpty()) return;
            try {
                JSONArray records = new JSONArray(encoded);
                int start = Math.max(0, records.length() - COMMAND_CACHE_SIZE);
                for (int index = start; index < records.length(); index++) {
                    JSONObject record = records.getJSONObject(index);
                    commandCache.put(
                            record.getString("command_id"),
                            new CachedResponse(
                                    record.getInt("kind"),
                                    record.getJSONObject("body"),
                                    record.getString("request_signature")
                            )
                    );
                }
            } catch (Exception error) {
                commandCache.clear();
                Log.e(TAG, "Ignoring corrupt command replay journal", error);
            }
        }
    }

    private static boolean persistReplayJournal() {
        JSONArray records = new JSONArray();
        try {
            for (Map.Entry<String, CachedResponse> entry : commandCache.entrySet()) {
                JSONObject record = new JSONObject();
                record.put("command_id", entry.getKey());
                record.put("kind", entry.getValue().kind);
                record.put("body", entry.getValue().body);
                record.put("request_signature", entry.getValue().requestSignature);
                records.put(record);
            }
            SharedPreferences preferences = appContext.getSharedPreferences(
                    REPLAY_PREFS, Context.MODE_PRIVATE
            );
            return preferences.edit().putString(REPLAY_JOURNAL, records.toString()).commit();
        } catch (Exception error) {
            Log.e(TAG, "Could not persist command replay journal", error);
            return false;
        }
    }

    public static synchronized void stop() {
        running = false;
        closeServer();
        ExecutorService pool = workers;
        workers = null;
        if (pool != null) pool.shutdownNow();
        synchronized (commandCache) {
            commandCache.clear();
        }
    }

    private static void closeServer() {
        ServerSocket server = serverSocket;
        serverSocket = null;
        if (server != null) {
            try {
                server.close();
            } catch (IOException ignored) {}
        }
    }

    public static boolean isRunning() {
        return running;
    }

    private static final class CachedResponse {
        final int kind;
        final JSONObject body;
        final String requestSignature;

        CachedResponse(int kind, JSONObject body) {
            this(kind, body, "");
        }

        CachedResponse(int kind, JSONObject body, String requestSignature) {
            this.kind = kind;
            this.body = body;
            this.requestSignature = requestSignature;
        }

        CachedResponse withRequestSignature(String signature) {
            return new CachedResponse(kind, body, signature);
        }
    }
}
