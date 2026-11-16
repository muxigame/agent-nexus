//! Desktop updates (docs/design.md section 9.4, decision D7).
//!
//! The shell checks `latest.json` 60 seconds after start and every 6 hours,
//! only on the Rust side. What it finds goes to `<root>/.anx/desktop-update.json`;
//! the manager serves that as `/api/app/update`, the console shows a banner and
//! the tray an orange arrow plus "更新到 vX.Y.Z". Nothing is downloaded or
//! installed in the background: only `--apply-update` (a click in the tray or the
//! console) runs the clean stop, `download_and_install` (which verifies the
//! minisign signature) and the restart.
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::Ordering;
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};
use tauri::{AppHandle, Manager, Url};
use tauri_plugin_updater::{Update, UpdaterExt};

use crate::bootstrap::{self, note_startup_log, StartupState};

pub const STATE_FILE: &str = ".anx/desktop-update.json";
pub const FIRST_CHECK: Duration = Duration::from_secs(60);
pub const CHECK_EVERY: Duration = Duration::from_secs(6 * 60 * 60);
/// Test endpoint (a local static `latest.json`); honoured by debug builds, or
/// when `ANX_UPDATER_TEST=1` marks a test run.
pub const ENDPOINT_OVERRIDE: &str = "ANX_UPDATER_ENDPOINT";

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Mode { Notify, Off }

/// `desktop.update.mode`: `notify` (default) or `off`. The retired `auto` and
/// unknown values mean `notify`, with a warning. The private edition is `off`.
pub fn mode(config: &Value, private: bool) -> (Mode, Option<String>) {
    if private { return (Mode::Off, None); }
    match config.pointer("/desktop/update/mode").and_then(Value::as_str) {
        None | Some("notify") => (Mode::Notify, None),
        Some("off") => (Mode::Off, None),
        Some("auto") => (Mode::Notify, Some(
            "desktop.update.mode \"auto\" is no longer supported; using \"notify\" (updates install only when you click)".into())),
        Some(other) => (Mode::Notify, Some(format!("unknown desktop.update.mode {other:?}; using \"notify\""))),
    }
}

fn now() -> u64 { SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0) }

/// The state file's content for a check result (`found` = version and notes).
pub fn state(current: &str, found: Option<(&str, Option<&str>)>, checked_at: u64) -> Value {
    match found {
        Some((version, notes)) => json!({"available": true, "version": version, "notes": notes,
            "current": current, "checked_at": checked_at}),
        None => json!({"available": false, "current": current, "checked_at": checked_at}),
    }
}

pub fn write_state(root: &Path, value: &Value) {
    let path = root.join(STATE_FILE);
    if let Some(parent) = path.parent() { let _ = fs::create_dir_all(parent); }
    // Replace atomically: the manager polls this file.
    let temporary = path.with_extension("json.tmp");
    if fs::write(&temporary, serde_json::to_string_pretty(value).unwrap()).is_ok() {
        let _ = fs::rename(&temporary, &path);
    }
}

/// After an update the old "available" record names the running version; drop it.
pub fn forget_installed(root: &Path, current: &str) {
    let path = root.join(STATE_FILE);
    let Some(old) = fs::read_to_string(&path).ok().and_then(|t| serde_json::from_str::<Value>(&t).ok()) else { return; };
    if old.get("version").and_then(Value::as_str) == Some(current) {
        write_state(root, &state(current, None, old.get("checked_at").and_then(Value::as_u64).unwrap_or(0)));
    }
}

fn endpoint_override() -> Option<Url> {
    let allowed = cfg!(debug_assertions) || std::env::var("ANX_UPDATER_TEST").as_deref() == Ok("1");
    if !allowed { return None; }
    std::env::var(ENDPOINT_OVERRIDE).ok().filter(|v| !v.is_empty()).and_then(|v| v.parse().ok())
}

async fn find(app: &AppHandle) -> Result<Option<Update>, String> {
    let mut builder = app.updater_builder();
    if let Some(url) = endpoint_override() { builder = builder.endpoints(vec![url]).map_err(|e| e.to_string())?; }
    builder.build().map_err(|e| e.to_string())?.check().await.map_err(|e| e.to_string())
}

/// One check: records the result and returns the update, if any. Never downloads.
pub async fn check(app: &AppHandle, root: &Path) -> Result<Option<Update>, String> {
    let current = app.package_info().version.to_string();
    match find(app).await {
        Ok(update) => {
            let found = update.as_ref().map(|u| (u.version.as_str(), u.body.as_deref()));
            write_state(root, &state(&current, found, now()));
            Ok(update)
        }
        Err(error) => {
            let mut value = fs::read_to_string(root.join(STATE_FILE)).ok()
                .and_then(|t| serde_json::from_str::<Value>(&t).ok()).filter(Value::is_object)
                .unwrap_or_else(|| state(&current, None, 0));
            value["error"] = json!(error);
            value["checked_at"] = json!(now());
            write_state(root, &value);
            note_startup_log(root, &format!("update check failed: {error}"));
            Err(error)
        }
    }
}

fn configured_mode(root: &Path) -> Mode {
    let config = bootstrap::config(root).unwrap_or(Value::Null);
    let (mode, warning) = mode(&config, bootstrap::private_edition());
    if let Some(warning) = warning { note_startup_log(root, &warning); }
    mode
}

/// Background checks: 60 s after start, then every 6 h. `notify` only writes the
/// state file; nothing is downloaded.
pub fn schedule(app: AppHandle, root: PathBuf) {
    forget_installed(&root, &app.package_info().version.to_string());
    thread::spawn(move || {
        let mut wait = FIRST_CHECK;
        loop {
            let deadline = std::time::Instant::now() + wait;
            while std::time::Instant::now() < deadline {
                if app.state::<StartupState>().quit.load(Ordering::SeqCst) { return; }
                thread::sleep(Duration::from_secs(1));
            }
            if configured_mode(&root) == Mode::Notify {
                let _ = tauri::async_runtime::block_on(check(&app, &root));
            }
            wait = CHECK_EVERY;
        }
    });
}

/// `--check-update`: an explicit check, also under `off` (the user asked).
/// `exit_after` ends a shell that was started only for this.
pub fn check_now(app: AppHandle, root: PathBuf, exit_after: bool) {
    tauri::async_runtime::spawn(async move {
        if bootstrap::private_edition() {
            note_startup_log(&root, "update check skipped: the private edition does not update itself");
        } else {
            let _ = check(&app, &root).await;
        }
        if exit_after { app.exit(0); }
    });
}

/// `--apply-update`: clean stop, verified download and install, restart. A
/// failure brings the manager back and records the error for the tray/console.
pub fn apply(app: AppHandle, root: PathBuf, exit_if_none: bool) {
    tauri::async_runtime::spawn(async move {
        if bootstrap::private_edition() {
            note_startup_log(&root, "update skipped: the private edition does not update itself");
            if exit_if_none { app.exit(0); }
            return;
        }
        let update = match check(&app, &root).await {
            Ok(Some(update)) => update,
            _ => { if exit_if_none { app.exit(0); } return; }
        };
        note_startup_log(&root, &format!("installing update {} (clean stop first)", update.version));
        bootstrap::suspend_watchdog(&app);
        let stop_root = root.clone();
        let code = tauri::async_runtime::spawn_blocking(move || crate::shutdown::run(&stop_root, Duration::from_secs(20)))
            .await.unwrap_or(crate::shutdown::EXIT_RESIDUE);
        if code != 0 { note_startup_log(&root, "clean stop left processes running; installing anyway"); }
        match update.download_and_install(|_, _| {}, || {}).await {
            Ok(()) => app.restart(),
            Err(error) => {
                let mut value = state(&app.package_info().version.to_string(), Some((&update.version, update.body.as_deref())), now());
                value["error"] = json!(format!("install failed: {error}"));
                write_state(&root, &value);
                note_startup_log(&root, &format!("update {} failed: {error}; restarting the manager", update.version));
                bootstrap::resume_watchdog(&app);
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mode_defaults_to_notify_and_retires_auto() {
        assert_eq!(mode(&Value::Null, false), (Mode::Notify, None));
        assert_eq!(mode(&json!({"desktop": {"update": {"mode": "off"}}}), false), (Mode::Off, None));
        let (auto, warning) = mode(&json!({"desktop": {"update": {"mode": "auto"}}}), false);
        assert_eq!(auto, Mode::Notify);
        assert!(warning.unwrap().contains("notify"));
        assert_eq!(mode(&json!({"desktop": {"update": {"mode": "notify"}}}), true), (Mode::Off, None));
    }

    #[test]
    fn state_records_and_forgets_an_installed_version() {
        let root = std::env::temp_dir().join(format!("anx-update-state-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        write_state(&root, &state("0.2.0", Some(("0.2.1", Some("notes"))), 5));
        let saved: Value = serde_json::from_str(&fs::read_to_string(root.join(STATE_FILE)).unwrap()).unwrap();
        assert_eq!(saved["available"], json!(true));
        assert_eq!(saved["version"], json!("0.2.1"));
        forget_installed(&root, "0.2.0");
        assert_eq!(serde_json::from_str::<Value>(&fs::read_to_string(root.join(STATE_FILE)).unwrap()).unwrap()["version"], json!("0.2.1"));
        forget_installed(&root, "0.2.1");
        let after: Value = serde_json::from_str(&fs::read_to_string(root.join(STATE_FILE)).unwrap()).unwrap();
        assert_eq!(after["available"], json!(false));
        fs::remove_dir_all(root).unwrap();
    }
}
