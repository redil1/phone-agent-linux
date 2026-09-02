package com.phoneagent.gateway;

import android.util.Log;

import java.io.DataInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.Socket;
import java.nio.ByteBuffer;
import java.security.MessageDigest;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.locks.ReentrantLock;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * Carries this phone's gateway ports to a runtime that is not on the other end
 * of a USB cable.
 *
 * <p>The Mac reaches the gateway today through {@code adb forward}, which needs
 * a cable and a machine beside the handset. Everything that crosses it is plain
 * TCP, so the cable is a transport rather than a capability. This service dials
 * out to the runtime and multiplexes the four local ports over that one socket,
 * which also solves carrier NAT: the phone connects out, and nothing has to
 * reach in.
 *
 * <p>The gateway keeps binding to loopback. This client runs in the same
 * process and connects to those ports locally, so the handset still exposes
 * nothing to the network.
 */
public final class RemoteLinkService {

    private static final String TAG = "PhoneAgentRemoteLink";

    private static final byte[] MAGIC = new byte[] {'P', 'H', 'R', 'L'};
    private static final int VERSION = 1;
    private static final int HEADER_BYTES = 16; // magic4 + ver1 + type1 + stream4 + port2 + len4
    private static final int AUTH_TAG_BYTES = 32;
    private static final int MAX_PAYLOAD_BYTES = 64 * 1024;

    private static final int TYPE_HELLO = 1;
    private static final int TYPE_READY = 2;
    private static final int TYPE_OPEN = 3;
    private static final int TYPE_DATA = 4;
    private static final int TYPE_CLOSE = 5;
    private static final int TYPE_PING = 6;
    private static final int TYPE_PONG = 7;

    /** Only these may be tunnelled. A relay cannot ask for another local port. */
    private static final int[] ALLOWED_PORTS = {8765, 8766, 8767, 8768};

    private static final int RECONNECT_MIN_MS = 1_000;
    private static final int RECONNECT_MAX_MS = 30_000;
    private static final int SOCKET_TIMEOUT_MS = 45_000;

    private final String host;
    private final int port;
    private final byte[] key;
    private final AtomicBoolean running = new AtomicBoolean(false);
    private final Map<Integer, Socket> streams = new ConcurrentHashMap<>();

    private volatile Socket tunnel;
    private volatile OutputStream out;
    private volatile Thread worker;
    // The downlink media pump writes one caller-audio frame every 20 ms. A
    // Java monitor is not fair, so that hot thread can release and immediately
    // reacquire it while the uplink ACK and control pumps remain blocked. The
    // phone then renders speech locally but its ACKs never reach the runtime.
    // FIFO fairness bounds their wait to one already-running tunnel frame.
    private final ReentrantLock writeLock = new ReentrantLock(true);

    public RemoteLinkService(String host, int port, byte[] key) {
        this.host = host;
        this.port = port;
        this.key = key;
    }

    public boolean isConnected() {
        Socket socket = tunnel;
        return socket != null && socket.isConnected() && !socket.isClosed();
    }

    public void start() {
        if (!running.compareAndSet(false, true)) return;
        worker = new Thread(this::runForever, "remote-link");
        worker.setDaemon(true);
        worker.start();
    }

    public void stop() {
        running.set(false);
        closeTunnel();
        Thread thread = worker;
        if (thread != null) thread.interrupt();
    }

    /**
     * Reconnect for as long as the service is enabled.
     *
     * <p>A phone roams between wifi and mobile data mid-call and the socket
     * simply dies. Backing off rather than giving up is what makes the link
     * survive a network change without an operator touching it.
     */
    private void runForever() {
        int backoff = RECONNECT_MIN_MS;
        while (running.get()) {
            try {
                connectAndServe();
                backoff = RECONNECT_MIN_MS;
            } catch (Exception failure) {
                if (!running.get()) return;
                Log.w(TAG, "remote link dropped: " + failure);
            }
            closeTunnel();
            if (!running.get()) return;
            try {
                Thread.sleep(backoff);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
                return;
            }
            backoff = Math.min(backoff * 2, RECONNECT_MAX_MS);
        }
    }

    private void connectAndServe() throws Exception {
        Socket socket = new Socket();
        socket.connect(new java.net.InetSocketAddress(host, port), 15_000);
        socket.setTcpNoDelay(true);
        socket.setSoTimeout(SOCKET_TIMEOUT_MS);
        tunnel = socket;
        out = socket.getOutputStream();
        Log.i(TAG, "remote link connected to " + host + ":" + port);

        send(TYPE_HELLO, 0, 0, new byte[0]);
        DataInputStream in = new DataInputStream(socket.getInputStream());
        byte[] header = new byte[HEADER_BYTES];
        while (running.get()) {
            in.readFully(header);
            ByteBuffer view = ByteBuffer.wrap(header);
            byte[] magic = new byte[4];
            view.get(magic);
            if (!MessageDigest.isEqual(magic, MAGIC)) {
                throw new IOException("remote link frame had a bad magic");
            }
            int version = view.get() & 0xFF;
            if (version != VERSION) {
                throw new IOException("unsupported remote link version " + version);
            }
            int type = view.get() & 0xFF;
            int streamId = view.getInt();
            int framePort = view.getShort() & 0xFFFF;
            int length = view.getInt();
            if (length < 0 || length > MAX_PAYLOAD_BYTES) {
                throw new IOException("remote link frame exceeded the payload limit");
            }
            byte[] payload = new byte[length];
            in.readFully(payload);
            byte[] tag = new byte[AUTH_TAG_BYTES];
            in.readFully(tag);

            byte[] body = new byte[HEADER_BYTES + length];
            System.arraycopy(header, 0, body, 0, HEADER_BYTES);
            System.arraycopy(payload, 0, body, HEADER_BYTES, length);
            if (!MessageDigest.isEqual(sign(body), tag)) {
                // Authentication covers the header too, so a relay cannot
                // rewrite a port to reach some other service on this handset.
                throw new IOException("remote link frame failed authentication");
            }
            onFrame(type, streamId, framePort, payload);
        }
    }

    private void onFrame(int type, int streamId, int framePort, byte[] payload) throws IOException {
        switch (type) {
            case TYPE_READY:
                Log.i(TAG, "remote link accepted by the runtime");
                break;
            case TYPE_OPEN:
                openStream(streamId, framePort);
                break;
            case TYPE_DATA: {
                Socket local = streams.get(streamId);
                if (local == null) return;
                try {
                    local.getOutputStream().write(payload);
                    local.getOutputStream().flush();
                } catch (IOException failure) {
                    closeStream(streamId, true);
                }
                break;
            }
            case TYPE_CLOSE:
                closeStream(streamId, false);
                break;
            case TYPE_PING:
                // Echo the token so the runtime can measure round-trip time.
                send(TYPE_PONG, streamId, 0, new byte[0]);
                break;
            default:
                Log.w(TAG, "ignoring unknown remote link frame type " + type);
        }
    }

    private void openStream(int streamId, int framePort) throws IOException {
        if (!isAllowedPort(framePort)) {
            Log.w(TAG, "refusing a tunnel to non-gateway port " + framePort);
            send(TYPE_CLOSE, streamId, framePort, new byte[0]);
            return;
        }
        Socket local;
        try {
            local = new Socket(InetAddress.getLoopbackAddress(), framePort);
            local.setTcpNoDelay(true);
        } catch (IOException failure) {
            Log.w(TAG, "gateway port " + framePort + " refused a local connection");
            send(TYPE_CLOSE, streamId, framePort, new byte[0]);
            return;
        }
        streams.put(streamId, local);
        Thread pump = new Thread(() -> pumpLocal(streamId, framePort, local), "remote-link-" + streamId);
        pump.setDaemon(true);
        pump.start();
    }

    /** Read one local gateway connection and forward it to the runtime. */
    private void pumpLocal(int streamId, int framePort, Socket local) {
        byte[] buffer = new byte[16 * 1024];
        try (InputStream in = local.getInputStream()) {
            while (running.get()) {
                int read = in.read(buffer);
                if (read < 0) break;
                if (read == 0) continue;
                byte[] chunk = new byte[read];
                System.arraycopy(buffer, 0, chunk, 0, read);
                send(TYPE_DATA, streamId, framePort, chunk);
            }
        } catch (IOException ignored) {
            // A closed gateway socket is ordinary; the close below reports it.
        } finally {
            closeStream(streamId, true);
        }
    }

    private void closeStream(int streamId, boolean notifyRuntime) {
        Socket local = streams.remove(streamId);
        if (local == null) return;
        try {
            local.close();
        } catch (IOException ignored) {
        }
        if (notifyRuntime && isConnected()) {
            try {
                send(TYPE_CLOSE, streamId, 0, new byte[0]);
            } catch (IOException ignored) {
            }
        }
    }

    private void send(int type, int streamId, int framePort, byte[] payload) throws IOException {
        if (payload.length > MAX_PAYLOAD_BYTES) {
            throw new IOException("remote link payload exceeded the limit");
        }
        ByteBuffer header = ByteBuffer.allocate(HEADER_BYTES);
        header.put(MAGIC);
        header.put((byte) VERSION);
        header.put((byte) type);
        header.putInt(streamId);
        header.putShort((short) framePort);
        header.putInt(payload.length);

        byte[] body = new byte[HEADER_BYTES + payload.length];
        System.arraycopy(header.array(), 0, body, 0, HEADER_BYTES);
        System.arraycopy(payload, 0, body, HEADER_BYTES, payload.length);
        byte[] tag = sign(body);

        OutputStream stream = out;
        if (stream == null) throw new IOException("remote link is not connected");
        // One writer at a time: media pumps run on their own threads and an
        // interleaved frame would corrupt the stream for everything else.
        writeLock.lock();
        try {
            stream.write(body);
            stream.write(tag);
            stream.flush();
        } finally {
            writeLock.unlock();
        }
    }

    private byte[] sign(byte[] body) throws IOException {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(key, "HmacSHA256"));
            return mac.doFinal(body);
        } catch (Exception failure) {
            throw new IOException("could not sign a remote link frame", failure);
        }
    }

    private static boolean isAllowedPort(int candidate) {
        for (int allowed : ALLOWED_PORTS) {
            if (allowed == candidate) return true;
        }
        return false;
    }

    private void closeTunnel() {
        for (Integer streamId : streams.keySet()) {
            closeStream(streamId, false);
        }
        Socket socket = tunnel;
        tunnel = null;
        out = null;
        if (socket != null) {
            try {
                socket.close();
            } catch (IOException ignored) {
            }
        }
    }
}
