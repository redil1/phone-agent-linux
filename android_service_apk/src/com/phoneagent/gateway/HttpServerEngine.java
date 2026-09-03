package com.phoneagent.gateway;

import android.content.Context;
import android.util.Log;

import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Android-safe loopback HTTP control server. */
public class HttpServerEngine {
    private static final String TAG = "PhoneAgentHttpServer";
    private static final int PORT = 8765;
    private static final int MAX_BODY_BYTES = 64 * 1024;

    private static volatile boolean running;
    private static ServerSocket serverSocket;
    private static Thread acceptThread;
    private static ExecutorService workers;

    public static synchronized void start(Context context) {
        if (running) return;
        running = true;
        workers = Executors.newFixedThreadPool(4);
        acceptThread = new Thread(() -> acceptLoop(context.getApplicationContext()), "gateway-http");
        acceptThread.setDaemon(true);
        acceptThread.start();
    }

    private static void acceptLoop(Context context) {
        try {
            ServerSocket socket = new ServerSocket();
            socket.setReuseAddress(true);
            socket.bind(new InetSocketAddress(InetAddress.getLoopbackAddress(), PORT), 16);
            serverSocket = socket;
            Log.i(TAG, "HTTP control server listening on 127.0.0.1:" + PORT);
            while (running) {
                try {
                    Socket client = socket.accept();
                    client.setSoTimeout(5000);
                    client.setTcpNoDelay(true);
                    ExecutorService pool = workers;
                    if (pool != null) pool.execute(() -> handleClient(context, client));
                } catch (IOException e) {
                    if (running) Log.e(TAG, "HTTP accept failed: " + e.getMessage(), e);
                }
            }
        } catch (IOException e) {
            if (running) Log.e(TAG, "HTTP server failed: " + e.getMessage(), e);
        } finally {
            running = false;
            closeServerSocket();
        }
    }

    private static void handleClient(Context context, Socket client) {
        try (Socket ignored = client;
             BufferedInputStream input = new BufferedInputStream(client.getInputStream());
             BufferedOutputStream output = new BufferedOutputStream(client.getOutputStream())) {
            try {
                HttpRequest request = readRequest(input);
                RouteResult result = route(context, request);
                writeResponse(output, result.statusCode, result.reason, result.body);
            } catch (RequestException e) {
                writeResponse(output, e.statusCode, e.reason, errorJson(e.getMessage()));
            } catch (Exception e) {
                Log.e(TAG, "HTTP request failed: " + e.getMessage(), e);
                writeResponse(output, 500, "Internal Server Error", errorJson(e.getMessage()));
            }
        } catch (Exception e) {
            Log.e(TAG, "HTTP client connection failed: " + e.getMessage(), e);
        }
    }

    private static HttpRequest readRequest(BufferedInputStream input) throws IOException, RequestException {
        String requestLine = readAsciiLine(input, 8192);
        if (requestLine == null || requestLine.trim().isEmpty()) {
            throw new RequestException(400, "Bad Request", "Missing request line");
        }
        String[] first = requestLine.split(" ", 3);
        if (first.length != 3) {
            throw new RequestException(400, "Bad Request", "Malformed request line");
        }

        Map<String, String> headers = new HashMap<>();
        while (true) {
            String line = readAsciiLine(input, 8192);
            if (line == null) throw new EOFException("Unexpected EOF in headers");
            if (line.isEmpty()) break;
            int separator = line.indexOf(':');
            if (separator > 0) {
                headers.put(line.substring(0, separator).trim().toLowerCase(),
                        line.substring(separator + 1).trim());
            }
        }

        int contentLength = 0;
        String rawLength = headers.get("content-length");
        if (rawLength != null && !rawLength.isEmpty()) {
            try {
                contentLength = Integer.parseInt(rawLength);
            } catch (NumberFormatException e) {
                throw new RequestException(400, "Bad Request", "Invalid Content-Length");
            }
        }
        if (contentLength < 0 || contentLength > MAX_BODY_BYTES) {
            throw new RequestException(413, "Payload Too Large", "Request body exceeds limit");
        }

        byte[] body = new byte[contentLength];
        int offset = 0;
        while (offset < contentLength) {
            int count = input.read(body, offset, contentLength - offset);
            if (count < 0) throw new EOFException("Unexpected EOF in request body");
            offset += count;
        }

        String target = first[1];
        int queryAt = target.indexOf('?');
        String path = queryAt >= 0 ? target.substring(0, queryAt) : target;
        String query = queryAt >= 0 ? target.substring(queryAt + 1) : "";
        return new HttpRequest(first[0].toUpperCase(), path, parseQuery(query),
                new String(body, StandardCharsets.UTF_8));
    }

    private static String readAsciiLine(BufferedInputStream input, int limit)
            throws IOException, RequestException {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        int previous = -1;
        while (bytes.size() <= limit) {
            int value = input.read();
            if (value < 0) return bytes.size() == 0 ? null : bytes.toString("US-ASCII");
            if (previous == '\r' && value == '\n') {
                byte[] raw = bytes.toByteArray();
                return new String(raw, 0, Math.max(0, raw.length - 1), StandardCharsets.US_ASCII);
            }
            bytes.write(value);
            previous = value;
        }
        throw new RequestException(431, "Request Header Fields Too Large", "HTTP line exceeds limit");
    }

    private static RouteResult route(Context context, HttpRequest request) throws Exception {
        if ("/health".equals(request.path)) {
            requireMethod(request, "GET");
            JSONObject result = baseStatus();
            result.put("gateway", "ready");
            result.put("dialer_role", GatewayService.isDialerRoleHeld(context));
            result.put("production_protocol_port", ProtocolControlServer.PORT);
            result.put("link_key_provisioned", LinkKeyStore.isProvisioned(context));
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
        if ("/call/status".equals(request.path)) {
            requireMethod(request, "GET");
            return ok(baseStatus());
        }
        if ("/audio/status".equals(request.path)) {
            requireMethod(request, "GET");
            return ok(DigitalAudioBridge.getStatusJson());
        }
        if (LinkKeyStore.isProvisioned(context)) {
            return new RouteResult(
                    426,
                    "Upgrade Required",
                    errorJson("Mutating operations require the authenticated PHAG v1 control channel")
            );
        }
        if ("/audio/flush".equals(request.path)) {
            requireMethod(request, "POST");
            long generation = DigitalAudioBridge.flushOutput();
            JSONObject result = new JSONObject();
            result.put("status", "ok");
            result.put("action", "audio_flushed");
            result.put("generation", generation);
            return ok(result);
        }
        if ("/audio/route".equals(request.path)) {
            requireMethod(request, "POST");
            // Which audio route to bridge: the modem, or an app's VoIP call.
            // Cellular is the default and is what an absent parameter means, so
            // an old client keeps behaving exactly as it did.
            String route = request.parameter("route");
            if (!"voip".equals(route) && !"cellular".equals(route)) {
                return badRequest("route must be 'cellular' or 'voip'");
            }
            DigitalAudioBridge.setVoipMode("voip".equals(route));
            JSONObject result = new JSONObject();
            result.put("status", "ok");
            result.put("route", route);
            return ok(result);
        }
        if ("/call/dial".equals(request.path)) {
            requireMethod(request, "POST");
            String number = request.parameter("number");
            if (number.isEmpty()) return badRequest("Missing number parameter");
            return actionResult(CallManager.placeCall(context, number), "dialing", "number", number);
        }
        if ("/call/answer".equals(request.path)) {
            requireMethod(request, "POST");
            return actionResult(CallManager.answerCall(), "answered", null, null);
        }
        if ("/call/reject".equals(request.path)) {
            requireMethod(request, "POST");
            return actionResult(CallManager.rejectCall(), "rejected", null, null);
        }
        if ("/call/hangup".equals(request.path)) {
            requireMethod(request, "POST");
            return actionResult(CallManager.hangupCall(), "hung_up", null, null);
        }
        if ("/call/dtmf".equals(request.path)) {
            requireMethod(request, "POST");
            String digit = request.parameter("digit");
            if (digit.length() != 1 || "0123456789*#".indexOf(digit.charAt(0)) < 0) {
                return badRequest("DTMF digit must be one of 0-9, *, #");
            }
            return actionResult(CallManager.sendDtmf(digit.charAt(0)), "dtmf_sent", "digit", digit);
        }
        return new RouteResult(404, "Not Found", errorJson("Endpoint not found: " + request.path));
    }

    private static JSONObject baseStatus() throws Exception {
        JSONObject result = new JSONObject();
        result.put("status", "ok");
        result.put("state", CallManager.getCallState());
        result.put("state_code", CallManager.getCallStateCode());
        result.put("incoming_number", CallManager.getActiveCallerNumber());
        return result;
    }

    private static RouteResult actionResult(boolean success, String action,
                                            String extraName, String extraValue) throws Exception {
        JSONObject result = new JSONObject();
        result.put("status", success ? "ok" : "error");
        result.put("action", action);
        if (extraName != null) result.put(extraName, extraValue);
        if (!success) result.put("message", CallManager.getLastError());
        return new RouteResult(success ? 200 : 409, success ? "OK" : "Conflict", result.toString());
    }

    private static void requireMethod(HttpRequest request, String expected) throws RequestException {
        if (!expected.equals(request.method)) {
            throw new RequestException(405, "Method Not Allowed", "Expected " + expected);
        }
    }

    private static RouteResult ok(JSONObject value) {
        return new RouteResult(200, "OK", value.toString());
    }

    private static RouteResult badRequest(String message) {
        return new RouteResult(400, "Bad Request", errorJson(message));
    }

    private static String errorJson(String message) {
        JSONObject result = new JSONObject();
        try {
            result.put("status", "error");
            result.put("message", message == null ? "Unknown error" : message);
        } catch (Exception ignored) {}
        return result.toString();
    }

    private static Map<String, String> parseQuery(String query) throws RequestException {
        Map<String, String> result = new HashMap<>();
        if (query == null || query.isEmpty()) return result;
        for (String pair : query.split("&")) {
            if (pair.isEmpty()) continue;
            int equals = pair.indexOf('=');
            String key = equals >= 0 ? pair.substring(0, equals) : pair;
            String value = equals >= 0 ? pair.substring(equals + 1) : "";
            try {
                result.put(URLDecoder.decode(key, "UTF-8"), URLDecoder.decode(value, "UTF-8"));
            } catch (Exception e) {
                throw new RequestException(400, "Bad Request", "Invalid URL encoding");
            }
        }
        return result;
    }

    private static void writeResponse(BufferedOutputStream output, int statusCode,
                                      String reason, String body) throws IOException {
        byte[] payload = body.getBytes(StandardCharsets.UTF_8);
        String headers = "HTTP/1.1 " + statusCode + " " + reason + "\r\n"
                + "Content-Type: application/json; charset=utf-8\r\n"
                + "Content-Length: " + payload.length + "\r\n"
                + "Connection: close\r\n"
                + "Cache-Control: no-store\r\n"
                + "Access-Control-Allow-Origin: *\r\n\r\n";
        output.write(headers.getBytes(StandardCharsets.US_ASCII));
        output.write(payload);
        output.flush();
    }

    public static synchronized void stop() {
        running = false;
        closeServerSocket();
        if (workers != null) {
            workers.shutdownNow();
            workers = null;
        }
        if (acceptThread != null) {
            acceptThread.interrupt();
            acceptThread = null;
        }
        Log.i(TAG, "HTTP control server stopped");
    }

    private static void closeServerSocket() {
        if (serverSocket != null) {
            try { serverSocket.close(); } catch (IOException ignored) {}
            serverSocket = null;
        }
    }

    private static final class HttpRequest {
        final String method;
        final String path;
        final Map<String, String> query;
        final String body;

        HttpRequest(String method, String path, Map<String, String> query, String body) {
            this.method = method;
            this.path = path;
            this.query = query;
            this.body = body;
        }

        String parameter(String name) {
            if (body != null && !body.trim().isEmpty()) {
                try {
                    String value = new JSONObject(body).optString(name, "");
                    if (!value.isEmpty()) return value;
                } catch (Exception ignored) {}
            }
            String value = query.get(name);
            return value == null ? "" : value;
        }
    }

    private static final class RouteResult {
        final int statusCode;
        final String reason;
        final String body;

        RouteResult(int statusCode, String reason, String body) {
            this.statusCode = statusCode;
            this.reason = reason;
            this.body = body;
        }
    }

    private static final class RequestException extends Exception {
        final int statusCode;
        final String reason;

        RequestException(int statusCode, String reason, String message) {
            super(message);
            this.statusCode = statusCode;
            this.reason = reason;
        }
    }
}
