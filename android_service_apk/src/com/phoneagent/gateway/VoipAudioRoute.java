package com.phoneagent.gateway;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioDeviceInfo;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioTrack;
import android.media.MediaRecorder;
import android.util.Log;

import java.lang.reflect.Method;

/**
 * The audio route for a VoIP call placed by an app on this phone.
 *
 * A cellular call is carried by the modem, so the telephony downlink can be
 * recorded and the uplink written to directly. A VoIP call has neither: WhatsApp
 * decodes to a normal playback stream and encodes from the microphone. So the
 * two halves are obtained differently.
 *
 * <p><b>Hearing the far end</b> — {@code REMOTE_SUBMIX} captures the system
 * output mix, which is where the caller's decoded speech lands. Apps normally
 * opt out of being captured and WhatsApp does, but that opt-out is waived for a
 * holder of {@code CAPTURE_AUDIO_OUTPUT}, which this app has as a privileged
 * system app.
 *
 * <p><b>Being heard</b> — nothing can be written into another app's microphone
 * directly. Instead an audio policy is registered with an <em>injector</em> mix:
 * a loopback mix matching a capture preset, whose {@link AudioTrack} becomes the
 * audio that apps recording with that preset receive. WhatsApp records with
 * {@code VOICE_COMMUNICATION} while the call runs, so writing to that track is
 * what the customer hears. This needs {@code MODIFY_AUDIO_ROUTING}, also held.
 *
 * <p>The audio policy classes are {@code @SystemApi} rather than public, so they
 * are reached by reflection. Failures are reported with the specific stage that
 * failed, because "no audio" has too many causes to guess between.
 *
 * <p>Nothing here is used by a cellular call. {@link DigitalAudioBridge} keeps
 * its telephony capture and injection exactly as they were and selects between
 * the two routes explicitly.
 */
final class VoipAudioRoute {

    private static final String TAG = "PhoneAgentVoipAudio";
    private static final String POLICY_PKG = "android.media.audiopolicy.";

    /** {@code AudioMixingRule.RULE_MATCH_ATTRIBUTE_CAPTURE_PRESET}. */
    private static final int RULE_MATCH_ATTRIBUTE_CAPTURE_PRESET = 1 << 1;
    /** {@code AudioMix.ROUTE_FLAG_LOOP_BACK}. */
    private static final int ROUTE_FLAG_LOOP_BACK = 1 << 1;
    /** {@code AudioManager.SUCCESS}, which is not in the public SDK. */
    private static final int AUDIO_MANAGER_SUCCESS = 0;

    /**
     * Presets to inject into, most likely first. A VoIP app in
     * {@code MODE_IN_COMMUNICATION} records with {@code VOICE_COMMUNICATION};
     * {@code MIC} is the fallback for apps that ask for the raw microphone.
     */
    private static final int[] INJECTION_PRESETS = {
            MediaRecorder.AudioSource.VOICE_COMMUNICATION,
            MediaRecorder.AudioSource.MIC,
    };

    private static volatile Object activePolicy;
    private static volatile AudioTrack preparedTrack;
    private static volatile String captureSource = "not_started";
    private static volatile String injectionRoute = "not_started";

    private VoipAudioRoute() {
    }

    static String captureSource() {
        return captureSource;
    }

    static String injectionRoute() {
        return injectionRoute;
    }

    /** Whether an app currently holds the audio mode a VoIP call runs in. */
    static boolean callInProgress(Context context) {
        AudioManager audio = context.getSystemService(AudioManager.class);
        return audio != null && audio.getMode() == AudioManager.MODE_IN_COMMUNICATION;
    }

    // -- hearing the far end ---------------------------------------------------

    /**
     * Record the system output mix, which carries the far end's decoded speech.
     *
     * <p>{@code VOICE_COMMUNICATION} is deliberately not a fallback here. It
     * would initialize happily and return the phone's own microphone, so the
     * agent would transcribe the room instead of the customer — a failure that
     * looks like success. Better to fail loudly.
     */
    static AudioRecord createRecorder(int sampleRate, int chunkBytes) {
        int minimum = AudioRecord.getMinBufferSize(
                sampleRate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
        );
        int bufferSize = Math.max(minimum, chunkBytes * 4);
        AudioRecord recorder = null;
        try {
            recorder = new AudioRecord.Builder()
                    .setAudioSource(MediaRecorder.AudioSource.REMOTE_SUBMIX)
                    .setAudioFormat(new AudioFormat.Builder()
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .setSampleRate(sampleRate)
                            .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                            .build())
                    .setBufferSizeInBytes(bufferSize)
                    .build();
            if (recorder.getState() == AudioRecord.STATE_INITIALIZED) {
                captureSource = "remote_submix_system_output_mix";
                Log.i(TAG, "VoIP downlink capture: " + captureSource);
                return recorder;
            }
            recorder.release();
        } catch (Exception failure) {
            if (recorder != null) recorder.release();
            throw new IllegalStateException(
                    "REMOTE_SUBMIX capture failed; CAPTURE_AUDIO_OUTPUT is required: "
                            + failure, failure
            );
        }
        throw new IllegalStateException(
                "REMOTE_SUBMIX did not initialize. Without it the far end cannot be heard."
        );
    }

    // -- being heard -----------------------------------------------------------

    /**
     * Register an injector mix and return the track whose audio the VoIP app
     * receives as its microphone input. Already started and primed.
     */
    static synchronized AudioTrack createStartedInjectionTrack(
            Context context, int sampleRate, byte[] primeFrame) throws Exception {
        // Registered ahead of the call by prepare(); reused here rather than
        // rebuilt, because rebuilding it now would be too late to be heard.
        AudioTrack ready = preparedTrack;
        if (ready != null) {
            Log.i(TAG, "VoIP uplink injection: reusing the pre-registered mix");
            return ready;
        }
        return openInjectionTrack(context, sampleRate, primeFrame);
    }

    /**
     * Register the injector mix before the VoIP call is placed.
     *
     * <p>Android binds a recording to a policy mix when the recorder starts, so
     * a mix registered after the call began matches nothing: WhatsApp keeps the
     * real microphone, nothing drains the injector track, and its writes stall.
     * The symptom is silence with no error — the agent speaks, the bytes are
     * accepted, and the playout acknowledgements the caller waits on never come.
     * Registering here, while the route is selected and before WhatsApp opens
     * its microphone, is what makes the injection audible.
     */
    static synchronized void prepare(Context context, int sampleRate, byte[] primeFrame) {
        if (preparedTrack != null) return;
        try {
            preparedTrack = openInjectionTrack(context, sampleRate, primeFrame);
        } catch (Exception failure) {
            // Not fatal here: the uplink retries when it connects, and reports
            // the failure then with the call in front of it.
            Log.w(TAG, "could not pre-register the injector mix: " + failure);
        }
    }

    /**
     * Point VoIP recording at the device the injector feeds.
     *
     * <p>Registering the mix is not enough on this platform: WhatsApp's capture
     * is routed through a {@code voip_tx} hardware input wired straight to the
     * built-in microphone, and a hardware path does not consult audio policy
     * mixes — so the injector is registered, correct, and never read. Pinning
     * the capture preset to the remote submix input moves that recording onto
     * the software path the mix actually feeds.
     *
     * <p>Done at runtime rather than by editing the vendor audio policy, which
     * on this GSI only survives a reflash: {@code adb remount} is an overlay
     * that a reboot discards, and the config is only read at boot.
     *
     * @return true if the preset was pinned
     */
    private static boolean pinCapturePresetToSubmix(Context context, int capturePreset) {
        AudioManager audio = context.getSystemService(AudioManager.class);
        if (audio == null) return false;
        AudioDeviceInfo submix = null;
        for (AudioDeviceInfo device : audio.getDevices(AudioManager.GET_DEVICES_INPUTS)) {
            if (device.getType() == AudioDeviceInfo.TYPE_REMOTE_SUBMIX) {
                submix = device;
                break;
            }
        }
        if (submix == null) {
            Log.w(TAG, "no remote submix input device to pin capture to");
            return false;
        }
        try {
            Class<?> attributesClass = Class.forName("android.media.AudioDeviceAttributes");
            Object attributes = attributesClass
                    .getConstructor(AudioDeviceInfo.class)
                    .newInstance(submix);
            Boolean pinned = (Boolean) AudioManager.class
                    .getMethod(
                            "setPreferredDeviceForCapturePreset",
                            int.class, attributesClass)
                    .invoke(audio, capturePreset, attributes);
            Log.i(TAG, "pinned capture preset " + capturePreset + " to remote submix: " + pinned);
            return Boolean.TRUE.equals(pinned);
        } catch (Exception failure) {
            Log.w(TAG, "could not pin capture preset " + capturePreset + ": " + failure);
            return false;
        }
    }

    /** Release the pin so ordinary recording returns to the microphone. */
    private static void unpinCapturePresets() {
        Context context = DigitalAudioBridge.applicationContext();
        if (context == null) return;
        AudioManager audio = context.getSystemService(AudioManager.class);
        if (audio == null) return;
        for (int preset : INJECTION_PRESETS) {
            try {
                AudioManager.class
                        .getMethod("clearPreferredDevicesForCapturePreset", int.class)
                        .invoke(audio, preset);
            } catch (Exception failure) {
                Log.w(TAG, "could not unpin capture preset " + preset + ": " + failure);
            }
        }
    }

    private static AudioTrack openInjectionTrack(
            Context context, int sampleRate, byte[] primeFrame) throws Exception {
        releasePolicy();
        AudioFormat format = new AudioFormat.Builder()
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setSampleRate(sampleRate)
                .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                .build();

        Exception lastFailure = null;
        for (int preset : INJECTION_PRESETS) {
            try {
                AudioTrack track = registerInjectorMix(context, format, preset);
                track.play();
                int primed = track.write(
                        primeFrame, 0, primeFrame.length, AudioTrack.WRITE_BLOCKING
                );
                if (primed != primeFrame.length) {
                    throw new IllegalStateException(
                            "injector prime wrote " + primed + " of " + primeFrame.length
                    );
                }
                injectionRoute = "audio_policy_injector_mix_preset_" + preset;
                if (pinCapturePresetToSubmix(context, preset)) {
                    injectionRoute += "_pinned_submix";
                }
                Log.i(TAG, "VoIP uplink injection: " + injectionRoute);
                return track;
            } catch (Exception failure) {
                lastFailure = failure;
                Log.w(TAG, "injector mix failed for capture preset " + preset + ": " + failure);
                releasePolicy();
            }
        }
        throw new IllegalStateException(
                "No injector mix could be registered; MODIFY_AUDIO_ROUTING and system-app "
                        + "hidden API access are required: " + lastFailure, lastFailure
        );
    }

    /**
     * Build and register the policy, returning its injector track.
     *
     * <p>Reflection throughout: every class used here is {@code @SystemApi}.
     */
    private static AudioTrack registerInjectorMix(
            Context context, AudioFormat format, int capturePreset) throws Exception {
        // An AudioAttributes carrying the capture preset the mix should match.
        AudioAttributes.Builder attributesBuilder = new AudioAttributes.Builder();
        Method setCapturePreset =
                AudioAttributes.Builder.class.getMethod("setCapturePreset", int.class);
        setCapturePreset.invoke(attributesBuilder, capturePreset);
        AudioAttributes attributes = attributesBuilder.build();

        Class<?> ruleBuilderClass = Class.forName(POLICY_PKG + "AudioMixingRule$Builder");
        Object ruleBuilder = ruleBuilderClass.getConstructor().newInstance();
        ruleBuilderClass
                .getMethod("addMixRule", int.class, Object.class)
                .invoke(ruleBuilder, RULE_MATCH_ATTRIBUTE_CAPTURE_PRESET, attributes);
        Object rule = ruleBuilderClass.getMethod("build").invoke(ruleBuilder);

        Class<?> mixBuilderClass = Class.forName(POLICY_PKG + "AudioMix$Builder");
        Class<?> ruleClass = Class.forName(POLICY_PKG + "AudioMixingRule");
        Object mixBuilder = mixBuilderClass.getConstructor(ruleClass).newInstance(rule);
        mixBuilderClass.getMethod("setFormat", AudioFormat.class).invoke(mixBuilder, format);
        mixBuilderClass
                .getMethod("setRouteFlags", int.class)
                .invoke(mixBuilder, ROUTE_FLAG_LOOP_BACK);
        Object mix = mixBuilderClass.getMethod("build").invoke(mixBuilder);

        Class<?> policyBuilderClass = Class.forName(POLICY_PKG + "AudioPolicy$Builder");
        Object policyBuilder =
                policyBuilderClass.getConstructor(Context.class).newInstance(context);
        Class<?> mixClass = Class.forName(POLICY_PKG + "AudioMix");
        policyBuilderClass.getMethod("addMix", mixClass).invoke(policyBuilder, mix);
        Object policy = policyBuilderClass.getMethod("build").invoke(policyBuilder);

        AudioManager audio = context.getSystemService(AudioManager.class);
        Class<?> policyClass = Class.forName(POLICY_PKG + "AudioPolicy");
        int status = (Integer) AudioManager.class
                .getMethod("registerAudioPolicy", policyClass)
                .invoke(audio, policy);
        if (status != AUDIO_MANAGER_SUCCESS) {
            throw new IllegalStateException("registerAudioPolicy returned " + status);
        }
        activePolicy = policy;

        AudioTrack track = (AudioTrack) policyClass
                .getMethod("createAudioTrackSource", mixClass)
                .invoke(policy, mix);
        if (track == null || track.getState() != AudioTrack.STATE_INITIALIZED) {
            if (track != null) track.release();
            throw new IllegalStateException("injector AudioTrack did not initialize");
        }
        return track;
    }

    /** Release the injector track and its policy. Ends VoIP mode entirely. */
    static synchronized void release() {
        AudioTrack track = preparedTrack;
        preparedTrack = null;
        if (track != null) {
            try { track.pause(); } catch (Exception ignored) { }
            try { track.release(); } catch (Exception ignored) { }
        }
        unpinCapturePresets();
        releasePolicy();
    }

    /** Unregister the policy so the microphone returns to the real device. */
    private static synchronized void releasePolicy() {
        Object policy = activePolicy;
        activePolicy = null;
        injectionRoute = "not_started";
        captureSource = "not_started";
        if (policy == null) return;
        try {
            AudioManager audio =
                    DigitalAudioBridge.applicationContext().getSystemService(AudioManager.class);
            Class<?> policyClass = Class.forName(POLICY_PKG + "AudioPolicy");
            AudioManager.class
                    .getMethod("unregisterAudioPolicy", policyClass)
                    .invoke(audio, policy);
        } catch (Exception failure) {
            Log.w(TAG, "unregistering the injector policy failed: " + failure);
        }
    }
}
