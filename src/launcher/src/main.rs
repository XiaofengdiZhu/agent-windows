//! agent-windows native launcher.
//!
//! A thin command forwarder: parse argv → build a JSON-RPC request → send it
//! to the daemon over the named pipe → print the response. The daemon (Python)
//! stays resident and does all the real work; this binary just replaces the
//! per-call Python CLI process (whose ~230ms startup was dominated by CPython
//! + Typer import).
//!
//! Usage mirrors the Python CLI: `agent-windows [--no-daemon] [--format json]
//! [--debug] <command> [args...]`.

mod commands;
mod daemon;
mod ipc;
mod output;
mod win32;

use std::process::ExitCode;
use std::time::Duration;

use commands::{parse, Action, GlobalFlags, LocalAction};
use serde_json::json;

fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().skip(1).collect();

    // ── Parse global flags (consumed before the command name) ──────────────
    let (global, cmd_argv) = strip_global_flags(&argv);

    let action = match parse(&cmd_argv, &global) {
        Ok(a) => a,
        Err(e) => {
            output::print_error(&e);
            return ExitCode::from(1);
        }
    };

    match action {
        Action::Help => {
            print_help();
            ExitCode::SUCCESS
        }
        Action::Local(local) => run_local(local, &global),
        Action::Call { method, params } => run_call(&method, &params, &global),
    }
}

/// Strip leading `--no-daemon` / `--format` / `--debug` and any `--help`/`-h`,
/// returning the remaining argv (command + its args).
fn strip_global_flags(argv: &[String]) -> (GlobalFlags, Vec<String>) {
    let mut g = GlobalFlags::default();
    let mut rest = Vec::new();
    let mut iter = argv.iter();
    while let Some(a) = iter.next() {
        match a.as_str() {
            "--no-daemon" => g.no_daemon = true,
            "--debug" => g.debug = true,
            "--format" => {
                if let Some(v) = iter.next() {
                    g.format_json = v == "json";
                }
            }
            s if s.starts_with("--format=") => {
                g.format_json = &s["--format=".len()..] == "json";
            }
            "--help" | "-h" => {
                rest.insert(0, "--help".into());
            }
            _ => rest.push(a.clone()),
        }
    }
    (g, rest)
}

/// Run a daemon call: ensure daemon up → build request → IPC → print.
fn run_call(method: &str, params: &serde_json::Value, global: &GlobalFlags) -> ExitCode {
    // --no-daemon isn't supported by the launcher (it needs the in-process
    // Python handlers). Route the user to the Python CLI.
    if global.no_daemon {
        output::print_error(
            "--no-daemon is not supported by the native launcher. \
             Use the Python CLI: agent-windows-python --no-daemon ...",
        );
        return ExitCode::from(1);
    }

    let python = resolve_python_exe();

    if let Err(e) = daemon::ensure_daemon_running(&python, global.debug) {
        output::print_error(&e);
        return ExitCode::from(1);
    }

    // Build the JSON-RPC request: {"jsonrpc":"2.0","method":..,"id":1,"params":..}
    let request = build_request(method, params);

    match ipc::send_to_daemon(&request) {
        Ok(response) => {
            let parsed: serde_json::Value = match serde_json::from_str(&response) {
                Ok(v) => v,
                Err(e) => {
                    output::print_error(&format!("invalid response from daemon: {}", e));
                    return ExitCode::from(1);
                }
            };
            // Error response → surface and exit non-zero.
            if let Some(err) = parsed.get("error") {
                let code = err.get("code").and_then(|c| c.as_i64()).unwrap_or(0);
                let msg = err.get("message").and_then(|m| m.as_str()).unwrap_or("unknown error");
                output::print_error(&format!("Daemon error [{}]: {}", code, msg));
                return ExitCode::from(exit_code_for(code));
            }
            // Success → print result.
            if let Some(result) = parsed.get("result") {
                output::print_result(result, global.format_json);
            }
            ExitCode::SUCCESS
        }
        Err(e) => {
            output::print_error(&e);
            ExitCode::from(1)
        }
    }
}

/// Run a local action (daemon start/stop/status). These never hit the IPC pipe
/// for start/status (they manage the process directly); stop sends a shutdown
/// request.
fn run_local(local: LocalAction, _global: &GlobalFlags) -> ExitCode {
    match local {
        LocalAction::DaemonStart => {
            let python = resolve_python_exe();
            match daemon_spawn_and_wait(&python) {
                Ok(msg) => {
                    output::print_text(&msg);
                    ExitCode::SUCCESS
                }
                Err(e) => {
                    output::print_error(&e);
                    ExitCode::from(1)
                }
            }
        }
        LocalAction::DaemonStop => {
            // Send a shutdown request via IPC (best-effort).
            let req = build_request("shutdown", &json!({}));
            match ipc::send_to_daemon(&req) {
                Ok(_) => {
                    output::print_text("Daemon shutdown requested");
                    ExitCode::SUCCESS
                }
                Err(e) => {
                    output::print_error(&format!("Error stopping daemon: {}", e));
                    ExitCode::from(1)
                }
            }
        }
        LocalAction::DaemonStatus => {
            let pipe_up = ipc::daemon_pipe_exists();
            let pid = read_daemon_pid();
            // A daemon is "running" if its PID is alive AND the pipe responds.
            let alive = match pid {
                Some(p) if win32::is_process_alive(p) => true,
                _ => false,
            };
            let running = alive && pipe_up;
            let version = read_daemon_version();
            let tool_version = env!("CARGO_PKG_VERSION");

            let mut lines = vec![format!(
                "Status: {}",
                if running { "running" } else { "stopped" }
            )];
            if running {
                lines.push(format!("PID: {}", pid.unwrap()));
                lines.push(format!("Daemon version: {}", version));
                lines.push(format!("Tool version: {}", tool_version));
                if version != tool_version {
                    lines.push(
                        "⚠ Version mismatch (will auto-restart on next command)".into(),
                    );
                }
            } else {
                lines.push(format!("Tool version: {}", tool_version));
            }
            output::print_text(&lines.join("\n"));
            ExitCode::SUCCESS
        }
    }
}

/// Spawn the daemon and wait for it to be ready (mirrors `daemon start`).
fn daemon_spawn_and_wait(python: &str) -> Result<String, String> {
    if ipc::daemon_pipe_exists() {
        return Ok("Daemon already running".into());
    }
    let (handle, pid) = win32::spawn_daemon(python, false)?;
    let ready = daemon::wait_daemon_ready(Duration::from_secs(5));
    unsafe { windows_sys::Win32::Foundation::CloseHandle(handle) };
    if ready {
        Ok(format!("Daemon started (PID {})", pid))
    } else {
        Err("Daemon did not become ready".into())
    }
}

/// Read the daemon PID from its PID file (best-effort).
fn read_daemon_pid() -> Option<u32> {
    let content = std::fs::read_to_string(daemon::pid_file_path()).ok()?;
    content.lines().next()?.trim().parse().ok()
}

/// Read the daemon's self-reported version from `~/.agent-windows/daemon.version`.
fn read_daemon_version() -> String {
    match std::fs::read_to_string(daemon::version_file_path()) {
        Ok(s) => {
            let trimmed = s.trim();
            if trimmed.is_empty() {
                "unknown".into()
            } else {
                trimmed.to_string()
            }
        }
        Err(_) => "unknown".into(),
    }
}

/// Locate the Python interpreter that has `agent_windows` installed.
/// Strategy: $AGENT_WINDOWS_PYTHON → "python" on PATH → fall back error.
fn resolve_python_exe() -> String {
    if let Ok(p) = std::env::var("AGENT_WINDOWS_PYTHON") {
        return p;
    }
    // Try `python` on PATH — the common case for a uv tool install / venv.
    "python".into()
}

/// Build the JSON-RPC 2.0 request string.
fn build_request(method: &str, params: &serde_json::Value) -> String {
    // Match the Python client: omit params when empty, include otherwise.
    if params.as_object().map(|o| o.is_empty()).unwrap_or(false) {
        json!({ "jsonrpc": "2.0", "method": method, "id": 1 }).to_string()
    } else {
        json!({ "jsonrpc": "2.0", "method": method, "id": 1, "params": params }).to_string()
    }
}

/// Map a JSON-RPC error code to a process exit code. We mirror the convention
/// that application errors (-1xxx) exit 1; protocol errors exit 2.
fn exit_code_for(code: i64) -> u8 {
    if code >= -3999 && code <= -32000 {
        2 // protocol error
    } else {
        1 // application error
    }
}

fn print_help() {
    let help = r#"agent-windows — Windows Computer-Use CLI for AI Agents.

Usage: agent-windows [OPTIONS] <COMMAND>

Global options (before the command):
  --no-daemon   Not supported by the launcher (use agent-windows-python)
  --format json JSON output instead of text
  --debug       Verbose (only affects daemon spawn logging)

Commands:
  observe               Combined observation (snapshot + screenshot)
  snapshot              UIA accessibility tree
  screenshot            Screen capture (optionally cropped)
  ground                Visual coordinate grounding
  ask-vision            Free-form visual Q&A
  detect                Multi-target detection
  get-text              Get text content of a UI element
  is visible/enabled    Check element state
  highlight             Highlight a region on a screenshot
  diff                  Pixel-level screenshot diff
  cleanup               Clean up old screenshots

  click / dblclick / hover / move  Mouse actions
  mouse-down / mouse-up            Press / release mouse button
  drag / scroll                    Drag / scroll
  type / shortcut                  Keyboard actions

  app launch/switch/list/resize    Application management
  fs read/write/list               File operations
  clipboard get/set                Clipboard access
  process list/kill                Process management

  wait                 Wait for seconds / element / text
  batch                Execute multiple commands in one IPC round-trip

  daemon start/stop/status         Daemon management

Native launcher (fast startup). For --no-daemon or config commands, use
agent-windows-python (the Python CLI).
"#;
    output::print_text(help);
}
