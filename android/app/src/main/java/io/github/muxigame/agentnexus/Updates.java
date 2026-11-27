package io.github.muxigame.agentnexus;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.net.ConnectivityManager;
import android.net.Uri;
import android.provider.Settings;
import android.util.Log;

import androidx.core.content.FileProvider;

import org.json.JSONObject;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;

/**
 * In-app updates. The core (agent_nexus/linux_update.py) finds a newer APK through the gateway's
 * download list, downloads it into {@code <root>/.anx/updates} and writes
 * {@code <root>/.anx/android-install.json}. Only the system's package installer can replace the
 * app and it always asks the user, so this offers the APK: a notification (from the service) and a
 * dialog while the console is open. The installer gets a FileProvider URI; installing needs the
 * user's one-time "install unknown apps" permission for this app, which is asked for first.
 * Android only accepts an APK signed with the installed app's key.
 */
final class Updates {
    static final String ACTION_INSTALL = "io.github.muxigame.agentnexus.INSTALL_UPDATE";
    private static final String TAG = "AgentNexus";
    private static final String CHANNEL = "updates";
    private static final int NOTIFICATION = 2;
    private static final String REQUEST = ".anx/android-install.json";
    private static final String FOLDER = ".anx/updates";
    /** The active network for the core: it does not download an update unattended on a metered one. */
    private static final String NETWORK = ".anx/android-network.json";
    private static volatile Boolean reportedMetered;
    private static volatile long notifiedVersion;
    /** The last parsed APK (path, size, mtime) -> its package info: parsing reads the whole manifest. */
    private static String cachedKey;
    private static PackageInfo cachedInfo;

    private Updates() {}

    /** The downloaded APK when it is this app at a higher version code; stale requests are removed. */
    static File pending(Context context) {
        File root = Runtime.root(context);
        File request = new File(root, REQUEST);
        if (!request.isFile()) return null;
        try {
            JSONObject data = new JSONObject(new String(Files.readAllBytes(request.toPath()), StandardCharsets.UTF_8));
            File apk = new File(data.optString("path", ""));
            File folder = new File(root, FOLDER).getCanonicalFile();
            if (!apk.isFile() || !folder.equals(apk.getCanonicalFile().getParentFile())) {
                forget(request, null);
                return null;
            }
            PackageInfo archive = archive(context, apk);
            PackageInfo installed = context.getPackageManager().getPackageInfo(context.getPackageName(), 0);
            if (archive == null || !context.getPackageName().equals(archive.packageName)
                    || archive.getLongVersionCode() <= installed.getLongVersionCode()) {
                forget(request, apk);  // already installed, or not an update of this app
                return null;
            }
            return apk;
        } catch (Exception e) {
            Log.w(TAG, "update request unreadable", e);
            return null;
        }
    }

    private static void forget(File request, File apk) {
        //noinspection ResultOfMethodCallIgnored
        request.delete();
        //noinspection ResultOfMethodCallIgnored
        if (apk != null) apk.delete();
    }

    private static synchronized PackageInfo archive(Context context, File apk) {
        String key = apk.getAbsolutePath() + "|" + apk.length() + "|" + apk.lastModified();
        if (!key.equals(cachedKey)) {
            cachedInfo = context.getPackageManager().getPackageArchiveInfo(apk.getAbsolutePath(), 0);
            cachedKey = key;
        }
        return cachedInfo;
    }

    static long versionCode(Context context, File apk) {
        PackageInfo archive = archive(context, apk);
        return archive == null ? 0 : archive.getLongVersionCode();
    }

    static String versionName(Context context, File apk) {
        PackageInfo archive = archive(context, apk);
        return archive == null || archive.versionName == null ? "" : archive.versionName;
    }

    /**
     * When the user asked for this update in the settings page ("立即更新"): the request's
     * {@code asked_at}, else "". A new value means: offer it again, even after "稍后".
     */
    static String askedAt(Context context) {
        File request = new File(Runtime.root(context), REQUEST);
        try {
            return new JSONObject(new String(Files.readAllBytes(request.toPath()), StandardCharsets.UTF_8)).optString("asked_at", "");
        } catch (Exception e) {
            return "";
        }
    }

    /** Whether the user still has to allow this app to install apps (Settings, special app access). */
    static boolean needsPermission(Context context) {
        return !context.getPackageManager().canRequestPackageInstalls();
    }

    static Intent permissionIntent(Context context) {
        return new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + context.getPackageName()));
    }

    /** The system installer for {@code apk}, reading it through this app's FileProvider. */
    static Intent installIntent(Context context, File apk) {
        Uri uri = FileProvider.getUriForFile(context, context.getPackageName() + ".files", apk);
        return new Intent(Intent.ACTION_VIEW)
                .setDataAndType(uri, "application/vnd.android.package-archive")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
    }

    /**
     * Tells the core whether the active network is metered (mobile data, a metered hotspot), so the
     * automatic update check waits for Wi-Fi before downloading the APK. Written when it changes.
     */
    static void reportNetwork(Context context) {
        ConnectivityManager connectivity = context.getSystemService(ConnectivityManager.class);
        boolean metered = connectivity != null && connectivity.isActiveNetworkMetered();
        File file = new File(Runtime.root(context), NETWORK);
        if (reportedMetered != null && reportedMetered == metered && file.isFile()) return;
        try {
            //noinspection ResultOfMethodCallIgnored
            file.getParentFile().mkdirs();
            File temporary = new File(file.getPath() + ".tmp");
            Files.write(temporary.toPath(), ("{\"metered\": " + metered + "}").getBytes(StandardCharsets.UTF_8));
            if (!temporary.renameTo(file)) throw new java.io.IOException("rename failed");
            reportedMetered = metered;
        } catch (Exception e) {
            Log.w(TAG, "network report", e);
        }
    }

    /** From the service: one notification per downloaded version; tapping it opens the console's install dialog. */
    static void notifyIfPending(Context context) {
        File apk = pending(context);
        if (apk == null) return;
        long code = versionCode(context, apk);
        if (code == notifiedVersion) return;
        notifiedVersion = code;
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        manager.createNotificationChannel(new NotificationChannel(CHANNEL, "更新", NotificationManager.IMPORTANCE_DEFAULT));
        Intent open = new Intent(context, MainActivity.class).setAction(ACTION_INSTALL)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent tap = PendingIntent.getActivity(context, 1, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        Notification notification = new Notification.Builder(context, CHANNEL)
                .setContentTitle("Agent Nexus " + versionName(context, apk) + " 已下载")
                .setContentText("点按安装更新")
                .setSmallIcon(R.drawable.ic_launcher)
                .setContentIntent(tap)
                .setAutoCancel(true)
                .build();
        manager.notify(NOTIFICATION, notification);
    }
}
