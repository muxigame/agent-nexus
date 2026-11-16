#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod autostart;
mod bootstrap;
mod console_window;
mod shutdown;
mod updates;
mod verbs;
fn main() { bootstrap::run(); }
