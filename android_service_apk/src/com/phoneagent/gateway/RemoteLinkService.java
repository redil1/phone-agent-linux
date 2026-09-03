package com.phoneagent.gateway;

import android.util.Log;

import java.io.DataInputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
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
 * Carries this phone's loopback-only gateway ports to a remote runtime.
 *
 * <p>Protocol v2 keeps one lightweight coordinator connection and gives every
 * opened gateway stream its own WAN connection. A saturated capture stream can
 * therefore never hold the write lock needed by uplink playout ACKs or control
 * traffic. The coordinator initially offers v2 but accepts a v1 READY so an
 * older relay remains usable during a rolling upgrade.
 */
public final class RemoteLinkService {

    private static final String TAG = "PhoneAgentRemoteLink";

    private static final byte[] MAGIC = new byte[] {'P', 'H', 'R', 'L'};
    private static final int VERSION_V1 = 1;
    private static final int VERSION_V2 = 2;
    private static final int HEADER_BYTES = 16; // magic4 + ver1 + type1 + stream4 + port2 + len4
    private static final int AUTH_TAG_BYTES = 32;
    private static final int MAX_PAYLOAD_BYTES = 64 * 1024;
    private static final int V2_CHALLENGE_BYTES = 32;

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
    private static final int CONNECT_TIMEOUT_MS = 15_000;
    private static final int SOCKET_TIMEOUT_MS = 45_000;

    private final String host;
    private final int port;
    private final byte[] key;
    private final int[] allowedPorts;
    private final RelayConnector relayConnector;
    private final AtomicBoolean running = new AtomicBoolean(false);

    /** v1 multiplexes these local sockets over the coordinator connection. */
    private final Map<Integer, Socket> v1Streams = new ConcurrentHashMap<>();
    /** v2 owns one local socket and one relay socket per stream. */
    private final Map<Integer, V2Stream> v2Streams = new ConcurrentHashMap<>();

    private volatile Socket coordinator;
    private volatile OutputStream coordinatorOut;
    private volatile int negotiatedVersion;
    private volatile Thread worker;
    private final ReentrantLock coordinatorWriteLock = new ReentrantLock(true);

    public RemoteLinkService(String host, int port, byte[] key) {
        this(host, port, key, ALLOWED_PORTS);
    }

    /** Package-private seam used by the executable loopback protocol test. */
    RemoteLinkService(String host, int port, byte[] key, int[] allowedPorts) {
        this(host, port, key, allowedPorts, null);
    }

    /** Package-private connector seam for deterministic transport-failure tests. */
    RemoteLinkService(
            String host,
            int port,
            byte[] key,
            int[] allowedPorts,
            RelayConnector relayConnector) {
        this.host = host;
        this.port = port;
        this.key = key.clone();
        this.allowedPorts = allowedPorts.clone();
        this.relayConnector = relayConnector == null
                ? this::connectConfiguredRelay : relayConnector;
    }

    public boolean isConnected() {
        Socket socket = coordinator;
        return socket != null && socket.isConnected() && !socket.isClosed()
                && negotiatedVersion != 0;
    }

    public int getNegotiatedVersion() {
        return negotiatedVersion;
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

    /** Reconnect forever so a phone network change does not require an operator. */
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

    /** Establish the coordinator and negotiate v2, with explicit v1 fallback. */
    private void connectAndServe() throws Exception {
        DataInputStream in;
        // A TCP failure says nothing about the relay's protocol support. Keep
        // it outside the rejection handler so runForever retries v2 after the
        // ordinary reconnect backoff instead of permanently downgrading a
        // phone merely because the runtime was still starting.
        Socket v2Socket = connectRelay();
        try {
            in = openCoordinator(v2Socket, VERSION_V2, true);
        } catch (IOException v2Rejected) {
            // A real v1 relay rejects the unknown HELLO before it can return a
            // READY. Reconnect and speak v1 from the first byte; otherwise a
            // rolling APK-first upgrade would loop on v2 forever.
            closeCoordinator();
            if (!running.get()) throw v2Rejected;
            Log.i(TAG, "remote link v2 unavailable; reconnecting with v1");
            in = openCoordinator(VERSION_V1, false);
        }
        Log.i(TAG, "remote link accepted protocol v" + negotiatedVersion);

        while (running.get()) {
            Frame frame = readFrame(in, negotiatedVersion);
            if (negotiatedVersion == VERSION_V1) {
                onV1Frame(frame);
            } else {
                onV2CoordinatorFrame(frame);
            }
        }
    }

    private DataInputStream openCoordinator(int offeredVersion, boolean allowV1Ready)
            throws IOException {
        return openCoordinator(connectRelay(), offeredVersion, allowV1Ready);
    }

    private DataInputStream openCoordinator(
            Socket socket, int offeredVersion, boolean allowV1Ready) throws IOException {
        coordinator = socket;
        coordinatorOut = socket.getOutputStream();
        Log.i(TAG, "remote link coordinator connected to " + host + ":" + port);

        send(coordinatorOut, coordinatorWriteLock, offeredVersion,
                TYPE_HELLO, 0, 0, new byte[0]);
        DataInputStream in = new DataInputStream(socket.getInputStream());
        Frame ready = readFrame(in, allowV1Ready ? 0 : offeredVersion);
        boolean acceptedVersion = ready.version == offeredVersion
                || (allowV1Ready && ready.version == VERSION_V1);
        if (ready.type != TYPE_READY || ready.streamId != 0 || ready.port != 0
                || !acceptedVersion) {
            throw new IOException("remote link coordinator returned an invalid READY");
        }
        negotiatedVersion = ready.version;
        return in;
    }

    private void onV1Frame(Frame frame) throws IOException {
        switch (frame.type) {
            case TYPE_OPEN:
                openV1Stream(frame.streamId, frame.port);
                break;
            case TYPE_DATA: {
                Socket local = v1Streams.get(frame.streamId);
                if (local == null) return;
                try {
                    local.getOutputStream().write(frame.payload);
                    local.getOutputStream().flush();
                } catch (IOException failure) {
                    closeV1Stream(frame.streamId, true);
                }
                break;
            }
            case TYPE_CLOSE:
                closeV1Stream(frame.streamId, false);
                break;
            case TYPE_PING:
                sendCoordinator(VERSION_V1, TYPE_PONG, frame.streamId, 0, new byte[0]);
                break;
            default:
                Log.w(TAG, "ignoring unknown v1 remote link frame type " + frame.type);
        }
    }

    /** v2 coordinator carries orchestration only; stream bytes never use it. */
    private void onV2CoordinatorFrame(Frame frame) throws IOException {
        switch (frame.type) {
            case TYPE_OPEN:
                openV2Stream(frame.streamId, frame.port, frame.payload);
                break;
            case TYPE_CLOSE:
                closeV2Stream(frame.streamId, false);
                break;
            case TYPE_PING:
                sendCoordinator(VERSION_V2, TYPE_PONG, frame.streamId, 0, new byte[0]);
                break;
            case TYPE_DATA:
                throw new IOException("v2 DATA is forbidden on the coordinator connection");
            default:
                Log.w(TAG, "ignoring unknown v2 coordinator frame type " + frame.type);
        }
    }

    private void openV1Stream(int streamId, int framePort) throws IOException {
        if (!isAllowedPort(framePort)) {
            refuseOnCoordinator(VERSION_V1, streamId, framePort);
            return;
        }
        Socket local;
        try {
            local = connectLocal(framePort);
        } catch (IOException failure) {
            Log.w(TAG, "gateway port " + framePort + " refused a v1 local connection");
            refuseOnCoordinator(VERSION_V1, streamId, framePort);
            return;
        }
        Socket previous = v1Streams.putIfAbsent(streamId, local);
        if (previous != null) {
            closeQuietly(local);
            refuseOnCoordinator(VERSION_V1, streamId, framePort);
            return;
        }
        Thread pump = new Thread(
                () -> pumpV1Local(streamId, framePort, local),
                "remote-link-v1-" + streamId);
        pump.setDaemon(true);
        pump.start();
    }

    /**
     * Reserve the id immediately, then perform all potentially blocking local
     * and WAN connects away from the coordinator reader.
     */
    private void openV2Stream(int streamId, int framePort, byte[] challenge) throws IOException {
        if (!isAllowedPort(framePort)) {
            Log.w(TAG, "refusing a tunnel to non-gateway port " + framePort);
            refuseOnCoordinator(VERSION_V2, streamId, framePort);
            return;
        }
        if (challenge.length != V2_CHALLENGE_BYTES) {
            Log.w(TAG, "refusing v2 stream without a 32-byte relay challenge");
            refuseOnCoordinator(VERSION_V2, streamId, framePort);
            return;
        }
        V2Stream stream = new V2Stream(streamId, framePort, challenge);
        if (v2Streams.putIfAbsent(streamId, stream) != null) {
            Log.w(TAG, "refusing duplicate v2 stream id " + streamId);
            refuseOnCoordinator(VERSION_V2, streamId, framePort);
            return;
        }
        Thread pump = new Thread(() -> serveV2Stream(stream), "remote-link-v2-" + streamId);
        pump.setDaemon(true);
        pump.start();
    }

    private void serveV2Stream(V2Stream stream) {
        try {
            // Authenticate and bind the WAN carrier before touching a local
            // gateway port. The control and media servers are deliberately
            // single-client/stateful; a speculative local socket left behind
            // by a rejected or late WAN attach can occupy the server and make
            // the next real call connect with zero audio.
            Socket relay = connectRelay();
            synchronized (stream) {
                if (stream.closed.get()) {
                    closeQuietly(relay);
                    return;
                }
                stream.relay = relay;
                stream.relayOut = relay.getOutputStream();
            }
            send(stream.relayOut, stream.writeLock, VERSION_V2,
                    TYPE_HELLO, stream.streamId, stream.port, stream.challenge);

            DataInputStream relayIn = new DataInputStream(stream.relay.getInputStream());
            Frame ready = readFrame(relayIn, VERSION_V2);
            if (ready.type != TYPE_READY || ready.streamId != stream.streamId
                    || ready.port != stream.port) {
                throw new IOException("v2 stream returned an unbound READY");
            }
            stream.ready.set(true);
            // The coordinator owns liveness. Data tunnels are deliberately
            // quiet when their gateway service is idle, so the coordinator's
            // 45-second read timeout must not kill them.
            stream.relay.setSoTimeout(0);

            Socket local = connectLocal(stream.port);
            synchronized (stream) {
                if (stream.closed.get()) {
                    closeQuietly(local);
                    return;
                }
                stream.local = local;
            }

            Thread localPump = new Thread(
                    () -> pumpV2Local(stream), "remote-link-v2-local-" + stream.streamId);
            localPump.setDaemon(true);
            localPump.start();

            while (running.get() && !stream.closed.get()) {
                Frame frame = readFrame(relayIn, VERSION_V2);
                if (frame.streamId != stream.streamId || frame.port != stream.port) {
                    throw new IOException("v2 stream frame identity changed");
                }
                if (frame.type == TYPE_DATA) {
                    OutputStream localOut = stream.local.getOutputStream();
                    localOut.write(frame.payload);
                    localOut.flush();
                } else if (frame.type == TYPE_CLOSE) {
                    break;
                } else if (frame.type == TYPE_PING) {
                    send(stream.relayOut, stream.writeLock, VERSION_V2,
                            TYPE_PONG, stream.streamId, stream.port, new byte[0]);
                } else {
                    throw new IOException("invalid frame type on v2 stream " + frame.type);
                }
            }
        } catch (IOException failure) {
            if (running.get() && !stream.closed.get()) {
                Log.w(TAG, "v2 stream " + stream.streamId + " dropped: " + failure);
                // Failed setup has no usable stream channel on which to send CLOSE.
                // The coordinator is the authenticated refusal path.
                if (!stream.ready.get()) {
                    try {
                        refuseOnCoordinator(VERSION_V2, stream.streamId, stream.port);
                    } catch (IOException ignored) {
                    }
                }
            }
        } finally {
            closeV2Stream(stream.streamId, false);
        }
    }

    /** Read one local gateway connection and forward it over its dedicated WAN socket. */
    private void pumpV2Local(V2Stream stream) {
        byte[] buffer = new byte[16 * 1024];
        try (InputStream in = stream.local.getInputStream()) {
            while (running.get() && !stream.closed.get()) {
                int read = in.read(buffer);
                if (read < 0) break;
                if (read == 0) continue;
                byte[] chunk = new byte[read];
                System.arraycopy(buffer, 0, chunk, 0, read);
                send(stream.relayOut, stream.writeLock, VERSION_V2,
                        TYPE_DATA, stream.streamId, stream.port, chunk);
            }
        } catch (IOException ignored) {
            // Local gateway closure is ordinary; CLOSE below reports it.
        } finally {
            closeV2Stream(stream.streamId, true);
        }
    }

    private void pumpV1Local(int streamId, int framePort, Socket local) {
        byte[] buffer = new byte[16 * 1024];
        try (InputStream in = local.getInputStream()) {
            while (running.get()) {
                int read = in.read(buffer);
                if (read < 0) break;
                if (read == 0) continue;
                byte[] chunk = new byte[read];
                System.arraycopy(buffer, 0, chunk, 0, read);
                sendCoordinator(VERSION_V1, TYPE_DATA, streamId, framePort, chunk);
            }
        } catch (IOException ignored) {
            // A closed gateway socket is ordinary; CLOSE below reports it.
        } finally {
            closeV1Stream(streamId, true);
        }
    }

    private void closeV1Stream(int streamId, boolean notifyRuntime) {
        Socket local = v1Streams.remove(streamId);
        if (local == null) return;
        closeQuietly(local);
        if (notifyRuntime && isConnected()) {
            try {
                sendCoordinator(VERSION_V1, TYPE_CLOSE, streamId, 0, new byte[0]);
            } catch (IOException ignored) {
            }
        }
    }

    private void closeV2Stream(int streamId, boolean notifyRuntime) {
        V2Stream stream = v2Streams.get(streamId);
        if (stream == null) return;
        synchronized (stream) {
            boolean ownsClose = stream.closed.compareAndSet(false, true);
            if (ownsClose) {
                v2Streams.remove(streamId, stream);
                if (notifyRuntime && stream.ready.get() && stream.relayOut != null) {
                    try {
                        send(stream.relayOut, stream.writeLock, VERSION_V2,
                                TYPE_CLOSE, stream.streamId, stream.port, new byte[0]);
                    } catch (IOException ignored) {
                    }
                }
            }
            // Always close assigned references. This also covers coordinator
            // CLOSE racing the blocking local/WAN attach path.
            closeQuietly(stream.local);
            closeQuietly(stream.relay);
        }
    }

    private void refuseOnCoordinator(int version, int streamId, int framePort) throws IOException {
        sendCoordinator(version, TYPE_CLOSE, streamId, framePort, new byte[0]);
    }

    private void sendCoordinator(
            int version, int type, int streamId, int framePort, byte[] payload) throws IOException {
        OutputStream stream = coordinatorOut;
        if (stream == null) throw new IOException("remote link coordinator is not connected");
        send(stream, coordinatorWriteLock, version, type, streamId, framePort, payload);
    }

    private void send(
            OutputStream stream,
            ReentrantLock lock,
            int version,
            int type,
            int streamId,
            int framePort,
            byte[] payload) throws IOException {
        if (payload.length > MAX_PAYLOAD_BYTES) {
            throw new IOException("remote link payload exceeded the limit");
        }
        ByteBuffer header = ByteBuffer.allocate(HEADER_BYTES);
        header.put(MAGIC);
        header.put((byte) version);
        header.put((byte) type);
        header.putInt(streamId);
        header.putShort((short) framePort);
        header.putInt(payload.length);

        byte[] body = new byte[HEADER_BYTES + payload.length];
        System.arraycopy(header.array(), 0, body, 0, HEADER_BYTES);
        System.arraycopy(payload, 0, body, HEADER_BYTES, payload.length);
        byte[] tag = sign(body);

        lock.lock();
        try {
            stream.write(body);
            stream.write(tag);
            stream.flush();
        } finally {
            lock.unlock();
        }
    }

    private Frame readFrame(DataInputStream in, int requiredVersion) throws IOException {
        byte[] header = new byte[HEADER_BYTES];
        try {
            in.readFully(header);
        } catch (EOFException closed) {
            throw new IOException("remote link connection closed", closed);
        }
        ByteBuffer view = ByteBuffer.wrap(header);
        byte[] magic = new byte[4];
        view.get(magic);
        if (!MessageDigest.isEqual(magic, MAGIC)) {
            throw new IOException("remote link frame had a bad magic");
        }
        int version = view.get() & 0xFF;
        if (requiredVersion != 0 && version != requiredVersion) {
            throw new IOException("remote link version changed from "
                    + requiredVersion + " to " + version);
        }
        if (version != VERSION_V1 && version != VERSION_V2) {
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
            throw new IOException("remote link frame failed authentication");
        }
        return new Frame(version, type, streamId, framePort, payload);
    }

    private Socket connectRelay() throws IOException {
        return relayConnector.connect();
    }

    private Socket connectConfiguredRelay() throws IOException {
        Socket socket = new Socket();
        socket.connect(new InetSocketAddress(host, port), CONNECT_TIMEOUT_MS);
        socket.setTcpNoDelay(true);
        socket.setSoTimeout(SOCKET_TIMEOUT_MS);
        return socket;
    }

    @FunctionalInterface
    interface RelayConnector {
        Socket connect() throws IOException;
    }

    private static Socket connectLocal(int framePort) throws IOException {
        Socket local = new Socket(InetAddress.getLoopbackAddress(), framePort);
        local.setTcpNoDelay(true);
        return local;
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

    private boolean isAllowedPort(int candidate) {
        for (int allowed : allowedPorts) {
            if (allowed == candidate) return true;
        }
        return false;
    }

    private void closeTunnel() {
        negotiatedVersion = 0;
        for (Integer streamId : v1Streams.keySet()) closeV1Stream(streamId, false);
        for (Integer streamId : v2Streams.keySet()) closeV2Stream(streamId, false);
        closeCoordinator();
    }

    private void closeCoordinator() {
        Socket socket = coordinator;
        coordinator = null;
        coordinatorOut = null;
        closeQuietly(socket);
    }

    private static void closeQuietly(Socket socket) {
        if (socket == null) return;
        try {
            socket.close();
        } catch (IOException ignored) {
        }
    }

    private static final class Frame {
        final int version;
        final int type;
        final int streamId;
        final int port;
        final byte[] payload;

        Frame(int version, int type, int streamId, int port, byte[] payload) {
            this.version = version;
            this.type = type;
            this.streamId = streamId;
            this.port = port;
            this.payload = payload;
        }
    }

    private static final class V2Stream {
        final int streamId;
        final int port;
        final byte[] challenge;
        final AtomicBoolean ready = new AtomicBoolean(false);
        final AtomicBoolean closed = new AtomicBoolean(false);
        final ReentrantLock writeLock = new ReentrantLock(true);
        volatile Socket local;
        volatile Socket relay;
        volatile OutputStream relayOut;

        V2Stream(int streamId, int port, byte[] challenge) {
            this.streamId = streamId;
            this.port = port;
            this.challenge = challenge.clone();
        }
    }
}
