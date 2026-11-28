//! System mode ("为所有用户安装"): the core is the Windows service `AgentNexus` (LocalSystem),
//! its root is `%ProgramData%\AgentNexus`, and this window is a per-user viewer of it -- the same
//! split as the Ubuntu package (packaging/ubuntu/ubuntu_launcher.py, agent-nexus.service).
//!
//! * `--setup-system` (the installer, elevated, after an all-users install or update): move a
//!   per-user root here once, unpack the runtime, register and (re)start the service.
//! * `--remove-system` (the uninstaller): stop and delete the service; the data stays.
//! * `--service`: what the service control manager runs (service.rs).
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::thread;
use std::time::{Duration, Instant};

use serde_json::Value;

use crate::bootstrap::{has_config, http, ping, quiet, CONFIG_NAMES};

pub const SERVICE: &str = "AgentNexus";
const MARKER: &str = ".system";
/// What moves from a per-user root (the runtime itself is unpacked fresh).
const MIGRATED: [&str; 6] = ["agent-nexus.json", "mcp-manager.json", "secrets", "plugins", "plugin-data", ".anx"];

/// `%ProgramData%\AgentNexus`.
pub fn system_root() -> PathBuf {
    let base = env::var_os("ProgramData").map(PathBuf::from).unwrap_or_else(|| PathBuf::from(r"C:\ProgramData"));
    base.join("AgentNexus")
}

/// The service is registered (`HKLM\SYSTEM\CurrentControlSet\Services\AgentNexus`).
pub fn service_installed() -> bool {
    #[cfg(target_os = "windows")]
    {
        use winreg::{enums::HKEY_LOCAL_MACHINE, RegKey};
        RegKey::predef(HKEY_LOCAL_MACHINE).open_subkey(format!(r"SYSTEM\CurrentControlSet\Services\{SERVICE}")).is_ok()
    }
    #[cfg(not(target_os = "windows"))]
    { false }
}

/// Installed for all users and set up: this window only views the service's core.
pub fn system_mode() -> bool {
    system_root().join(MARKER).is_file() && service_installed()
}

fn run(program: &str, args: &[&str]) -> (bool, String) {
    let mut command = Command::new(program);
    command.args(args);
    quiet(&mut command);
    match command.output() {
        Ok(out) => (out.status.success(), format!("{}{}", String::from_utf8_lossy(&out.stdout), String::from_utf8_lossy(&out.stderr))),
        Err(error) => (false, error.to_string()),
    }
}

fn say(line: &str) { println!("{line}"); }

/// Per-user roots on this machine: `C:\Users\*\AppData\Local\AgentNexus` with a configuration.
fn user_roots() -> Vec<PathBuf> {
    let users = env::var_os("SystemDrive").map(|d| PathBuf::from(format!("{}\\Users", d.to_string_lossy())))
        .unwrap_or_else(|| PathBuf::from(r"C:\Users"));
    let Ok(entries) = fs::read_dir(users) else { return Vec::new(); };
    entries.flatten().map(|e| e.path().join(r"AppData\Local\AgentNexus")).filter(|p| has_config(p)).collect()
}

fn joined(root: &Path) -> bool { root.join(r"secrets\cloud\device-token").is_file() }

fn config_time(root: &Path) -> std::time::SystemTime {
    CONFIG_NAMES.iter().filter_map(|n| fs::metadata(root.join(n)).and_then(|m| m.modified()).ok()).max()
        .unwrap_or(std::time::UNIX_EPOCH)
}

/// The root to move: one that joined a gateway, else the newest.
fn pick(roots: &[PathBuf]) -> Option<PathBuf> {
    let mut ranked = roots.to_vec();
    ranked.sort_by_key(|root| (joined(root), config_time(root)));
    ranked.pop()
}

/// Ask a per-user manager to stop (it would hold the port the service needs).
fn stop_user_manager(root: &Path) {
    let Ok(url) = crate::bootstrap::configured_url(root) else { return; };
    if let Some(info) = ping(&url) {
        let same = info.get("root").and_then(Value::as_str)
            .map(|r| Path::new(r).canonicalize().ok() == root.canonicalize().ok()).unwrap_or(false);
        if same {
            say(&format!("stopping the per-user manager of {}", root.display()));
            let _ = http(&url, "POST", "/api/manager/shutdown");
            let deadline = Instant::now() + Duration::from_secs(25);
            while Instant::now() < deadline && ping(&url).is_some() { thread::sleep(Duration::from_millis(500)); }
        }
    }
}

fn copy_tree(from: &Path, to: &Path, skip_logs: bool) -> std::io::Result<()> {
    if from.is_file() {
        if let Some(parent) = to.parent() { fs::create_dir_all(parent)?; }
        fs::copy(from, to)?;
        return Ok(());
    }
    fs::create_dir_all(to)?;
    for entry in fs::read_dir(from)?.flatten() {
        let name = entry.file_name();
        if skip_logs && name == "logs" { continue; }
        copy_tree(&entry.path(), &to.join(&name), false)?;
    }
    Ok(())
}

fn migrate(from: &Path, to: &Path) -> Result<(), String> {
    for name in MIGRATED {
        let source = from.join(name);
        if source.exists() {
            copy_tree(&source, &to.join(name), name == ".anx").map_err(|e| format!("{name}: {e}"))?;
        }
    }
    fs::write(to.join("migrated-from"), from.display().to_string()).map_err(|e| e.to_string())?;
    say(&format!("moved {} to {}", from.display(), to.display()));
    Ok(())
}

/// Owner-only, as mcpkit's protect(): SYSTEM and Administrators (the core runs as SYSTEM).
///
/// Only the folder gets its own list; everything in it is reset to inherit from it. (`/grant`
/// with `/T` would apply the folder's (OI)(CI) entries to each file, where they mean nothing, and
/// leave files that nobody -- SYSTEM included -- can open.)
fn protect(path: &Path) {
    if path.is_dir() { lock_down(path, &["*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"]); }
}

/// The folder (and everything below it) owned by Administrators, with exactly `grants` (inherited by
/// every child). Ownership first: an owner keeps an implicit WRITE_DAC, so a folder or file a standard
/// user created (squatted before the install, planted while it ran) could otherwise be opened again by
/// them. `/reset` before `/inheritance:r` drops explicit ACEs such an owner may have added.
fn lock_down(path: &Path, grants: &[&str]) {
    let target = path.display().to_string();
    let (ok, out) = run("takeown", &["/F", &target, "/R", "/A", "/D", "Y"]);
    if !ok { say(&format!("takeown {target}: {out}")); }
    let (ok, out) = run("icacls", &[&target, "/setowner", "*S-1-5-32-544", "/T", "/C", "/Q"]);
    if !ok { say(&format!("icacls {target} /setowner: {out}")); }
    let (ok, out) = run("icacls", &[&target, "/reset", "/Q"]);
    if !ok { say(&format!("icacls {target} /reset: {out}")); }
    let mut args = vec![target.as_str(), "/inheritance:r", "/grant:r"];
    args.extend_from_slice(grants);
    args.push("/Q");
    let (ok, out) = run("icacls", &args);
    if !ok { say(&format!("icacls {target}: {out}")); }
    let children = format!("{target}\\*");
    let (ok, out) = run("icacls", &[&children, "/reset", "/T", "/C", "/Q"]);
    if !ok { say(&format!("icacls {children} /reset: {out}")); }
}

/// Readable, not writable, by standard users: the runtime the service (LocalSystem) runs. ProgramData's
/// inherited ACL lets every user create files below it, so a planted `.pth`, `sitecustomize.py` or DLL in
/// the venv would run as SYSTEM (and as any administrator who runs `anx`).
fn protect_read_only(path: &Path) {
    if path.is_dir() { lock_down(path, &["*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F", "*S-1-5-32-545:(OI)(CI)RX"]); }
}

/// `HKLM\Software\AgentNexus` value: `runtime` and `tools` have been locked down (and emptied) once.
/// In HKLM, which only administrators can write: a marker file under the data root could be planted.
#[cfg_attr(not(target_os = "windows"), allow(dead_code))]
const PROTECTED_KEY: &str = r"Software\AgentNexus";  // the key cli_path.rs also uses
#[cfg_attr(not(target_os = "windows"), allow(dead_code))]
const PROTECTED_VALUE: &str = "RuntimeProtected";

fn protected_before() -> bool {
    #[cfg(target_os = "windows")]
    {
        use winreg::{enums::HKEY_LOCAL_MACHINE, RegKey};
        RegKey::predef(HKEY_LOCAL_MACHINE).open_subkey(PROTECTED_KEY)
            .and_then(|key| key.get_value::<u32, _>(PROTECTED_VALUE)).map(|v| v == 1).unwrap_or(false)
    }
    #[cfg(not(target_os = "windows"))]
    { false }
}

fn set_protected(on: bool) {
    #[cfg(target_os = "windows")]
    {
        use winreg::{enums::HKEY_LOCAL_MACHINE, RegKey};
        let machine = RegKey::predef(HKEY_LOCAL_MACHINE);
        if on {
            if let Ok((key, _)) = machine.create_subkey(PROTECTED_KEY) { let _ = key.set_value(PROTECTED_VALUE, &1u32); }
        } else if let Ok(key) = machine.open_subkey_with_flags(PROTECTED_KEY, winreg::enums::KEY_SET_VALUE) {
            let _ = key.delete_value(PROTECTED_VALUE);
        }
    }
    #[cfg(not(target_os = "windows"))]
    { let _ = on; }
}

/// Before anything is extracted into them: `runtime` and `tools` (prepended to PATH as a fallback) exist,
/// owned by Administrators, readable but not writable by standard users. The first time (a fresh install,
/// an upgrade from a version without this, or the data root re-created after an uninstall) whatever is
/// already in them is untrusted -- a standard user could have written it -- and is moved away, so the
/// runtime is extracted and synced afresh.
fn secure_runtime_dirs(root: &Path) -> Result<(), String> {
    let fresh = !protected_before();
    for name in ["runtime", "tools"] {
        let dir = root.join(name);
        if fresh && dir.exists() {
            let aside = root.join(format!("{name}.untrusted-{}", std::process::id()));
            // Take it over first: a squatted folder may deny even its own removal to others.
            lock_down(&dir, &["*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"]);
            if fs::rename(&dir, &aside).is_ok() {
                let _ = fs::remove_dir_all(&aside);
            } else {
                fs::remove_dir_all(&dir).map_err(|e| format!("无法清理 {}（请关闭正在使用它的程序后重试）：{e}", dir.display()))?;
            }
        }
        fs::create_dir_all(&dir).map_err(|e| format!("无法创建 {}：{e}", dir.display()))?;
        protect_read_only(&dir);
    }
    set_protected(true);
    Ok(())
}

/// accounts.default: plugins and terminals run as whoever is at the screen (kept when set).
fn default_account(root: &Path) {
    let path = CONFIG_NAMES.iter().map(|n| root.join(n)).find(|p| p.is_file()).unwrap_or_else(|| root.join(CONFIG_NAMES[0]));
    let Ok(text) = fs::read_to_string(&path) else { return; };
    let Ok(mut data) = serde_json::from_str::<Value>(text.trim_start_matches('\u{feff}')) else { return; };
    let Some(object) = data.as_object_mut() else { return; };
    let accounts = object.entry("accounts").or_insert_with(|| Value::Object(Default::default()));
    if let Some(map) = accounts.as_object_mut() {
        if map.get("default").and_then(Value::as_str).is_some() { return; }
        map.insert("default".into(), Value::String("active".into()));
    }
    if let Ok(text) = serde_json::to_string_pretty(&data) { let _ = fs::write(&path, text + "\n"); }
}

fn register_service(exe: &Path) -> Result<(), String> {
    let bin = format!("\"{}\" --service", exe.display());
    let (exists, _) = run("sc.exe", &["query", SERVICE]);
    let (ok, out) = if exists {
        run("sc.exe", &["config", SERVICE, "binPath=", &bin, "start=", "auto", "obj=", "LocalSystem"])
    } else {
        run("sc.exe", &["create", SERVICE, "binPath=", &bin, "start=", "auto", "obj=", "LocalSystem",
                        "DisplayName=", "Agent Nexus"])
    };
    if !ok { return Err(format!("sc {}：{out}", if exists { "config" } else { "create" })); }
    run("sc.exe", &["description", SERVICE, "Agent Nexus 核心：网关连接、自动更新、P2P 远程、MCP 插件"]);
    run("sc.exe", &["failure", SERVICE, "reset=", "86400", "actions=", "restart/5000/restart/5000/restart/30000"]);
    Ok(())
}

fn wait_service(state: &str, seconds: u64) -> bool {
    let deadline = Instant::now() + Duration::from_secs(seconds);
    while Instant::now() < deadline {
        let (_, out) = run("sc.exe", &["query", SERVICE]);
        if out.contains(state) { return true; }
        thread::sleep(Duration::from_millis(500));
    }
    false
}

pub fn stop_service() {
    if service_installed() {
        run("sc.exe", &["stop", SERVICE]);
        wait_service("STOPPED", 40);
    }
}

/// The window at logon, for every user (tray only; it shows the service's console).
fn logon_entry(exe: &Path, on: bool) {
    #[cfg(target_os = "windows")]
    {
        use winreg::{enums::{HKEY_LOCAL_MACHINE, KEY_SET_VALUE}, RegKey};
        if let Ok(key) = RegKey::predef(HKEY_LOCAL_MACHINE)
            .open_subkey_with_flags(r"Software\Microsoft\Windows\CurrentVersion\Run", KEY_SET_VALUE) {
            if on { let _ = key.set_value("Agent Nexus", &format!("\"{}\" --autostart", exe.display())); }
            else { let _ = key.delete_value("Agent Nexus"); }
        }
    }
    #[cfg(not(target_os = "windows"))]
    { let _ = (exe, on); }
}

/// A per-user installation's login entry (`HKCU\...\Run`, pointing into `AppData\Local`) would start
/// that copy too; the all-users entry starts the window for everyone now.
fn retire_user_logon_entries() {
    #[cfg(target_os = "windows")]
    {
        use winreg::{enums::{HKEY_USERS, KEY_READ, KEY_SET_VALUE}, RegKey};
        let users = RegKey::predef(HKEY_USERS);
        for sid in users.enum_keys().flatten() {
            let path = format!(r"{sid}\Software\Microsoft\Windows\CurrentVersion\Run");
            let Ok(key) = users.open_subkey_with_flags(&path, KEY_READ | KEY_SET_VALUE) else { continue; };
            if let Ok(value) = key.get_value::<String, _>("Agent Nexus") {
                if value.to_ascii_lowercase().contains(r"\appdata\local\") {
                    let _ = key.delete_value("Agent Nexus");
                    say(&format!("removed the per-user login entry of {sid}"));
                }
            }
        }
    }
}

/// The installing user's own per-user copy (`%LOCALAPPDATA%`): uninstall it, data kept. Left in
/// place, its shortcuts would start that copy and its updater would run the installer as that user.
/// (Run by the core as SYSTEM for an update, there is no such entry: nothing to do.)
fn retire_user_install() {
    #[cfg(target_os = "windows")]
    {
        use winreg::{enums::HKEY_CURRENT_USER, RegKey};
        let Ok(key) = RegKey::predef(HKEY_CURRENT_USER)
            .open_subkey(r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Agent Nexus") else { return; };
        let Ok(command) = key.get_value::<String, _>("UninstallString") else { return; };
        let uninstaller = PathBuf::from(command.trim().trim_matches('"').split("\" ").next().unwrap_or("").trim_matches('"'));
        let Some(dir) = uninstaller.parent().map(Path::to_path_buf) else { return; };
        if !dir.display().to_string().to_ascii_lowercase().contains(r"\appdata\local\") || !uninstaller.is_file() { return; }
        if env::current_exe().ok().and_then(|exe| exe.parent().map(Path::to_path_buf)) == Some(dir.clone()) { return; }
        say(&format!("removing the per-user installation in {}", dir.display()));
        // `_?=` runs the uninstaller in place, so this waits for it; the data root stays.
        let mut command = Command::new(&uninstaller);
        command.arg("/S").arg(format!("_?={}", dir.display()));
        quiet(&mut command);
        let _ = command.status();
        let _ = fs::remove_file(&uninstaller);
        let _ = fs::remove_dir(&dir);
    }
}

/// `--setup-system`: run elevated by the all-users installer (also after each update).
pub fn setup() -> Result<(), String> {
    let root = system_root();
    let exe = env::current_exe().map_err(|e| e.to_string())?;
    stop_service();
    fs::create_dir_all(&root).map_err(|e| format!("无法创建 {}：{e}", root.display()))?;
    let users = user_roots();
    for user_root in &users { stop_user_manager(user_root); }
    if !has_config(&root) {
        if let Some(from) = pick(&users) { migrate(&from, &root)?; }
    }
    crate::bootstrap::set_root(root.clone());
    // Locked down before the runtime is extracted into it (a file planted meanwhile would run as SYSTEM).
    secure_runtime_dirs(&root)?;
    crate::bootstrap::prepare_for_service(&root)?;
    default_account(&root);
    protect(&root.join("secrets"));
    protect(&root.join(".anx"));
    protect_read_only(&root.join("runtime"));  // what extraction created: owned by Administrators too
    fs::write(root.join(MARKER), "1\n").map_err(|e| e.to_string())?;
    register_service(&exe)?;
    logon_entry(&exe, true);
    retire_user_logon_entries();
    retire_user_install();
    let (ok, out) = run("sc.exe", &["start", SERVICE]);
    if !ok && !out.contains("1056") { return Err(format!("sc start：{out}")); }  // 1056: already running
    wait_service("RUNNING", 30);
    say(&format!("system service {SERVICE} is running (root {})", root.display()));
    Ok(())
}

/// `--remove-system`: the uninstaller removes the service; the data stays in %ProgramData%.
pub fn remove() {
    stop_service();
    run("sc.exe", &["delete", SERVICE]);
    if let Ok(exe) = env::current_exe() { logon_entry(&exe, false); }
    let _ = fs::remove_file(system_root().join(MARKER));
    // The data root may be deleted after this and re-created by anyone: the next install trusts nothing in it.
    set_protected(false);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pick_prefers_a_joined_root() {
        let base = env::temp_dir().join(format!("anx-pick-{}", std::process::id()));
        let (a, b) = (base.join("a"), base.join("b"));
        for root in [&a, &b] { fs::create_dir_all(root).unwrap(); fs::write(root.join("agent-nexus.json"), "{}").unwrap(); }
        fs::create_dir_all(b.join(r"secrets\cloud")).unwrap();
        fs::write(b.join(r"secrets\cloud\device-token"), "x").unwrap();
        thread::sleep(Duration::from_millis(20));
        fs::write(a.join("agent-nexus.json"), "{ }").unwrap(); // a is newer, b joined
        assert_eq!(pick(&[a.clone(), b.clone()]), Some(b.clone()));
        assert_eq!(pick(&[]), None);
        let _ = fs::remove_dir_all(base);
    }

    #[cfg(target_os = "windows")]  // Windows paths: system mode is Windows only
    #[test]
    fn migrate_skips_logs_and_the_default_account_is_active() {
        let base = env::temp_dir().join(format!("anx-migrate-{}", std::process::id()));
        let (from, to) = (base.join("from"), base.join("to"));
        fs::create_dir_all(from.join(r".anx\logs")).unwrap();
        fs::write(from.join(r".anx\logs\anx.log"), "old").unwrap();
        fs::write(from.join(r".anx\lock.json"), "{}").unwrap();
        fs::create_dir_all(from.join("runtime")).unwrap();
        fs::write(from.join("agent-nexus.json"), r#"{"cloud": {"enabled": true}}"#).unwrap();
        fs::create_dir_all(&to).unwrap();
        migrate(&from, &to).unwrap();
        assert!(to.join(r".anx\lock.json").is_file());
        assert!(!to.join(r".anx\logs").exists());
        assert!(!to.join("runtime").exists());
        default_account(&to);
        let data: Value = serde_json::from_str(&fs::read_to_string(to.join("agent-nexus.json")).unwrap()).unwrap();
        assert_eq!(data["accounts"]["default"], "active");
        assert_eq!(data["cloud"]["enabled"], true);
        let _ = fs::remove_dir_all(base);
    }
}
