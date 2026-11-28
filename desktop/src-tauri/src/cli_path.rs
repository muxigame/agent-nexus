//! `anx` in the owner's own terminals on Windows (docs/design.md section 9.7).
//!
//! * `--install-cli` (the installer, after `--prepare-runtime` / `--setup-system`, and after every update):
//!   copies the current runtime's `anx.exe` (uv's launcher, which names that runtime's python by absolute path)
//!   into `<install dir>\bin` together with an extensionless sh `anx` for Git Bash, and appends that folder to
//!   the PATH of the install scope (HKCU for 仅为我安装, HKLM for 为所有用户安装). Nothing else goes in the
//!   folder, so no `python.exe` or other tool is shadowed; the entry is appended, never prepended.
//! * `--remove-cli` (the uninstaller, not on an update): removes exactly the PATH entry it added.
//! * every start, once the core answers (the window per user, the service for all users): [`refresh`]
//!   copies the launcher again when the installer set it up, and finishes the PATH entry when the installer
//!   could not (`CliPathPending`: the runtime was not ready yet). A PATH entry the user removed stays removed.
//!
//! PATH is edited here and not in NSIS: NSIS strings are about 1024 characters, a longer PATH would be cut.
//! The value keeps its type (REG_EXPAND_SZ) and its `%VAR%` references.

#![cfg_attr(not(target_os = "windows"), allow(dead_code))]

/// The extensionless `anx` beside `anx.exe` (same text as agent_nexus/agents/anx_shim.py GIT_BASH_SHIM).
pub const GIT_BASH_SHIM: &str = "#!/bin/sh\nexport MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'\nexec \"${0%/*}/anx.exe\" \"$@\"\n";
#[allow(dead_code)]
const OWNER_KEY: &str = r"Software\AgentNexus";

fn same_entry(a: &str, b: &str) -> bool {
    let norm = |s: &str| s.trim().trim_end_matches(['\\', '/']).to_ascii_lowercase();
    !a.trim().is_empty() && norm(a) == norm(b)
}

/// `path` with `entry` appended, or None when it is already there (any case, with or without a trailing `\`).
pub fn path_with(entry: &str, path: &str) -> Option<String> {
    if path.split(';').any(|part| same_entry(part, entry)) { return None; }
    let trimmed = path.trim_end_matches(';');
    Some(if trimmed.is_empty() { entry.to_string() } else { format!("{trimmed};{entry}") })
}

/// `path` without `entry` (every copy of it), or None when it is not there. Empty segments elsewhere are kept.
pub fn path_without(entry: &str, path: &str) -> Option<String> {
    let parts: Vec<&str> = path.split(';').collect();
    if !parts.iter().any(|part| same_entry(part, entry)) { return None; }
    Some(parts.into_iter().filter(|part| !same_entry(part, entry)).collect::<Vec<_>>().join(";"))
}

#[cfg(target_os = "windows")]
mod windows {
    use std::env;
    use std::fs;
    use std::path::{Path, PathBuf};

    use winreg::enums::{RegType, HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE, KEY_READ, KEY_SET_VALUE};
    use winreg::{RegKey, RegValue};

    use super::{path_with, path_without, GIT_BASH_SHIM, OWNER_KEY};

    const USER_ENV: &str = "Environment";
    const MACHINE_ENV: &str = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment";

    fn scope() -> (RegKey, &'static str) {
        if crate::system::system_mode() {
            (RegKey::predef(HKEY_LOCAL_MACHINE), MACHINE_ENV)
        } else {
            (RegKey::predef(HKEY_CURRENT_USER), USER_ENV)
        }
    }

    fn owner_hive() -> RegKey {
        if crate::system::system_mode() { RegKey::predef(HKEY_LOCAL_MACHINE) } else { RegKey::predef(HKEY_CURRENT_USER) }
    }

    fn bin_dir() -> Result<PathBuf, String> {
        let exe = env::current_exe().map_err(|e| e.to_string())?;
        Ok(exe.parent().ok_or("no install folder")?.join("bin"))
    }

    /// PATH of the scope as stored (type kept; REG_EXPAND_SZ when it did not exist).
    fn read_path(key: &RegKey) -> (String, RegType) {
        match key.get_raw_value("Path") {
            Ok(value) if matches!(value.vtype, RegType::REG_SZ | RegType::REG_EXPAND_SZ) => {
                let words: Vec<u16> = value.bytes.chunks_exact(2).map(|c| u16::from_le_bytes([c[0], c[1]])).collect();
                let text = String::from_utf16_lossy(&words).trim_end_matches('\0').to_string();
                (text, if matches!(value.vtype, RegType::REG_SZ) { RegType::REG_SZ } else { RegType::REG_EXPAND_SZ })
            }
            _ => (String::new(), RegType::REG_EXPAND_SZ),
        }
    }

    fn write_path(key: &RegKey, text: &str, vtype: RegType) -> std::io::Result<()> {
        let mut bytes: Vec<u8> = Vec::with_capacity(text.len() * 2 + 2);
        for unit in text.encode_utf16().chain(std::iter::once(0)) { bytes.extend_from_slice(&unit.to_le_bytes()); }
        key.set_raw_value("Path", &RegValue { bytes: bytes.into(), vtype })
    }

    /// Remove `name` from `bin`, renaming it aside first when it is in use (a running anx.exe).
    fn retire(bin: &Path, name: &str) {
        let path = bin.join(name);
        if !path.exists() || fs::remove_file(&path).is_ok() { return; }
        let aside = bin.join(format!("{name}.old"));
        let _ = fs::remove_file(&aside);
        let _ = fs::rename(&path, &aside);
    }

    fn copy_launcher(root: &Path, bin: &Path) -> Result<bool, String> {
        let base = root.join("runtime");
        let tag = fs::read_to_string(base.join("current")).map_err(|e| format!("runtime/current: {e}"))?;
        let tag = tag.trim();
        if tag.is_empty() || tag.contains(['/', '\\']) || tag.contains("..") { return Err("bad runtime/current".into()); }
        let launcher = base.join(tag).join(r".venv\Scripts\anx.exe");
        fs::create_dir_all(bin).map_err(|e| e.to_string())?;
        let old = bin.join("anx.exe.old");
        let _ = fs::remove_file(&old);
        if launcher.is_file() {
            let staged = bin.join("anx.exe.new");
            fs::copy(&launcher, &staged).map_err(|e| e.to_string())?;
            let target = bin.join("anx.exe");
            // a running anx.exe cannot be overwritten, but it can be renamed
            if target.exists() { let _ = fs::rename(&target, &old); }
            fs::rename(&staged, &target).map_err(|e| e.to_string())?;
            let _ = fs::remove_file(&old);
            let _ = fs::remove_file(bin.join("anx.cmd"));
            fs::write(bin.join("anx"), GIT_BASH_SHIM).map_err(|e| e.to_string())?;
            return Ok(true);
        }
        // a runtime without uv's launcher: a .cmd with the runtime's python (as the per-run shim does). An older
        // anx.exe would win over it (PATHEXT puts .EXE first) and names a runtime that is pruned later: it goes,
        // with the sh `anx` that starts it.
        let python = base.join(tag).join(r".venv\Scripts\python.exe");
        fs::write(bin.join("anx.cmd"), format!("@\"{}\" -P -m agent_nexus.cli --root \"{}\" %*\r\n",
                                                python.display(), root.display())).map_err(|e| e.to_string())?;
        retire(bin, "anx.exe");
        let _ = fs::remove_file(bin.join("anx"));
        Ok(false)
    }

    pub fn install() -> Result<String, String> {
        let result = add();
        if let Ok((owner, _)) = owner_hive().create_subkey(OWNER_KEY) {
            // not done (the runtime was not ready): the next start finishes it (refresh)
            if result.is_err() { let _ = owner.set_value("CliPathPending", &1u32); } else { let _ = owner.delete_value("CliPathPending"); }
        }
        result
    }

    /// At every start once the core answers: see the module doc.
    pub fn refresh() {
        let Ok(owner) = owner_hive().open_subkey(OWNER_KEY) else { return; };  // never set up by an installer
        if owner.get_value::<u32, _>("CliPathPending").unwrap_or(0) == 1 {
            let _ = install();
            return;
        }
        let Ok(entry) = owner.get_value::<String, _>("CliPath") else { return; };
        let (Ok(root), Ok(bin)) = (crate::bootstrap::resolve_root(), bin_dir()) else { return; };
        if super::same_entry(&entry, &bin.display().to_string()) { let _ = copy_launcher(&root, &bin); }
    }

    fn add() -> Result<String, String> {
        let root = crate::bootstrap::resolve_root()?;
        let bin = bin_dir()?;
        let launcher = copy_launcher(&root, &bin)?;
        let entry = bin.display().to_string();
        let (hive, sub) = scope();
        let key = hive.open_subkey_with_flags(sub, KEY_READ | KEY_SET_VALUE).map_err(|e| format!("PATH: {e}"))?;
        let (path, vtype) = read_path(&key);
        let added = match path_with(&entry, &path) {
            Some(next) => { write_path(&key, &next, vtype).map_err(|e| format!("PATH: {e}"))?; true }
            None => false,
        };
        if let Ok((owner, _)) = owner_hive().create_subkey(OWNER_KEY) {
            let _ = owner.set_value("CliPath", &entry);
            // only an entry we added is ever removed again (one the user had stays)
            if added { let _ = owner.set_value("CliPathAdded", &1u32); }
        }
        Ok(format!("{} ({})", entry, if launcher { "anx.exe" } else { "anx.cmd" }))
    }

    pub fn remove() {
        let hive = owner_hive();
        let Ok(owner) = hive.open_subkey_with_flags(OWNER_KEY, KEY_READ | KEY_SET_VALUE) else { return; };
        let entry: String = owner.get_value("CliPath").unwrap_or_default();
        let added: u32 = owner.get_value("CliPathAdded").unwrap_or(0);
        if added == 1 && !entry.is_empty() {
            let (hive, sub) = scope();
            if let Ok(key) = hive.open_subkey_with_flags(sub, KEY_READ | KEY_SET_VALUE) {
                let (path, vtype) = read_path(&key);
                if let Some(next) = path_without(&entry, &path) { let _ = write_path(&key, &next, vtype); }
            }
        }
        let _ = owner.delete_value("CliPath");
        let _ = owner.delete_value("CliPathAdded");
        let _ = owner.delete_value("CliPathPending");
        if let Ok(bin) = bin_dir() {
            for name in ["anx.exe", "anx.exe.old", "anx.exe.new", "anx", "anx.cmd"] { let _ = fs::remove_file(bin.join(name)); }
            let _ = fs::remove_dir(&bin);
        }
    }
}

/// `--install-cli`; never fails the install (the reason is printed for the installer's log).
pub fn install() -> i32 {
    #[cfg(target_os = "windows")]
    {
        match windows::install() {
            Ok(done) => { println!("anx: {done}"); 0 }
            Err(error) => { println!("anx: not added to PATH ({error})"); 1 }
        }
    }
    #[cfg(not(target_os = "windows"))]
    { 0 }
}

/// After a start (the core answered): keep `<install dir>\bin` current, or finish what the installer could not.
pub fn refresh() {
    #[cfg(target_os = "windows")]
    windows::refresh();
}

/// `--remove-cli`.
pub fn remove() {
    #[cfg(target_os = "windows")]
    windows::remove();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn appends_once_ignoring_case_and_trailing_slash() {
        let entry = r"C:\Program Files\Agent Nexus\bin";
        assert_eq!(path_with(entry, r"%SystemRoot%\system32;C:\Tools").as_deref(),
                   Some(r"%SystemRoot%\system32;C:\Tools;C:\Program Files\Agent Nexus\bin"));
        assert_eq!(path_with(entry, r"C:\Tools;c:\program files\agent nexus\BIN\"), None);
        assert_eq!(path_with(entry, "").as_deref(), Some(entry));
        assert_eq!(path_with(entry, r"C:\Tools;").as_deref(), Some(r"C:\Tools;C:\Program Files\Agent Nexus\bin"));
    }

    #[test]
    fn removes_only_our_entry_and_keeps_the_rest() {
        let entry = r"C:\Users\user\AppData\Local\Agent Nexus\bin";
        let path = r"%USERPROFILE%\bin;;C:\Users\user\AppData\Local\Agent Nexus\bin\;C:\Go\bin";
        assert_eq!(path_without(entry, path).as_deref(), Some(r"%USERPROFILE%\bin;;C:\Go\bin"));
        assert_eq!(path_without(entry, r"C:\Go\bin"), None);
        assert_eq!(path_without(entry, entry).as_deref(), Some(""));
    }
}
