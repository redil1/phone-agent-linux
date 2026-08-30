package com.phoneagent.gateway;

import android.app.Activity;
import android.app.role.RoleManager;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.telephony.PhoneNumberUtils;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

/**
 * Minimal dialer and recovery UI.
 *
 * The gateway is intentionally headless during normal operation, but Android's
 * ROLE_DIALER contract requires a dial surface and an in-call UI capability.
 * This activity also gives an operator a safe way to recover the appliance.
 */
public class DialerActivity extends Activity {
    private static final int REQUEST_DIALER_ROLE = 1001;

    private TextView statusView;
    private EditText numberInput;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        buildUi();
        populateNumberFromIntent(getIntent());
        refreshStatus();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        populateNumberFromIntent(intent);
        refreshStatus();
    }

    @Override
    protected void onResume() {
        super.onResume();
        refreshStatus();
    }

    private void buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(36, 36, 36, 36);
        root.setGravity(Gravity.CENTER_HORIZONTAL);

        TextView title = new TextView(this);
        title.setText("PhoneAgent Cellular Gateway");
        title.setTextSize(24);
        title.setTextColor(Color.BLACK);
        title.setPadding(0, 0, 0, 24);
        root.addView(title, matchWrap());

        statusView = new TextView(this);
        statusView.setTextSize(16);
        statusView.setTextColor(Color.DKGRAY);
        statusView.setPadding(0, 0, 0, 24);
        root.addView(statusView, matchWrap());

        Button roleButton = new Button(this);
        roleButton.setText("Make PhoneAgent the default phone app");
        roleButton.setOnClickListener(v -> requestDialerRole());
        root.addView(roleButton, matchWrap());

        Button startButton = new Button(this);
        startButton.setText("Start gateway service");
        startButton.setOnClickListener(v -> startGatewayService());
        root.addView(startButton, matchWrap());

        numberInput = new EditText(this);
        numberInput.setHint("Telephone number");
        numberInput.setInputType(android.text.InputType.TYPE_CLASS_PHONE);
        root.addView(numberInput, matchWrap());

        Button dialButton = new Button(this);
        dialButton.setText("Dial");
        dialButton.setOnClickListener(v -> dialNumber());
        root.addView(dialButton, matchWrap());

        LinearLayout callActions = new LinearLayout(this);
        callActions.setOrientation(LinearLayout.HORIZONTAL);

        Button answerButton = new Button(this);
        answerButton.setText("Answer");
        answerButton.setOnClickListener(v -> {
            boolean ok = CallManager.answerCall();
            toast(ok ? "Answer requested" : "No ringing call");
            refreshStatus();
        });
        callActions.addView(answerButton, weighted());

        Button hangupButton = new Button(this);
        hangupButton.setText("Hang up");
        hangupButton.setOnClickListener(v -> {
            boolean ok = CallManager.hangupCall();
            toast(ok ? "Hangup requested" : "No active call");
            refreshStatus();
        });
        callActions.addView(hangupButton, weighted());
        root.addView(callActions, matchWrap());

        setContentView(root);
    }

    private LinearLayout.LayoutParams matchWrap() {
        return new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
        );
    }

    private LinearLayout.LayoutParams weighted() {
        return new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
    }

    private void populateNumberFromIntent(Intent intent) {
        if (intent == null || numberInput == null) return;
        Uri data = intent.getData();
        if (data != null && "tel".equals(data.getScheme())) {
            numberInput.setText(data.getSchemeSpecificPart());
        }
    }

    private void requestDialerRole() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            toast("ROLE_DIALER requires Android 10 or newer");
            return;
        }
        RoleManager roleManager = (RoleManager) getSystemService(Context.ROLE_SERVICE);
        if (roleManager == null || !roleManager.isRoleAvailable(RoleManager.ROLE_DIALER)) {
            toast("Dialer role is unavailable on this ROM");
            return;
        }
        if (roleManager.isRoleHeld(RoleManager.ROLE_DIALER)) {
            toast("PhoneAgent already holds the dialer role");
            startGatewayService();
            return;
        }
        startActivityForResult(
                roleManager.createRequestRoleIntent(RoleManager.ROLE_DIALER),
                REQUEST_DIALER_ROLE
        );
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQUEST_DIALER_ROLE) {
            refreshStatus();
            if (resultCode == RESULT_OK) {
                toast("PhoneAgent is now the default phone app");
                startGatewayService();
            } else {
                toast("Dialer role was not granted");
            }
        }
    }

    private void startGatewayService() {
        Intent intent = new Intent(this, GatewayService.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent);
        } else {
            startService(intent);
        }
        toast("Gateway start requested");
    }

    private void dialNumber() {
        String number = numberInput.getText().toString().trim();
        if (number.isEmpty()) {
            toast("Enter a telephone number");
            return;
        }
        if (PhoneNumberUtils.isEmergencyNumber(number)) {
            toast("AI gateway refuses automated emergency calls");
            return;
        }
        boolean ok = CallManager.placeCall(this, number);
        toast(ok ? "Dial requested" : "Dial failed: " + CallManager.getLastError());
        refreshStatus();
    }

    private void refreshStatus() {
        statusView.setText(
                "Dialer role: " + (GatewayService.isDialerRoleHeld(this) ? "GRANTED" : "NOT GRANTED")
                        + "\nCall: " + CallManager.getCallState()
                        + "\nAudio: " + DigitalAudioBridge.getSummary()
        );
    }

    private void toast(String message) {
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show();
    }
}
