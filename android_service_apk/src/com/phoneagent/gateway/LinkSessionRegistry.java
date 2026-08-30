package com.phoneagent.gateway;

import org.json.JSONObject;

import java.io.InputStream;
import java.io.OutputStream;
import java.util.UUID;

/** Binds authenticated sockets to one call and one reconnect epoch. */
public final class LinkSessionRegistry {
    private static UUID activeCallId;
    private static UUID activeEpoch;

    private LinkSessionRegistry() {}

    public static final class Binding {
        public final UUID callId;
        public final UUID epoch;
        public final String channel;

        Binding(UUID callId, UUID epoch, String channel) {
            this.callId = callId;
            this.epoch = epoch;
            this.channel = channel;
        }
    }

    public static Binding handshake(InputStream input, OutputStream output, byte[] key,
                                    String expectedChannel) throws Exception {
        ProtocolCodec.Frame hello = ProtocolCodec.read(input, key);
        if (hello == null
                || hello.kind != ProtocolCodec.KIND_CONTROL
                || hello.direction != ProtocolCodec.DIRECTION_BIDIRECTIONAL) {
            throw new IllegalStateException("first frame must be gateway.hello");
        }
        JSONObject body = hello.json();
        if (!"gateway.hello".equals(body.optString("type"))) {
            throw new IllegalStateException("first frame must be gateway.hello");
        }
        String channel = body.optString("channel");
        if (!expectedChannel.equals(channel)) {
            throw new IllegalStateException("handshake channel mismatch");
        }
        UUID epoch = UUID.fromString(body.getString("link_epoch"));
        Binding binding = establish(hello.callId, epoch, channel, hello.generation);

        JSONObject ready = new JSONObject();
        ready.put("type", "gateway.ready");
        ready.put("status", "ok");
        ready.put("link_epoch", epoch.toString());
        ready.put("channel", channel);
        ready.put("generation", DigitalAudioBridge.getGeneration());
        ProtocolCodec.write(
                output,
                ProtocolCodec.jsonFrame(
                        ProtocolCodec.KIND_ACK,
                        ProtocolCodec.DIRECTION_PHONE_TO_MAC,
                        ProtocolCodec.FLAG_URGENT,
                        hello.callId,
                        DigitalAudioBridge.getGeneration(),
                        hello.sequence,
                        ready
                ),
                key
        );
        return binding;
    }

    private static synchronized Binding establish(UUID callId, UUID epoch, String channel,
                                                  long requestedGeneration) {
        if (activeEpoch == null || !activeEpoch.equals(epoch)) {
            activeEpoch = epoch;
            activeCallId = callId;
            DigitalAudioBridge.resynchronizeGeneration(requestedGeneration);
        } else if (!activeCallId.equals(callId)) {
            throw new IllegalStateException("link epoch is already bound to another call");
        }
        return new Binding(callId, epoch, channel);
    }

    public static synchronized boolean isCurrent(Binding binding) {
        return binding != null
                && binding.callId.equals(activeCallId)
                && binding.epoch.equals(activeEpoch);
    }

    public static synchronized boolean matches(UUID callId, String epoch) {
        return activeCallId != null
                && activeEpoch != null
                && activeCallId.equals(callId)
                && activeEpoch.toString().equals(epoch);
    }

    public static synchronized String activeEpoch() {
        return activeEpoch == null ? "" : activeEpoch.toString();
    }
}
