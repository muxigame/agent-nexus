//! The console is disposable UI. Closing it must never stop the manager.
use std::path::PathBuf;
use std::sync::Mutex;
use std::thread;
use std::time::Duration;

use serde_json::Value;
use tauri::{AppHandle, LogicalSize, Manager, PhysicalPosition, RunEvent, Url,
    WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent};

pub const LABEL: &str = "main";

#[derive(Clone, Debug, PartialEq)]
pub enum Target {
    Startup,
    Console(Url),
}

impl Target {
    fn webview_url(&self) -> WebviewUrl {
        match self {
            Self::Startup => WebviewUrl::App("index.html".into()),
            Self::Console(url) => WebviewUrl::External(url.clone()),
        }
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq)]
enum Phase {
    #[default]
    Absent,
    Creating,
    Open,
    Closing,
}

#[derive(Clone, Copy, Debug, PartialEq)]
enum OpenAction {
    Create(u64),
    Focus,
    Wait,
}

#[derive(Clone, Debug)]
struct Geometry {
    size: LogicalSize<f64>,
    position: Option<PhysicalPosition<i32>>,
    maximized: bool,
}

impl Default for Geometry {
    fn default() -> Self {
        Self { size: LogicalSize::new(1100.0, 760.0), position: None, maximized: false }
    }
}

struct Lifecycle {
    phase: Phase,
    generation: u64,
    target: Target,
    reopen: bool,
    geometry: Geometry,
    startup: Value,
}

impl Default for Lifecycle {
    fn default() -> Self {
        Self { phase: Phase::Absent, generation: 0, target: Target::Startup,
            reopen: false, geometry: Geometry::default(), startup: Value::Null }
    }
}

impl Lifecycle {
    fn request_open(&mut self, target: Target) -> OpenAction {
        self.target = target;
        match self.phase {
            Phase::Absent => {
                self.generation += 1;
                self.phase = Phase::Creating;
                OpenAction::Create(self.generation)
            }
            Phase::Open => OpenAction::Focus,
            Phase::Creating => OpenAction::Wait,
            Phase::Closing => { self.reopen = true; OpenAction::Wait }
        }
    }

    fn created(&mut self, generation: u64) -> bool {
        if self.phase != Phase::Creating || self.generation != generation { return false; }
        self.phase = Phase::Open;
        true
    }

    fn destroyed(&mut self) -> Option<Target> {
        self.phase = Phase::Absent;
        if std::mem::take(&mut self.reopen) { Some(self.target.clone()) } else { None }
    }

    fn close_failed(&mut self) -> bool {
        self.phase = Phase::Open;
        std::mem::take(&mut self.reopen)
    }
}

#[derive(Default)]
pub struct ConsoleWindowState {
    inner: Mutex<Lifecycle>,
    profile: Option<PathBuf>,
}

impl ConsoleWindowState {
    /// A separate WebView profile for the standalone lifecycle verification example.
    #[allow(dead_code)]
    pub fn isolated(profile: PathBuf) -> Self {
        Self { profile: Some(profile), ..Self::default() }
    }
}

pub fn startup_payload(app: &AppHandle, payload: Value) {
    app.state::<ConsoleWindowState>().inner.lock().unwrap().startup = payload.clone();
    if let Some(window) = app.get_webview_window(LABEL) {
        let _ = window.eval(format!("window.updateStartup && window.updateStartup({payload});"));
    }
}

/// Bootstrap may finish before the first WebView exists, or after the user
/// closed it. Update the pending destination without opening/focusing a window.
pub fn navigate_if_open(app: &AppHandle, target: Url) {
    let is_open = {
        let state = app.state::<ConsoleWindowState>();
        let mut current = state.inner.lock().unwrap();
        current.target = Target::Console(target.clone());
        current.phase == Phase::Open
    };
    if is_open { display_current(app, false); }
}

/// WebView2 does not reliably change document.hidden when its native parent
/// is hidden or minimized. Keep the page's read-only subscriptions in sync.
pub fn sync_visibility(window: &WebviewWindow) {
    if let (Ok(visible), Ok(minimized)) = (window.is_visible(), window.is_minimized()) {
        let active = visible && !minimized;
        let _ = window.eval(format!(
            "window.__mcpmNativeVisible={active};window.dispatchEvent(new Event('mcpmvisibilitychange'));"
        ));
    }
}

fn display_current(app: &AppHandle, focus: bool) {
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        let target = {
            let state = handle.state::<ConsoleWindowState>();
            let current = state.inner.lock().unwrap();
            if current.phase != Phase::Open { return; }
            current.target.clone()
        };
        if let Some(window) = handle.get_webview_window(LABEL) {
            if let Target::Console(url) = target {
                if window.url().ok().as_ref() != Some(&url) { let _ = window.navigate(url); }
            }
            if focus {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
            sync_visibility(&window);
        }
    });
}

fn restore_geometry(window: &WebviewWindow, geometry: &Geometry) {
    let _ = window.set_size(geometry.size);
    if let Some(position) = geometry.position {
        let visible = window.available_monitors().map(|monitors| monitors.iter().any(|monitor| {
            let origin = monitor.position();
            let size = monitor.size();
            i64::from(position.x) >= i64::from(origin.x)
                && i64::from(position.y) >= i64::from(origin.y)
                && i64::from(position.x) < i64::from(origin.x) + i64::from(size.width)
                && i64::from(position.y) < i64::from(origin.y) + i64::from(size.height)
        })).unwrap_or(false);
        if visible { let _ = window.set_position(position); }
        else { let _ = window.center(); }
    }
    if geometry.maximized { let _ = window.maximize(); }
}

fn create_window(app: AppHandle, generation: u64) {
    // Build outside Tauri event callbacks: WebView2 creation can otherwise
    // deadlock on Windows. A close event can precede removal from Tauri's map.
    for _ in 0..200 {
        if app.get_webview_window(LABEL).is_none() { break; }
        thread::sleep(Duration::from_millis(10));
    }
    let (target, geometry, profile) = {
        let state = app.state::<ConsoleWindowState>();
        let current = state.inner.lock().unwrap();
        if current.phase != Phase::Creating || current.generation != generation { return; }
        (current.target.clone(), current.geometry.clone(), state.profile.clone())
    };
    let mut builder = WebviewWindowBuilder::new(&app, LABEL, target.webview_url())
        .title(if profile.is_some() { "Agent Nexus · 隔离验证" } else { "Agent Nexus" })
        .inner_size(geometry.size.width, geometry.size.height)
        .min_inner_size(720.0, 520.0).visible(false)
        .on_page_load(|window, _| {
            sync_visibility(&window);
            let payload = window.app_handle().state::<ConsoleWindowState>()
                .inner.lock().unwrap().startup.clone();
            if !payload.is_null() {
                let _ = window.eval(format!("window.updateStartup && window.updateStartup({payload});"));
            }
        });
    if let Some(profile) = profile { builder = builder.data_directory(profile); }
    match builder.build() {
        Ok(window) => {
            let created = {
                let state = app.state::<ConsoleWindowState>();
                let mut current = state.inner.lock().unwrap();
                current.created(generation)
            };
            if created {
                restore_geometry(&window, &geometry);
                // Bootstrap completion or a second tray click may have changed
                // the target while the WebView was being created.
                display_current(&app, true);
            } else {
                let _ = window.destroy();
            }
        }
        Err(error) => {
            let state = app.state::<ConsoleWindowState>();
            let mut current = state.inner.lock().unwrap();
            if current.generation == generation && current.phase == Phase::Creating {
                current.phase = Phase::Absent;
            }
            eprintln!("无法打开 MCP 管理窗口：{error}");
        }
    }
}

pub fn open(app: &AppHandle, target: Target) {
    let action = app.state::<ConsoleWindowState>().inner.lock().unwrap().request_open(target.clone());
    match action {
        OpenAction::Create(generation) => {
            let app = app.clone();
            thread::spawn(move || create_window(app, generation));
        }
        OpenAction::Focus => {
            if app.get_webview_window(LABEL).is_some() { display_current(app, true); }
            else {
                // Recover from an externally destroyed window without starting
                // another backend or retaining an unusable window handle.
                let state = app.state::<ConsoleWindowState>();
                let mut current = state.inner.lock().unwrap();
                if current.phase == Phase::Open { current.phase = Phase::Absent; }
                drop(current);
                open(app, target);
            }
        }
        OpenAction::Wait => {}
    }
}

fn remember_geometry(app: &AppHandle) {
    let Some(window) = app.get_webview_window(LABEL) else { return; };
    if window.is_minimized().unwrap_or(false) { return; }
    let maximized = window.is_maximized().unwrap_or(false);
    let normal = if !maximized {
        window.inner_size().ok().zip(window.scale_factor().ok())
            .filter(|(size, scale)| size.width > 0 && size.height > 0 && *scale > 0.0)
            .map(|(size, scale)| (size.to_logical::<f64>(scale), window.outer_position().ok()))
    } else { None };
    let state = app.state::<ConsoleWindowState>();
    let mut current = state.inner.lock().unwrap();
    if current.phase != Phase::Open { return; }
    current.geometry.maximized = maximized;
    if let Some((size, position)) = normal {
        current.geometry.size = size;
        current.geometry.position = position;
    }
}

pub fn keep_running(exit_code: Option<i32>, quitting: bool) -> bool {
    exit_code.is_none() && !quitting
}

/// Shared by the production application and the isolated native lifecycle test.
pub fn handle_event(app: &AppHandle, event: &RunEvent, quitting: bool) {
    match event {
        RunEvent::WindowEvent { label, event: WindowEvent::CloseRequested { api, .. }, .. }
            if label == LABEL && !quitting => {
                api.prevent_close();
                remember_geometry(app);
                app.state::<ConsoleWindowState>().inner.lock().unwrap().phase = Phase::Closing;
                if let Some(window) = app.get_webview_window(LABEL) {
                    // Destroy the WebView/controller; hide() retains its heap,
                    // DOM, network subscriptions and browser subprocesses.
                    if let Err(error) = window.destroy() {
                        let reopen = app.state::<ConsoleWindowState>().inner.lock().unwrap().close_failed();
                        if reopen { display_current(app, true); }
                        eprintln!("无法释放 MCP 管理窗口：{error}");
                    }
                } else {
                    let pending = app.state::<ConsoleWindowState>().inner.lock().unwrap().destroyed();
                    if let Some(target) = pending { open(app, target); }
                }
            }
        RunEvent::WindowEvent { label, event: WindowEvent::Destroyed, .. } if label == LABEL => {
            let pending = app.state::<ConsoleWindowState>().inner.lock().unwrap().destroyed();
            if !quitting { if let Some(target) = pending { open(app, target); } }
        }
        RunEvent::WindowEvent { label, event: WindowEvent::Resized(_), .. } if label == LABEL => {
            remember_geometry(app);
            if let Some(window) = app.get_webview_window(LABEL) { sync_visibility(&window); }
        }
        RunEvent::WindowEvent { label, event: WindowEvent::Moved(_), .. }
            if label == LABEL => remember_geometry(app),
        RunEvent::ExitRequested { code, api, .. } if keep_running(*code, quitting) => api.prevent_exit(),
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn console(tab: &str) -> Target {
        Target::Console(format!("http://127.0.0.1:8399/#tab={tab}").parse().unwrap())
    }

    #[test]
    fn repeated_open_builds_one_window_and_keeps_latest_target() {
        let mut state = Lifecycle::default();
        assert_eq!(state.request_open(Target::Startup), OpenAction::Create(1));
        for index in 0..20 {
            assert_eq!(state.request_open(console(&index.to_string())), OpenAction::Wait);
        }
        assert!(state.created(1));
        assert_eq!(state.target, console("19"));
        assert_eq!(state.request_open(console("wechat")), OpenAction::Focus);
    }

    #[test]
    fn opening_while_closing_waits_for_destroyed_and_preserves_latest_link() {
        let mut state = Lifecycle::default();
        state.request_open(console("ones"));
        state.created(1);
        state.phase = Phase::Closing;
        assert_eq!(state.request_open(console("wechat")), OpenAction::Wait);
        assert_eq!(state.request_open(console("workspace")), OpenAction::Wait);
        let target = state.destroyed().unwrap();
        assert_eq!(target, console("workspace"));
        assert_eq!(state.request_open(target), OpenAction::Create(2));
        assert!(!state.created(1));
        assert!(state.created(2));
    }

    #[test]
    fn close_without_reopen_retains_geometry_but_no_window() {
        let mut state = Lifecycle::default();
        state.request_open(console("ones"));
        state.created(1);
        state.geometry.size = LogicalSize::new(900.0, 600.0);
        state.phase = Phase::Closing;
        assert!(state.destroyed().is_none());
        assert_eq!(state.phase, Phase::Absent);
        assert_eq!(state.geometry.size.width, 900.0);
    }

    #[test]
    fn only_implicit_last_window_exit_is_prevented() {
        assert!(keep_running(None, false));
        assert!(!keep_running(Some(0), false));
        assert!(!keep_running(Some(1), false));
        assert!(!keep_running(None, true));
    }

    #[test]
    fn failed_close_consumes_pending_reopen_instead_of_reopening_a_later_close() {
        let mut state = Lifecycle::default();
        state.request_open(console("ones"));
        state.created(1);
        state.phase = Phase::Closing;
        state.request_open(console("wechat"));
        assert!(state.close_failed());
        assert!(!state.reopen);
        state.phase = Phase::Closing;
        assert!(state.destroyed().is_none());
    }
}
