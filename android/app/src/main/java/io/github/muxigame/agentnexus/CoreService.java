package io.github.muxigame.agentnexus;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;

import java.io.File;
import java.util.concurrent.TimeUnit;

/**
 * Keeps the core running as a foreground service (the notification is what lets Android keep it):
 * unpacks the runtime, starts {@code python -m agent_nexus.cli serve}, and starts it again when it
 * ends unexpectedly (at most 5 times in 10 minutes, like the desktop shell). {@link #ACTION_STOP}
 * asks the core to shut down cleanly and stops the service.
 */
public final class CoreService extends Service {
    static final String ACTION_STOP = "io.github.muxigame.agentnexus.STOP";
    private static final String TAG = "AgentNexus";
    private static final String CHANNEL = "core";
    private static final int NOTIFICATION = 1;
    private static final long WINDOW_MS = 10 * 60 * 1000;
    private static final int RESTART_LIMIT = 5;

    private volatile boolean stopping;
    private Thread worker;
    private Process process;
    private PowerManager.WakeLock wakeLock;
    static volatile String status = "正在启动…";

    static void start(Context context) {
        Intent intent = new Intent(context, CoreService.class);
        if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(intent); else context.startService(intent);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            stopping = true;
            new Thread(this::shutdown, "anx-stop").start();
            return START_NOT_STICKY;
        }
        startInForeground();
        if (worker == null || !worker.isAlive()) {
            stopping = false;
            worker = new Thread(this::supervise, "anx-core");
            worker.start();
        }
        return START_STICKY;
    }

    private void startInForeground() {
        NotificationManager manager = getSystemService(NotificationManager.class);
        if (Build.VERSION.SDK_INT >= 26) {
            manager.createNotificationChannel(new NotificationChannel(CHANNEL, "Agent Nexus", NotificationManager.IMPORTANCE_LOW));
        }
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, CHANNEL) : new Notification.Builder(this);
        Notification notification = builder.setContentTitle("Agent Nexus")
                .setContentText("保持与网关的连接")
                .setSmallIcon(R.drawable.ic_launcher)
                .setContentIntent(open)
                .setOngoing(true)
                .build();
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIFICATION, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(NOTIFICATION, notification);
        }
        if (wakeLock == null) {
            // A partial wake lock keeps the gateway link alive while the screen is off.
            wakeLock = ((PowerManager) getSystemService(POWER_SERVICE)).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "AgentNexus:core");
            wakeLock.setReferenceCounted(false);
            wakeLock.acquire();
        }
    }

    private void supervise() {
        long[] starts = new long[RESTART_LIMIT];
        int count = 0;
        while (!stopping) {
            try {
                if (Runtime.alive(this)) {  // a core from an earlier service instance is still running
                    status = "运行中";
                    notifyUpdate();
                    Thread.sleep(3000);
                    continue;
                }
                status = "正在准备运行环境…";
                File runtime = Runtime.install(this);
                long now = System.currentTimeMillis();
                if (count >= RESTART_LIMIT && now - starts[count % RESTART_LIMIT] < WINDOW_MS) {
                    status = "Agent Nexus 反复意外退出，已停止自动重启（见 .anx/logs/android.log）";
                    Log.e(TAG, status);
                    return;
                }
                starts[count % RESTART_LIMIT] = now;
                count++;
                status = "正在启动核心…";
                notifyUpdate();  // the network report is there before the core's first update round
                process = Runtime.command(this, runtime).start();
                // Wait in short steps, not one blocking waitFor(): the core downloads updates while it
                // runs, and the "update downloaded" notification is posted from here between steps.
                while (!process.waitFor(3, TimeUnit.SECONDS)) {
                    if (Runtime.alive(this)) status = "运行中";
                    notifyUpdate();
                }
                int code = process.exitValue();
                process = null;
                if (!stopping) {
                    status = "核心已退出（" + code + "），正在重新启动…";
                    // The end of the core's output, for `adb logcat -s AgentNexus` (the app's files are private).
                    Log.w(TAG, "core exited with " + code + "\n" + MainActivity.tail(Runtime.log(this), 3000));
                    Thread.sleep(2000);
                }
            } catch (InterruptedException e) {
                return;
            } catch (Exception e) {
                status = "启动失败：" + e;
                Log.e(TAG, "core start failed", e);
                try { Thread.sleep(10000); } catch (InterruptedException interrupted) { return; }
            }
        }
    }

    /**
     * Keeps the core's network report current and posts the "update downloaded" notification once
     * per downloaded version (Updates.pending). Called every few seconds while the core runs.
     */
    private void notifyUpdate() {
        try {
            Updates.reportNetwork(this);
            Updates.notifyIfPending(this);
        } catch (RuntimeException e) {
            Log.w(TAG, "update notice", e);
        }
    }

    private void shutdown() {
        Runtime.request(this, "POST", "/api/manager/shutdown");
        Process running = process;
        if (running != null) {
            try {
                for (int i = 0; i < 50 && running.isAlive(); i++) Thread.sleep(200);
            } catch (InterruptedException ignored) {
            }
            if (running.isAlive()) running.destroyForcibly();
        }
        status = "已停止";
        if (worker != null) worker.interrupt();
        stopForeground(true);
        stopSelf();
    }

    @Override
    public void onDestroy() {
        stopping = true;
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }
}
