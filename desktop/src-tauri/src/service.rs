//! `agent-nexus.exe --service`: the Windows service `AgentNexus` (system.rs). It runs the Python
//! core from `%ProgramData%\AgentNexus` as LocalSystem, restarts it when it ends on its own, and
//! stops it cleanly (`POST /api/manager/shutdown`, then the process tree) when the service stops.
#![cfg(target_os = "windows")]

use std::ffi::OsString;
use std::fs;
use std::process::Command;
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

use windows_service::service::{ServiceControl, ServiceControlAccept, ServiceExitCode, ServiceState, ServiceStatus,
                               ServiceType};
use windows_service::service_control_handler::{self, ServiceControlHandlerResult};
use windows_service::{define_windows_service, service_dispatcher};

use crate::bootstrap;
use crate::system::{system_root, SERVICE};

define_windows_service!(ffi_service_main, service_main);

pub fn run() -> Result<(), String> {
    service_dispatcher::start(SERVICE, ffi_service_main).map_err(|e| format!("service dispatcher: {e}"))
}

fn service_main(_arguments: Vec<OsString>) {
    let root = system_root();
    bootstrap::note_startup_log(&root, "service: started by the service control manager");
    if let Err(error) = serve() {
        bootstrap::note_startup_log(&root, &format!("service ended: {error}"));
    }
}

fn status(state: ServiceState, accept: ServiceControlAccept, wait: Duration) -> ServiceStatus {
    ServiceStatus { service_type: ServiceType::OWN_PROCESS, current_state: state, controls_accepted: accept,
                    exit_code: ServiceExitCode::Win32(0), checkpoint: 0, wait_hint: wait, process_id: None }
}

fn serve() -> Result<(), String> {
    let (stop_tx, stop_rx) = mpsc::channel::<()>();
    let handler = service_control_handler::register(SERVICE, move |control| match control {
        ServiceControl::Stop | ServiceControl::Shutdown => { let _ = stop_tx.send(()); ServiceControlHandlerResult::NoError }
        ServiceControl::Interrogate => ServiceControlHandlerResult::NoError,
        _ => ServiceControlHandlerResult::NotImplemented,
    }).map_err(|e| e.to_string())?;
    handler.set_service_status(status(ServiceState::Running, ServiceControlAccept::STOP | ServiceControlAccept::SHUTDOWN,
                                      Duration::default())).map_err(|e| e.to_string())?;
    let root = system_root();
    let _ = fs::create_dir_all(&root);
    bootstrap::set_root(root.clone());
    let mut restarts: Vec<Instant> = Vec::new();
    bootstrap::note_startup_log(&root, "service: running; starting the core");
    loop {
        let mut child = match bootstrap::start_core(&root) {
            Ok(child) => Some(child),
            Err(error) => { bootstrap::note_startup_log(&root, &format!("the service could not start the core: {error}")); None }
        };
        // Wait for a stop request or the core ending.
        let stopped = loop {
            if stop_rx.recv_timeout(Duration::from_secs(1)).is_ok() { break true; }
            match child.as_mut().map(|c| c.try_wait()) {
                Some(Ok(Some(exit))) => { bootstrap::note_startup_log(&root, &format!("core ended ({exit})")); break false; }
                Some(Ok(None)) => continue,
                _ => break false,
            }
        };
        if stopped {
            let _ = handler.set_service_status(status(ServiceState::StopPending, ServiceControlAccept::empty(),
                                                      Duration::from_secs(30)));
            if let Some(child) = child.as_mut() { bootstrap::stop_core(&root, child); }
            break;
        }
        // An update stops the service itself; anything else ending the core is restarted, with a brake.
        restarts.retain(|t| t.elapsed() < Duration::from_secs(600));
        restarts.push(Instant::now());
        let pause = if restarts.len() > 5 { Duration::from_secs(60) } else { Duration::from_secs(3) };
        if stop_rx.recv_timeout(pause).is_ok() { break; }
    }
    handler.set_service_status(status(ServiceState::Stopped, ServiceControlAccept::empty(), Duration::default()))
        .map_err(|e| e.to_string())?;
    Ok(())
}

/// The process tree of the core, by pid (Python's own children included).
pub fn kill_tree(pid: u32) {
    let mut stop = Command::new("taskkill");
    stop.args(["/F", "/T", "/PID", &pid.to_string()]);
    bootstrap::quiet(&mut stop);
    let _ = stop.output();
    thread::sleep(Duration::from_millis(200));
}
