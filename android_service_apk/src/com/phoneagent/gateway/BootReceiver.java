package com.phoneagent.gateway;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.util.Log;

public class BootReceiver extends BroadcastReceiver {
    private static final String TAG = "PhoneAgentBootReceiver";

    @Override
    public void onReceive(Context context, Intent intent) {
        Log.i(TAG, "BootReceiver received action: " + intent.getAction());
        if (!GatewayService.isDialerRoleHeld(context)) {
            Log.w(TAG, "Gateway not started after boot because ROLE_DIALER is not held");
            return;
        }
        Intent serviceIntent = new Intent(context, GatewayService.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            context.startForegroundService(serviceIntent);
        } else {
            context.startService(serviceIntent);
        }
    }
}
