//! argv verbs (docs/design.md section 9.2).
//!
//! The console page has no Tauri API and the pystray tray cannot call Tauri, so
//! everything they want from the shell is a second `agent-nexus.exe <verb>`
//! process. The single-instance plugin forwards its argv to the running shell's
//! `launch()`; when no shell runs, the new process handles the verb itself.
use std::path::PathBuf;

pub const DEEP_LINK_SCHEME: &str = "agentnexus://";
pub const DEFAULT_SHUTDOWN_TIMEOUT_S: u64 = 20;

#[derive(Debug, PartialEq)]
pub struct Verbs {
    /// Started by the login Run value: bootstrap only, no window.
    pub autostart: bool,
    pub set_autostart: Option<bool>,
    pub check_update: bool,
    pub apply_update: bool,
    pub shutdown_manager: bool,
    pub timeout_s: u64,
    pub root: Option<PathBuf>,
    pub deep_link: Option<String>,
}

impl Default for Verbs {
    fn default() -> Self {
        Self { autostart: false, set_autostart: None, check_update: false, apply_update: false,
            shutdown_manager: false, timeout_s: DEFAULT_SHUTDOWN_TIMEOUT_S, root: None, deep_link: None }
    }
}

impl Verbs {
    /// A verb that does one thing and needs neither the window nor a bootstrap.
    pub fn one_shot(&self) -> bool {
        self.set_autostart.is_some() || self.check_update || self.apply_update
    }
}

fn switch(value: &str) -> Option<bool> {
    match value.to_ascii_lowercase().as_str() {
        "on" | "true" | "1" | "yes" => Some(true),
        "off" | "false" | "0" | "no" => Some(false),
        _ => None,
    }
}

/// `args[0]` is the executable and is skipped.
pub fn parse(args: &[String]) -> Verbs {
    let mut verbs = Verbs::default();
    let mut index = 1;
    while index < args.len() {
        let arg = args[index].as_str();
        match arg {
            "--autostart" => verbs.autostart = true,
            "--check-update" => verbs.check_update = true,
            "--apply-update" => verbs.apply_update = true,
            "--shutdown-manager" => verbs.shutdown_manager = true,
            "--timeout" | "--root" | "--set-autostart" => {
                if let Some(value) = args.get(index + 1) {
                    match arg {
                        "--timeout" => if let Ok(seconds) = value.parse() { verbs.timeout_s = seconds; },
                        "--root" => verbs.root = Some(PathBuf::from(value)),
                        _ => verbs.set_autostart = switch(value),
                    }
                    index += 1;
                }
            }
            _ => {
                if let Some(value) = arg.strip_prefix("--set-autostart=") { verbs.set_autostart = switch(value); }
                else if let Some(value) = arg.strip_prefix("--root=") { verbs.root = Some(PathBuf::from(value)); }
                else if let Some(value) = arg.strip_prefix("--timeout=") {
                    if let Ok(seconds) = value.parse() { verbs.timeout_s = seconds; }
                }
                else if arg.len() > DEEP_LINK_SCHEME.len()
                    && arg[..DEEP_LINK_SCHEME.len()].eq_ignore_ascii_case(DEEP_LINK_SCHEME) {
                    verbs.deep_link = Some(arg.to_string());
                }
            }
        }
        index += 1;
    }
    verbs
}

/// The console fragment an `agentnexus://` link opens (section 11.5):
/// `agentnexus://enroll?invite=<token>` -> `#/enroll?invite=<token>`.
pub fn deep_link_fragment(link: &str) -> Option<String> {
    let rest = link.get(DEEP_LINK_SCHEME.len()..)?.trim_end_matches('/');
    let (action, query) = rest.split_once('?').unwrap_or((rest, ""));
    let action = action.trim_matches('/');
    if action.is_empty() || !action.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_') { return None; }
    let query: String = query.chars().filter(|c| c.is_ascii_alphanumeric() || "-_.~=&%+".contains(*c)).collect();
    Some(if query.is_empty() { format!("#/{action}") } else { format!("#/{action}?{query}") })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn argv(items: &[&str]) -> Vec<String> {
        std::iter::once("agent-nexus.exe").chain(items.iter().copied()).map(String::from).collect()
    }

    #[test]
    fn parses_every_verb() {
        let verbs = parse(&argv(&["--autostart", "--root", r"D:\Data\x\AgentNexus"]));
        assert!(verbs.autostart && !verbs.one_shot());
        assert_eq!(verbs.root, Some(PathBuf::from(r"D:\Data\x\AgentNexus")));
        assert_eq!(parse(&argv(&["--set-autostart=on"])).set_autostart, Some(true));
        assert_eq!(parse(&argv(&["--set-autostart=off"])).set_autostart, Some(false));
        assert_eq!(parse(&argv(&["--set-autostart", "off"])).set_autostart, Some(false));
        assert!(parse(&argv(&["--check-update"])).one_shot());
        assert!(parse(&argv(&["--apply-update"])).apply_update);
        let stop = parse(&argv(&["--shutdown-manager", "--timeout", "7", "--root=D:/r"]));
        assert!(stop.shutdown_manager);
        assert_eq!(stop.timeout_s, 7);
        assert_eq!(stop.root, Some(PathBuf::from("D:/r")));
        assert_eq!(parse(&argv(&["--shutdown-manager"])).timeout_s, DEFAULT_SHUTDOWN_TIMEOUT_S);
        // A console URL is not a verb; launch() reads it separately.
        assert_eq!(parse(&argv(&["http://127.0.0.1:8399/#tab=x"])), Verbs::default());
    }

    #[test]
    fn deep_links_become_console_fragments() {
        let verbs = parse(&argv(&["agentnexus://enroll?invite=abc-123"]));
        assert_eq!(verbs.deep_link.as_deref(), Some("agentnexus://enroll?invite=abc-123"));
        assert_eq!(deep_link_fragment("agentnexus://enroll?invite=abc-123").as_deref(), Some("#/enroll?invite=abc-123"));
        assert_eq!(deep_link_fragment("agentnexus://enroll/?invite=a\"b<c").as_deref(), Some("#/enroll?invite=abc"));
        assert_eq!(deep_link_fragment("agentnexus://inbox").as_deref(), Some("#/inbox"));
        assert_eq!(deep_link_fragment("agentnexus://../x"), None);
    }
}
