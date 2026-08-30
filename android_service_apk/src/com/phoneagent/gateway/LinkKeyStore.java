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

    /**
     * Replace the shared key from a pairing scan.
     *
     * <p>Written to a temporary file and moved into place: a half-written key
     * authenticates nothing and would break the tunnel and the USB media path
     * at once, with no visible cause.
     */
    public static void storeKey(Context context, byte[] key) throws IOException {
        if (key == null || key.length < 16) {
            throw new IOException("a link key must be at least 16 bytes");
        }
        java.io.File target = new java.io.File(context.getFilesDir(), FILE_NAME);
        java.io.File temporary = new java.io.File(context.getFilesDir(), FILE_NAME + ".tmp");
        try (java.io.FileOutputStream stream = new java.io.FileOutputStream(temporary)) {
            stream.write(key);
            stream.flush();
        }
        if (!temporary.renameTo(target)) {
            temporary.delete();
            throw new IOException("could not replace the link key");
        }
        target.setReadable(false, false);
        target.setReadable(true, true);
        target.setWritable(false, false);
        target.setWritable(true, true);
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
