package com.phoneagent.gateway;

import android.telecom.Call;
import android.telecom.InCallService;
import android.util.Log;

public class PhoneAgentInCallService extends InCallService {
    private static final String TAG = "PhoneAgentInCallService";
    private static volatile PhoneAgentInCallService activeService = null;

    @Override
    public void onCallAdded(Call call) {
        super.onCallAdded(call);
        activeService = this;
        Log.i(TAG, "Headless InCallService: onCallAdded received");
        CallManager.onCallAdded(call);
        try {
            setMuted(true);
            Log.i(TAG, "InCallService setMuted(true) applied immediately on call add");
        } catch (Exception e) {
            Log.e(TAG, "Failed to apply initial setMuted: " + e.getMessage());
        }
    }

    @Override
    public void onCallRemoved(Call call) {
        super.onCallRemoved(call);
        Log.i(TAG, "Headless InCallService: onCallRemoved received");
        CallManager.onCallRemoved(call);
        if (activeService == this) {
            activeService = null;
        }
    }

    public static void mutePhysicalMicrophone(boolean muted) {
        PhoneAgentInCallService service = activeService;
        if (service != null) {
            try {
                service.setMuted(muted);
                Log.i(TAG, "InCallService setMuted(" + muted + ") executed successfully");
            } catch (Exception e) {
                Log.e(TAG, "Failed to execute setMuted: " + e.getMessage());
            }
        }
    }
}
