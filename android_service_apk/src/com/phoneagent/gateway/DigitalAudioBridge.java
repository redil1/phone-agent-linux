package com.phoneagent.gateway;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageManager;
import android.media.AudioAttributes;
import android.media.AudioDeviceInfo;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioTrack;
import android.media.MediaRecorder;
import android.util.Log;

import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.SocketException;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Privileged authenticated telephony audio bridge.
 *
 * Downlink and uplink use PCM16/16 kHz/mono. Android's stateful AudioFlinger
 * resampler converts uplink speech to the Telephony TX device rate. Every
 * network frame is authenticated and bound to a call, link epoch, generation,
 * and sequence.
 */
public class DigitalAudioBridge {
    private static final String TAG = "PhoneAgentDigitalAudio";

    public static final int NETWORK_SAMPLE_RATE = 16000;
    private static final int INJECTION_SAMPLE_RATE = NETWORK_SAMPLE_RATE;
    private static final int RX_PORT = 8766;
    private static final int TX_PORT = 8767;
    private static final int NETWORK_CHUNK_BYTES = 640;
    private static final int PLAYOUT_QUEUE_FRAMES = 12;
    private static final int PLAYOUT_PREBUFFER_FRAMES = 5;
    private static final int PLAYOUT_MAX_ADAPTIVE_PREBUFFER_FRAMES = 8;
    private static final int STARVATION_GRACE_MS = 12;
    // The MTK/GSI audio policy restores setPreferredDevice(TYPE_TELEPHONY)
    // tracks onto INCALL_MUSIC. A failed retry can leave that restored native
    // track alive even after Java releases its AudioTrack. Never amplify one
    // failed media attach into several permanently occupied mixer slots.
    private static final int TRACK_START_ATTEMPTS = 1;
    private static final int PLAYOUT_JOIN_ATTEMPTS = 4;
    private static final int PLAYOUT_JOIN_TIMEOUT_MS = 250;
    private static final byte[] SILENCE_FRAME = new byte[NETWORK_CHUNK_BYTES];
    // phh-su authorizes commands by exact string. Keep this byte-for-byte aligned
    // with provision_phh_su_audio_recovery.sh. A bare killall returns 1 while
    // audioserver is already restarting and says nothing about whether Android
    // brought the service back. This command accepts that harmless race but
    // succeeds only after a live, different audioserver process is observed.
    private static final String AUDIO_SERVER_RECOVERY_COMMAND =
            "old=$(pidof audioserver || true); "
            + "if [ -n \"$old\" ]; then killall audioserver || exit 1; fi; "
            + "i=0; while [ \"$i\" -lt 50 ]; do "
            + "new=$(pidof audioserver || true); "
            + "if [ -n \"$new\" ] && [ \"$new\" != \"$old\" ]; then "
            + "echo \"$old->$new\"; exit 0; fi; "
            + "i=$((i + 1)); sleep 0.1; done; exit 2";

    private static final Object OUTPUT_LOCK = new Object();
    private static final AtomicLong downlinkBytes = new AtomicLong();
    private static final AtomicLong uplinkBytes = new AtomicLong();
    private static final AtomicLong generation = new AtomicLong(1);
    private static final AtomicLong lastAcceptedSequence = new AtomicLong(-1);
    private static final AtomicLong lastRenderedSequence = new AtomicLong(-1);
    private static final AtomicLong staleUplinkFrames = new AtomicLong();
    private static final AtomicLong audioPlayoutFrames = new AtomicLong();
    private static final AtomicLong silencePlayoutFrames = new AtomicLong();
    private static final AtomicLong speechSegments = new AtomicLong();
    private static final AtomicLong endMarkersReceived = new AtomicLong();
    private static final AtomicLong midSpeechStarvationEvents = new AtomicLong();
    private static final AtomicLong midSpeechConcealmentFrames = new AtomicLong();
    private static final AtomicLong peakPlayoutQueueDepth = new AtomicLong();
    private static final AtomicLong playoutAcksSent = new AtomicLong();
    private static final AtomicLong telephonyTrackStartAttempts = new AtomicLong();
    private static final AtomicLong audioServerRecoveries = new AtomicLong();
    private static final AtomicBoolean audioServerRecoveryScheduled = new AtomicBoolean();

    private static volatile boolean running;
    private static volatile boolean rxConnected;
    private static volatile boolean txConnected;
    private static volatile String captureSource = "not_started";
    private static volatile String injectionRoute = "not_started";
    private static volatile String injectionProof = "not_started";
    private static volatile String lastError = "";
    private static volatile boolean playoutPrebuffering = true;
    private static volatile int adaptivePlayoutPrebufferFrames = PLAYOUT_PREBUFFER_FRAMES;
    private static volatile boolean cellularRouteTouched;
    private static volatile String audioServerRecoveryStatus = "not_needed";
    private static volatile String audioServerRecoveryDetail = "";

    /**
     * Whether the call to bridge is a VoIP call placed by an app on this phone
     * rather than a cellular call. It selects the audio route and nothing else:
     * the telephony capture and injection below are untouched by it.
     */
    private static volatile boolean voipMode;

    private static Context appContext;

    /** Set before connecting media. Cellular is the default and stays so. */
    public static void setVoipMode(boolean enabled) {
        voipMode = enabled;
        Log.i(TAG, "audio route set to " + (enabled ? "VoIP" : "cellular"));
        if (enabled) {
            // Before the call, not when media connects: a mix registered after
            // the VoIP app has opened its microphone matches nothing.
            VoipAudioRoute.prepare(appContext, INJECTION_SAMPLE_RATE, SILENCE_FRAME);
        } else {
            VoipAudioRoute.release();
        }
    }

    public static boolean isVoipMode() {
        return voipMode;
    }

    static Context applicationContext() {
        return appContext;
    }
    private static Thread rxServerThread;
    private static Thread txServerThread;
    private static ServerSocket rxServerSocket;
    private static ServerSocket txServerSocket;
    private static Socket activeTxSocket;
    private static AudioTrack activeTrack;
    private static ArrayBlockingQueue<QueuedAudio> activePlaybackQueue;
    private static volatile int audioTrackUnderruns;

    private static final class QueuedAudio {
        final byte[] pcm;
        final long generation;
        final long sequence;
        final boolean endOfStream;

        QueuedAudio(byte[] pcm, long generation, long sequence, boolean endOfStream) {
            this.pcm = pcm;
            this.generation = generation;
            this.sequence = sequence;
            this.endOfStream = endOfStream;
        }

        static QueuedAudio audio(byte[] pcm, long generation, long sequence) {
            return new QueuedAudio(pcm, generation, sequence, false);
        }

        static QueuedAudio end(long generation, long sequence) {
            return new QueuedAudio(null, generation, sequence, true);
        }
    }

    public static synchronized void start(Context context) {
        if (running) return;
        appContext = context.getApplicationContext();
        running = true;
        lastError = "";
        rxServerThread = new Thread(DigitalAudioBridge::runDownlinkServer, "gateway-audio-rx");
        txServerThread = new Thread(DigitalAudioBridge::runUplinkServer, "gateway-audio-tx");
        rxServerThread.setDaemon(true);
        txServerThread.setDaemon(true);
        rxServerThread.start();
        txServerThread.start();
        Log.i(TAG, "Authenticated telephony audio bridge starting on loopback ports 8766/8767");
    }

    private static void runDownlinkServer() {
        try {
            ServerSocket server = loopbackServer(RX_PORT);
            rxServerSocket = server;
            Log.i(TAG, "Downlink server listening on 127.0.0.1:" + RX_PORT);
            while (running) {
                try (Socket client = server.accept();
                     BufferedInputStream input = new BufferedInputStream(client.getInputStream());
                     BufferedOutputStream output = new BufferedOutputStream(client.getOutputStream())) {
                    client.setTcpNoDelay(true);
                    client.setReceiveBufferSize(8 * 1024);
                    client.setSoTimeout(15000);
                    byte[] key = LinkKeyStore.requireKey(appContext);
                    LinkSessionRegistry.Binding binding = LinkSessionRegistry.handshake(
                            input, output, key, "downlink"
                    );
                    client.setSoTimeout(0);
                    rxConnected = true;
                    downlinkBytes.set(0);
                    lastError = "";
                    GatewayService.enableMicrophoneForeground();
                    streamDownlink(output, key, binding);
                } catch (Exception e) {
                    if (running && isExpectedPeerDisconnect(e)) {
                        Log.i(TAG, "Downlink client closed the completed capture stream");
                    } else if (running) {
                        recordError("Downlink connection failed", e);
                    }
                } finally {
                    rxConnected = false;
                    captureSource = "not_started";
                }
            }
        } catch (Exception e) {
            if (running) recordError("Downlink server failed", e);
        }
    }

    private static void streamDownlink(OutputStream output, byte[] key,
                                       LinkSessionRegistry.Binding binding) throws Exception {
        AudioRecord recorder = voipMode
                ? VoipAudioRoute.createRecorder(NETWORK_SAMPLE_RATE, NETWORK_CHUNK_BYTES)
                : createTelephonyRecorder();
        try {
            recorder.startRecording();
            if (recorder.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) {
                throw new IllegalStateException("AudioRecord did not enter RECORDSTATE_RECORDING");
            }
            byte[] buffer = new byte[NETWORK_CHUNK_BYTES];
            long sequence = 0;
            while (running && LinkSessionRegistry.isCurrent(binding)) {
                int offset = 0;
                while (offset < buffer.length) {
                    int count = recorder.read(
                            buffer, offset, buffer.length - offset, AudioRecord.READ_BLOCKING
                    );
                    if (count < 0) throw new IOException("AudioRecord read error " + count);
                    if (count == 0) continue;
                    offset += count;
                }
                if (!LinkSessionRegistry.isCurrent(binding)) return;
                ProtocolCodec.write(
                        output,
                        new ProtocolCodec.Frame(
                                ProtocolCodec.KIND_AUDIO,
                                ProtocolCodec.DIRECTION_PHONE_TO_MAC,
                                0,
                                binding.callId,
                                generation.get(),
                                sequence++,
                                System.nanoTime(),
                                NETWORK_SAMPLE_RATE,
                                1,
                                2,
                                buffer
                        ),
                        key
                );
                downlinkBytes.addAndGet(buffer.length);
            }
        } finally {
            try { recorder.stop(); } catch (Exception ignored) {}
            recorder.release();
        }
    }

    private static AudioRecord createTelephonyRecorder() {
        int[] sources = new int[] {
                MediaRecorder.AudioSource.VOICE_DOWNLINK,
                MediaRecorder.AudioSource.VOICE_CALL,
                MediaRecorder.AudioSource.VOICE_COMMUNICATION
        };
        StringBuilder failures = new StringBuilder();
        int minimum = AudioRecord.getMinBufferSize(
                NETWORK_SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT
        );
        int bufferSize = Math.max(minimum, NETWORK_CHUNK_BYTES * 4);

        for (int source : sources) {
            AudioRecord recorder = null;
            try {
                recorder = new AudioRecord.Builder()
                        .setAudioSource(source)
                        .setAudioFormat(new AudioFormat.Builder()
                                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                                .setSampleRate(NETWORK_SAMPLE_RATE)
                                .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                                .build())
                        .setBufferSizeInBytes(bufferSize)
                        .build();
                if (recorder.getState() == AudioRecord.STATE_INITIALIZED) {
                    captureSource = sourceName(source);
                    Log.i(TAG, "Selected downlink capture candidate: " + captureSource);
                    return recorder;
                }
                failures.append(sourceName(source)).append(":uninitialized; ");
            } catch (Exception e) {
                failures.append(sourceName(source)).append(':')
                        .append(e.getClass().getSimpleName()).append("; ");
            }
            if (recorder != null) recorder.release();
        }
        throw new IllegalStateException("No AudioRecord candidate initialized: " + failures);
    }

    private static void runUplinkServer() {
        try {
            ServerSocket server = loopbackServer(TX_PORT);
            txServerSocket = server;
            Log.i(TAG, "Uplink server listening on 127.0.0.1:" + TX_PORT);
            while (running) {
                AudioTrack track = null;
                try (Socket client = server.accept();
                     BufferedInputStream input = new BufferedInputStream(client.getInputStream());
                     BufferedOutputStream output = new BufferedOutputStream(client.getOutputStream())) {
                    client.setTcpNoDelay(true);
                    client.setSoTimeout(15000);
                    byte[] key = LinkKeyStore.requireKey(appContext);
                    // Telephony TX only exists while a call is up. Building a track
                    // against it otherwise makes AudioFlinger restore a dead
                    // IAudioTrack ("creating a new one from setOutputDevice()"),
                    // which orphans the original on the INCALL_MUSIC output and
                    // permanently consumes one of its limited track slots even
                    // though release() is called correctly.
                    if (voipMode) {
                        // No gate here, deliberately. The cellular one below exists
                        // because building a track against Telephony TX with no call
                        // up orphans an AudioTrack slot permanently; an audio policy
                        // mix carries no such hazard, so there is nothing to protect
                        // against. It is also not checkable from here: getMode()
                        // reports MODE_NORMAL even while dumpsys shows the call,
                        // because an app does not reliably observe another app's
                        // audio mode — so the check refused every WhatsApp call it
                        // was meant to allow. The caller proves the call is up, from
                        // the mode's owning uid, before dialling ever returns.
                        Log.i(TAG, "VoIP uplink: skipping the telephony call gate");
                    } else {
                        String callState = CallManager.getCallState();
                        if (!"ACTIVE".equals(callState)) {
                            throw new IllegalStateException(
                                    "uplink requires an ACTIVE call; Telecom reports " + callState
                            );
                        }
                    }
                    // Prove the modem injection route works BEFORE acknowledging the
                    // handshake. Starting it afterwards let the Mac complete a
                    // successful handshake and then stream a whole call into a route
                    // that never existed, so the only symptom was silence plus a byte
                    // counter nobody was reading. A failure here closes the socket and
                    // surfaces as an immediate connect_media error on the Mac.
                    track = voipMode
                            ? VoipAudioRoute.createStartedInjectionTrack(
                                    appContext, INJECTION_SAMPLE_RATE, SILENCE_FRAME)
                            : createStartedTelephonyInjectionTrack();
                    LinkSessionRegistry.Binding binding = LinkSessionRegistry.handshake(
                            input, output, key, "uplink"
                    );
                    client.setSoTimeout(0);
                    txConnected = true;
                    uplinkBytes.set(0);
                    audioPlayoutFrames.set(0);
                    silencePlayoutFrames.set(0);
                    speechSegments.set(0);
                    endMarkersReceived.set(0);
                    midSpeechStarvationEvents.set(0);
                    midSpeechConcealmentFrames.set(0);
                    peakPlayoutQueueDepth.set(0);
                    playoutAcksSent.set(0);
                    audioTrackUnderruns = 0;
                    playoutPrebuffering = true;
                    lastAcceptedSequence.set(-1);
                    lastRenderedSequence.set(-1);
                    activeTxSocket = client;
                    streamUplink(track, input, output, key, binding);
                    // streamUplink owns the track for its whole lifetime and has
                    // already released it by this point.
                    track = null;
                } catch (Exception e) {
                    if (running && (isExpectedPeerDisconnect(e)
                            || !"ACTIVE".equals(CallManager.getCallState()))) {
                        Log.i(TAG, "Uplink client closed the completed playout stream");
                    } else if (running) {
                        recordError("Uplink connection failed", e);
                    }
                } finally {
                    // Only reached when the handshake failed between starting the
                    // route and entering streamUplink. Every other path released
                    // the track itself.
                    releaseTrackQuietly(track);
                    synchronized (OUTPUT_LOCK) {
                        releaseActiveTrack();
                        activeTxSocket = null;
                    }
                    txConnected = false;
                    injectionRoute = "not_started";
                    injectionProof = "not_started";
                    // The injector mix deliberately outlives a media reconnect.
                    // Releasing it here would unbind the recorder WhatsApp
                    // already attached to it, and re-registering mid-call is too
                    // late to be heard. It is released when the route returns to
                    // cellular, which the client does as the call ends.
                }
            }
        } catch (Exception e) {
            if (running) recordError("Uplink server failed", e);
        }
    }

    private static void streamUplink(AudioTrack track, InputStream input, OutputStream output,
                                     byte[] key, LinkSessionRegistry.Binding binding)
            throws Exception {
        ArrayBlockingQueue<QueuedAudio> queue = new ArrayBlockingQueue<>(PLAYOUT_QUEUE_FRAMES);
        AtomicBoolean playoutRunning = new AtomicBoolean(true);
        Thread playoutThread = new Thread(
                () -> runContinuousPlayout(track, queue, output, key, binding, playoutRunning),
                "gateway-telephony-playout"
        );
        playoutThread.setDaemon(true);
        synchronized (OUTPUT_LOCK) {
            activeTrack = track;
            activePlaybackQueue = queue;
            lastError = "";
        }
        playoutThread.start();

        try {
            while (running && LinkSessionRegistry.isCurrent(binding)) {
                ProtocolCodec.Frame frame = ProtocolCodec.read(input, key);
                if (frame == null) return;
                long currentGeneration = generation.get();
                boolean isEndMarker = frame.kind == ProtocolCodec.KIND_CONTROL
                        && (frame.flags & ProtocolCodec.FLAG_END_OF_STREAM) != 0;
                if (isEndMarker) {
                    if (!LinkSessionRegistry.isCurrent(binding)
                            || frame.direction != ProtocolCodec.DIRECTION_MAC_TO_PHONE
                            || !frame.callId.equals(binding.callId)
                            || frame.generation != currentGeneration
                            || frame.sequence <= lastAcceptedSequence.get()) {
                        staleUplinkFrames.incrementAndGet();
                        continue;
                    }
                    lastAcceptedSequence.set(frame.sequence);
                    if (!enqueueCurrent(
                            queue, QueuedAudio.end(frame.generation, frame.sequence), binding
                    )) {
                        staleUplinkFrames.incrementAndGet();
                        continue;
                    }
                    endMarkersReceived.incrementAndGet();
                    updatePeakQueueDepth(queue.size());
                    continue;
                }
                if (!LinkSessionRegistry.isCurrent(binding)
                        || frame.kind != ProtocolCodec.KIND_AUDIO
                        || frame.direction != ProtocolCodec.DIRECTION_MAC_TO_PHONE
                        || !frame.callId.equals(binding.callId)
                        || frame.generation != currentGeneration
                        || frame.sequence <= lastAcceptedSequence.get()
                        || frame.sampleRate != NETWORK_SAMPLE_RATE
                        || frame.channels != 1
                        || frame.sampleWidth != 2
                        || frame.payload.length != NETWORK_CHUNK_BYTES) {
                    staleUplinkFrames.incrementAndGet();
                    continue;
                }
                lastAcceptedSequence.set(frame.sequence);
                QueuedAudio queued = QueuedAudio.audio(
                        frame.payload.clone(), frame.generation, frame.sequence
                );
                // Preserve every authenticated PCM frame while it is current.
                // Re-check the generation during a blocked admission so audio
                // cancelled by a concurrent flush cannot re-enter the queue.
                if (!enqueueCurrent(queue, queued, binding)) {
                    staleUplinkFrames.incrementAndGet();
                    continue;
                }
                updatePeakQueueDepth(queue.size());
            }
        } finally {
            // This connection owns this track for its entire lifetime and must
            // release it exactly once, on every exit path. An AudioTrack that is
            // abandoned here stays registered and active inside AudioFlinger for
            // the life of the process; each orphan permanently consumes one of the
            // telephony output's limited track slots until the output saturates and
            // no further injection track can ever be created.
            //
            // Order matters. Thread.interrupt() cannot break a blocking
            // AudioTrack.write(), so the track is stopped first to make that call
            // return; the writer is then joined so the track is provably not in use
            // before it is released, because releasing underneath an in-flight
            // native write is a use-after-free.
            playoutRunning.set(false);
            stopTrackQuietly(track);
            boolean writerExited = joinPlayoutThread(playoutThread);
            synchronized (OUTPUT_LOCK) {
                if (activeTrack == track) activeTrack = null;
                if (activePlaybackQueue == queue) activePlaybackQueue = null;
                queue.clear();
            }
            if (writerExited) {
                releaseTrackQuietly(track);
            }
        }
    }

    /** Stop a track so any blocked WRITE_BLOCKING call returns promptly. */
    private static void stopTrackQuietly(AudioTrack track) {
        if (track == null) return;
        try { audioTrackUnderruns = track.getUnderrunCount(); } catch (Exception ignored) {}
        try { track.pause(); } catch (Exception ignored) {}
        try { track.flush(); } catch (Exception ignored) {}
        try { track.stop(); } catch (Exception ignored) {}
    }

    /**
     * Wait for the playout writer to leave AudioTrack.write before release.
     *
     * @return true when the writer has provably exited and the track is safe to
     *         release; false only when it is still running, in which case leaking
     *         one track is strictly safer than a native use-after-free.
     */
    private static boolean joinPlayoutThread(Thread playoutThread) {
        for (int attempt = 1; attempt <= PLAYOUT_JOIN_ATTEMPTS; attempt++) {
            try {
                playoutThread.join(PLAYOUT_JOIN_TIMEOUT_MS);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
                return !playoutThread.isAlive();
            }
            if (!playoutThread.isAlive()) return true;
            playoutThread.interrupt();
        }
        recordError(
                "Telephony playout writer did not exit",
                new IllegalStateException(
                        "track release skipped to avoid a native use-after-free; the telephony "
                                + "output has lost one track slot until this process restarts"
                )
        );
        return false;
    }

    private static void releaseTrackQuietly(AudioTrack track) {
        if (track == null) return;
        try { track.release(); } catch (Exception ignored) {}
    }

    private static void runContinuousPlayout(
            AudioTrack track,
            ArrayBlockingQueue<QueuedAudio> queue,
            OutputStream output,
            byte[] key,
            LinkSessionRegistry.Binding binding,
            AtomicBoolean playoutRunning
    ) {
        boolean speechActive = false;
        long activeGeneration = generation.get();
        int targetPrebufferFrames = PLAYOUT_PREBUFFER_FRAMES;
        adaptivePlayoutPrebufferFrames = targetPrebufferFrames;
        while (running && playoutRunning.get() && LinkSessionRegistry.isCurrent(binding)) {
            // Telecom can end the call before the Mac closes its media socket.
            // Continuing to clock silence into TYPE_TELEPHONY after that point
            // makes the MTK HAL restore/dead-track loop and contaminates the
            // next call's route. Stop writing immediately; streamUplink's
            // finally block will join and release this track when the socket
            // teardown arrives.
            if (!"ACTIVE".equals(CallManager.getCallState())) {
                try { track.pause(); } catch (Exception ignored) {}
                try { track.flush(); } catch (Exception ignored) {}
                return;
            }
            long currentGeneration = generation.get();
            if (activeGeneration != currentGeneration) {
                activeGeneration = currentGeneration;
                speechActive = false;
                targetPrebufferFrames = PLAYOUT_PREBUFFER_FRAMES;
                adaptivePlayoutPrebufferFrames = targetPrebufferFrames;
            }

            // Start from a protected 100 ms reservoir. A completed short
            // segment may start with fewer frames because its explicit end
            // marker proves that every frame has already arrived.
            if (!speechActive && (queue.size() >= targetPrebufferFrames
                    || containsEndMarker(queue, currentGeneration))) {
                speechActive = true;
                speechSegments.incrementAndGet();
            }

            QueuedAudio queued = null;
            if (speechActive) {
                queued = queue.poll();
                if (queued == null) {
                    try {
                        queued = queue.poll(STARVATION_GRACE_MS, TimeUnit.MILLISECONDS);
                    } catch (InterruptedException interrupted) {
                        Thread.currentThread().interrupt();
                        return;
                    }
                    if (queued == null) {
                        midSpeechStarvationEvents.incrementAndGet();
                        midSpeechConcealmentFrames.incrementAndGet();
                        // One concealed 20 ms frame is preferable to exposing a
                        // run of jitter gaps. Re-enter prebuffering and grow the
                        // reservoir only after real starvation; stable calls keep
                        // the original 100 ms startup latency.
                        speechActive = false;
                        targetPrebufferFrames = Math.min(
                                PLAYOUT_MAX_ADAPTIVE_PREBUFFER_FRAMES,
                                targetPrebufferFrames + 1
                        );
                        adaptivePlayoutPrebufferFrames = targetPrebufferFrames;
                    }
                }
            }

            QueuedAudio consumed = queued;
            if (queued != null && queued.endOfStream) {
                speechActive = false;
                targetPrebufferFrames = PLAYOUT_PREBUFFER_FRAMES;
                adaptivePlayoutPrebufferFrames = targetPrebufferFrames;
                queued = null;
            }
            playoutPrebuffering = !speechActive;
            byte[] pcm = SILENCE_FRAME;
            long writeGeneration = generation.get();
            boolean isCurrentAudio = queued != null && !queued.endOfStream
                    && queued.generation == writeGeneration;
            if (isCurrentAudio) pcm = queued.pcm;
            synchronized (OUTPUT_LOCK) {
                if (generation.get() != writeGeneration
                        || (queued != null && queued.generation != writeGeneration)) {
                    isCurrentAudio = false;
                    pcm = SILENCE_FRAME;
                }
            }
            // Never hold OUTPUT_LOCK across a blocking write. The HAL decides how
            // long this call takes, and both the urgent interruption flush and the
            // connection teardown that releases this track need that lock promptly.
            // Holding it here made a stalled modem route block cleanup until the
            // join timed out, which is how tracks were being abandoned.
            // WRITE_BLOCKING is allowed to return a positive short count. More
            // importantly, an urgent interruption deliberately pauses and flushes
            // this track from the control thread, which makes an in-flight write
            // return the bytes accepted before the flush. That is a generation
            // boundary, not a dead modem route. Finish ordinary short writes and
            // discard only the stale remainder when a flush advanced generation.
            int writeOffset = 0;
            int zeroWriteRetries = 0;
            boolean generationInterruptedWrite = false;
            while (writeOffset < pcm.length) {
                int written = track.write(
                        pcm,
                        writeOffset,
                        pcm.length - writeOffset,
                        AudioTrack.WRITE_BLOCKING
                );
                try { audioTrackUnderruns = track.getUnderrunCount(); } catch (Exception ignored) {}

                if (!playoutRunning.get() || !running) return;
                // Telecom can tear the modem route down while a blocking write
                // is in progress. Vendor AudioTrack implementations commonly
                // return a positive short write followed by zero in that race.
                // That is normal call teardown, not evidence that in-call
                // playout failed. Re-check the authoritative call state before
                // retrying or publishing a persistent media error.
                if (!"ACTIVE".equals(CallManager.getCallState())) return;
                if (generation.get() != writeGeneration) {
                    generationInterruptedWrite = true;
                    break;
                }
                if (written > 0) {
                    writeOffset += written;
                    zeroWriteRetries = 0;
                    continue;
                }
                if (written == 0 && zeroWriteRetries++ < 8) {
                    Thread.yield();
                    continue;
                }
                recordError(
                        "Telephony playout write failed",
                        new IOException(
                                "AudioTrack.write returned " + written + " after "
                                        + writeOffset + " of " + pcm.length + " bytes"
                        )
                );
                return;
            }
            if (generationInterruptedWrite) {
                // flushOutput() already cleared the old generation from AudioTrack
                // and the application queue. Resume the same long-lived writer on
                // the new generation instead of permanently silencing the call.
                speechActive = false;
                playoutPrebuffering = true;
                continue;
            }
            if (isCurrentAudio) {
                lastRenderedSequence.set(queued.sequence);
                uplinkBytes.addAndGet(pcm.length);
                audioPlayoutFrames.incrementAndGet();
            } else {
                silencePlayoutFrames.incrementAndGet();
            }
            if (consumed != null && consumed.generation == generation.get()) {
                try {
                    sendPlayoutAck(output, key, binding, consumed);
                } catch (Exception error) {
                    // onCellularCallEnded deliberately closes activeTxSocket so
                    // no additional bytes can reach a dead modem route. A writer
                    // already finishing its last frame can observe that close;
                    // this is normal teardown, not failed in-call playout.
                    if (isExpectedPeerDisconnect(error)
                            || !"ACTIVE".equals(CallManager.getCallState())) {
                        Log.i(TAG, "Playout ACK stopped after the call ended");
                    } else {
                        recordError("Telephony playout acknowledgement failed", error);
                    }
                    return;
                }
            }
        }
    }

    private static boolean enqueueCurrent(
            ArrayBlockingQueue<QueuedAudio> queue,
            QueuedAudio queued,
            LinkSessionRegistry.Binding binding
    ) throws InterruptedException {
        while (running && LinkSessionRegistry.isCurrent(binding)
                && queued.generation == generation.get()) {
            if (!queue.offer(queued, 20, TimeUnit.MILLISECONDS)) continue;
            if (queued.generation == generation.get()
                    && LinkSessionRegistry.isCurrent(binding)) return true;
            queue.remove(queued);
            return false;
        }
        return false;
    }

    private static void sendPlayoutAck(
            OutputStream output,
            byte[] key,
            LinkSessionRegistry.Binding binding,
            QueuedAudio consumed
    ) throws Exception {
        JSONObject body = new JSONObject();
        body.put("type", "audio.playout.ack");
        body.put("status", "ok");
        body.put("generation", consumed.generation);
        body.put("sequence", consumed.sequence);
        ProtocolCodec.write(
                output,
                ProtocolCodec.jsonFrame(
                        ProtocolCodec.KIND_ACK,
                        ProtocolCodec.DIRECTION_PHONE_TO_MAC,
                        0,
                        binding.callId,
                        consumed.generation,
                        consumed.sequence,
                        body
                ),
                key
        );
        playoutAcksSent.incrementAndGet();
    }

    private static boolean containsEndMarker(
            ArrayBlockingQueue<QueuedAudio> queue, long currentGeneration
    ) {
        for (QueuedAudio queued : queue) {
            if (queued.endOfStream && queued.generation == currentGeneration) return true;
        }
        return false;
    }

    private static void updatePeakQueueDepth(long depth) {
        while (true) {
            long previous = peakPlayoutQueueDepth.get();
            if (depth <= previous || peakPlayoutQueueDepth.compareAndSet(previous, depth)) return;
        }
    }

    private static AudioTrack createTelephonyInjectionTrack() {
        Context context = appContext;
        if (context == null) throw new IllegalStateException("Audio bridge context is unavailable");
        AudioManager manager = (AudioManager) context.getSystemService(Context.AUDIO_SERVICE);
        if (manager == null) throw new IllegalStateException("AudioManager unavailable");

        AudioDeviceInfo telephonyTx = null;
        for (AudioDeviceInfo device : manager.getDevices(AudioManager.GET_DEVICES_OUTPUTS)) {
            if (device.getType() == AudioDeviceInfo.TYPE_TELEPHONY) {
                telephonyTx = device;
                break;
            }
        }
        if (telephonyTx == null) {
            throw new IllegalStateException("Telephony TX output is not exposed in the current call state");
        }

        int minimum = AudioTrack.getMinBufferSize(
                INJECTION_SAMPLE_RATE,
                AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT
        );
        int bufferSize = Math.max(minimum, NETWORK_CHUNK_BYTES * 2);
        AudioTrack track = new AudioTrack.Builder()
                .setAudioAttributes(new AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build())
                .setAudioFormat(new AudioFormat.Builder()
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(INJECTION_SAMPLE_RATE)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .build())
                .setBufferSizeInBytes(bufferSize)
                .setTransferMode(AudioTrack.MODE_STREAM)
                .build();

        if (track.getState() != AudioTrack.STATE_INITIALIZED) {
            track.release();
            throw new IllegalStateException("Telephony injection AudioTrack did not initialize");
        }
        if (!track.setPreferredDevice(telephonyTx)) {
            track.release();
            throw new IllegalStateException("Audio policy rejected explicit Telephony TX selection");
        }
        track.setVolume(AudioTrack.getMaxVolume());
        return track;
    }

    /** Start the modem-routed track without Android's incompatible FAST output flag. */
    private static AudioTrack createStartedTelephonyInjectionTrack() throws Exception {
        Exception lastFailure = null;
        for (int attempt = 1; attempt <= TRACK_START_ATTEMPTS; attempt++) {
            cellularRouteTouched = true;
            telephonyTrackStartAttempts.incrementAndGet();
            // Re-check the call on every attempt, not once before the loop. The
            // retries sleep, and a call that ends inside one of those sleeps
            // leaves Telephony TX still exposed with nothing behind it. Building
            // a track against it then permanently orphans one of the
            // INCALL_MUSIC output's limited slots even though release() is
            // called correctly. Once enough slots are gone every later call
            // fails its prime write with -1 and the remote party hears silence,
            // which is the fault this loop was previously creating for itself.
            String callState = CallManager.getCallState();
            if (!"ACTIVE".equals(callState)) {
                throw new IllegalStateException(
                        "telephony injection requires an ACTIVE call before attempt "
                                + attempt + "; Telecom reports " + callState,
                        lastFailure
                );
            }
            AudioTrack track = null;
            try {
                track = createTelephonyInjectionTrack();
                // Starting first settles the preferred telephony route before
                // the blocking prime write enters AudioFlinger.
                track.play();
                int primed = track.write(
                        SILENCE_FRAME, 0, SILENCE_FRAME.length, AudioTrack.WRITE_BLOCKING
                );
                if (primed != SILENCE_FRAME.length) {
                    throw new IllegalStateException(
                            "Telephony TX prime wrote " + primed + " of " + SILENCE_FRAME.length
                    );
                }
                AudioDeviceInfo routedDevice = null;
                // Audio policy routing becomes observable asynchronously after
                // play(). Give the HAL a short bounded window, then fail closed
                // rather than reporting a route that exists only as a preferred
                // device request.
                for (int routeCheck = 0; routeCheck < 5; routeCheck++) {
                    routedDevice = track.getRoutedDevice();
                    if (routedDevice != null
                            && routedDevice.getType() == AudioDeviceInfo.TYPE_TELEPHONY) {
                        break;
                    }
                    Thread.sleep(20L);
                }
                if (routedDevice == null
                        || routedDevice.getType() != AudioDeviceInfo.TYPE_TELEPHONY) {
                    throw new IllegalStateException(
                            "Audio policy did not route the active track to Telephony TX; "
                                    + "actual device="
                                    + (routedDevice == null
                                            ? "none"
                                            : routedDevice.getType())
                    );
                }
                injectionRoute = "telephony_tx_modem_clock_standard_stream";
                injectionProof = "android_audio_policy_routed_to_telephony";
                Log.i(TAG, "Started modem-clock Telephony TX route attempt=" + attempt);
                return track;
            } catch (Exception failure) {
                lastFailure = failure;
                Log.w(
                        TAG,
                        "Telephony TX route start failed attempt=" + attempt + "/"
                                + TRACK_START_ATTEMPTS + ": " + failure,
                        failure
                );
                if (track != null) {
                    try { track.pause(); } catch (Exception ignored) {}
                    try { track.flush(); } catch (Exception ignored) {}
                    track.release();
                }
                if (attempt < TRACK_START_ATTEMPTS) {
                    try {
                        Thread.sleep(120L * attempt);
                    } catch (InterruptedException interrupted) {
                        Thread.currentThread().interrupt();
                        throw interrupted;
                    }
                }
            }
        }
        // Surface the real reason. The wrapper message alone was indistinguishable
        // between "no call is up", "audio policy refused the route", and "the
        // telephony output has no free track slots left", which are three very
        // different faults with three different remedies.
        throw new IllegalStateException(
                "Could not start Telephony TX AudioTrack after " + TRACK_START_ATTEMPTS
                        + " attempts; last cause: " + describeCause(lastFailure)
                        + ". If this persists while a call is ACTIVE, the telephony output is"
                        + " probably saturated by orphaned tracks; restarting audioserver"
                        + " releases them.",
                lastFailure
        );
    }

    /**
     * Tear down call-owned media and clear vendor-restored Telephony TX tracks.
     *
     * <p>On this reviewed MTK/GSI device, setPreferredDevice(TYPE_TELEPHONY)
     * restores the Java AudioTrack onto INCALL_MUSIC. AudioFlinger can retain
     * that restored track after stop/flush/release and eventually reaches its
     * 40-track ceiling. Binder-level release is therefore not a sufficient
     * cleanup boundary. Restarting audioserver after Telecom has removed the
     * call is the only verified operation that releases those native orphans.
     */
    public static void onCellularCallEnded() {
        if (voipMode || !cellularRouteTouched) return;

        synchronized (OUTPUT_LOCK) {
            if (activePlaybackQueue != null) activePlaybackQueue.clear();
            if (activeTrack != null) stopTrackQuietly(activeTrack);
            if (activeTxSocket != null) {
                try { activeTxSocket.close(); } catch (Exception ignored) {}
            }
        }

        if (!audioServerRecoveryScheduled.compareAndSet(false, true)) return;
        audioServerRecoveryStatus = "scheduled";
        audioServerRecoveryDetail = "waiting_for_call_media_release";
        Thread recovery = new Thread(() -> {
            try {
                // Let streamUplink stop, join and release its Java track first.
                Thread.sleep(1500L);
                String state = CallManager.getCallState();
                if (!"IDLE".equals(state) && !"DISCONNECTED".equals(state)) {
                    audioServerRecoveryStatus = "deferred_call_" + state.toLowerCase();
                    return;
                }

                // Execute audioserver restart with resilient multi-binary fallback
                // 1) phh-su with explicit target uid 0
                // 2) standard su -c
                // 3) explicit root binary paths (/system/bin/su, /system/xbin/su)
                String[][] candidates = new String[][] {
                        new String[] { "su", "-c", AUDIO_SERVER_RECOVERY_COMMAND, "0" },
                        new String[] { "su", "-c", AUDIO_SERVER_RECOVERY_COMMAND },
                        new String[] { "/system/bin/su", "-c", AUDIO_SERVER_RECOVERY_COMMAND },
                        new String[] { "/system/xbin/su", "-c", AUDIO_SERVER_RECOVERY_COMMAND }
                };

                Process process = null;
                String commandOutput = "";
                IOException lastError = null;

                for (String[] cmd : candidates) {
                    try {
                        process = new ProcessBuilder(cmd).redirectErrorStream(true).start();
                        if (!process.waitFor(8, TimeUnit.SECONDS)) {
                            process.destroy();
                            lastError = new IOException("audioserver recovery command timed out");
                            continue;
                        }
                        commandOutput = readBoundedProcessOutput(process.getInputStream());
                        if (process.exitValue() == 0) {
                            lastError = null;
                            break;
                        } else {
                            lastError = new IOException(
                                    "command exited " + process.exitValue()
                                            + (commandOutput.isEmpty() ? "" : ": " + commandOutput)
                            );
                        }
                    } catch (IOException ioExc) {
                        lastError = ioExc;
                    }
                }

                if (lastError != null) {
                    throw lastError;
                }
                audioServerRecoveries.incrementAndGet();
                cellularRouteTouched = false;
                audioServerRecoveryStatus = "completed";
                audioServerRecoveryDetail = commandOutput.isEmpty()
                        ? "audioserver_restart_verified"
                        : commandOutput;
                clearError("Post-call audioserver recovery failed");
                Log.i(TAG, "Restarted audioserver after cellular call to release Telephony TX tracks");
            } catch (Exception failure) {
                audioServerRecoveryStatus = "failed:" + failure.getClass().getSimpleName();
                audioServerRecoveryDetail = describeCause(failure);
                recordError("Post-call audioserver recovery failed", failure);
            } finally {
                audioServerRecoveryScheduled.set(false);
            }
        }, "gateway-post-call-audioserver-recovery");
        recovery.setDaemon(true);
        recovery.start();
    }

    private static String describeCause(Throwable failure) {
        if (failure == null) return "unknown";
        StringBuilder description = new StringBuilder();
        Throwable current = failure;
        for (int depth = 0; current != null && depth < 4; depth++) {
            if (depth > 0) description.append(" <- ");
            description.append(current.getClass().getSimpleName());
            if (current.getMessage() != null) {
                description.append(": ").append(current.getMessage());
            }
            current = current.getCause();
        }
        return description.toString();
    }

    private static String readBoundedProcessOutput(InputStream input) throws IOException {
        byte[] buffer = new byte[256];
        StringBuilder output = new StringBuilder();
        int count;
        while (output.length() < 1024 && (count = input.read(buffer)) != -1) {
            int accepted = Math.min(count, 1024 - output.length());
            output.append(new String(buffer, 0, accepted));
        }
        return output.toString().trim();
    }

    public static long flushOutput() {
        return flushOutput(generation.get() + 1);
    }

    public static long flushOutput(long requestedGeneration) {
        long next = resynchronizeGeneration(Math.max(requestedGeneration, generation.get() + 1));
        synchronized (OUTPUT_LOCK) {
            if (activePlaybackQueue != null) activePlaybackQueue.clear();
            if (activeTrack != null) {
                try {
                    activeTrack.pause();
                    activeTrack.flush();
                    activeTrack.play();
                } catch (Exception e) {
                    recordError("AudioTrack flush failed", e);
                }
            }
        }
        Log.i(TAG, "Flushed authenticated uplink output; generation=" + next);
        return next;
    }

    public static long resynchronizeGeneration(long requestedGeneration) {
        if (requestedGeneration < 1) throw new IllegalArgumentException("generation must be >= 1");
        while (true) {
            long current = generation.get();
            if (requestedGeneration <= current) return current;
            if (generation.compareAndSet(current, requestedGeneration)) return requestedGeneration;
        }
    }

    public static long getGeneration() {
        return generation.get();
    }

    public static long getLastAcceptedSequence() {
        return lastAcceptedSequence.get();
    }

    public static long getLastRenderedSequence() {
        return lastRenderedSequence.get();
    }

    public static JSONObject getStatusJson() {
        JSONObject result = new JSONObject();
        try {
            Context context = appContext;
            boolean capturePermission = context != null
                    && context.checkSelfPermission(Manifest.permission.CAPTURE_AUDIO_OUTPUT)
                    == PackageManager.PERMISSION_GRANTED;
            boolean telephonyOutputPresent = false;
            if (context != null) {
                AudioManager manager = (AudioManager) context.getSystemService(Context.AUDIO_SERVICE);
                if (manager != null) {
                    for (AudioDeviceInfo device : manager.getDevices(AudioManager.GET_DEVICES_OUTPUTS)) {
                        if (device.getType() == AudioDeviceInfo.TYPE_TELEPHONY) {
                            telephonyOutputPresent = true;
                            break;
                        }
                    }
                }
            }
            result.put("status", "ok");
            result.put("running", running);
            result.put("rx_port", RX_PORT);
            result.put("tx_port", TX_PORT);
            result.put("rx_connected", rxConnected);
            result.put("tx_connected", txConnected);
            result.put("capture_audio_output_granted", capturePermission);
            result.put("telephony_output_present", telephonyOutputPresent);
            result.put("call_route", voipMode ? "voip" : "cellular");
            result.put("capture_source",
                    voipMode ? VoipAudioRoute.captureSource() : captureSource);
            result.put("capture_proof", "unverified_remote_caller_only");
            result.put("injection_route",
                    voipMode ? VoipAudioRoute.injectionRoute() : injectionRoute);
            result.put("injection_proof",
                    voipMode ? "unverified_remote_endpoint" : injectionProof);
            result.put("network_format", "pcm_s16le_16000_mono");
            result.put("protocol", "phag_v1_hmac_sha256");
            result.put("link_key_provisioned", context != null && LinkKeyStore.isProvisioned(context));
            result.put("link_epoch", LinkSessionRegistry.activeEpoch());
            result.put("injection_device_rate", INJECTION_SAMPLE_RATE);
            result.put("generation", generation.get());
            result.put("last_accepted_sequence", lastAcceptedSequence.get());
            result.put("last_rendered_sequence", lastRenderedSequence.get());
            result.put("stale_uplink_frames", staleUplinkFrames.get());
            result.put("audio_playout_frames", audioPlayoutFrames.get());
            result.put("silence_playout_frames", silencePlayoutFrames.get());
            result.put("speech_segments", speechSegments.get());
            result.put("end_markers_received", endMarkersReceived.get());
            result.put("mid_speech_starvation_events", midSpeechStarvationEvents.get());
            result.put("mid_speech_concealment_frames", midSpeechConcealmentFrames.get());
            result.put("peak_playout_queue_depth", peakPlayoutQueueDepth.get());
            result.put("playout_acks_sent", playoutAcksSent.get());
            result.put("playout_queue_depth",
                    activePlaybackQueue == null ? 0 : activePlaybackQueue.size());
            result.put("playout_prebuffer_frames", PLAYOUT_PREBUFFER_FRAMES);
            result.put("adaptive_playout_prebuffer_frames", adaptivePlayoutPrebufferFrames);
            result.put(
                    "max_adaptive_playout_prebuffer_frames",
                    PLAYOUT_MAX_ADAPTIVE_PREBUFFER_FRAMES
            );
            result.put("playout_prebuffering", playoutPrebuffering);
            result.put("audio_track_underruns", audioTrackUnderruns);
            result.put("telephony_track_start_attempts", telephonyTrackStartAttempts.get());
            result.put("audioserver_recoveries", audioServerRecoveries.get());
            result.put("audioserver_recovery_status", audioServerRecoveryStatus);
            result.put("audioserver_recovery_detail", audioServerRecoveryDetail);
            result.put("downlink_bytes", downlinkBytes.get());
            result.put("uplink_bytes", uplinkBytes.get());
            result.put("last_error", lastError);
        } catch (Exception ignored) {}
        return result;
    }

    public static String getSummary() {
        return (running ? "servers_ready" : "stopped")
                + ", capture=" + captureSource
                + ", injection=" + injectionRoute;
    }

    private static String sourceName(int source) {
        if (source == MediaRecorder.AudioSource.VOICE_DOWNLINK) return "VOICE_DOWNLINK";
        if (source == MediaRecorder.AudioSource.VOICE_CALL) return "VOICE_CALL_MIXED_CANDIDATE";
        if (source == MediaRecorder.AudioSource.VOICE_COMMUNICATION) return "VOICE_COMMUNICATION_MIC_FALLBACK";
        return "source_" + source;
    }

    private static ServerSocket loopbackServer(int port) throws IOException {
        ServerSocket server = new ServerSocket();
        server.setReuseAddress(true);
        server.bind(new InetSocketAddress(InetAddress.getLoopbackAddress(), port), 2);
        return server;
    }

    private static void recordError(String prefix, Exception error) {
        lastError = prefix + ": " + error.getClass().getSimpleName() + ": " + error.getMessage();
        Log.e(TAG, lastError, error);
    }

    private static void clearError(String prefix) {
        if (lastError.startsWith(prefix + ":")) lastError = "";
    }

    private static boolean isExpectedPeerDisconnect(Exception error) {
        if (!(error instanceof SocketException)) return false;
        String message = error.getMessage();
        if (message == null) return false;
        String normalized = message.toLowerCase();
        return normalized.contains("broken pipe")
                || normalized.contains("connection reset")
                || normalized.contains("socket closed");
    }

    private static void releaseActiveTrack() {
        if (activeTrack != null) {
            try { audioTrackUnderruns = activeTrack.getUnderrunCount(); } catch (Exception ignored) {}
            try { activeTrack.stop(); } catch (Exception ignored) {}
            try { activeTrack.flush(); } catch (Exception ignored) {}
            activeTrack.release();
            activeTrack = null;
        }
        if (activePlaybackQueue != null) {
            activePlaybackQueue.clear();
            activePlaybackQueue = null;
        }
    }

    public static synchronized void stop() {
        running = false;
        closeServer(rxServerSocket);
        closeServer(txServerSocket);
        rxServerSocket = null;
        txServerSocket = null;
        synchronized (OUTPUT_LOCK) {
            releaseActiveTrack();
            if (activeTxSocket != null) {
                try { activeTxSocket.close(); } catch (Exception ignored) {}
                activeTxSocket = null;
            }
        }
        if (rxServerThread != null) rxServerThread.interrupt();
        if (txServerThread != null) txServerThread.interrupt();
        rxServerThread = null;
        txServerThread = null;
        rxConnected = false;
        txConnected = false;
        appContext = null;
        Log.i(TAG, "Authenticated telephony audio bridge stopped");
    }

    private static void closeServer(ServerSocket server) {
        if (server != null) {
            try { server.close(); } catch (Exception ignored) {}
        }
    }
}
