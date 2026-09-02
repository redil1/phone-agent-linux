package com.phoneagent.gateway;

import org.json.JSONObject;

import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.util.UUID;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/** Exact Java implementation of ai_bridge/media_protocol.py protocol version 1. */
public final class ProtocolCodec {
    public static final int VERSION = 1;
    public static final int KIND_AUDIO = 1;
    public static final int KIND_CONTROL = 2;
    public static final int KIND_ACK = 3;
    public static final int KIND_ERROR = 4;
    public static final int KIND_METRICS = 5;

    public static final int DIRECTION_PHONE_TO_MAC = 1;
    public static final int DIRECTION_MAC_TO_PHONE = 2;
    public static final int DIRECTION_BIDIRECTIONAL = 3;

    public static final int FLAG_AUTHENTICATED = 1;
    public static final int FLAG_URGENT = 1 << 1;
    public static final int FLAG_END_OF_STREAM = 1 << 2;

    private static final byte[] MAGIC = new byte[] {'P', 'H', 'A', 'G'};
    private static final int HEADER_BYTES = 58;
    private static final int AUTH_TAG_BYTES = 32;
    private static final int MAX_PAYLOAD_BYTES = 256 * 1024;

    private ProtocolCodec() {}

    public static final class Frame {
        public final int kind;
        public final int direction;
        public final int flags;
        public final UUID callId;
        public final long generation;
        public final long sequence;
        public final long monotonicNs;
        public final int sampleRate;
        public final int channels;
        public final int sampleWidth;
        public final byte[] payload;

        public Frame(int kind, int direction, int flags, UUID callId, long generation,
                     long sequence, long monotonicNs, int sampleRate, int channels,
                     int sampleWidth, byte[] payload) {
            if (callId == null) throw new IllegalArgumentException("callId is required");
            if (generation < 1) throw new IllegalArgumentException("generation must be >= 1");
            if (sequence < 0 || monotonicNs < 0) {
                throw new IllegalArgumentException("sequence and timestamp must be non-negative");
            }
            if (payload == null || payload.length > MAX_PAYLOAD_BYTES) {
                throw new IllegalArgumentException("invalid payload length");
            }
            if (kind == KIND_AUDIO) {
                if (sampleRate <= 0 || (channels != 1 && channels != 2)
                        || (sampleWidth != 1 && sampleWidth != 2 && sampleWidth != 4)
                        || payload.length % (channels * sampleWidth) != 0) {
                    throw new IllegalArgumentException("invalid PCM metadata");
                }
            } else if (sampleRate != 0 || channels != 0 || sampleWidth != 0) {
                throw new IllegalArgumentException("non-audio frame carries PCM metadata");
            }
            this.kind = kind;
            this.direction = direction;
            this.flags = flags;
            this.callId = callId;
            this.generation = generation;
            this.sequence = sequence;
            this.monotonicNs = monotonicNs;
            this.sampleRate = sampleRate;
            this.channels = channels;
            this.sampleWidth = sampleWidth;
            this.payload = payload;
        }

        public JSONObject json() throws Exception {
            if (kind == KIND_AUDIO) throw new IOException("audio payload is not JSON");
            return new JSONObject(new String(payload, StandardCharsets.UTF_8));
        }
    }

    public static Frame jsonFrame(int kind, int direction, int flags, UUID callId,
                                  long generation, long sequence, JSONObject body) {
        return new Frame(
                kind,
                direction,
                flags,
                callId,
                generation,
                sequence,
                System.nanoTime(),
                0,
                0,
                0,
                body.toString().getBytes(StandardCharsets.UTF_8)
        );
    }

    public static Frame read(InputStream input, byte[] key) throws Exception {
        requireKey(key);
        byte[] header = readExact(input, HEADER_BYTES, true);
        if (header == null) return null;

        ByteBuffer values = ByteBuffer.wrap(header).order(ByteOrder.BIG_ENDIAN);
        for (byte expected : MAGIC) {
            if (values.get() != expected) throw new IOException("invalid frame magic");
        }
        int version = unsigned(values.get());
        int kind = unsigned(values.get());
        int direction = unsigned(values.get());
        int flags = unsigned(values.get());
        long most = values.getLong();
        long least = values.getLong();
        long generation = values.getLong();
        long sequence = values.getLong();
        long monotonicNs = values.getLong();
        int sampleRate = values.getInt();
        int channels = unsigned(values.get());
        int sampleWidth = unsigned(values.get());
        int payloadLength = values.getInt();

        if (version != VERSION) throw new IOException("unsupported protocol version " + version);
        if ((flags & FLAG_AUTHENTICATED) == 0) throw new IOException("unauthenticated frame rejected");
        if (payloadLength < 0 || payloadLength > MAX_PAYLOAD_BYTES) {
            throw new IOException("invalid payload length " + payloadLength);
        }
        byte[] payload = readExact(input, payloadLength, false);
        byte[] receivedTag = readExact(input, AUTH_TAG_BYTES, false);
        byte[] signed = new byte[header.length + payload.length];
        System.arraycopy(header, 0, signed, 0, header.length);
        System.arraycopy(payload, 0, signed, header.length, payload.length);
        if (!MessageDigest.isEqual(hmac(key, signed), receivedTag)) {
            throw new IOException("frame authentication failed");
        }
        return new Frame(
                kind,
                direction,
                flags,
                new UUID(most, least),
                generation,
                sequence,
                monotonicNs,
                sampleRate,
                channels,
                sampleWidth,
                payload
        );
    }

    /**
     * Write one authenticated frame to its channel.
     *
     * <p>Each gateway socket has exactly one writer. Synchronizing this method
     * globally therefore added no frame-safety, but it made unrelated sockets
     * contend: the continuous 50 Hz caller-audio writer could starve playout
     * acknowledgements on the separate uplink socket. Per-channel serialization
     * belongs to the owner of that channel (the remote tunnel uses a fair lock).
     */
    public static void write(OutputStream output, Frame frame, byte[] key)
            throws Exception {
        requireKey(key);
        int flags = frame.flags | FLAG_AUTHENTICATED;
        ByteBuffer header = ByteBuffer.allocate(HEADER_BYTES).order(ByteOrder.BIG_ENDIAN);
        header.put(MAGIC);
        header.put((byte) VERSION);
        header.put((byte) frame.kind);
        header.put((byte) frame.direction);
        header.put((byte) flags);
        header.putLong(frame.callId.getMostSignificantBits());
        header.putLong(frame.callId.getLeastSignificantBits());
        header.putLong(frame.generation);
        header.putLong(frame.sequence);
        header.putLong(frame.monotonicNs);
        header.putInt(frame.sampleRate);
        header.put((byte) frame.channels);
        header.put((byte) frame.sampleWidth);
        header.putInt(frame.payload.length);

        byte[] signed = new byte[HEADER_BYTES + frame.payload.length];
        System.arraycopy(header.array(), 0, signed, 0, HEADER_BYTES);
        System.arraycopy(frame.payload, 0, signed, HEADER_BYTES, frame.payload.length);
        output.write(signed);
        output.write(hmac(key, signed));
        output.flush();
    }

    private static byte[] readExact(InputStream input, int length, boolean allowCleanEof)
            throws IOException {
        byte[] value = new byte[length];
        int offset = 0;
        while (offset < length) {
            int count = input.read(value, offset, length - offset);
            if (count < 0) {
                if (allowCleanEof && offset == 0) return null;
                throw new EOFException("unexpected EOF in authenticated frame");
            }
            offset += count;
        }
        return value;
    }

    private static byte[] hmac(byte[] key, byte[] value)
            throws GeneralSecurityException {
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(key, "HmacSHA256"));
        return mac.doFinal(value);
    }

    private static void requireKey(byte[] key) {
        if (key == null || key.length < 32 || key.length > 4096) {
            throw new IllegalArgumentException("link key must contain 32-4096 bytes");
        }
    }

    private static int unsigned(byte value) {
        return value & 0xff;
    }
}
