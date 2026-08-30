package com.phoneagent.gateway;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.app.role.RoleManager;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;

public class GatewayService extends Service {
    private static final String TAG = "PhoneAgentGatewayService";
    private static final String CHANNEL_ID = "phoneagent_gateway_channel";
    private static final int NOTIFICATION_ID = 1001;
    private static volatile GatewayService instance;
    private volatile RemoteLinkService remoteLink;

    @Override
    public void onCreate() {
        super.onCreate();
        instance = this;
        Log.i(TAG, "GatewayService onCreate: Starting servers...");

        createNotificationChannel();
        Notification notification = buildForegroundNotification();
        startSafeForeground(notification, false);

        // Start background engines
        HttpServerEngine.start(this);
        ProtocolControlServer.start(this);
        DigitalAudioBridge.start(this);
        startRemoteLinkIfConfigured();
    }

    /**
     * Dial out to a runtime that is not on the other end of a USB cable.
     *
     * <p>Off unless an operator has paired this handset, so a phone that has
     * only ever been used over adb behaves exactly as before.
     */
    /** Restart the tunnel after the settings screen changed it. */
    public static void applyRemoteLinkSettings(android.content.Context context) {
        GatewayService service = instance;
        if (service == null) {
            // Not running yet; starting it will read the new settings.
            context.startForegroundService(new Intent(context, GatewayService.class));
            return;
        }
        if (service.remoteLink != null) {
            service.remoteLink.stop();
            service.remoteLink = null;
        }
        service.startRemoteLinkIfConfigured();
    }

    /** null when the service is not running, otherwise the tunnel state. */
    public static Boolean remoteLinkConnected() {
        GatewayService service = instance;
        if (service == null) return null;
        RemoteLinkService link = service.remoteLink;
        return link != null && link.isConnected();
    }

    private void startRemoteLinkIfConfigured() {
        try {
            java.io.File config = new java.io.File(getFilesDir(), "remote-link.json");
            if (!config.isFile()) return;
            String text = new String(
                    java.nio.file.Files.readAllBytes(config.toPath()), "UTF-8");
            org.json.JSONObject parsed = new org.json.JSONObject(text);
            if (!parsed.optBoolean("enabled", false)) return;
            String host = parsed.optString("host", "").trim();
            int port = parsed.optInt("port", 8770);
            if (host.isEmpty()) {
                Log.w(TAG, "remote link is enabled but no host is configured");
                return;
            }
            byte[] key = LinkKeyStore.requireKey(this);
            remoteLink = new RemoteLinkService(host, port, key);
            remoteLink.start();
            Log.i(TAG, "remote link starting towards " + host + ":" + port);
        } catch (Exception failure) {
            // The cable path must keep working even if the tunnel cannot start.
            Log.w(TAG, "remote link could not start: " + failure);
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        Log.i(TAG, "GatewayService onStartCommand");
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        super.onDestroy();
        Log.i(TAG, "GatewayService onDestroy");
        HttpServerEngine.stop();
        ProtocolControlServer.stop();
        DigitalAudioBridge.stop();
        if (remoteLink != null) {
            remoteLink.stop();
            remoteLink = null;
        }
        instance = null;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID,
                    "PhoneAgent Gateway Service",
                    NotificationManager.IMPORTANCE_LOW
            );
            channel.setDescription("Background Cellular Telephony & Digital Audio Gateway");
            NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm != null) {
                nm.createNotificationChannel(channel);
            }
        }
    }

    private Notification buildForegroundNotification() {
        Notification.Builder builder;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            builder = new Notification.Builder(this, CHANNEL_ID);
        } else {
            builder = new Notification.Builder(this);
        }
        return builder
                .setContentTitle("PhoneAgent Gateway Active")
                .setContentText("Cellular Telephony & Digital Audio Server Online")
                .setSmallIcon(android.R.drawable.stat_sys_phone_call)
                .setOngoing(true)
                .build();
    }

    private void startSafeForeground(Notification notification, boolean includeMicrophone) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            int type;
            if (isDialerRoleHeld(this)) {
                type = ServiceInfo.FOREGROUND_SERVICE_TYPE_PHONE_CALL;
                if (includeMicrophone) {
                    type |= ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE;
                }
            } else {
                // Setup/recovery mode: stay alive long enough for the operator to
                // grant ROLE_DIALER without requesting restricted phone-call FGS.
                type = ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC;
            }
            startForeground(NOTIFICATION_ID, notification, type);
        } else {
            startForeground(NOTIFICATION_ID, notification);
        }
    }

    public static boolean isDialerRoleHeld(Context context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return false;
        RoleManager roleManager = (RoleManager) context.getSystemService(Context.ROLE_SERVICE);
        return roleManager != null
                && roleManager.isRoleAvailable(RoleManager.ROLE_DIALER)
                && roleManager.isRoleHeld(RoleManager.ROLE_DIALER);
    }

    public static void enableMicrophoneForeground() {
        GatewayService service = instance;
        if (service == null) return;
        try {
            service.startSafeForeground(service.buildForegroundNotification(), true);
        } catch (Exception e) {
            Log.e(TAG, "Could not add microphone foreground-service type: " + e.getMessage(), e);
        }
    }
}
