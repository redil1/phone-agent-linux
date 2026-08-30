package com.phoneagent.gateway;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.Gravity;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;

/**
 * Pair this handset with a runtime that is not on the other end of a cable.
 *
 * <p>The tunnel used to be configured by writing a JSON file over adb, which
 * meant the phone could only be set up from a terminal attached to it. That is
 * the one thing a cable-free link should not require, so the same settings live
 * here: type the address the runtime shows, switch it on, and the service
 * reconnects on its own from then on.
 */
public class RemoteLinkActivity extends Activity {

    private static final String CONFIG_NAME = "remote-link.json";
    private static final int DEFAULT_PORT = 8770;

    private EditText hostInput;
    private EditText portInput;
    private Switch enabledSwitch;
    private TextView statusView;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable poller = new Runnable() {
        @Override
        public void run() {
            refreshStatus();
            handler.postDelayed(this, 2000);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        buildUi();
        loadExistingConfig();
    }

    @Override
    protected void onResume() {
        super.onResume();
        handler.post(poller);
    }

    @Override
    protected void onPause() {
        super.onPause();
        handler.removeCallbacks(poller);
    }

    private void buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(36, 36, 36, 36);

        TextView title = new TextView(this);
        title.setText("Connect to PhoneAgent runtime");
        title.setTextSize(22);
        title.setTextColor(Color.BLACK);
        title.setPadding(0, 0, 0, 12);
        root.addView(title, matchWrap());

        TextView help = new TextView(this);
        help.setText(
                "Open PhoneAgent Studio on the computer, go to Remote Phone, and type"
                        + " the address it shows below. The phone connects out, so it works"
                        + " on wifi or mobile data without any cable.");
        help.setTextSize(14);
        help.setTextColor(Color.DKGRAY);
        help.setPadding(0, 0, 0, 24);
        root.addView(help, matchWrap());

        TextView hostLabel = new TextView(this);
        hostLabel.setText("Runtime address");
        hostLabel.setTextColor(Color.DKGRAY);
        root.addView(hostLabel, matchWrap());

        hostInput = new EditText(this);
        hostInput.setHint("192.168.1.10");
        hostInput.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        hostInput.setSingleLine(true);
        root.addView(hostInput, matchWrap());

        TextView portLabel = new TextView(this);
        portLabel.setText("Port");
        portLabel.setTextColor(Color.DKGRAY);
        portLabel.setPadding(0, 16, 0, 0);
        root.addView(portLabel, matchWrap());

        portInput = new EditText(this);
        portInput.setText(String.valueOf(DEFAULT_PORT));
        portInput.setInputType(InputType.TYPE_CLASS_NUMBER);
        portInput.setSingleLine(true);
        root.addView(portInput, matchWrap());

        enabledSwitch = new Switch(this);
        enabledSwitch.setText("Use the remote runtime instead of the cable");
        enabledSwitch.setPadding(0, 24, 0, 24);
        root.addView(enabledSwitch, matchWrap());

        Button save = new Button(this);
        save.setText("Save and connect");
        save.setOnClickListener(v -> saveAndApply());
        root.addView(save, matchWrap());

        statusView = new TextView(this);
        statusView.setTextSize(16);
        statusView.setPadding(0, 28, 0, 0);
        statusView.setGravity(Gravity.START);
        root.addView(statusView, matchWrap());

        setContentView(root);
    }

    private void loadExistingConfig() {
        try {
            File config = new File(getFilesDir(), CONFIG_NAME);
            if (!config.isFile()) return;
            JSONObject parsed = new JSONObject(
                    new String(Files.readAllBytes(config.toPath()), StandardCharsets.UTF_8));
            hostInput.setText(parsed.optString("host", ""));
            portInput.setText(String.valueOf(parsed.optInt("port", DEFAULT_PORT)));
            enabledSwitch.setChecked(parsed.optBoolean("enabled", false));
        } catch (Exception ignored) {
            // A corrupt file simply leaves the form empty rather than crashing
            // the only screen that can fix it.
        }
    }

    private void saveAndApply() {
        String host = hostInput.getText().toString().trim();
        boolean enabled = enabledSwitch.isChecked();
        if (enabled && host.isEmpty()) {
            Toast.makeText(this, "Enter the runtime address first", Toast.LENGTH_SHORT).show();
            return;
        }
        int port = DEFAULT_PORT;
        try {
            port = Integer.parseInt(portInput.getText().toString().trim());
        } catch (NumberFormatException ignored) {
        }
        if (port < 1 || port > 65535) {
            Toast.makeText(this, "Port must be between 1 and 65535", Toast.LENGTH_SHORT).show();
            return;
        }

        try {
            JSONObject payload = new JSONObject();
            payload.put("enabled", enabled);
            payload.put("host", host);
            payload.put("port", port);
            File config = new File(getFilesDir(), CONFIG_NAME);
            try (FileOutputStream stream = new FileOutputStream(config)) {
                stream.write(payload.toString().getBytes(StandardCharsets.UTF_8));
            }
        } catch (Exception failure) {
            Toast.makeText(this, "Could not save: " + failure, Toast.LENGTH_LONG).show();
            return;
        }

        // Restarting the service is what re-reads the file and dials out. Doing
        // it here is why this screen replaces the adb steps entirely.
        GatewayService.applyRemoteLinkSettings(this);
        Toast.makeText(
                        this,
                        enabled ? "Connecting to " + host + "…" : "Remote link switched off",
                        Toast.LENGTH_SHORT)
                .show();
        refreshStatus();
    }

    private void refreshStatus() {
        if (statusView == null) return;
        Boolean connected = GatewayService.remoteLinkConnected();
        if (connected == null) {
            statusView.setText("Gateway service is not running.\nStart it from the main screen.");
            statusView.setTextColor(Color.parseColor("#B26A00"));
        } else if (connected) {
            statusView.setText("● Connected to the runtime.\nThe cable is no longer needed.");
            statusView.setTextColor(Color.parseColor("#1B7F3B"));
        } else if (enabledSwitch != null && enabledSwitch.isChecked()) {
            statusView.setText("○ Not connected yet. Retrying…\nCheck the address and that the"
                    + " computer is on the same network.");
            statusView.setTextColor(Color.parseColor("#B26A00"));
        } else {
            statusView.setText("Remote link is off. The runtime reaches this phone over USB.");
            statusView.setTextColor(Color.DKGRAY);
        }
    }

    static Intent intentFor(android.content.Context context) {
        return new Intent(context, RemoteLinkActivity.class);
    }

    private ViewGroup.LayoutParams matchWrap() {
        return new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
    }
}
