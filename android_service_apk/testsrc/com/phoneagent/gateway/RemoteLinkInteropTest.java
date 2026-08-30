package com.phoneagent.gateway;

import java.lang.reflect.Method;
import java.nio.ByteBuffer;
import java.util.Arrays;

/**
 * Golden-vector check shared with tests/test_remote_link.py.
 *
 * <p>The phone frames a tunnel message and the runtime verifies it. A single
 * byte-order or field-width disagreement would authenticate on neither side and
 * strand every call, so the two encoders are pinned to the same bytes here.
 */
public final class RemoteLinkInteropTest {

    private static final String GOLDEN =
            "5048524c010400000007223f0000000401020304"
            + "e24fffdd48954123b5a64020616c11815fb507801ec383a355ce517364784c93";

    private RemoteLinkInteropTest() {}

    public static void main(String[] arguments) throws Exception {
        byte[] key = new byte[32];
        for (int index = 0; index < key.length; index++) key[index] = (byte) index;

        RemoteLinkService service = new RemoteLinkService("127.0.0.1", 1, key);
        Method sign = RemoteLinkService.class.getDeclaredMethod("sign", byte[].class);
        sign.setAccessible(true);

        byte[] payload = new byte[] {1, 2, 3, 4};
        ByteBuffer header = ByteBuffer.allocate(16);
        header.put(new byte[] {'P', 'H', 'R', 'L'});
        header.put((byte) 1);   // version
        header.put((byte) 4);   // TYPE_DATA
        header.putInt(7);       // stream id
        header.putShort((short) 8767);
        header.putInt(payload.length);

        byte[] body = new byte[16 + payload.length];
        System.arraycopy(header.array(), 0, body, 0, 16);
        System.arraycopy(payload, 0, body, 16, payload.length);
        byte[] tag = (byte[]) sign.invoke(service, (Object) body);

        byte[] whole = new byte[body.length + tag.length];
        System.arraycopy(body, 0, whole, 0, body.length);
        System.arraycopy(tag, 0, whole, body.length, tag.length);

        String encoded = toHex(whole);
        if (!encoded.equals(GOLDEN)) {
            throw new AssertionError(
                    "remote link framing diverged from the runtime\n  java  : "
                            + encoded + "\n  python: " + GOLDEN);
        }
        if (!Arrays.equals(Arrays.copyOf(whole, 4), new byte[] {'P', 'H', 'R', 'L'})) {
            throw new AssertionError("magic is wrong");
        }
        System.out.println("remote-link-interop-ok");
    }

    private static String toHex(byte[] data) {
        StringBuilder text = new StringBuilder(data.length * 2);
        for (byte value : data) text.append(String.format("%02x", value));
        return text.toString();
    }
}
