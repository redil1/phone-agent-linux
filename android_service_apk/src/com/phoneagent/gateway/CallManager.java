package com.phoneagent.gateway;

import android.content.Context;
import android.net.Uri;
import android.os.Bundle;
import android.telecom.Call;
import android.telecom.TelecomManager;
import android.telecom.VideoProfile;
import android.telephony.PhoneNumberUtils;
import android.util.Log;

public class CallManager {
    private static final String TAG = "PhoneAgentCallManager";

    private static volatile Call activeCall = null;
    private static volatile String activeCallerNumber = "";
    private static volatile String callState = "IDLE";
    private static volatile int callStateCode = 0;
    private static volatile String lastError = "";

    public static synchronized void onCallAdded(Call call) {
        if (activeCall != null && activeCall != call
                && activeCall.getState() != Call.STATE_DISCONNECTED) {
            Log.w(TAG, "Replacing tracked call because Telecom delivered a newer Call object");
        }
        activeCall = call;
        lastError = "";
        updateCallState(call);

        call.registerCallback(new Call.Callback() {
            @Override
            public void onStateChanged(Call call, int state) {
                updateCallState(call);
            }
        });
    }

    public static synchronized void onCallRemoved(Call call) {
        if (activeCall == call) {
            activeCall = null;
            activeCallerNumber = "";
            callState = "IDLE";
            callStateCode = 0;
            Log.i(TAG, "Call removed, state reset to IDLE");
        }
    }

    private static void updateCallState(Call call) {
        int state = call.getState();
        callStateCode = state;

        if (call.getDetails() != null && call.getDetails().getHandle() != null) {
            activeCallerNumber = call.getDetails().getHandle().getSchemeSpecificPart();
        }

        switch (state) {
            case Call.STATE_RINGING:
                callState = "RINGING";
                break;
            case Call.STATE_DIALING:
            case Call.STATE_CONNECTING:
                callState = "DIALING";
                PhoneAgentInCallService.mutePhysicalMicrophone(true);
                break;
            case Call.STATE_ACTIVE:
                callState = "ACTIVE";
                PhoneAgentInCallService.mutePhysicalMicrophone(true);
                break;
            case Call.STATE_HOLDING:
                callState = "HOLDING";
                break;
            case Call.STATE_SELECT_PHONE_ACCOUNT:
                callState = "SELECT_PHONE_ACCOUNT";
                break;
            case Call.STATE_NEW:
                callState = "NEW";
                break;
            case Call.STATE_DISCONNECTED:
            case Call.STATE_DISCONNECTING:
                callState = "DISCONNECTED";
                break;
            default:
                callState = "UNKNOWN";
                break;
        }
        Log.i(TAG, "Call State Updated: " + callState + " (" + state + "), Caller: " + activeCallerNumber);
    }

    public static boolean placeCall(Context context, String number) {
        lastError = "";
        try {
            if (number == null || number.trim().isEmpty()) {
                lastError = "Missing telephone number";
                return false;
            }
            if (PhoneNumberUtils.isEmergencyNumber(number)) {
                lastError = "Automated emergency calls are forbidden";
                Log.w(TAG, lastError);
                return false;
            }
            Call current = activeCall;
            if (current != null
                    && current.getState() != Call.STATE_DISCONNECTED
                    && current.getState() != Call.STATE_DISCONNECTING) {
                lastError = "Another call is already in progress";
                return false;
            }
            TelecomManager tm = (TelecomManager) context.getSystemService(Context.TELECOM_SERVICE);
            if (tm != null) {
                // Normalize Moroccan numbers if dialing via local SIM
                String cleanNumber = number.trim().replaceAll("[\\s\\-\\(\\)]", "");
                if (cleanNumber.startsWith("00212")) {
                    cleanNumber = "0" + cleanNumber.substring(5);
                } else if (cleanNumber.startsWith("+212")) {
                    cleanNumber = "0" + cleanNumber.substring(4);
                }

                Uri uri = Uri.parse("tel:" + cleanNumber);
                Bundle extras = new Bundle();

                // Target active SIM account explicitly
                try {
                    java.util.List<android.telecom.PhoneAccountHandle> accounts = tm.getCallCapablePhoneAccounts();
                    if (accounts != null && !accounts.isEmpty()) {
                        extras.putParcelable(TelecomManager.EXTRA_PHONE_ACCOUNT_HANDLE, accounts.get(0));
                    }
                } catch (Exception ignored) {}

                tm.placeCall(uri, extras);
                Log.i(TAG, "Headless placeCall executed for normalized number: " + cleanNumber);
                return true;
            }
            lastError = "TelecomManager unavailable";
        } catch (Exception e) {
            lastError = e.getClass().getSimpleName() + ": " + e.getMessage();
            Log.e(TAG, "Failed to place call headlessly: " + e.getMessage(), e);
        }
        return false;
    }

    public static boolean answerCall() {
        lastError = "";
        if (activeCall != null && activeCall.getState() == Call.STATE_RINGING) {
            try {
                activeCall.answer(VideoProfile.STATE_AUDIO_ONLY);
                Log.i(TAG, "Headless answerCall executed successfully");
                return true;
            } catch (Exception e) {
                lastError = e.getClass().getSimpleName() + ": " + e.getMessage();
                Log.e(TAG, "Error answering call: " + e.getMessage(), e);
            }
        }
        if (lastError.isEmpty()) lastError = "No ringing call";
        return false;
    }

    public static boolean rejectCall() {
        lastError = "";
        if (activeCall != null && activeCall.getState() == Call.STATE_RINGING) {
            try {
                activeCall.reject(false, null);
                Log.i(TAG, "Headless rejectCall executed successfully");
                return true;
            } catch (Exception e) {
                lastError = e.getClass().getSimpleName() + ": " + e.getMessage();
                Log.e(TAG, "Error rejecting call: " + e.getMessage(), e);
            }
        }
        if (lastError.isEmpty()) lastError = "No ringing call";
        return false;
    }

    public static boolean hangupCall() {
        lastError = "";
        if (activeCall != null) {
            try {
                activeCall.disconnect();
                Log.i(TAG, "Headless hangupCall executed successfully");
                return true;
            } catch (Exception e) {
                lastError = e.getClass().getSimpleName() + ": " + e.getMessage();
                Log.e(TAG, "Error hanging up call: " + e.getMessage(), e);
            }
        }
        if (lastError.isEmpty()) lastError = "No tracked call";
        return false;
    }

    public static boolean sendDtmf(char digit) {
        lastError = "";
        if ("0123456789*#".indexOf(digit) < 0) {
            lastError = "Invalid DTMF digit";
            return false;
        }
        if (activeCall != null && activeCall.getState() == Call.STATE_ACTIVE) {
            try {
                activeCall.playDtmfTone(digit);
                try {
                    Thread.sleep(120);
                } catch (InterruptedException interrupted) {
                    Thread.currentThread().interrupt();
                }
                activeCall.stopDtmfTone();
                Log.i(TAG, "Sent DTMF digit: " + digit);
                return true;
            } catch (Exception e) {
                lastError = e.getClass().getSimpleName() + ": " + e.getMessage();
                Log.e(TAG, "Error sending DTMF: " + e.getMessage(), e);
            }
        }
        if (lastError.isEmpty()) lastError = "No active call";
        return false;
    }

    public static String getCallState() {
        return callState;
    }

    public static int getCallStateCode() {
        return callStateCode;
    }

    public static String getActiveCallerNumber() {
        return activeCallerNumber;
    }

    public static String getLastError() {
        return lastError;
    }
}
