//! Isolated native verification. This example never imports bootstrap, starts a
//! manager, installs the single-instance plugin, or controls another process.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
#[path = "../src/console_window.rs"]
mod console_window;

use std::{env, fs, path::PathBuf, sync::{Arc, atomic::{AtomicU64, Ordering}}, thread, time::{Duration, Instant}};
use serde_json::{json, Value};
use tauri::{Manager, RunEvent, Url, WindowEvent};
use console_window::{ConsoleWindowState, Target, LABEL};

fn write_status(path: &PathBuf, value: &Value) {
    let temporary = path.with_extension("tmp");
    if fs::write(&temporary, serde_json::to_vec(value).unwrap()).is_ok() {
        let _ = fs::rename(temporary, path);
    }
}

fn main() {
    let args: Vec<String> = env::args().collect();
    assert_eq!(args.len(), 5, "expected fixture URL, isolated profile, control file, status file");
    let url: Url = args[1].parse().expect("fixture URL");
    assert_eq!(url.scheme(), "http");
    assert_eq!(url.host_str(), Some("127.0.0.1"));
    let profile = PathBuf::from(&args[2]);
    let control = PathBuf::from(&args[3]);
    let status = PathBuf::from(&args[4]);
    let mut context = tauri::generate_context!();
    context.config_mut().identifier = format!("io.github.muxigame.agentnexus.traytest.p{}", std::process::id());
    let close_requests = Arc::new(AtomicU64::new(0));
    let destroyed = Arc::new(AtomicU64::new(0));
    let close_in_worker = close_requests.clone();
    let destroyed_in_worker = destroyed.clone();
    let app = tauri::Builder::default().manage(ConsoleWindowState::isolated(profile))
        .setup(move |app| {
            let app = app.handle().clone();
            console_window::open(&app, Target::Console(url.clone()));
            thread::spawn(move || {
                let started = Instant::now();
                let mut last_id = 0;
                let mut error = String::new();
                loop {
                    if started.elapsed() > Duration::from_secs(240) { app.exit(2); return; }
                    if let Ok(command) = fs::read(&control).ok().and_then(|data| serde_json::from_slice::<Value>(&data).ok()).ok_or(()) {
                        let id = command["id"].as_u64().unwrap_or(0);
                        if id > last_id {
                            last_id = id;
                            let action = command["action"].as_str().unwrap_or("");
                            let window = app.get_webview_window(LABEL);
                            let result: tauri::Result<()> = match action {
                                "open" => {
                                    let target = command["url"].as_str().and_then(|value| value.parse::<Url>().ok()).unwrap_or_else(|| url.clone());
                                    if target.scheme() != "http" || target.host_str() != Some("127.0.0.1") {
                                        error = "only isolated loopback fixture URLs are allowed".into();
                                    } else {
                                        for _ in 0..command["repeat"].as_u64().unwrap_or(1).min(50) {
                                            console_window::open(&app, Target::Console(target.clone()));
                                        }
                                    }
                                    Ok(())
                                }
                                "close" => window.as_ref().map(|window| window.close()).unwrap_or(Ok(())),
                                "hide" => window.as_ref().map(|window| {
                                    let result = window.hide();
                                    console_window::sync_visibility(window);
                                    result
                                }).unwrap_or(Ok(())),
                                "minimize" => window.as_ref().map(|window| window.minimize()).unwrap_or(Ok(())),
                                "maximize" => window.as_ref().map(|window| window.maximize()).unwrap_or(Ok(())),
                                "unmaximize" => window.as_ref().map(|window| window.unmaximize()).unwrap_or(Ok(())),
                                "resize" => window.as_ref().map(|window| window.set_size(tauri::LogicalSize::new(920.0, 640.0))).unwrap_or(Ok(())),
                                "eval" => window.as_ref().map(|window| window.eval(command["script"].as_str().unwrap_or(""))).unwrap_or(Ok(())),
                                "navigate-ready" => {
                                    let target = command["url"].as_str().and_then(|value| value.parse().ok()).unwrap_or_else(|| url.clone());
                                    console_window::navigate_if_open(&app, target);
                                    Ok(())
                                }
                                "exit" => { app.exit(0); return; }
                                _ => { error = format!("unknown test command: {action}"); Ok(()) }
                            };
                            if let Err(problem) = result { error = problem.to_string(); }
                        }
                    }
                    let window = app.get_webview_window(LABEL);
                    let details = window.as_ref().map(|window| {
                        let size = window.inner_size().ok();
                        let scale = window.scale_factor().ok().unwrap_or(1.0);
                        let logical = size.map(|value| value.to_logical::<f64>(scale));
                        json!({"url":window.url().ok().map(|value|value.to_string()),
                            "visible":window.is_visible().ok(), "minimized":window.is_minimized().ok(),
                            "maximized":window.is_maximized().ok(),
                            "width":logical.map(|value|value.width), "height":logical.map(|value|value.height)})
                    });
                    write_status(&status, &json!({"pid":std::process::id(),"last_command":last_id,
                        "windows":app.webview_windows().len(),"window":details,"error":error,
                        "close_requests":close_in_worker.load(Ordering::SeqCst),
                        "destroyed":destroyed_in_worker.load(Ordering::SeqCst)}));
                    thread::sleep(Duration::from_millis(100));
                }
            });
            Ok(())
        }).build(context).expect("isolated Tauri fixture");
    app.run(move |app, event| {
        match &event {
            RunEvent::WindowEvent { label, event: WindowEvent::CloseRequested { .. }, .. } if label == LABEL => {
                close_requests.fetch_add(1, Ordering::SeqCst);
            }
            RunEvent::WindowEvent { label, event: WindowEvent::Destroyed, .. } if label == LABEL => {
                destroyed.fetch_add(1, Ordering::SeqCst);
            }
            _ => {}
        }
        console_window::handle_event(app, &event, false);
    });
}
