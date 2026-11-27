//! Login autostart (docs/design.md section 9.6).
//!
//! `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\Agent Nexus` =
//! `"<exe>" --autostart --root "<root>"`, per user, no elevation. The value is
//! written here rather than through tauri-plugin-autostart's `enable()`: that
//! joins the executable path and arguments without quotes, which breaks for the
//! default install folder (`%LOCALAPPDATA%\Agent Nexus`) and any root with a
//! space. The plugin is still registered, under the same value name, so its
//! `is_enabled()` reports the same entry.
//!
//! macOS: a per-user LaunchAgent `~/Library/LaunchAgents/io.github.muxigame.agentnexus.plist`
//! that runs `<app>/Contents/MacOS/agent-nexus --autostart --root <root>` at login.
//!
//! Autostart is turned on once, at the first run of a root
//! (`.anx/desktop.json` gets `autostart_initialized`); after that the user's
//! choice is respected, upgrades included.
use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};

#[cfg_attr(not(target_os = "windows"), allow(dead_code))]
pub const RUN_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";
pub const VALUE_NAME: &str = "Agent Nexus";
pub const MARKER_FILE: &str = ".anx/desktop.json";

/// The Run value's command line. Trailing separators are dropped from the root:
/// `"C:\x\"` would make the closing quote an escaped one.
#[cfg(not(target_os = "macos"))]
pub fn command(exe: &Path, root: &Path) -> String {
    let root = root.display().to_string();
    let root = root.trim_end_matches(['\\', '/']);
    format!("\"{}\" --autostart --root \"{}\"", exe.display(), root)
}

/// macOS: the whole LaunchAgent property list (compared as a whole, like the Run value).
#[cfg(target_os = "macos")]
pub fn command(exe: &Path, root: &Path) -> String {
    fn xml(text: &str) -> String {
        text.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;").replace('"', "&quot;")
    }
    let root = root.display().to_string();
    let root = root.trim_end_matches('/');
    format!(r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{exe}</string>
    <string>--autostart</string>
    <string>--root</string>
    <string>{root}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>ProcessType</key>
  <string>Interactive</string>
</dict>
</plist>
"#, label = registry::LABEL, exe = xml(&exe.display().to_string()), root = xml(root))
}

fn marker(root: &Path) -> Value {
    fs::read_to_string(root.join(MARKER_FILE)).ok()
        .and_then(|text| serde_json::from_str(&text).ok()).filter(Value::is_object).unwrap_or_else(|| json!({}))
}

pub fn initialized(root: &Path) -> bool {
    marker(root).get("autostart_initialized").and_then(Value::as_bool) == Some(true)
}

fn mark_initialized(root: &Path) -> Result<(), String> {
    let mut data = marker(root);
    data["autostart_initialized"] = json!(true);
    let path = root.join(MARKER_FILE);
    fs::create_dir_all(path.parent().unwrap()).map_err(|e| e.to_string())?;
    fs::write(&path, serde_json::to_string_pretty(&data).unwrap()).map_err(|e| e.to_string())
}

#[cfg(target_os = "windows")]
mod registry {
    use winreg::enums::{HKEY_CURRENT_USER, KEY_READ};
    use winreg::RegKey;

    pub fn read(name: &str) -> Option<String> {
        RegKey::predef(HKEY_CURRENT_USER).open_subkey_with_flags(super::RUN_KEY, KEY_READ).ok()?
            .get_value::<String, _>(name).ok()
    }

    pub fn write(name: &str, value: Option<&str>) -> Result<(), String> {
        let (key, _) = RegKey::predef(HKEY_CURRENT_USER).create_subkey(super::RUN_KEY).map_err(|e| e.to_string())?;
        match value {
            Some(value) => key.set_value(name, &value.to_string()).map_err(|e| e.to_string()),
            None => match key.delete_value(name) {
                Err(error) if error.kind() != std::io::ErrorKind::NotFound => Err(error.to_string()),
                _ => Ok(()),
            },
        }
    }
}

#[cfg(target_os = "macos")]
mod registry {
    //! The "registry" is one LaunchAgent file; its content is the value.
    use std::path::PathBuf;

    pub const LABEL: &str = "io.github.muxigame.agentnexus";

    fn plist() -> Option<PathBuf> {
        std::env::var_os("HOME").map(|home| PathBuf::from(home).join("Library/LaunchAgents").join(format!("{LABEL}.plist")))
    }

    pub fn read(_name: &str) -> Option<String> { std::fs::read_to_string(plist()?).ok() }

    pub fn write(_name: &str, value: Option<&str>) -> Result<(), String> {
        let path = plist().ok_or("HOME is not set")?;
        match value {
            Some(value) => {
                std::fs::create_dir_all(path.parent().unwrap()).map_err(|e| e.to_string())?;
                // launchd reads the file at the next login; nothing is loaded now (the app runs).
                std::fs::write(&path, value).map_err(|e| e.to_string())
            }
            None => match std::fs::remove_file(&path) {
                Err(error) if error.kind() != std::io::ErrorKind::NotFound => Err(error.to_string()),
                _ => Ok(()),
            },
        }
    }
}

#[cfg(not(any(target_os = "windows", target_os = "macos")))]
mod registry {
    // Linux uses a systemd user unit and XDG autostart (section 9.6), not built here yet.
    pub fn read(_name: &str) -> Option<String> { None }
    pub fn write(_name: &str, _value: Option<&str>) -> Result<(), String> {
        Err("autostart is only implemented for Windows in this build".into())
    }
}

pub fn current() -> Option<String> { registry::read(VALUE_NAME) }

pub fn set(enabled: bool, exe: &Path, root: &Path) -> Result<(), String> {
    if enabled { registry::write(VALUE_NAME, Some(&command(exe, root))) } else { registry::write(VALUE_NAME, None) }
}

/// What a start does with autostart: the first run of a root turns it on; later
/// runs only refresh an enabled entry (the exe may have moved, e.g. a reinstall
/// elsewhere) and never re-enable one the user turned off.
#[derive(Debug, PartialEq)]
pub enum Startup { Enabled, Refreshed, Unchanged }

pub fn on_start(exe: &Path, root: &Path) -> Result<Startup, String> {
    if !initialized(root) {
        set(true, exe, root)?;
        mark_initialized(root)?;
        return Ok(Startup::Enabled);
    }
    let wanted = command(exe, root);
    match current() {
        Some(existing) if existing != wanted && existing.contains("--autostart") => {
            set(true, exe, root)?;
            Ok(Startup::Refreshed)
        }
        _ => Ok(Startup::Unchanged),
    }
}

#[cfg(target_os = "macos")]
#[test]
fn the_launch_agent_escapes_paths() {
    let text = command(Path::new("/Applications/Agent Nexus.app/Contents/MacOS/agent-nexus"), Path::new("/Users/a&b/AgentNexus/"));
    assert!(text.contains("<string>/Applications/Agent Nexus.app/Contents/MacOS/agent-nexus</string>"));
    assert!(text.contains("<string>/Users/a&amp;b/AgentNexus</string>"));
    assert!(text.contains("--autostart"));
}

pub fn exe() -> PathBuf { std::env::current_exe().unwrap_or_else(|_| PathBuf::from("agent-nexus.exe")) }

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(not(target_os = "macos"))]
    #[test]
    fn the_run_value_quotes_paths_with_spaces() {
        let exe = Path::new(r"D:\Data A\Local\Agent Nexus\agent-nexus.exe");
        assert_eq!(command(exe, Path::new(r"D:\Data A\Local\AgentNexus\")),
            r#""D:\Data A\Local\Agent Nexus\agent-nexus.exe" --autostart --root "D:\Data A\Local\AgentNexus""#);
    }

    #[test]
    fn the_marker_is_merged_into_desktop_json() {
        let root = std::env::temp_dir().join(format!("anx-autostart-marker-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join(".anx")).unwrap();
        fs::write(root.join(MARKER_FILE), r#"{"other": 1}"#).unwrap();
        assert!(!initialized(&root));
        mark_initialized(&root).unwrap();
        assert!(initialized(&root));
        assert_eq!(marker(&root)["other"], json!(1));
        fs::remove_dir_all(root).unwrap();
    }
}
