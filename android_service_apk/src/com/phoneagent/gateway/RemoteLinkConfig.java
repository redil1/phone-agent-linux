package com.phoneagent.gateway;

import android.content.Context;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;

/** Where the tunnel settings live, shared by the scanner and the manual screen. */
public final class RemoteLinkConfig {

    public static final String FILE_NAME = "remote-link.json";

    private RemoteLinkConfig() {}

    public static void write(Context context, boolean enabled, String host, int port)
            throws Exception {
        JSONObject payload = new JSONObject();
        payload.put("enabled", enabled);
        payload.put("host", host);
        payload.put("port", port);
        File config = new File(context.getFilesDir(), FILE_NAME);
        try (FileOutputStream stream = new FileOutputStream(config)) {
            stream.write(payload.toString().getBytes(StandardCharsets.UTF_8));
        }
    }

    public static JSONObject read(Context context) {
        try {
            File config = new File(context.getFilesDir(), FILE_NAME);
            if (!config.isFile()) return null;
            return new JSONObject(
                    new String(Files.readAllBytes(config.toPath()), StandardCharsets.UTF_8));
        } catch (Exception ignored) {
            return null;
        }
    }
}
