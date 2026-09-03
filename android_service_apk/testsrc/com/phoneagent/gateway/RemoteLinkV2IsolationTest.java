package com.phoneagent.gateway;

import java.io.DataInputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ConnectException;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.nio.ByteBuffer;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.Instant;
import java.util.Arrays;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/** Executable host-JVM proof for v2 channel isolation and v1 fallback. */
public final class RemoteLinkV2IsolationTest {
    private static final byte[] MAGIC = new byte[] {'P', 'H', 'R', 'L'};
    private static final int HELLO = 1;
    private static final int READY = 2;
    private static final int OPEN = 3;
    private static final int DATA = 4;
    private static final int V1 = 1;
    private static final int V2 = 2;
    private static final byte[] KEY = new byte[32];

    private RemoteLinkV2IsolationTest() {}

    public static void main(String[] arguments) throws Exception {
        for (int index = 0; index < KEY.length; index++) KEY[index] = (byte) index;
        proveIndependentDataTunnels();
        proveRejectedDataTunnelNeverTouchesGateway();
        proveTransportOutageDoesNotDowngrade();
        proveReconnectFallbackToV1();
        System.out.println("remote-link-v2-isolation-ok");
    }

    private static void proveRejectedDataTunnelNeverTouchesGateway() throws Exception {
        try (ServerSocket localGateway = loopbackServer();
             ServerSocket relay = loopbackServer()) {
            AtomicReference<Throwable> failure = new AtomicReference<>();
            CountDownLatch dataHelloReceived = new CountDownLatch(1);
            Thread relayThread = daemon("v2-rejected-data-relay", () -> {
                try (Socket coordinator = relay.accept()) {
                    coordinator.setSoTimeout(3_000);
                    Frame hello = read(coordinator);
                    require(hello.version == V2 && hello.type == HELLO,
                            "phone did not establish the v2 coordinator");
                    write(coordinator, new Frame(V2, READY, 0, 0, new byte[0]));
                    byte[] challenge = repeated((byte) 0x55, 32);
                    write(coordinator, new Frame(
                            V2, OPEN, 31, localGateway.getLocalPort(), challenge));
                    try (Socket rejectedData = relay.accept()) {
                        rejectedData.setSoTimeout(3_000);
                        Frame streamHello = read(rejectedData);
                        require(streamHello.version == V2 && streamHello.type == HELLO,
                                "phone did not authenticate the v2 data tunnel");
                        require(streamHello.streamId == 31
                                        && streamHello.port == localGateway.getLocalPort()
                                        && MessageDigest.isEqual(challenge, streamHello.payload),
                                "v2 data HELLO was not bound to its OPEN");
                        dataHelloReceived.countDown();
                        // Reject by closing without READY. The local gateway
                        // must never see a speculative half-open connection.
                    }
                    Thread.sleep(100);
                } catch (Throwable error) {
                    failure.compareAndSet(null, error);
                }
            });
            relayThread.start();

            RemoteLinkService service = new RemoteLinkService(
                    "127.0.0.1", relay.getLocalPort(), KEY,
                    new int[] {localGateway.getLocalPort()});
            service.start();
            require(dataHelloReceived.await(3, TimeUnit.SECONDS),
                    "phone did not attempt the rejected data tunnel");
            localGateway.setSoTimeout(300);
            try (Socket ignored = localGateway.accept()) {
                throw new AssertionError(
                        "rejected WAN tunnel opened the local gateway before authentication");
            } catch (SocketTimeoutException expected) {
                // Required: no local connection exists until relay READY.
            } finally {
                service.stop();
            }
            relayThread.join(2_000);
            rethrow(failure.get());
        }
    }

    private static void proveTransportOutageDoesNotDowngrade() throws Exception {
        try (ServerSocket relay = loopbackServer()) {
            AtomicInteger connectionAttempts = new AtomicInteger();
            AtomicReference<Throwable> failure = new AtomicReference<>();
            CountDownLatch v2Ready = new CountDownLatch(1);
            Thread relayThread = daemon("v2-transport-retry-relay", () -> {
                try (Socket coordinator = relay.accept()) {
                    coordinator.setSoTimeout(3_000);
                    Frame offered = read(coordinator);
                    require(offered.version == V2,
                            "transient TCP outage incorrectly downgraded the next HELLO to v1");
                    write(coordinator, new Frame(V2, READY, 0, 0, new byte[0]));
                    v2Ready.countDown();
                    Thread.sleep(100);
                } catch (Throwable error) {
                    failure.compareAndSet(null, error);
                }
            });
            relayThread.start();

            RemoteLinkService.RelayConnector connector = () -> {
                if (connectionAttempts.getAndIncrement() == 0) {
                    throw new ConnectException("simulated relay startup outage");
                }
                Socket socket = new Socket();
                socket.connect(relay.getLocalSocketAddress(), 3_000);
                socket.setTcpNoDelay(true);
                socket.setSoTimeout(3_000);
                return socket;
            };
            RemoteLinkService service = new RemoteLinkService(
                    "127.0.0.1", relay.getLocalPort(), KEY, new int[0], connector);
            service.start();
            relayThread.join(3_000);
            service.stop();
            require(!relayThread.isAlive(), "phone did not retry after transport recovery");
            rethrow(failure.get());
            require(v2Ready.getCount() == 0, "v2 transport retry did not complete");
            require(connectionAttempts.get() == 2,
                    "transport retry used an unexpected number of connections");
        }
    }

    private static void proveIndependentDataTunnels() throws Exception {
        try (ServerSocket captureGateway = loopbackServer();
             ServerSocket acknowledgementGateway = loopbackServer();
             ServerSocket relay = loopbackServer()) {
            int capturePort = captureGateway.getLocalPort();
            int acknowledgementPort = acknowledgementGateway.getLocalPort();
            byte[] captureChallenge = repeated((byte) 0x41, 32);
            byte[] acknowledgementChallenge = repeated((byte) 0x42, 32);
            AtomicReference<Throwable> failure = new AtomicReference<>();
            CountDownLatch attached = new CountDownLatch(2);
            Map<Integer, Socket> dataSockets = new ConcurrentHashMap<>();

            Thread relayThread = daemon("v2-test-relay", () -> {
                try (Socket coordinator = relay.accept()) {
                    coordinator.setSoTimeout(3_000);
                    Frame hello = read(coordinator);
                    require(hello.version == V2 && hello.type == HELLO && hello.streamId == 0,
                            "phone did not offer a v2 coordinator");
                    write(coordinator, new Frame(V2, READY, 0, 0, new byte[0]));
                    write(coordinator, new Frame(
                            V2, OPEN, 11, capturePort, captureChallenge));
                    write(coordinator, new Frame(
                            V2, OPEN, 12, acknowledgementPort, acknowledgementChallenge));

                    for (int index = 0; index < 2; index++) {
                        Socket data = relay.accept();
                        data.setSoTimeout(3_000);
                        Frame streamHello = read(data);
                        byte[] expected = streamHello.streamId == 11
                                ? captureChallenge : acknowledgementChallenge;
                        require(streamHello.version == V2 && streamHello.type == HELLO,
                                "data tunnel did not authenticate with v2 HELLO");
                        require((streamHello.streamId == 11 || streamHello.streamId == 12)
                                        && MessageDigest.isEqual(expected, streamHello.payload),
                                "data tunnel did not echo its exact OPEN challenge");
                        dataSockets.put(streamHello.streamId, data);
                        write(data, new Frame(
                                V2, READY, streamHello.streamId, streamHello.port, new byte[0]));
                        attached.countDown();
                    }

                    require(attached.await(3, TimeUnit.SECONDS), "v2 data tunnels did not attach");
                    Socket acknowledgement = dataSockets.get(12);
                    Frame rendered = read(acknowledgement);
                    require(rendered.type == DATA && rendered.streamId == 12,
                            "acknowledgement used the wrong data tunnel");
                    require(Arrays.equals(rendered.payload, "rendered:42".getBytes()),
                            "acknowledgement payload changed");
                } catch (Throwable error) {
                    failure.compareAndSet(null, error);
                } finally {
                    for (Socket socket : dataSockets.values()) close(socket);
                }
            });
            relayThread.start();

            RemoteLinkService service = new RemoteLinkService(
                    "127.0.0.1", relay.getLocalPort(), KEY,
                    new int[] {capturePort, acknowledgementPort});
            service.start();
            try (Socket capture = accept(captureGateway);
                 Socket acknowledgement = accept(acknowledgementGateway)) {
                require(attached.await(3, TimeUnit.SECONDS), "phone did not establish both streams");

                // The relay deliberately never reads stream 11 after READY.
                // Fill that carrier in a daemon writer while stream 12 must
                // still traverse its physically independent TCP connection.
                daemon("blocked-capture-writer", () -> {
                    try {
                        byte[] audio = new byte[64 * 1024];
                        for (int count = 0; count < 256; count++) {
                            capture.getOutputStream().write(audio);
                        }
                    } catch (Exception ignored) {
                    }
                }).start();
                Thread.sleep(50);
                Instant started = Instant.now();
                acknowledgement.getOutputStream().write("rendered:42".getBytes());
                acknowledgement.getOutputStream().flush();
                relayThread.join(2_000);
                require(!relayThread.isAlive(), "blocked capture carrier blocked ACK stream");
                require(Duration.between(started, Instant.now()).toMillis() < 2_000,
                        "ACK stream exceeded its isolation budget");
                rethrow(failure.get());
            } finally {
                service.stop();
            }
        }
    }

    private static void proveReconnectFallbackToV1() throws Exception {
        try (ServerSocket relay = loopbackServer()) {
            AtomicReference<Throwable> failure = new AtomicReference<>();
            CountDownLatch v1Ready = new CountDownLatch(1);
            Thread relayThread = daemon("v1-fallback-relay", () -> {
                try (Socket rejected = relay.accept()) {
                    rejected.setSoTimeout(3_000);
                    Frame offered = read(rejected);
                    require(offered.version == V2, "first coordinator offer was not v2");
                    // A real old relay rejects the unknown version by closing.
                } catch (Throwable error) {
                    failure.compareAndSet(null, error);
                    return;
                }
                try (Socket fallback = relay.accept()) {
                    fallback.setSoTimeout(3_000);
                    Frame offered = read(fallback);
                    require(offered.version == V1, "phone did not reconnect speaking v1");
                    write(fallback, new Frame(V1, READY, 0, 0, new byte[0]));
                    v1Ready.countDown();
                    Thread.sleep(100);
                } catch (Throwable error) {
                    failure.compareAndSet(null, error);
                }
            });
            relayThread.start();

            RemoteLinkService service = new RemoteLinkService(
                    "127.0.0.1", relay.getLocalPort(), KEY, new int[0]);
            service.start();
            require(v1Ready.await(3, TimeUnit.SECONDS), "v1 reconnect fallback did not complete");
            rethrow(failure.get());
            service.stop();
            relayThread.join(2_000);
        }
    }

    private static ServerSocket loopbackServer() throws Exception {
        return new ServerSocket(0, 16, InetAddress.getLoopbackAddress());
    }

    private static Socket accept(ServerSocket server) throws Exception {
        server.setSoTimeout(3_000);
        Socket socket = server.accept();
        socket.setSoTimeout(3_000);
        return socket;
    }

    private static Thread daemon(String name, ThrowingRunnable action) {
        Thread thread = new Thread(() -> {
            try {
                action.run();
            } catch (Throwable error) {
                throw new RuntimeException(error);
            }
        }, name);
        thread.setDaemon(true);
        return thread;
    }

    private static byte[] repeated(byte value, int length) {
        byte[] result = new byte[length];
        Arrays.fill(result, value);
        return result;
    }

    private static Frame read(Socket socket) throws Exception {
        DataInputStream input = new DataInputStream(socket.getInputStream());
        byte[] header = input.readNBytes(16);
        require(header.length == 16, "remote frame header ended early");
        ByteBuffer values = ByteBuffer.wrap(header);
        byte[] magic = new byte[4];
        values.get(magic);
        require(MessageDigest.isEqual(magic, MAGIC), "remote frame magic changed");
        int version = values.get() & 0xff;
        int type = values.get() & 0xff;
        int streamId = values.getInt();
        int port = values.getShort() & 0xffff;
        int length = values.getInt();
        require(length >= 0 && length <= 64 * 1024, "remote frame length is invalid");
        byte[] payload = input.readNBytes(length);
        byte[] tag = input.readNBytes(32);
        byte[] body = new byte[header.length + payload.length];
        System.arraycopy(header, 0, body, 0, header.length);
        System.arraycopy(payload, 0, body, header.length, payload.length);
        require(MessageDigest.isEqual(tag, sign(body)), "remote frame authentication failed");
        return new Frame(version, type, streamId, port, payload);
    }

    private static void write(Socket socket, Frame frame) throws Exception {
        ByteBuffer header = ByteBuffer.allocate(16);
        header.put(MAGIC);
        header.put((byte) frame.version);
        header.put((byte) frame.type);
        header.putInt(frame.streamId);
        header.putShort((short) frame.port);
        header.putInt(frame.payload.length);
        byte[] body = new byte[16 + frame.payload.length];
        System.arraycopy(header.array(), 0, body, 0, 16);
        System.arraycopy(frame.payload, 0, body, 16, frame.payload.length);
        OutputStream output = socket.getOutputStream();
        output.write(body);
        output.write(sign(body));
        output.flush();
    }

    private static byte[] sign(byte[] body) throws Exception {
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(KEY, "HmacSHA256"));
        return mac.doFinal(body);
    }

    private static void close(Socket socket) {
        try {
            socket.close();
        } catch (Exception ignored) {
        }
    }

    private static void require(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }

    private static void rethrow(Throwable failure) throws Exception {
        if (failure == null) return;
        if (failure instanceof Exception exception) throw exception;
        throw new AssertionError(failure);
    }

    private record Frame(int version, int type, int streamId, int port, byte[] payload) {}

    @FunctionalInterface
    private interface ThrowingRunnable {
        void run() throws Exception;
    }
}
