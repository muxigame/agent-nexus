use std::env;
use std::fs::{self, OpenOptions};
use std::io::{Cursor, Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{atomic::{AtomicBool, Ordering}, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};
#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;
use serde_json::{json, Value};
use tauri::{menu::{Menu, MenuItem}, tray::{TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, Url};
use crate::console_window::{self, ConsoleWindowState, Target};
use crate::{autostart, updates, verbs};

#[cfg(test)]
const DEFAULT_URL: &str = "http://127.0.0.1:8399";
const PAYLOAD: &[u8] = include_bytes!(env!("ANX_RUNTIME_ARCHIVE_PATH"));
/// Configuration file names, preferred first (same rule as agent_nexus/paths.py).
pub(crate) const CONFIG_NAMES: [&str; 2] = ["agent-nexus.json", "mcp-manager.json"];
/// The manager's state file, then where a legacy (mcpm) runtime keeps it.
const STATE_FILES: [&str; 2] = [".anx/run/anx-manager.state", ".mcp-manager/run/mcpm-manager.state"];
const STARTUP_LOG: &str = ".anx/logs/startup.log";
/// Portable layouts: the client folder next to the executable.
const SIBLING_ROOTS: [&str; 2] = ["agent-nexus-client", "mcpmanager-client"];
/// Tauri `identifier`; the shell's own data folder under %LOCALAPPDATA% is named after it.
const IDENTIFIER: &str = "io.github.muxigame.agentnexus";
/// Versioned runtimes (section 9.5): `<root>/runtime/<version>-<hash8>/`, the one
/// in use named by `runtime/current`, the one before it by `runtime/previous`.
const RUNTIME_DIR: &str = "runtime";
const COMPLETE_MARKER: &str = ".anx-runtime-complete";
/// Written by `--prepare-runtime` once every Python environment of a runtime is built.
const ENV_READY_MARKER: &str = ".anx-env-ready";
const PYTHON_VERSION: &str = "3.12";
const PRIVATE_ENROLLMENT: &str = ".private/enrollment.json";
/// `--root <path>` of this process.
static ROOT_ARG: OnceLock<PathBuf> = OnceLock::new();

/// `ANX_<name>`, else the legacy `MCPM_<name>` (empty values count as unset).
fn env_compat(name: &str) -> Option<std::ffi::OsString> {
    [format!("ANX_{name}"), format!("MCPM_{name}")].iter()
        .filter_map(|key| env::var_os(key)).find(|value| !value.is_empty())
}

pub(crate) fn has_config(directory: &Path) -> bool {
    CONFIG_NAMES.iter().any(|name| directory.join(name).is_file())
}

fn config_path(root: &Path) -> PathBuf {
    CONFIG_NAMES.iter().map(|name| root.join(name)).find(|path| path.is_file())
        .unwrap_or_else(|| root.join(CONFIG_NAMES[0]))
}
#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Default)]
pub(crate) struct StartupState {
    starting: AtomicBool,
    pub(crate) quit: AtomicBool,
    connected: AtomicBool,
    monitoring: AtomicBool,
    /// Console fragment to show once the manager answers (an `agentnexus://` link).
    pending_fragment: Mutex<Option<String>>,
    root: Mutex<Option<PathBuf>>,
    target: Mutex<String>,
    bootstrap_pid: Mutex<Option<u32>>,
    restarts: Mutex<Vec<Instant>>,
}

/// A manager that stopped answering is resurrected only when its process is
/// really gone and nobody asked it to exit. The manager records both facts in
/// `.anx/run/anx-manager.state` (see agent_nexus/lifecycle.py; a legacy mcpm
/// runtime writes `.mcp-manager/run/mcpm-manager.state`).
const RESTART_LIMIT: usize = 5;
const RESTART_WINDOW: Duration = Duration::from_secs(600);

#[derive(Debug, PartialEq)]
enum ManagerFate { Requested, Alive, Died(String) }

pub(crate) fn read_state(root: &Path) -> Value {
    STATE_FILES.iter().find_map(|name| fs::read_to_string(root.join(name)).ok())
        .and_then(|text| serde_json::from_str(&text).ok()).unwrap_or(Value::Null)
}

fn manager_fate(root: &Path) -> ManagerFate {
    let state = read_state(root);
    if state.get("state").and_then(Value::as_str) == Some("exited")
        && state.get("requested").and_then(Value::as_bool) == Some(true) {
        return ManagerFate::Requested;
    }
    let pid = state.get("pid").and_then(Value::as_u64);
    if let Some(pid) = pid { if process_alive(pid as u32) { return ManagerFate::Alive; } }
    let reason = state.get("reason").and_then(Value::as_str).unwrap_or("no exit was recorded: killed or crashed");
    ManagerFate::Died(format!("pid {} {}", pid.map(|p| p.to_string()).unwrap_or_else(|| "?".into()), reason))
}

pub(crate) fn process_alive(pid: u32) -> bool {
    #[cfg(target_os = "windows")]
    {
        let mut check = Command::new("tasklist");
        check.args(["/FI", &format!("PID eq {pid}"), "/FO", "CSV", "/NH"]);
        quiet(&mut check);
        check.output().map(|out| String::from_utf8_lossy(&out.stdout).contains(&format!("\"{pid}\""))).unwrap_or(false)
    }
    #[cfg(not(target_os = "windows"))]
    { Path::new(&format!("/proc/{pid}")).exists() }
}

fn restart_allowed(app: &AppHandle) -> bool {
    let state = app.state::<StartupState>();
    let mut restarts = state.restarts.lock().unwrap();
    restarts.retain(|at| at.elapsed() < RESTART_WINDOW);
    if restarts.len() >= RESTART_LIMIT { return false; }
    restarts.push(Instant::now());
    true
}

pub(crate) fn note_startup_log(root: &Path, line: &str) {
    let _ = fs::create_dir_all(root.join(STARTUP_LOG).parent().unwrap());
    if let Ok(mut log) = OpenOptions::new().create(true).append(true).open(root.join(STARTUP_LOG)) {
        let _ = writeln!(log, "[desktop] {line}");
    }
}

fn local_url(url: &Url) -> bool {
    url.scheme() == "http" && matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "[::1]"))
}

pub(crate) fn http(url: &Url, method: &str, path: &str) -> Option<(u16, Value)> {
    if !local_url(url) { return None; }
    let host = url.host_str()?;
    let port = url.port_or_known_default()?;
    for address in (host, port).to_socket_addrs().ok()? {
        let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(500)) else { continue; };
        let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
        let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));
        // X-ANX for agent-nexus, X-MCPM for a legacy manager on the same root.
        let request = format!("{method} {path} HTTP/1.1\r\nHost: {host}:{port}\r\nX-ANX: 1\r\nX-MCPM: 1\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
        if stream.write_all(request.as_bytes()).is_err() { continue; }
        let mut bytes = Vec::new();
        let _ = stream.take(1024 * 1024).read_to_end(&mut bytes);
        let text = String::from_utf8_lossy(&bytes);
        let (head, body) = text.split_once("\r\n\r\n")?;
        let status = head.lines().next()?.split_whitespace().nth(1)?.parse().ok()?;
        return Some((status, serde_json::from_str(body).unwrap_or(Value::Null)));
    }
    None
}

pub(crate) fn ping(url: &Url) -> Option<Value> {
    let (status, value) = http(url, "GET", "/api/ping")?;
    let manager = value.get("anx").is_some() || value.get("mcpm").is_some();
    if status == 200 && manager && value.get("root").is_some() { Some(value) } else { None }
}

fn root_from_starts(starts: &[PathBuf]) -> Option<PathBuf> {
    for start in starts {
        for directory in start.ancestors() {
            if has_config(directory) { return Some(directory.to_path_buf()); }
        }
        for name in SIBLING_ROOTS {
            let sibling = start.join(name);
            if has_config(&sibling) { return Some(sibling); }
        }
    }
    None
}

fn root_from_env(value: Option<std::ffi::OsString>) -> Option<PathBuf> {
    value.map(PathBuf::from).filter(|path| has_config(path))
}

fn find_root() -> Option<PathBuf> {
    if let Some(path) = ROOT_ARG.get() { return Some(path.clone()); }
    if let Some(path) = root_from_env(env_compat("ROOT")) { return Some(path); }
    let mut starts = Vec::new();
    if let Ok(exe) = env::current_exe() { if let Some(parent) = exe.parent() { starts.push(parent.to_path_buf()); } }
    if let Ok(cwd) = env::current_dir() { starts.push(cwd); }
    root_from_starts(&starts)
}

pub(crate) fn config(root: &Path) -> Result<Value, String> {
    let text = fs::read_to_string(config_path(root)).map_err(|e| e.to_string())?;
    serde_json::from_str(text.trim_start_matches('\u{feff}')).map_err(|e| format!("配置文件格式错误：{e}"))
}

pub(crate) fn configured_url(root: &Path) -> Result<Url, String> {
    let data = config(root)?;
    let port = data.pointer("/manager/port").and_then(Value::as_u64).unwrap_or(8399);
    if !(1..=65535).contains(&port) { return Err("管理器端口必须在 1–65535 之间".into()); }
    format!("http://127.0.0.1:{port}").parse().map_err(|e| format!("控制台地址错误：{e}"))
}

/// `%LOCALAPPDATA%\AgentNexus`. The legacy `Roc\MCPManager\client` root is
/// never used in place: it stays the rollback target (copying it is `anx migrate`).
fn default_root() -> Result<PathBuf, String> {
    let local = env_compat("DATA_HOME").or_else(|| env::var_os("LOCALAPPDATA"))
        .ok_or("找不到当前用户的数据目录")?;
    Ok(PathBuf::from(local).join("AgentNexus"))
}

/// The root this shell serves: `--root`, else the usual discovery, else the system service's
/// root when installed for all users, else the per-user default.
pub(crate) fn resolve_root() -> Result<PathBuf, String> {
    find_root().map(Ok).unwrap_or_else(|| if crate::system::system_mode() { Ok(crate::system::system_root()) } else { default_root() })
}

/// `--root` for this process (the service and `--setup-system` serve the system root).
pub(crate) fn set_root(root: PathBuf) {
    let _ = ROOT_ARG.set(root);
}

/// `--setup-system`: unpack the runtime and build its environments in the system root.
pub(crate) fn prepare_for_service(_root: &Path) -> Result<(), String> {
    prepare_runtime()
}

/// The service: start the core (no tray: nobody sees session 0) from the system root.
pub(crate) fn start_core(root: &Path) -> Result<Child, String> {
    let project = match source_project(root) { Some(project) => project, None => install_runtime(root)? };
    note_startup_log(root, &format!("service: runtime {}", project.display()));
    let uv = ensure_uv(&project)?;
    let log = root.join(STARTUP_LOG);
    fs::create_dir_all(log.parent().unwrap()).map_err(|e| e.to_string())?;
    if log.is_file() { let _ = fs::rename(&log, log.with_extension("log.1")); }
    fs::write(&log, "").map_err(|e| e.to_string())?;
    let child = spawn_manager(root, &project, &uv, &log, true)?;
    note_startup_log(root, &format!("service: core started (pid {})", child.id()));
    let started = Instant::now();
    if let Ok(url) = configured_url(root) {
        while started.elapsed() < Duration::from_secs(120) {
            if ping(&url).is_some() { prune_runtimes(root); break; }
            thread::sleep(Duration::from_secs(1));
        }
    }
    Ok(child)
}

/// The service stops: ask the core to stop its plugins and exit, then end what is left.
pub(crate) fn stop_core(root: &Path, child: &mut Child) {
    if let Ok(url) = configured_url(root) { let _ = http(&url, "POST", "/api/manager/shutdown"); }
    let deadline = Instant::now() + Duration::from_secs(25);
    while Instant::now() < deadline {
        if let Ok(Some(_)) = child.try_wait() { return; }
        thread::sleep(Duration::from_millis(300));
    }
    #[cfg(target_os = "windows")]
    crate::service::kill_tree(child.id());
}

/// The shell's own data folder (Tauri's app local data dir), holding `active-root`.
fn shell_data_dir() -> Option<PathBuf> {
    env::var_os("LOCALAPPDATA").map(|local| PathBuf::from(local).join(IDENTIFIER))
}

/// Remember which root this installation serves, for `--shutdown-manager` run by
/// the installer (which has no `--root` and must never guess from its cwd).
fn record_active_root(root: &Path) {
    if let Some(dir) = shell_data_dir() {
        let _ = fs::create_dir_all(&dir);
        let _ = fs::write(dir.join("active-root"), root.display().to_string());
    }
}

/// Root for `--shutdown-manager`: `--root`, the recorded active root, the
/// environment or the folders around the executable, else the default root. The
/// working directory is deliberately not searched.
pub(crate) fn shutdown_root() -> Result<PathBuf, String> {
    if let Some(path) = ROOT_ARG.get() { return Ok(path.clone()); }
    if let Some(text) = shell_data_dir().and_then(|dir| fs::read_to_string(dir.join("active-root")).ok()) {
        let path = PathBuf::from(text.trim());
        if path.is_dir() { return Ok(path); }
    }
    if let Some(path) = root_from_env(env_compat("ROOT")) { return Ok(path); }
    let exe_dir = env::current_exe().ok().and_then(|exe| exe.parent().map(Path::to_path_buf));
    if let Some(path) = exe_dir.and_then(|dir| root_from_starts(&[dir])) { return Ok(path); }
    default_root()
}

/// Whether this build carries the private enrollment profile (section 11.4).
pub(crate) fn private_edition() -> bool {
    zip::ZipArchive::new(Cursor::new(PAYLOAD)).map(|mut archive| archive.by_name(PRIVATE_ENROLLMENT).is_ok()).unwrap_or(false)
}

/// `<version>-<hash8>` of the embedded runtime.
fn runtime_tag() -> Result<String, String> {
    let mut archive = zip::ZipArchive::new(Cursor::new(PAYLOAD)).map_err(|e| e.to_string())?;
    let crc = archive.by_name("SHA256.json").map_err(|e| e.to_string())?.crc32();
    Ok(format!("{}-{:08x}", env!("CARGO_PKG_VERSION"), crc ^ (PAYLOAD.len() as u32)))
}

/// Unpack the embedded runtime into `dir`. Configuration files are skipped when
/// `keep_config` says the target already has the user's.
fn extract_into(dir: &Path, keep_config: bool) -> Result<(), String> {
    let mut archive = zip::ZipArchive::new(Cursor::new(PAYLOAD)).map_err(|e| e.to_string())?;
    fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    for index in 0..archive.len() {
        let mut entry = archive.by_index(index).map_err(|e| e.to_string())?;
        let relative = entry.enclosed_name().ok_or("运行包包含无效文件路径")?.to_path_buf();
        let target = dir.join(relative);
        if entry.is_dir() { fs::create_dir_all(&target).map_err(|e| e.to_string())?; continue; }
        if CONFIG_NAMES.contains(&entry.name()) && (keep_config || target.exists()) { continue; }
        if let Some(parent) = target.parent() { fs::create_dir_all(parent).map_err(|e| e.to_string())?; }
        let mut output = fs::File::create(target).map_err(|e| e.to_string())?;
        std::io::copy(&mut entry, &mut output).map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// Flat extraction (`--bootstrap-only <dir>`): the runtime and its initial
/// configuration straight into `root`, an existing configuration kept.
fn extract_runtime(root: &Path) -> Result<(), String> {
    let mut archive = zip::ZipArchive::new(Cursor::new(PAYLOAD)).map_err(|e| e.to_string())?;
    let version = format!("{}-{}", PAYLOAD.len(), archive.by_name("SHA256.json").map_err(|e| e.to_string())?.crc32());
    if fs::read_to_string(root.join(".bundled-version")).ok().as_deref() == Some(&version)
        && root.join("tools/uv.exe").is_file() && root.join("agent_nexus/cli.py").is_file() { return Ok(()); }
    extract_into(root, has_config(root))?;
    fs::write(root.join(".bundled-version"), version).map_err(|e| e.to_string())
}

/// A development checkout or portable client package that runs from the root
/// itself. A root that only holds an earlier flat extraction (`.bundled-version`)
/// moves to the versioned layout.
fn source_project(root: &Path) -> Option<PathBuf> {
    if root.join(".bundled-version").is_file() { return None; }
    project(root).ok()
}

/// Versioned extraction (section 9.5): unpack to `runtime/<tag>.tmp/`, rename it
/// into place, then point `runtime/current` at it. The root keeps only the
/// configuration and data; the bundled `agent-nexus.json` is copied there when
/// the root has no configuration yet.
fn install_runtime(root: &Path) -> Result<PathBuf, String> {
    let tag = runtime_tag()?;
    let base = root.join(RUNTIME_DIR);
    let dir = base.join(&tag);
    if !dir.join(COMPLETE_MARKER).is_file() {
        let staging = base.join(format!("{tag}.tmp"));
        let _ = fs::remove_dir_all(&staging);
        extract_into(&staging, false)?;
        fs::write(staging.join(COMPLETE_MARKER), &tag).map_err(|e| e.to_string())?;
        if dir.exists() { fs::remove_dir_all(&dir).map_err(|e| format!("无法替换不完整的运行环境 {}：{e}", dir.display()))?; }
        fs::rename(&staging, &dir).map_err(|e| e.to_string())?;
    }
    if !has_config(root) {
        let bundled = dir.join(CONFIG_NAMES[0]);
        if bundled.is_file() { fs::copy(&bundled, root.join(CONFIG_NAMES[0])).map_err(|e| e.to_string())?; }
    }
    let current = fs::read_to_string(base.join("current")).ok().map(|t| t.trim().to_string());
    if current.as_deref() != Some(tag.as_str()) {
        if let Some(previous) = current.filter(|p| !p.is_empty() && base.join(p).is_dir()) {
            fs::write(base.join("previous"), previous).map_err(|e| e.to_string())?;
        }
        fs::write(base.join("current"), &tag).map_err(|e| e.to_string())?;
    }
    Ok(dir)
}

/// After the new runtime answered once: keep `current` and `previous`, remove the
/// rest (and staging leftovers). Locked files simply stay for the next start.
fn prune_runtimes(root: &Path) {
    let base = root.join(RUNTIME_DIR);
    let keep: Vec<String> = ["current", "previous"].iter()
        .filter_map(|name| fs::read_to_string(base.join(name)).ok()).map(|t| t.trim().to_string()).collect();
    let Ok(entries) = fs::read_dir(&base) else { return; };
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        if entry.path().is_dir() && !keep.contains(&name) { let _ = fs::remove_dir_all(entry.path()); }
    }
}

fn ensure_uv(root: &Path) -> Result<PathBuf, String> {
    let executable = root.join("tools/uv.exe");
    if !executable.is_file() {
        fs::create_dir_all(root.join("tools")).map_err(|e| e.to_string())?;
        let mut archive = zip::ZipArchive::new(Cursor::new(PAYLOAD)).map_err(|e| e.to_string())?;
        let mut entry = archive.by_name("tools/uv.exe").map_err(|e| e.to_string())?;
        std::io::copy(&mut entry, &mut fs::File::create(&executable).map_err(|e| e.to_string())?).map_err(|e| e.to_string())?;
    }
    Ok(executable)
}

/// Point uv at the Python and package cache an offline package carries in
/// `tools` (runtime layout) so it never goes online for them. True when the
/// cache is there, i.e. uv may run with `UV_OFFLINE`.
fn bundled_uv_env(command: &mut Command, tools: &Path) -> bool {
    if tools.join("python").is_dir() {
        command.env("UV_PYTHON_INSTALL_DIR", tools.join("python")).env("UV_PYTHON_PREFERENCE", "managed");
    }
    let cache = tools.join("uv-cache");
    if cache.is_dir() { command.env("UV_CACHE_DIR", &cache); }
    cache.is_dir()
}

/// The install selection of a `uv run ...` service command (flags that also
/// choose what `uv sync` installs), or None when it is not `uv run`. Same rule
/// as `sync_flags` in scripts/offline_runtime.py, which fills the cache.
fn sync_flags(command: &[String]) -> Option<Vec<String>> {
    let program = Path::new(command.first()?).file_stem()?.to_str()?;
    if program != "uv" || command.get(1).map(String::as_str) != Some("run") { return None; }
    let mut flags = Vec::new();
    let mut rest = command[2..].iter().peekable();
    while let Some(flag) = rest.next_if(|value| value.starts_with("--")) {
        match flag.as_str() {
            "--extra" | "--group" | "--only-group" | "--no-group" => {
                if let Some(value) = rest.next() { flags.push(flag.clone()); flags.push(value.clone()); }
            }
            "--no-dev" | "--all-extras" | "--all-groups" | "--no-default-groups" => flags.push(flag.clone()),
            "--python" | "--project" | "--directory" | "--with" | "--package" => { rest.next(); }
            _ => {}
        }
    }
    Some(flags)
}

/// (label, project, sync flags) of every Python environment a runtime needs:
/// the manager itself, then each builtin plugin that starts with `uv run`.
fn runtime_projects(dir: &Path) -> Vec<(String, PathBuf, Vec<String>)> {
    let mut found = vec![("agent-nexus".to_string(), dir.to_path_buf(), Vec::new())];
    let mut specs: Vec<PathBuf> = fs::read_dir(dir.join("builtins")).map(|entries| entries.flatten()
        .map(|entry| entry.path().join("mcp-service.json")).filter(|path| path.is_file()).collect())
        .unwrap_or_default();
    specs.sort();
    for spec in specs {
        let Ok(text) = fs::read_to_string(&spec) else { continue; };
        let Ok(service) = serde_json::from_str::<Value>(text.trim_start_matches('\u{feff}')) else { continue; };
        let command: Vec<String> = service.get("command").and_then(Value::as_array)
            .map(|items| items.iter().filter_map(|item| item.as_str().map(String::from)).collect()).unwrap_or_default();
        let Some(flags) = sync_flags(&command) else { continue; };
        let Some(base) = spec.parent() else { continue; };
        let project = base.join(service.get("cwd").and_then(Value::as_str).unwrap_or("."));
        if project.join("pyproject.toml").is_file() && project.join("uv.lock").is_file() {
            let label = base.file_name().map(|name| name.to_string_lossy().to_string()).unwrap_or_default();
            found.push((label, project, flags));
        }
    }
    found
}

fn say(line: &str) {
    // The installer shows this in its details list (nsExec::ExecToLog). Without a
    // console the handle is invalid and the write is simply dropped.
    let mut out = std::io::stdout();
    let _ = writeln!(out, "{line}");
    let _ = out.flush();
}

/// `--prepare-runtime`, run by the installer after it copied the files (section
/// 9.7): unpack the embedded runtime and build every Python environment now, from
/// the bundled interpreter and package cache, so opening Agent Nexus after an
/// install or update starts at once. Startup still repairs anything left over.
fn prepare_runtime() -> Result<(), String> {
    let root = shutdown_root()?;
    fs::create_dir_all(&root).map_err(|e| format!("无法创建数据目录 {}：{e}", root.display()))?;
    if source_project(&root).is_some() {
        say(&format!("{} 是开发目录，不需要准备运行环境", root.display()));
        return Ok(());
    }
    say(&format!("正在释放运行环境到 {}…", root.join(RUNTIME_DIR).display()));
    let dir = install_runtime(&root)?;
    let tag = runtime_tag()?;
    if fs::read_to_string(dir.join(ENV_READY_MARKER)).ok().as_deref() == Some(tag.as_str()) {
        say("运行环境已就绪");
        return Ok(());
    }
    let uv = ensure_uv(&dir)?;
    let tools = dir.join("tools");
    for (label, project, flags) in runtime_projects(&dir) {
        say(&format!("正在安装 {label} 的 Python 依赖…"));
        let mut command = Command::new(&uv);
        command.args(["sync", "--frozen", "--python", PYTHON_VERSION, "--project"]).arg(&project).args(&flags)
            .current_dir(&project).env_remove("VIRTUAL_ENV").env_remove("UV_PROJECT_ENVIRONMENT")
            .stdin(Stdio::null());
        if bundled_uv_env(&mut command, &tools) { command.env("UV_OFFLINE", "1"); }
        quiet(&mut command);
        let output = command.output().map_err(|e| format!("无法运行 uv：{e}"))?;
        if !output.status.success() {
            let detail = String::from_utf8_lossy(&output.stderr);
            let last: Vec<&str> = detail.lines().rev().take(12).collect();
            return Err(format!("{label} 的依赖安装失败（{}）：\n{}", output.status,
                               last.into_iter().rev().collect::<Vec<_>>().join("\n")));
        }
    }
    fs::write(dir.join(ENV_READY_MARKER), &tag).map_err(|e| e.to_string())?;
    note_startup_log(&root, &format!("runtime {tag} prepared by the installer"));
    say("运行环境已就绪");
    Ok(())
}

pub(crate) fn quiet(command: &mut Command) {
    #[cfg(target_os = "windows")]
    command.creation_flags(CREATE_NO_WINDOW);
}

fn project(root: &Path) -> Result<PathBuf, String> {
    ["", "agent-nexus", "mcp-manager"].iter().map(|sub| if sub.is_empty() { root.to_path_buf() } else { root.join(sub) })
        .find(|dir| dir.join("pyproject.toml").is_file())
        .ok_or_else(|| "找不到 client 程序文件；请使用重新构建的完整 exe 或 client 包".into())
}

/// Module and console script of the runtime in `project`: agent-nexus, or a
/// legacy mcpm runtime found in a development root.
fn entry_point(project: &Path) -> (&'static str, &'static str) {
    if project.join("agent_nexus/cli.py").is_file() || !project.join("mcpm/cli.py").is_file() { ("agent_nexus.cli", "anx") }
    else { ("mcpm.cli", "mcpm") }
}

fn spawn_manager(root: &Path, project: &Path, uv: &Path, log: &Path, service: bool) -> Result<Child, String> {
    let project = project.to_path_buf();
    let (module, script) = entry_point(&project);
    let python = project.join(".venv/Scripts/python.exe");
    let mut check = Command::new(&python);
    check.args(["-c", &format!("import {module}, mcpkit, pystray, uvicorn")]).current_dir(&project)
        .env("ANX_ROOT", root).env("MCPM_ROOT", root);
    quiet(&mut check);
    let usable = python.is_file() && check.output().map(|result| result.status.success()).unwrap_or(false);
    let mut command = if usable {
        let mut value = Command::new(python);
        value.args(["-m", module]);
        value
    } else {
        let mut value = Command::new(uv);
        value.args(["run", "--python", "3.12", "--project"]).arg(&project).arg(script);
        value
    };
    command.arg("--root").arg(root).arg("serve");
    if !service { command.arg("--tray"); }
    let output = OpenOptions::new().create(true).append(true).open(log).map_err(|e| e.to_string())?;
    // An offline package ships its Python and package cache; uv must not go online for them.
    // They live in the runtime (versioned layout) or the root (portable layout).
    let tools = if project.join("tools").is_dir() { project.join("tools") } else { root.join("tools") };
    bundled_uv_env(&mut command, &tools);
    // Both spellings: a legacy runtime only reads MCPM_*; agent-nexus prefers ANX_*.
    let exe = env::current_exe().map_err(|e| e.to_string())?;
    command.current_dir(root).env("ANX_ROOT", root).env("MCPM_ROOT", root).env("PYTHONUNBUFFERED", "1").env("PYTHONUTF8", "1")
        .env("PATH", format!("{};{}", tools.display(), env::var("PATH").unwrap_or_default()));
    if service {
        // The system service: no window of its own; the core updates the installation itself.
        command.env("ANX_SYSTEM_SERVICE", "1").env("ANX_GUI_EXE", &exe);
    } else {
        command.env("ANX_DESKTOP_APP", &exe).env("MCPM_DESKTOP_APP", &exe);
    }
    command
        .stdin(Stdio::null()).stdout(Stdio::from(output.try_clone().map_err(|e| e.to_string())?)).stderr(Stdio::from(output));
    quiet(&mut command);
    command.spawn().map_err(|e| format!("无法启动运行环境：{e}"))
}

fn tail(log: &Path) -> String {
    let text = fs::read(log).map(|value| String::from_utf8_lossy(&value).to_string()).unwrap_or_default();
    let lines: Vec<&str> = text.lines().rev().take(45).collect();
    lines.into_iter().rev().collect::<Vec<_>>().join("\n")
}

fn progress(app: &AppHandle, stage: usize, message: &str, root: Option<&Path>, logs: &str, error: bool, elapsed: u64) {
    let payload = json!({"stage":stage,"message":message,"root":root.map(|p|p.display().to_string()),
        "logs":logs,"error":error,"elapsed":elapsed});
    console_window::startup_payload(app, payload);
}

fn bootstrap(app: AppHandle, override_url: Option<Url>) {
    if app.state::<StartupState>().connected.load(Ordering::SeqCst) { return; }
    if app.state::<StartupState>().starting.swap(true, Ordering::SeqCst) { return; }
    thread::spawn(move || {
        let started = Instant::now();
        let result: Result<(), String> = (|| {
            progress(&app, 0, "正在查找配置…", None, "", false, 0);
            let root = resolve_root()?;
            fs::create_dir_all(&root).map_err(|e| format!("无法创建数据目录 {}：{e}", root.display()))?;
            *app.state::<StartupState>().root.lock().unwrap() = Some(root.clone());
            if crate::system::system_mode() {
                // Installed for all users: the core is the Windows service; this window only shows it.
                let target = override_url.clone().unwrap_or(configured_url(&root)?);
                *app.state::<StartupState>().target.lock().unwrap() = target.to_string();
                loop {
                    if app.state::<StartupState>().quit.load(Ordering::SeqCst) { return Ok(()); }
                    if let Some(info) = ping(&target) { ready(&app, &target, info); return Ok(()); }
                    let waited = started.elapsed().as_secs();
                    if waited > 90 {
                        return Err("Agent Nexus 核心服务没有响应。请在“服务”里启动 Agent Nexus，或重新安装。".into());
                    }
                    progress(&app, 3, "正在连接 Agent Nexus 核心服务…", Some(&root), "", false, waited);
                    thread::sleep(Duration::from_secs(1));
                }
            }
            record_active_root(&root);
            let (project, versioned) = match source_project(&root) {
                Some(project) => (project, false),
                None => {
                    progress(&app, 1, "正在释放随程序携带的运行环境…", Some(&root), "", false, started.elapsed().as_secs());
                    (install_runtime(&root)?, true)
                }
            };
            // Autostart is set up for installed roots; a development checkout or a
            // portable client folder is left alone.
            if versioned {
                if let Err(error) = autostart::on_start(&autostart::exe(), &root) {
                    note_startup_log(&root, &format!("autostart could not be set up: {error}"));
                }
            }
            let target = override_url.unwrap_or(configured_url(&root)?);
            *app.state::<StartupState>().target.lock().unwrap() = target.to_string();
            if let Some(info) = ping(&target) { ready(&app, &target, info); return Ok(()); }
            let uv = ensure_uv(&project)?;
            progress(&app, 2, "正在准备 Python 环境（通常已由安装程序完成）…", Some(&root), "", false, started.elapsed().as_secs());
            let log = root.join(STARTUP_LOG);
            fs::create_dir_all(log.parent().unwrap()).map_err(|e| e.to_string())?;
            // The previous run's output is the only record of how it ended.
            if log.is_file() { let _ = fs::rename(&log, log.with_extension("log.1")); }
            fs::write(&log, "").map_err(|e| e.to_string())?;
            let state = app.state::<StartupState>();
            let mut process_slot = state.bootstrap_pid.lock().unwrap();
            if app.state::<StartupState>().quit.load(Ordering::SeqCst) { return Ok(()); }
            let mut child = spawn_manager(&root, &project, &uv, &log, false)?;
            *process_slot = Some(child.id());
            drop(process_slot);
            loop {
                if app.state::<StartupState>().quit.load(Ordering::SeqCst) {
                    let mut stop = Command::new("taskkill");
                    stop.args(["/F", "/T", "/PID", &child.id().to_string()]);
                    quiet(&mut stop); let _ = stop.output(); return Ok(());
                }
                if let Some(info) = ping(&target) {
                    ready(&app, &target, info);
                    *app.state::<StartupState>().bootstrap_pid.lock().unwrap() = None;
                    if versioned { prune_runtimes(&root); }
                    return Ok(());
                }
                let logs = tail(&log);
                let stage = if logs.contains("started (pid") || logs.contains("agent-nexus console:") || logs.contains("mcpm console:") { 4 }
                    else if logs.contains("Installing") || logs.contains("Installed") || logs.contains("Building") || logs.contains("Downloading") { 3 } else { 2 };
                let message = match stage { 4 => "正在启动 MCP 服务和控制台…", 3 => "正在安装所需依赖…", _ => "正在准备 Python 运行环境…" };
                progress(&app, stage, message, Some(&root), &logs, false, started.elapsed().as_secs());
                if let Some(exit) = child.try_wait().map_err(|e| e.to_string())? {
                    *app.state::<StartupState>().bootstrap_pid.lock().unwrap() = None;
                    return Err(format!("启动进程已退出（{exit}）。请查看下方启动日志。\n{logs}"));
                }
                thread::sleep(Duration::from_secs(1));
            }
        })();
        if let Err(error) = result {
            let root = app.state::<StartupState>().root.lock().unwrap().clone();
            progress(&app, 0, "启动失败", root.as_deref(), &error, true, started.elapsed().as_secs());
        }
        app.state::<StartupState>().starting.store(false, Ordering::SeqCst);
    });
}

fn with_fragment(target: &Url, fragment: Option<String>) -> Url {
    match fragment {
        Some(fragment) => target.join(&fragment).unwrap_or_else(|_| target.clone()),
        None => target.clone(),
    }
}

fn ready(app: &AppHandle, target: &Url, info: Value) {
    app.state::<StartupState>().connected.store(true, Ordering::SeqCst);
    let fragment = app.state::<StartupState>().pending_fragment.lock().unwrap().take();
    console_window::navigate_if_open(app, with_fragment(target, fragment));
    if app.state::<StartupState>().monitoring.swap(true, Ordering::SeqCst) { return; }
    let app = app.clone(); let target = target.clone();
    thread::spawn(move || {
        let mut misses = 0; let mut current = info;
        loop {
            if app.state::<StartupState>().quit.load(Ordering::SeqCst) { return; }
            if current.get("tray_visible").and_then(Value::as_bool) == Some(true) { hide_startup_tray(&app); }
            thread::sleep(Duration::from_secs(1));
            if let Some(info) = ping(&target) { current = info; misses = 0; continue; }
            misses += 1;
            if misses < 4 { continue; }
            misses = 0;
            let Some(root) = app.state::<StartupState>().root.lock().unwrap().clone() else { app.exit(0); return; };
            // The service restarts its own core (updates included); the window just waits for it.
            if crate::system::system_mode() { continue; }
            match manager_fate(&root) {
                // A stalled manager is still the manager; starting a second one
                // would only fight it for its ports.
                ManagerFate::Alive => continue,
                ManagerFate::Requested => { app.exit(0); return; }
                ManagerFate::Died(why) => {
                    if !restart_allowed(&app) {
                        note_startup_log(&root, &format!("manager ended again ({why}); stopped restarting after {RESTART_LIMIT} restarts in 10 minutes"));
                        console_window::open(&app, Target::Startup);
                        progress(&app, 0, "Agent Nexus 反复意外退出，已停止自动重启", Some(&root),
                                 &format!("{why}\n详见 .anx/logs/anx.log 和 anx-faults.log"), true, 0);
                        app.state::<StartupState>().monitoring.store(false, Ordering::SeqCst);
                        app.state::<StartupState>().connected.store(false, Ordering::SeqCst);
                        return;
                    }
                    note_startup_log(&root, &format!("manager ended unexpectedly ({why}); restarting it"));
                    let state = app.state::<StartupState>();
                    state.connected.store(false, Ordering::SeqCst);
                    state.monitoring.store(false, Ordering::SeqCst);
                    show_startup_tray(&app);
                    bootstrap(app.clone(), None);
                    return;
                }
            }
        }
    });
}

/// One argv, from this process (`primary`) or forwarded by the single-instance
/// plugin from a second `agent-nexus.exe` (section 9.2).
fn launch(app: &AppHandle, args: &[String], primary: bool) {
    let verbs = verbs::parse(args);
    if verbs.one_shot() {
        let root = match resolve_root() { Ok(root) => root, Err(_) => { if primary { app.exit(1); } return; } };
        if let Some(on) = verbs.set_autostart {
            if let Err(error) = autostart::set(on, &autostart::exe(), &root) {
                note_startup_log(&root, &format!("--set-autostart failed: {error}"));
            }
            if primary && !verbs.check_update && !verbs.apply_update { app.exit(0); }
        }
        if verbs.apply_update { updates::apply(app.clone(), root, primary); }
        else if verbs.check_update { updates::check_now(app.clone(), root, primary); }
        return;
    }
    if verbs.autostart {
        // Login start: tray only. A forwarded --autostart has nothing left to do.
        if primary { bootstrap(app.clone(), None); }
        return;
    }
    let fragment = verbs.deep_link.as_deref().and_then(verbs::deep_link_fragment);
    let override_url = args.iter().skip(1).filter_map(|value| value.parse::<Url>().ok()).find(local_url);
    if app.state::<StartupState>().connected.load(Ordering::SeqCst) {
        let configured = app.state::<StartupState>().target.lock().unwrap().parse::<Url>().ok();
        if let Some(target) = override_url.or(configured) {
            console_window::open(app, Target::Console(with_fragment(&target, fragment)));
        }
        return;
    }
    if fragment.is_some() { *app.state::<StartupState>().pending_fragment.lock().unwrap() = fragment; }
    console_window::open(app, Target::Startup);
    // A fast bootstrap may complete between the first connected check and
    // opening the window. Never overwrite its destination with the splash page.
    if app.state::<StartupState>().connected.load(Ordering::SeqCst) {
        let configured = app.state::<StartupState>().target.lock().unwrap().parse::<Url>().ok();
        if let Some(target) = override_url.or(configured) {
            let pending = app.state::<StartupState>().pending_fragment.lock().unwrap().take();
            console_window::navigate_if_open(app, with_fragment(&target, pending));
        }
        return;
    }
    bootstrap(app.clone(), override_url);
}

/// The updater stops the manager on purpose; the watchdog must neither restart
/// it nor end the shell meanwhile.
pub(crate) fn suspend_watchdog(app: &AppHandle) {
    app.state::<StartupState>().quit.store(true, Ordering::SeqCst);
}

/// An update failed after the clean stop: bring the manager back.
pub(crate) fn resume_watchdog(app: &AppHandle) {
    let state = app.state::<StartupState>();
    state.quit.store(false, Ordering::SeqCst);
    state.connected.store(false, Ordering::SeqCst);
    state.monitoring.store(false, Ordering::SeqCst);
    show_startup_tray(app);
    bootstrap(app.clone(), None);
}

fn quit(app: &AppHandle) {
    app.state::<StartupState>().quit.store(true, Ordering::SeqCst);
    if crate::system::system_mode() { app.exit(0); return; }  // the window only; the service keeps the core
    let text = app.state::<StartupState>().target.lock().unwrap().clone();
    if let Ok(url) = text.parse::<Url>() { let _ = http(&url, "POST", "/api/manager/shutdown"); }
    if !app.state::<StartupState>().connected.load(Ordering::SeqCst) {
        if let Some(pid) = *app.state::<StartupState>().bootstrap_pid.lock().unwrap() {
            let mut stop = Command::new("taskkill");
            stop.args(["/F", "/T", "/PID", &pid.to_string()]);
            quiet(&mut stop); let _ = stop.output();
        }
    }
    app.exit(0);
}

/// Tray icons belong to the thread that created them. Dropping one elsewhere
/// deletes the icon but cannot destroy its hidden window, and that orphaned
/// window re-adds the icon when Explorer rebuilds the taskbar (an Explorer
/// restart, or a remote desktop reconnect at another resolution): the second
/// icon beside the manager's own. Both changes therefore run on the main thread.
fn hide_startup_tray(app: &AppHandle) {
    if app.tray_by_id("startup-tray").is_none() { return; }
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || { drop(handle.remove_tray_by_id("startup-tray")); });
}

fn show_startup_tray(app: &AppHandle) {
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        if handle.tray_by_id("startup-tray").is_none() { let _ = startup_tray(&handle); }
    });
}

fn startup_tray(app: &AppHandle) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "打开 Agent Nexus / 重试启动", true, None::<&str>)?;
    let label = if crate::system::system_mode() { "退出界面（核心服务继续运行）" } else { "退出管理器并停止所有服务" };
    let exit = MenuItem::with_id(app, "quit", label, true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &exit])?;
    TrayIconBuilder::with_id("startup-tray").icon(app.default_window_icon().unwrap().clone())
        .tooltip("Agent Nexus — 正在准备启动").menu(&menu).show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "open" => launch(app, &[], false), "quit" => quit(app), _ => () })
        .on_tray_icon_event(|tray, event| { if let TrayIconEvent::DoubleClick { .. } = event { launch(tray.app_handle(), &[], false); } })
        .build(app)?;
    Ok(())
}

pub fn run() {
    let args: Vec<String> = env::args().collect();
    // System mode verbs (system.rs, service.rs): never forwarded to a running window.
    #[cfg(target_os = "windows")]
    if args.iter().any(|value| value == "--service") {
        if let Err(error) = crate::service::run() { eprintln!("{error}"); std::process::exit(1); }
        return;
    }
    if args.iter().any(|value| value == "--setup-system") {
        let code = match crate::system::setup() { Ok(()) => 0, Err(error) => { say(&format!("--setup-system: {error}")); 1 } };
        std::process::exit(code);
    }
    if args.iter().any(|value| value == "--remove-system") {
        crate::system::remove();
        std::process::exit(0);
    }
    if args.iter().any(|value| value == "--stop-service") {
        crate::system::stop_service();
        std::process::exit(0);
    }
    if args.iter().any(|value| value == "--prepare-runtime") {
        if let Some(root) = verbs::parse(&args).root {
            let root = if root.is_absolute() { root } else { env::current_dir().map(|cwd| cwd.join(&root)).unwrap_or(root) };
            let _ = ROOT_ARG.set(root);
        }
        let code = match prepare_runtime() {
            Ok(()) => 0,
            Err(error) => { say(&format!("运行环境未能全部准备好，首次打开时会自动补全：{error}")); 1 }
        };
        std::process::exit(code);
    }
    if let Some(index) = args.iter().position(|value| value == "--bootstrap-only") {
        let result = args.get(index + 1).ok_or_else(|| "missing output directory".to_string()).and_then(|path| extract_runtime(Path::new(path)));
        if result.is_err() { std::process::exit(1); } return;
    }
    let parsed = verbs::parse(&args);
    if let Some(root) = &parsed.root {
        let root = if root.is_absolute() { root.clone() } else { env::current_dir().map(|cwd| cwd.join(root)).unwrap_or_else(|_| root.clone()) };
        let _ = ROOT_ARG.set(root);
    }
    if parsed.shutdown_manager {
        // Runs in this process, never forwarded: the installer waits for it.
        let code = match shutdown_root() {
            Ok(root) => crate::shutdown::run(&root, Duration::from_secs(parsed.timeout_s)),
            Err(error) => { eprintln!("shutdown-manager: {error}"); 0 }
        };
        std::process::exit(code);
    }
    // Plugin order (section 9.1): single instance first, so a second process
    // forwards its argv before any other plugin acts on it.
    let app = tauri::Builder::default().manage(StartupState::default()).manage(ConsoleWindowState::default())
        .plugin(tauri_plugin_single_instance::init(|app, args, _| launch(app, &args, false)))
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_autostart::init(tauri_plugin_autostart::MacosLauncher::LaunchAgent, Some(vec!["--autostart"])))
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .setup(move |app| {
            let args = env::args().collect::<Vec<_>>();
            if verbs::parse(&args).one_shot() { launch(app.handle(), &args, true); return Ok(()); }
            startup_tray(app.handle())?;
            // Installed for all users, the core updates the installation itself (linux_update.py).
            if !crate::system::system_mode() {
                if let Ok(root) = resolve_root() { updates::schedule(app.handle().clone(), root); }
            }
            launch(app.handle(), &args, true);
            Ok(())
        })
        .build(tauri::generate_context!()).expect("无法创建 Agent Nexus 桌面窗口");
    app.run(|app, event| {
        console_window::handle_event(app, &event, app.state::<StartupState>().quit.load(Ordering::SeqCst));
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn state_root(name: &str, state: Option<Value>) -> PathBuf {
        let root = env::temp_dir().join(format!("anx-fate-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join(".anx/run")).unwrap();
        if let Some(state) = state {
            fs::write(root.join(STATE_FILES[0]), state.to_string()).unwrap();
        }
        root
    }

    #[test]
    fn a_legacy_runtime_state_file_is_still_read() {
        let root = env::temp_dir().join(format!("anx-fate-legacy-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join(".mcp-manager/run")).unwrap();
        fs::write(root.join(STATE_FILES[1]), json!({"pid": 1, "state": "exited", "requested": true}).to_string()).unwrap();
        assert_eq!(manager_fate(&root), ManagerFate::Requested);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn find_root_accepts_both_config_names() {
        let base = env::temp_dir().join(format!("anx-find-root-{}", std::process::id()));
        let _ = fs::remove_dir_all(&base);
        let new_root = base.join("new"); let old_root = base.join("old"); let both = base.join("both");
        for dir in [&new_root, &old_root, &both] { fs::create_dir_all(dir.join("deep/er")).unwrap(); }
        fs::write(new_root.join("agent-nexus.json"), r#"{"manager":{"port":8501}}"#).unwrap();
        fs::write(old_root.join("mcp-manager.json"), r#"{"manager":{"port":8502}}"#).unwrap();
        fs::write(both.join("agent-nexus.json"), r#"{"manager":{"port":8503}}"#).unwrap();
        fs::write(both.join("mcp-manager.json"), r#"{"manager":{"port":8504}}"#).unwrap();
        assert_eq!(root_from_starts(&[new_root.join("deep/er")]), Some(new_root.clone()));
        assert_eq!(root_from_starts(&[old_root.join("deep/er")]), Some(old_root.clone()));
        assert_eq!(root_from_env(Some(old_root.clone().into_os_string())), Some(old_root.clone()));
        assert_eq!(root_from_env(Some(base.join("missing").into_os_string())), None);
        // agent-nexus.json wins when both exist; the legacy file is only a fallback.
        assert_eq!(configured_url(&both).unwrap(), "http://127.0.0.1:8503".parse::<Url>().unwrap());
        assert_eq!(configured_url(&old_root).unwrap(), "http://127.0.0.1:8502".parse::<Url>().unwrap());
        let sibling = base.join("portable"); fs::create_dir_all(sibling.join("agent-nexus-client")).unwrap();
        fs::write(sibling.join("agent-nexus-client/agent-nexus.json"), "{}").unwrap();
        assert_eq!(root_from_starts(&[sibling.clone()]), Some(sibling.join("agent-nexus-client")));
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn env_compat_prefers_the_new_name() {
        // Unique names so parallel tests never race on a shared variable.
        let suffix = format!("COMPAT_TEST_{}", std::process::id());
        env::set_var(format!("MCPM_{suffix}"), "old");
        assert_eq!(env_compat(&suffix).unwrap(), "old");
        env::set_var(format!("ANX_{suffix}"), "new");
        assert_eq!(env_compat(&suffix).unwrap(), "new");
        env::set_var(format!("ANX_{suffix}"), "");
        assert_eq!(env_compat(&suffix).unwrap(), "old");
        env::remove_var(format!("ANX_{suffix}"));
        env::remove_var(format!("MCPM_{suffix}"));
        assert!(env_compat(&suffix).is_none());
    }

    #[test]
    fn a_requested_exit_is_never_resurrected() {
        let root = state_root("requested", Some(json!({"pid": 1, "state": "exited", "requested": true, "reason": "tray menu"})));
        assert_eq!(manager_fate(&root), ManagerFate::Requested);
    }

    #[test]
    fn a_live_but_silent_manager_is_left_alone() {
        let root = state_root("alive", Some(json!({"pid": std::process::id(), "state": "running"})));
        assert_eq!(manager_fate(&root), ManagerFate::Alive);
    }

    #[test]
    fn a_vanished_manager_is_restarted() {
        let root = state_root("died", Some(json!({"pid": 4_000_000_000u64, "state": "running"})));
        assert!(matches!(manager_fate(&root), ManagerFate::Died(why) if why.contains("killed or crashed")));
        let missing = state_root("missing", None);
        assert!(matches!(manager_fate(&missing), ManagerFate::Died(_)));
    }
    #[test]
    fn standalone_runtime_is_complete_and_preserves_connection_config() {
        let root = env::temp_dir().join(format!("anx-bootstrap-{}", std::process::id()));
        fs::create_dir_all(&root).unwrap();
        // A legacy-only root: the bundled agent-nexus.json must not shadow it.
        fs::write(root.join("mcp-manager.json"), b"{\"cloud\":{\"enabled\":true}}").unwrap();
        extract_runtime(&root).unwrap();
        assert!(root.join("tools/uv.exe").is_file());
        assert!(root.join("builtins/device/src/server.py").is_file());
        assert!(root.join("agent_nexus/cli.py").is_file());
        assert!(root.join("mcpm/cli.py").is_file());
        assert!(!root.join("agent-nexus.json").exists());
        assert!(config(&root).unwrap()["cloud"]["enabled"].as_bool().unwrap());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn runtime_is_versioned_and_pruned_to_current_and_previous() {
        let root = env::temp_dir().join(format!("anx-versioned-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("runtime/0.1.9-old")).unwrap();
        fs::create_dir_all(root.join("runtime/0.1.8-older")).unwrap();
        fs::write(root.join("runtime/current"), "0.1.9-old").unwrap();
        // A user configuration is never replaced by the bundled one.
        fs::write(root.join("agent-nexus.json"), r#"{"manager":{"port":8555}}"#).unwrap();
        assert!(source_project(&root).is_none());
        let dir = install_runtime(&root).unwrap();
        let tag = runtime_tag().unwrap();
        assert_eq!(dir, root.join("runtime").join(&tag));
        assert!(tag.starts_with(env!("CARGO_PKG_VERSION")));
        assert!(dir.join("agent_nexus/cli.py").is_file() && dir.join("tools/uv.exe").is_file());
        assert!(!root.join("agent_nexus").exists(), "the root holds only configuration and data");
        assert_eq!(fs::read_to_string(root.join("runtime/current")).unwrap(), tag);
        assert_eq!(fs::read_to_string(root.join("runtime/previous")).unwrap(), "0.1.9-old");
        assert_eq!(configured_url(&root).unwrap().port(), Some(8555));
        // Idempotent: a second start reuses the complete directory.
        assert_eq!(install_runtime(&root).unwrap(), dir);
        prune_runtimes(&root);
        assert!(dir.is_dir() && root.join("runtime/0.1.9-old").is_dir());
        assert!(!root.join("runtime/0.1.8-older").exists());
        fs::remove_dir_all(&root).unwrap();
        // A fresh root gets the bundled configuration.
        install_runtime(&root).unwrap();
        assert!(root.join("agent-nexus.json").is_file());
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn sync_flags_follow_the_service_command() {
        let command = |items: &[&str]| items.iter().map(|item| item.to_string()).collect::<Vec<_>>();
        assert_eq!(sync_flags(&command(&["uv", "run", "python", "-m", "src.server"])), Some(vec![]));
        assert_eq!(sync_flags(&command(&["C:/tools/uv.exe", "run", "--extra", "gpu", "--no-dev", "--python", "3.12", "x"])),
                   Some(command(&["--extra", "gpu", "--no-dev"])));
        assert_eq!(sync_flags(&command(&["node", "server.js"])), None);
        assert_eq!(sync_flags(&command(&["uv"])), None);
    }

    #[test]
    fn runtime_projects_are_the_manager_and_uv_run_builtins() {
        let dir = env::temp_dir().join(format!("anx-projects-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        for (name, command, locked) in [("device", r#"["uv","run","python","-m","src.server"]"#, true),
                                        ("node", r#"["node","index.js"]"#, true),
                                        ("unlocked", r#"["uv","run","x"]"#, false)] {
            let base = dir.join("builtins").join(name);
            fs::create_dir_all(&base).unwrap();
            fs::write(base.join("mcp-service.json"), format!("\u{feff}{{\"command\": {command}}}")).unwrap();
            fs::write(base.join("pyproject.toml"), "").unwrap();
            if locked { fs::write(base.join("uv.lock"), "").unwrap(); }
        }
        let found = runtime_projects(&dir);
        let labels: Vec<&str> = found.iter().map(|(label, _, _)| label.as_str()).collect();
        assert_eq!(labels, ["agent-nexus", "device"]);
        assert_eq!(found[1].1, dir.join("builtins/device").join("."));
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn discovers_sibling_and_bom_config_with_default_port() {
        let root = env::temp_dir().join(format!("anx-discovery-{}", std::process::id()));
        let client = root.join("mcpmanager-client"); fs::create_dir_all(&client).unwrap();
        fs::write(client.join("mcp-manager.json"), "\u{feff}{}").unwrap();
        assert_eq!(root_from_starts(&[root.clone()]), Some(client.clone()));
        assert_eq!(configured_url(&client).unwrap(), DEFAULT_URL.parse::<Url>().unwrap());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn only_allows_loopback_console_urls() {
        assert!(local_url(&DEFAULT_URL.parse().unwrap()));
        assert!(!local_url(&"https://example.com".parse().unwrap()));
    }
}
