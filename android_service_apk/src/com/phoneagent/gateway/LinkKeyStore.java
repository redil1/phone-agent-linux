package com.phoneagent.gateway;

import android.content.Context;

import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;

/** Loads the root-provisioned shared link key from app-private storage. */
public final class LinkKeyStore {
    public static final String FILE_NAME = "link.key";

    private LinkKeyStore() {}

    public static byte[] requireKey(Context context) throws IOException {
        File file = new File(context.getFilesDir(), FILE_NAME);
        long length = file.length();
        if (!file.isFile() || length < 32 || length > 4096) {
            throw new IOException("production link key is not provisioned");
        }
        byte[] key = new byte[(int) length];
        try (FileInputStream input = new FileInputStream(file)) {
            int offset = 0;
            while (offset < key.length) {
                int count = input.read(key, offset, key.length - offset);
                if (count < 0) throw new IOException("link key was truncated");
                offset += count;
            }
        }
        return key;
    }

    public static boolean isProvisioned(Context context) {
        try {
            requireKey(context);
            return true;
        } catch (IOException ignored) {
            return false;
        }
    }
}
