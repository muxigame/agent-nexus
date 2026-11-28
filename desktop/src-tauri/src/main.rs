#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod autostart;
mod bootstrap;
mod cli_path;
mod console_window;
mod shutdown;
#[cfg(target_os = "windows")]
mod service;
mod system;
mod updates;
mod verbs;
fn main() { bootstrap::run(); }
