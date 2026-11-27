//! Clean stop (docs/design.md section 9.3), shared by `--shutdown-manager`, the
//! NSIS hooks and `--apply-update`.
//!
//! 1. `POST /api/manager/shutdown`: the manager records `requested: true`, so no
//!    watchdog (this shell's or the legacy bootstrap's) brings it back.
//! 2. Wait until the console stops answering and the recorded pid is gone.
//! 3. Services are started with CREATE_BREAKAWAY_FROM_JOB and outlive the
//!    manager, so look for what is still running from the root's program
//!    folders (`.venv`, `runtime`, `plugins`, `builtins`, `tools`), plus other
//!    copies of this very executable (a shell that has not noticed yet).
//! 4. `taskkill /T /F` what is left, check again; exit code 3 with the list
//!    when something survives.
use std::path::Path;
use std::process::Command;
use std::thread;
use std::time::{Duration, Instant};

use serde_json::Value;

use crate::bootstrap::{configured_url, http, note_startup_log, ping, process_alive, quiet, read_state};

pub const EXIT_RESIDUE: i32 = 3;
/// Program folders of a root; processes running from them belong to it.
pub const PROGRAM_DIRS: [&str; 5] = [".venv", "runtime", "plugins", "builtins", "tools"];

#[derive(Debug, Clone, PartialEq)]
pub struct Residue { pub pid: u32, pub name: String }

#[cfg_attr(not(target_os = "windows"), allow(dead_code))]
fn ps_quote(text: &str) -> String { format!("'{}'", text.replace('\'', "''")) }

/// PowerShell that prints `pid<TAB>name` for every process belonging to `root`.
#[cfg_attr(not(target_os = "windows"), allow(dead_code))]
pub fn residue_script(root: &Path, exe: &Path, me: u32) -> String {
    let base = root.display().to_string();
    let base = base.trim_end_matches(['\\', '/']);
    let dirs: Vec<String> = PROGRAM_DIRS.iter().map(|dir| ps_quote(&format!("{base}\\{dir}\\"))).collect();
    format!(concat!(
        "$ErrorActionPreference='SilentlyContinue';",
        "$dirs=@({dirs});$exe={exe};$me={me};",
        "Get-CimInstance Win32_Process | Where-Object {{",
        "$p=$_; if ($p.ProcessId -eq $me -or $p.ProcessId -eq $PID) {{ return $false }};",
        "if ($p.ExecutablePath -and $p.ExecutablePath -ieq $exe) {{ return $true }};",
        "foreach ($d in $dirs) {{",
        "if ($p.ExecutablePath -and $p.ExecutablePath.StartsWith($d, [StringComparison]::OrdinalIgnoreCase)) {{ return $true }};",
        "if ($p.CommandLine -and $p.CommandLine.IndexOf($d, [StringComparison]::OrdinalIgnoreCase) -ge 0) {{ return $true }} }};",
        "$false }} | ForEach-Object {{ \"$($_.ProcessId)`t$($_.Name)\" }}"),
        dirs = dirs.join(","), exe = ps_quote(&exe.display().to_string()), me = me)
}

#[cfg_attr(not(target_os = "windows"), allow(dead_code))]
pub fn parse_residue(text: &str) -> Vec<Residue> {
    text.lines().filter_map(|line| {
        let (pid, name) = line.trim().split_once('\t')?;
        Some(Residue { pid: pid.trim().parse().ok()?, name: name.trim().to_string() })
    }).collect()
}

#[cfg(target_os = "windows")]
fn residue(root: &Path) -> Vec<Residue> {
    let exe = std::env::current_exe().unwrap_or_default();
    let mut command = Command::new("powershell");
    command.args(["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
        &residue_script(root, &exe, std::process::id())]);
    quiet(&mut command);
    command.output().map(|out| parse_residue(&String::from_utf8_lossy(&out.stdout))).unwrap_or_default()
}

#[cfg(not(target_os = "windows"))]
fn residue(root: &Path) -> Vec<Residue> {
    let mut found = Vec::new();
    for dir in PROGRAM_DIRS {
        let pattern = root.join(dir).display().to_string();
        if let Ok(out) = Command::new("pgrep").args(["-f", &pattern]).output() {
            for pid in String::from_utf8_lossy(&out.stdout).lines().filter_map(|l| l.trim().parse::<u32>().ok()) {
                if pid != std::process::id() { found.push(Residue { pid, name: pattern.clone() }); }
            }
        }
    }
    found
}

fn kill_tree(pid: u32) {
    #[cfg(target_os = "windows")]
    let mut command = { let mut c = Command::new("taskkill"); c.args(["/T", "/F", "/PID", &pid.to_string()]); c };
    #[cfg(not(target_os = "windows"))]
    let mut command = { let mut c = Command::new("kill"); c.args(["-9", &pid.to_string()]); c };
    quiet(&mut command);
    let _ = command.output();
}

fn ports(root: &Path) -> Vec<u16> {
    let mut ports: Vec<u16> = configured_url(root).ok().and_then(|url| url.port()).into_iter().collect();
    if let Ok(data) = crate::bootstrap::config(root) {
        if let Some(services) = data.get("services").and_then(Value::as_object) {
            ports.extend(services.values().filter_map(|svc| svc.get("port").and_then(Value::as_u64)).map(|p| p as u16));
        }
    }
    ports
}

fn port_open(port: u16) -> bool {
    std::net::TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), Duration::from_millis(200)).is_ok()
}

/// Runs the whole flow and returns the process exit code (0, or 3 with residue).
pub fn run(root: &Path, timeout: Duration) -> i32 {
    let log = |line: &str| { println!("{line}"); note_startup_log(root, line); };
    let started = Instant::now();
    let url = configured_url(root).ok();
    let pid = read_state(root).get("pid").and_then(Value::as_u64).map(|pid| pid as u32);
    if let Some(url) = &url {
        if ping(url).is_some() {
            let answered = http(url, "POST", "/api/manager/shutdown").map(|(status, _)| status);
            log(&format!("shutdown-manager: asked {url} to stop (HTTP {})", answered.map(|s| s.to_string()).unwrap_or_else(|| "-".into())));
        }
    }
    // 2. The manager gone: console silent and its pid exited.
    while started.elapsed() < timeout {
        let answering = url.as_ref().map(|url| ping(url).is_some()).unwrap_or(false);
        let alive = pid.map(process_alive).unwrap_or(false);
        if !answering && !alive { break; }
        thread::sleep(Duration::from_millis(500));
    }
    // A shell notices a requested exit within a few seconds and quits by itself;
    // give the rest of the budget to that before calling anything residue.
    let mut left = residue(root);
    while !left.is_empty() && started.elapsed() < timeout {
        thread::sleep(Duration::from_millis(500));
        left = residue(root);
    }
    if !left.is_empty() {
        for process in &left {
            log(&format!("shutdown-manager: stopping leftover {} (pid {})", process.name, process.pid));
            kill_tree(process.pid);
        }
        thread::sleep(Duration::from_secs(1));
        left = residue(root);
    }
    let busy: Vec<u16> = ports(root).into_iter().filter(|port| port_open(*port)).collect();
    if !busy.is_empty() {
        log(&format!("shutdown-manager: ports still answering: {busy:?} (not owned by this root's processes)"));
    }
    if left.is_empty() {
        log(&format!("shutdown-manager: {} stopped in {:.1}s", root.display(), started.elapsed().as_secs_f32()));
        0
    } else {
        let names: Vec<String> = left.iter().map(|p| format!("{} (pid {})", p.name, p.pid)).collect();
        log(&format!("shutdown-manager: still running: {}", names.join(", ")));
        EXIT_RESIDUE
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn residue_is_matched_by_program_folders_only() {
        let script = residue_script(Path::new(r"D:\Data\O'Neil\AgentNexus\"), Path::new(r"C:\Apps\agent-nexus.exe"), 42);
        assert!(script.contains(r"'D:\Data\O''Neil\AgentNexus\.venv\'"));
        assert!(script.contains(r"'D:\Data\O''Neil\AgentNexus\runtime\'"));
        assert!(script.contains(r"'D:\Data\O''Neil\AgentNexus\tools\'"));
        assert!(script.contains("$me=42"));
        // The root itself is not a program folder: a shell opened in it is not residue.
        assert!(!script.contains(r"'D:\Data\O''Neil\AgentNexus\'"));
        assert_eq!(parse_residue("123\tpython.exe\r\n\nbad\n7\tuv.exe\n"),
            vec![Residue { pid: 123, name: "python.exe".into() }, Residue { pid: 7, name: "uv.exe".into() }]);
    }

    #[test]
    fn an_idle_root_stops_cleanly() {
        let root = std::env::temp_dir().join(format!("anx-shutdown-idle-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(root.join(".anx/logs")).unwrap();
        // A port nothing listens on: nothing to ask, nothing left over.
        let port = std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        std::fs::write(root.join("agent-nexus.json"), format!(r#"{{"manager":{{"port":{port}}}}}"#)).unwrap();
        assert_eq!(run(&root, Duration::from_secs(2)), 0);
        std::fs::remove_dir_all(root).unwrap();
    }
}
