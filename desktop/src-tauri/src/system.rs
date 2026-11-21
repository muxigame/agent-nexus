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
    if path.is_dir() {
        let target = path.display().to_string();
        let (ok, out) = run("icacls", &[&target, "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F",
                                        "*S-1-5-32-544:(OI)(CI)F", "/Q"]);
        if !ok { say(&format!("icacls {target}: {out}")); }
        let children = format!("{target}\\*");
        let (ok, out) = run("icacls", &[&children, "/reset", "/T", "/C", "/Q"]);
        if !ok { say(&format!("icacls {children} /reset: {out}")); }
    }
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
    crate::bootstrap::prepare_for_service(&root)?;
    default_account(&root);
    protect(&root.join("secrets"));
    protect(&root.join(".anx"));
    fs::write(root.join(MARKER), "1\n").map_err(|e| e.to_string())?;
    register_service(&exe)?;
    logon_entry(&exe, true);
    retire_user_logon_entries();
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
