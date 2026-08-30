package com.phoneagent.gateway;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.util.Arrays;
import java.util.UUID;

/** Golden-vector check shared with tests/test_media_protocol.py. */
public final class ProtocolCodecInteropTest {
    private static final String GOLDEN =
            "504841470101020100112233445566778899aabbccddeeff0000000000000007"
            + "0000000000000013000000000001e24000003e8001020000000401020304d348"
            + "2ea78cd067be360fb3bb59d378f70fb6c839358fe4e787b02e4709c39154";

    private ProtocolCodecInteropTest() {}

    public static void main(String[] arguments) throws Exception {
        byte[] key = new byte[32];
        for (int index = 0; index < key.length; index++) key[index] = (byte) index;
        ProtocolCodec.Frame frame = new ProtocolCodec.Frame(
                ProtocolCodec.KIND_AUDIO,
                ProtocolCodec.DIRECTION_MAC_TO_PHONE,
                0,
                UUID.fromString("00112233-4455-6677-8899-aabbccddeeff"),
                7,
                19,
                123456,
                16000,
                1,
                2,
                new byte[] {1, 2, 3, 4}
        );
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        ProtocolCodec.write(output, frame, key);
        byte[] golden = decodeHex(GOLDEN);
        if (!Arrays.equals(output.toByteArray(), golden)) {
            throw new AssertionError("Java encoding differs from the Python golden vector");
        }

        ProtocolCodec.Frame decoded = ProtocolCodec.read(new ByteArrayInputStream(golden), key);
        if (decoded == null
                || decoded.kind != ProtocolCodec.KIND_AUDIO
                || decoded.direction != ProtocolCodec.DIRECTION_MAC_TO_PHONE
                || decoded.generation != 7
                || decoded.sequence != 19
                || decoded.monotonicNs != 123456
                || decoded.sampleRate != 16000
                || decoded.channels != 1
                || decoded.sampleWidth != 2
                || !decoded.callId.equals(frame.callId)
                || !Arrays.equals(decoded.payload, frame.payload)) {
            throw new AssertionError("Java could not decode the Python golden vector");
        }
        System.out.println("ProtocolCodec Java/Python golden vector passed");
    }

    private static byte[] decodeHex(String value) {
        byte[] result = new byte[value.length() / 2];
        for (int index = 0; index < result.length; index++) {
            result[index] = (byte) Integer.parseInt(value.substring(index * 2, index * 2 + 2), 16);
        }
        return result;
    }
}
