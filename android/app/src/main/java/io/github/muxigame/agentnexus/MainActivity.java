package io.github.muxigame.agentnexus;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.View;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.TextView;

import java.io.File;
import java.io.RandomAccessFile;
import java.nio.charset.StandardCharsets;

/**
 * The console window: the same page the core serves on loopback (as the desktop shell shows it),
 * in a WebView. Until the core answers, a status text with the end of the startup log.
 */
public final class MainActivity extends Activity {
    private final Handler handler = new Handler(Looper.getMainLooper());
    private WebView web;
    private TextView splash;
    private boolean loaded;
    private String fragment;
    /** The version code offered in this window (asked once; again after the permission screen or the installer). */
    private long offered;
    private boolean installWanted;
    /** Set when install() hands over to the permission screen or the installer: onResume asks again. */
    private boolean awayToInstall;
    /** The version the user put off with "稍后", for as long as the app runs: not asked again unprompted. */
    private static volatile long declined;
    /** The last {@code asked_at} seen (Updates.askedAt): a newer one comes from "立即更新" in the settings page. */
    private static volatile String asked = "";

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        FrameLayout frame = new FrameLayout(this);
        web = new WebView(this);
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setTextZoom(100);
        web.setWebViewClient(new WebViewClient());
        web.setWebChromeClient(new WebChromeClient());
        web.setVisibility(View.GONE);
        splash = new TextView(this);
        splash.setGravity(Gravity.CENTER);
        splash.setPadding(48, 48, 48, 48);
        splash.setTextSize(15);
        frame.addView(web);
        frame.addView(splash);
        setContentView(frame);
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
        handleLink(getIntent());
        installWanted = getIntent() != null && Updates.ACTION_INSTALL.equals(getIntent().getAction());
        CoreService.start(this);
        handler.post(this::poll);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        handleLink(intent);
        if (Updates.ACTION_INSTALL.equals(intent.getAction())) {
            installWanted = true;
            offered = 0;
        }
        if (loaded && fragment != null) web.loadUrl(Runtime.consoleUrl(this) + fragment);
    }

    /** {@code agentnexus://...} links open a console page, as on the desktop. */
    private void handleLink(Intent intent) {
        Uri data = intent == null ? null : intent.getData();
        if (data != null && "agentnexus".equals(data.getScheme())) {
            String rest = data.getEncodedSchemeSpecificPart();
            fragment = rest == null ? null : "#" + rest.replaceFirst("^/+", "");
        }
    }

    private void poll() {
        if (isFinishing()) return;
        new Thread(() -> {
            boolean alive = Runtime.alive(this);
            String tail = alive ? "" : tail(Runtime.log(this), 1500);
            File update = null;
            String askedAt = "";
            if (alive) {
                try { update = Updates.pending(this); } catch (RuntimeException ignored) { }
                if (update != null) askedAt = Updates.askedAt(this);
            }
            File pendingUpdate = update;
            String pendingAsked = askedAt;
            handler.post(() -> {
                if (alive && !loaded) {
                    loaded = true;
                    web.loadUrl(Runtime.consoleUrl(this) + (fragment != null ? fragment : ""));
                    web.setVisibility(View.VISIBLE);
                    splash.setVisibility(View.GONE);
                } else if (!alive) {
                    loaded = false;
                    web.setVisibility(View.GONE);
                    splash.setVisibility(View.VISIBLE);
                    splash.setText("Agent Nexus\n\n" + CoreService.status + (tail.isEmpty() ? "" : "\n\n" + tail));
                }
                if (pendingUpdate != null && !pendingAsked.isEmpty() && !pendingAsked.equals(asked)) {
                    asked = pendingAsked;  // "立即更新" in the settings page: ask now, even after "稍后"
                    offered = 0;
                    declined = 0;
                }
                if (pendingUpdate != null) offerUpdate(pendingUpdate);
                handler.postDelayed(this::poll, alive ? 3000 : 1000);
            });
        }, "anx-poll").start();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (awayToInstall) {  // back from the "install unknown apps" screen or the installer: ask again if still pending
            awayToInstall = false;
            offered = 0;
        }
    }

    /** A downloaded update (Updates.pending): ask once per window, then hand it to the installer. */
    private void offerUpdate(File apk) {
        long code = Updates.versionCode(this, apk);
        if (code == offered || isFinishing()) return;
        offered = code;
        if (installWanted) {  // the user tapped the notification
            installWanted = false;
            install(apk);
            return;
        }
        if (code == declined) return;  // "稍后": the notification or the settings page can still install it
        new AlertDialog.Builder(this)
                .setTitle("更新 Agent Nexus")
                .setMessage("新版本 " + Updates.versionName(this, apk) + " 已下载。安装期间 Agent Nexus 会短暂断开，装好后自动重新连接。")
                .setPositiveButton("安装", (dialog, which) -> install(apk))
                .setNegativeButton("稍后", (dialog, which) -> declined = code)
                .show();
    }

    private void install(File apk) {
        awayToInstall = true;
        try {
            if (Updates.needsPermission(this)) {
                // One-time: Settings > Apps > special access > Install unknown apps > Agent Nexus.
                startActivity(Updates.permissionIntent(this));
            } else {
                startActivity(Updates.installIntent(this, apk));
            }
        } catch (RuntimeException e) {
            splash.setText("无法打开系统安装程序：" + e);
        }
    }

    static String tail(File file, int bytes) {
        try (RandomAccessFile in = new RandomAccessFile(file, "r")) {
            long start = Math.max(0, in.length() - bytes);
            byte[] data = new byte[(int) (in.length() - start)];
            in.seek(start);
            in.readFully(data);
            return new String(data, StandardCharsets.UTF_8);
        } catch (Exception e) {
            return "";
        }
    }

    @Override
    public void onBackPressed() {
        if (web.getVisibility() == View.VISIBLE && web.canGoBack()) web.goBack();
        else moveTaskToBack(true);  // the core keeps running; quit from the console or the notification
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        web.destroy();
        super.onDestroy();
    }
}
