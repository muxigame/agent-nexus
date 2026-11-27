package io.github.muxigame.agentnexus;

import android.content.Context;
import android.content.res.AssetManager;

import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.Map;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

/**
 * The Python runtime the app carries (scripts/build_android.py) and how the core starts.
 *
 * <ul>
 *   <li>{@code assets/runtime.zip}: {@code python/} (the standard library), {@code site/} (the
 *       manager's packages) and {@code app/} (agent_nexus, the builtin plugins with their own
 *       {@code .site}, the initial agent-nexus.json). Unpacked once per version into
 *       {@code files/runtime/<tag>/}; {@code assets/runtime.tag} names the version.</li>
 *   <li>The native library folder: libpython and {@code libpyexe.so}, the {@code python} executable
 *       (Android only lets an app execute files from there).</li>
 *   <li>The data root {@code files/AgentNexus}: configuration, secrets, logs; kept across updates.</li>
 * </ul>
 */
final class Runtime {
    static final int PORT_DEFAULT = 8399;
    private static final String CONFIG = "agent-nexus.json";

    private Runtime() {}

    static File root(Context context) { return new File(context.getFilesDir(), "AgentNexus"); }

    static File log(Context context) { return new File(root(context), ".anx/logs/android.log"); }

    static String tag(Context context) throws IOException {
        try (InputStream in = context.getAssets().open("runtime.tag")) {
            return new String(readAll(in), StandardCharsets.UTF_8).trim();
        }
    }

    /** {@code files/runtime/<tag>}, unpacked now if it is not complete yet. Older versions are removed. */
    static synchronized File install(Context context) throws IOException {
        String tag = tag(context);
        File base = new File(context.getFilesDir(), "runtime");
        File dir = new File(base, tag);
        File done = new File(dir, ".complete");
        if (!done.isFile()) {
            File staging = new File(base, tag + ".tmp");
            deleteTree(staging);
            unzip(context.getAssets(), "runtime.zip", staging);
            Files.write(new File(staging, ".complete").toPath(), tag.getBytes(StandardCharsets.UTF_8));
            deleteTree(dir);
            if (!staging.renameTo(dir)) throw new IOException("cannot move the runtime into place");
        }
        File[] entries = base.listFiles();
        if (entries != null) for (File entry : entries) if (!entry.getName().equals(tag)) deleteTree(entry);
        File root = root(context);
        if (!root.isDirectory() && !root.mkdirs()) throw new IOException("cannot create " + root);
        File config = new File(root, CONFIG);
        if (!config.isFile()) {
            Files.copy(new File(dir, "app/" + CONFIG).toPath(), config.toPath());
        }
        return dir;
    }

    /** The console port from the root's configuration ({@code manager.port}). */
    static int port(Context context) {
        try {
            byte[] data = Files.readAllBytes(new File(root(context), CONFIG).toPath());
            JSONObject manager = new JSONObject(new String(data, StandardCharsets.UTF_8)).optJSONObject("manager");
            if (manager != null) return manager.optInt("port", PORT_DEFAULT);
        } catch (Exception ignored) {
        }
        return PORT_DEFAULT;
    }

    static String consoleUrl(Context context) { return "http://127.0.0.1:" + port(context) + "/"; }

    /** The core: {@code python -m agent_nexus.cli --root <root> serve}, with the bundled runtime. */
    static ProcessBuilder command(Context context, File runtime) {
        String libs = context.getApplicationInfo().nativeLibraryDir;
        File python = new File(libs, "libpyexe.so");
        File root = root(context);
        ProcessBuilder builder = new ProcessBuilder(python.getAbsolutePath(), "-m", "agent_nexus.cli",
                "--root", root.getAbsolutePath(), "serve");
        Map<String, String> env = builder.environment();
        File home = new File(context.getFilesDir(), "home");
        //noinspection ResultOfMethodCallIgnored
        home.mkdirs();
        env.put("HOME", home.getAbsolutePath());
        env.put("TMPDIR", context.getCacheDir().getAbsolutePath());
        env.put("PYTHONHOME", new File(runtime, "python").getAbsolutePath());
        env.put("PYTHONPATH", new File(runtime, "app").getAbsolutePath() + ":" + new File(runtime, "site").getAbsolutePath());
        env.put("PYTHONUNBUFFERED", "1");
        env.put("PYTHONUTF8", "1");
        env.put("LD_LIBRARY_PATH", libs);
        env.put("PATH", libs + ":/system/bin:/system/xbin");
        // Android's own CA store is not in a form OpenSSL reads; certifi's bundle is.
        env.put("SSL_CERT_FILE", new File(runtime, "site/certifi/cacert.pem").getAbsolutePath());
        env.put("ANX_ROOT", root.getAbsolutePath());
        env.put("ANX_ANDROID_APP", context.getPackageName());
        // The device's own name for the gateway (Python only sees "localhost" here).
        env.put("ANX_ANDROID_DEVICE_NAME", deviceName(context));
        // The only folder Android lets the app execute from (the tunnel agent ships there as lib*.so).
        env.put("ANX_ANDROID_NATIVE_LIBS", libs);
        // Writable folder for the OpenSSL links .NET opens by name (agent_nexus.p2p.child_env).
        env.put("ANX_ANDROID_LINK_DIR", context.getFilesDir().getAbsolutePath());
        builder.directory(root);
        builder.redirectErrorStream(true);
        File log = log(context);
        //noinspection ResultOfMethodCallIgnored
        log.getParentFile().mkdirs();
        builder.redirectOutput(ProcessBuilder.Redirect.appendTo(log));
        return builder;
    }

    /** The name the user gave the device (Settings, About), else maker and model. */
    static String deviceName(Context context) {
        String name = null;
        try {
            name = android.provider.Settings.Global.getString(context.getContentResolver(),
                    android.provider.Settings.Global.DEVICE_NAME);
        } catch (Exception ignored) {
        }
        if (name == null || name.trim().isEmpty()) {
            String maker = android.os.Build.MANUFACTURER == null ? "" : android.os.Build.MANUFACTURER.trim();
            String model = android.os.Build.MODEL == null ? "" : android.os.Build.MODEL.trim();
            name = model.toLowerCase().startsWith(maker.toLowerCase()) ? model : (maker + " " + model).trim();
        }
        return name.trim().isEmpty() ? "android" : name.trim();
    }

    /** {@code GET /api/ping} answered by the core. */
    static boolean alive(Context context) {
        return request(context, "GET", "/api/ping") == 200;
    }

    static int request(Context context, String method, String path) {
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL("http://127.0.0.1:" + port(context) + path).openConnection();
            connection.setRequestMethod(method);
            connection.setRequestProperty("X-ANX", "1");
            connection.setConnectTimeout(1000);
            connection.setReadTimeout(3000);
            if (method.equals("POST")) {
                connection.setDoOutput(true);
                connection.getOutputStream().close();
            }
            return connection.getResponseCode();
        } catch (IOException e) {
            return -1;
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    private static void unzip(AssetManager assets, String name, File target) throws IOException {
        String canonical = target.getCanonicalPath() + File.separator;
        try (ZipInputStream zip = new ZipInputStream(new BufferedInputStream(assets.open(name), 1 << 16))) {
            byte[] buffer = new byte[1 << 16];
            ZipEntry entry;
            while ((entry = zip.getNextEntry()) != null) {
                File out = new File(target, entry.getName());
                if (!out.getCanonicalPath().startsWith(canonical)) throw new IOException("bad path in runtime: " + entry.getName());
                if (entry.isDirectory()) {
                    //noinspection ResultOfMethodCallIgnored
                    out.mkdirs();
                    continue;
                }
                //noinspection ResultOfMethodCallIgnored
                out.getParentFile().mkdirs();
                try (OutputStream stream = new FileOutputStream(out)) {
                    int read;
                    while ((read = zip.read(buffer)) > 0) stream.write(buffer, 0, read);
                }
            }
        }
    }

    static void deleteTree(File file) {
        if (file == null || !file.exists()) return;
        File[] children = file.isDirectory() && !Files.isSymbolicLink(file.toPath()) ? file.listFiles() : null;
        if (children != null) for (File child : children) deleteTree(child);
        //noinspection ResultOfMethodCallIgnored
        file.delete();
    }

    private static byte[] readAll(InputStream in) throws IOException {
        java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
        byte[] buffer = new byte[8192];
        int read;
        while ((read = in.read(buffer)) > 0) out.write(buffer, 0, read);
        return out.toByteArray();
    }
}
