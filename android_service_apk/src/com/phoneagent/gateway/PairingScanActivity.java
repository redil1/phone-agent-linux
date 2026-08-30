package com.phoneagent.gateway;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.ImageFormat;
import android.graphics.SurfaceTexture;
import android.hardware.camera2.CameraAccessException;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.media.Image;
import android.media.ImageReader;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.Log;
import android.util.Size;
import android.view.Gravity;
import android.view.TextureView;
import android.view.ViewGroup;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import com.google.zxing.BinaryBitmap;
import com.google.zxing.DecodeHintType;
import com.google.zxing.PlanarYUVLuminanceSource;
import com.google.zxing.Result;
import com.google.zxing.common.HybridBinarizer;
import com.google.zxing.qrcode.QRCodeReader;

import java.nio.ByteBuffer;
import java.util.Arrays;
import java.util.EnumMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Read the pairing QR that Studio shows, and configure this handset from it.
 *
 * <p>The shared key authenticates both the USB media protocol and the remote
 * tunnel, so the two sides disagreeing breaks everything at once and silently.
 * Moving that key by hand is what caused it; one scan now carries the key, the
 * address and the port together, so the phone cannot end up correctly keyed but
 * pointed at the wrong host.
 *
 * <p>The camera is used for pairing and nothing else: the preview stops as soon
 * as a code is read.
 */
public class PairingScanActivity extends Activity {

    private static final String TAG = "PhoneAgentPairing";
    private static final int REQUEST_CAMERA = 2001;
    private static final Size TARGET = new Size(1280, 720);

    private TextureView preview;
    private TextView statusView;
    private CameraDevice camera;
    private CameraCaptureSession session;
    private ImageReader reader;
    private HandlerThread cameraThread;
    private Handler cameraHandler;
    private final AtomicBoolean handled = new AtomicBoolean(false);
    private final QRCodeReader qrReader = new QRCodeReader();

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        buildUi();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] {Manifest.permission.CAMERA}, REQUEST_CAMERA);
            return;
        }
        startCamera();
    }

    @Override
    protected void onPause() {
        stopCamera();
        super.onPause();
    }

    @Override
    public void onRequestPermissionsResult(
            int requestCode, String[] permissions, int[] results) {
        if (requestCode != REQUEST_CAMERA) return;
        if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
            startCamera();
        } else {
            setStatus("Camera access is needed to scan the pairing code.", "#B26A00");
        }
    }

    private void buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(24, 24, 24, 24);

        TextView title = new TextView(this);
        title.setText("Scan the pairing code");
        title.setTextSize(22);
        title.setTextColor(Color.BLACK);
        root.addView(title, wrap());

        TextView help = new TextView(this);
        help.setText("In PhoneAgent Studio open Remote Phone and point the camera at the"
                + " QR code it shows.");
        help.setTextColor(Color.DKGRAY);
        help.setPadding(0, 6, 0, 16);
        root.addView(help, wrap());

        preview = new TextureView(this);
        LinearLayout.LayoutParams previewParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f);
        root.addView(preview, previewParams);

        statusView = new TextView(this);
        statusView.setTextSize(16);
        statusView.setGravity(Gravity.CENTER);
        statusView.setPadding(0, 16, 0, 0);
        root.addView(statusView, wrap());

        setContentView(root);
        setStatus("Looking for a code…", "#555555");
    }

    private void startCamera() {
        if (camera != null) return;
        handled.set(false);
        cameraThread = new HandlerThread("pairing-camera");
        cameraThread.start();
        cameraHandler = new Handler(cameraThread.getLooper());
        try {
            CameraManager manager = getSystemService(CameraManager.class);
            String cameraId = backFacingCamera(manager);
            if (cameraId == null) {
                setStatus("No camera available on this device.", "#B26A00");
                return;
            }
            reader = ImageReader.newInstance(
                    TARGET.getWidth(), TARGET.getHeight(), ImageFormat.YUV_420_888, 2);
            reader.setOnImageAvailableListener(this::onFrame, cameraHandler);
            manager.openCamera(cameraId, new CameraDevice.StateCallback() {
                @Override
                public void onOpened(CameraDevice device) {
                    camera = device;
                    createSession();
                }

                @Override
                public void onDisconnected(CameraDevice device) {
                    device.close();
                    camera = null;
                }

                @Override
                public void onError(CameraDevice device, int error) {
                    device.close();
                    camera = null;
                    runOnUiThread(() -> setStatus("Camera error " + error, "#B26A00"));
                }
            }, cameraHandler);
        } catch (CameraAccessException | SecurityException | IllegalArgumentException failure) {
            setStatus("Could not open the camera: " + failure, "#B26A00");
        }
    }

    private String backFacingCamera(CameraManager manager) throws CameraAccessException {
        for (String id : manager.getCameraIdList()) {
            Integer facing = manager.getCameraCharacteristics(id)
                    .get(CameraCharacteristics.LENS_FACING);
            if (facing != null && facing == CameraCharacteristics.LENS_FACING_BACK) return id;
        }
        String[] all = manager.getCameraIdList();
        return all.length > 0 ? all[0] : null;
    }

    private void createSession() {
        try {
            SurfaceTexture texture = preview.getSurfaceTexture();
            CaptureRequest.Builder request =
                    camera.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
            request.addTarget(reader.getSurface());
            java.util.List<android.view.Surface> targets;
            if (texture != null) {
                texture.setDefaultBufferSize(TARGET.getWidth(), TARGET.getHeight());
                android.view.Surface previewSurface = new android.view.Surface(texture);
                request.addTarget(previewSurface);
                targets = Arrays.asList(previewSurface, reader.getSurface());
            } else {
                // Decoding does not need the preview; only the operator does.
                targets = java.util.Collections.singletonList(reader.getSurface());
            }
            camera.createCaptureSession(targets, new CameraCaptureSession.StateCallback() {
                @Override
                public void onConfigured(CameraCaptureSession configured) {
                    session = configured;
                    try {
                        configured.setRepeatingRequest(request.build(), null, cameraHandler);
                    } catch (CameraAccessException failure) {
                        Log.w(TAG, "could not start the preview: " + failure);
                    }
                }

                @Override
                public void onConfigureFailed(CameraCaptureSession configured) {
                    runOnUiThread(() -> setStatus("Camera could not start.", "#B26A00"));
                }
            }, cameraHandler);
        } catch (CameraAccessException failure) {
            setStatus("Camera error: " + failure, "#B26A00");
        }
    }

    /** Decode one frame; the luma plane alone is what a QR needs. */
    private void onFrame(ImageReader source) {
        Image image = source.acquireLatestImage();
        if (image == null) return;
        try {
            if (handled.get()) return;
            ByteBuffer buffer = image.getPlanes()[0].getBuffer();
            byte[] luma = new byte[buffer.remaining()];
            buffer.get(luma);
            int width = image.getWidth();
            int height = image.getHeight();
            PlanarYUVLuminanceSource luminance = new PlanarYUVLuminanceSource(
                    luma, width, height, 0, 0, width, height, false);
            Map<DecodeHintType, Object> hints = new EnumMap<>(DecodeHintType.class);
            hints.put(DecodeHintType.TRY_HARDER, Boolean.TRUE);
            Result result;
            try {
                result = qrReader.decode(new BinaryBitmap(new HybridBinarizer(luminance)), hints);
            } catch (Exception noCode) {
                return; // ordinary: most frames contain no code
            } finally {
                qrReader.reset();
            }
            if (result != null && handled.compareAndSet(false, true)) {
                String text = result.getText();
                runOnUiThread(() -> applyPairing(text));
            }
        } finally {
            image.close();
        }
    }

    private void applyPairing(String text) {
        stopCamera();
        PairingPayload payload;
        try {
            payload = PairingPayload.parse(text);
        } catch (Exception failure) {
            setStatus("That is not a PhoneAgent pairing code.", "#B26A00");
            handled.set(false);
            startCamera();
            return;
        }
        try {
            LinkKeyStore.storeKey(this, payload.key);
            RemoteLinkConfig.write(this, true, payload.host, payload.port);
        } catch (Exception failure) {
            setStatus("Could not save the pairing: " + failure, "#B26A00");
            return;
        }
        GatewayService.applyRemoteLinkSettings(this);
        setStatus("● Paired with " + payload.host + "\nKey " + payload.fingerprint(), "#1B7F3B");
        Toast.makeText(this, "Paired. Connecting…", Toast.LENGTH_LONG).show();
        preview.postDelayed(this::finish, 2500);
    }

    private void stopCamera() {
        if (session != null) {
            session.close();
            session = null;
        }
        if (camera != null) {
            camera.close();
            camera = null;
        }
        if (reader != null) {
            reader.close();
            reader = null;
        }
        if (cameraThread != null) {
            cameraThread.quitSafely();
            cameraThread = null;
            cameraHandler = null;
        }
    }

    private void setStatus(String text, String colour) {
        if (statusView == null) return;
        statusView.setText(text);
        statusView.setTextColor(Color.parseColor(colour));
    }

    static Intent intentFor(android.content.Context context) {
        return new Intent(context, PairingScanActivity.class);
    }

    private ViewGroup.LayoutParams wrap() {
        return new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
    }
}
