package com.phoneagent.gateway;

import android.util.Base64;

import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/**
 * The contents of a Studio pairing QR.
 *
 * <p>Pinned to ai_bridge/pairing.py: one scan has to carry the key, the address
 * and the port together, because a handset that is correctly keyed but pointed
 * at the wrong host fails exactly as silently as a mismatched key.
 */
public final class PairingPayload {

    private static final String SCHEME = "phoneagent-pair";
    private static final int VERSION = 1;

    public final byte[] key;
    public final String host;
    public final int port;

    private PairingPayload(byte[] key, String host, int port) {
        this.key = key;
        this.host = host;
        this.port = port;
    }

    public static PairingPayload parse(String text) throws Exception {
        String value = text == null ? "" : text.trim();
        if (!value.startsWith(SCHEME + ":")) {
            throw new IllegalArgumentException("not a PhoneAgent pairing code");
        }
        String encoded = value.substring(SCHEME.length() + 1);
        byte[] raw = Base64.decode(encoded, Base64.URL_SAFE | Base64.NO_PADDING | Base64.NO_WRAP);
        JSONObject body = new JSONObject(new String(raw, StandardCharsets.UTF_8));
        if (body.optInt("v", -1) != VERSION) {
            throw new IllegalArgumentException("unsupported pairing version");
        }
        byte[] key = Base64.decode(
                body.optString("k", ""), Base64.URL_SAFE | Base64.NO_PADDING | Base64.NO_WRAP);
        if (key.length < 16) throw new IllegalArgumentException("key too short");
        String host = body.optString("h", "").trim();
        if (host.isEmpty()) throw new IllegalArgumentException("no address");
        int port = body.optInt("p", 0);
        if (port < 1 || port > 65535) throw new IllegalArgumentException("invalid port");
        return new PairingPayload(key, host, port);
    }

    /** Shown next to Studio's, so an operator can see the two sides agree. */
    public String fingerprint() {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(key);
            StringBuilder text = new StringBuilder();
            for (int index = 0; index < 6; index++) {
                text.append(String.format("%02X", digest[index]));
            }
            return text.toString();
        } catch (Exception failure) {
            return "";
        }
    }
}
