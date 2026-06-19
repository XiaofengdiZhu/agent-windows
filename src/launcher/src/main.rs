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

use commands::{parse, Action, GlobalFlags, LocalAction, WaitProbe, WaitSpec};
use serde_json::json;

fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().skip(1).collect();

    // ── Parse global flags (consumed before the command name) ──────────────
    let (global, cmd_argv) = strip_global_flags(&argv);

    let action = match parse(&cmd_argv, &global) {
        Ok(a) => a,
        Err(e) => {
            // Parse errors carry their own typed classification for the JSON
            // `type` field (unknown_command / missing_argument / invalid_value
            // / unknown_subcommand) so an agent consumer can pick a retry
            // strategy without parsing free text.
            report_error(e.message(), e.type_name(), &global);
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
        Action::Wait(spec) => run_wait(spec, &global),
    }
}

/// Report an error honoring the output contract: in `--format json` mode every
/// line on stdout must be JSON (including errors), so consumers read one
/// stream; otherwise the human-readable text goes to stderr.
///
/// `error_type` is one of this **closed set** (the JSON `type` field an agent
/// consumer switches on):
///
/// **Parse errors** (from `commands::ParseError::type_name`) — the caller
/// passed bad arguments; retry by fixing the invocation, not the environment:
///   - `unknown_command`    — top-level command not recognized
///   - `unknown_subcommand` — a group's sub wasn't recognized (lists valid ones)
///   - `missing_argument`   — a required positional/value was absent
///   - `invalid_value`      — a value failed to parse (non-integer, bad format)
///
/// **Runtime errors** — something went wrong executing a valid command;
///   - `daemon`  — daemon lifecycle/startup failure or a JSON-RPC error reply
///   - `ipc`     — named-pipe transport failure (connect/send/recv)
///   - `usage`   — `--no-daemon` unsupported by the launcher
///   - `timeout` — a `wait` probe exceeded its timeout
fn report_error(msg: &str, error_type: &str, global: &GlobalFlags) {
    if global.format_json {
        output::print_error_json(msg, error_type);
    } else {
        output::print_error(msg);
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
        report_error(
            "--no-daemon is not supported by the native launcher. \
             Use the Python CLI: agent-windows-python --no-daemon ...",
            "usage",
            global,
        );
        return ExitCode::from(1);
    }

    let python = resolve_python_exe();

    if let Err(e) = daemon::ensure_daemon_running(&python, global.debug) {
        report_error(&e, "daemon", global);
        return ExitCode::from(1);
    }

    // Build the JSON-RPC request: {"jsonrpc":"2.0","method":..,"id":1,"params":..}
    let request = ipc::build_request(method, params);

    match ipc::send_to_daemon(&request) {
        Ok(response) => {
            let parsed: serde_json::Value = match serde_json::from_str(&response) {
                Ok(v) => v,
                Err(e) => {
                    report_error(&format!("invalid response from daemon: {}", e), "ipc", global);
                    return ExitCode::from(1);
                }
            };
            // Error response → surface and exit non-zero.
            if let Some(err) = parsed.get("error") {
                let code = err.get("code").and_then(|c| c.as_i64()).unwrap_or(0);
                let msg = err.get("message").and_then(|m| m.as_str()).unwrap_or("unknown error");
                report_error(&format!("Daemon error [{}]: {}", code, msg), "daemon", global);
                return ExitCode::from(exit_code_for(code));
            }
            // Success → print result.
            if let Some(result) = parsed.get("result") {
                output::print_result(result, global.format_json);
            }
            ExitCode::SUCCESS
        }
        Err(e) => {
            report_error(&e, "ipc", global);
            ExitCode::from(1)
        }
    }
}

/// Run the `wait` polling loop (selector or text mode). Mirrors
/// cli/commands/wait.py: repeatedly probe `wait_check` with local sleeps in
/// between, so the daemon's STA worker is never blocked for the wait duration.
fn run_wait(spec: WaitSpec, global: &GlobalFlags) -> ExitCode {
    if global.no_daemon {
        report_error(
            "--no-daemon is not supported by the native launcher. \
             Use the Python CLI: agent-windows-python --no-daemon ...",
            "usage",
            global,
        );
        return ExitCode::from(1);
    }

    let python = resolve_python_exe();
    if let Err(e) = daemon::ensure_daemon_running(&python, global.debug) {
        report_error(&e, "daemon", global);
        return ExitCode::from(1);
    }

    let (probe_key, probe_val, describe) = match &spec.probe {
        WaitProbe::Selector(s) => ("selector", s.clone(), format!("Element {}", s)),
        WaitProbe::Text(t) => ("text", t.clone(), format!("Text '{}'", t)),
    };

    let poll = if spec.interval > 0.0 {
        spec.interval
    } else {
        0.5
    };
    let start = std::time::Instant::now();

    loop {
        // Enforce the deadline *during* a probe, not only between probes.
        // Each wait_check round-trip can otherwise block up to 150s (the read
        // timeout) on a wedged daemon, so a `wait --timeout 5` would actually
        // stall ~150s before the timeout check below ran. Bound each probe's
        // read to the remaining budget so the caller's --timeout is honored.
        let remaining = spec.timeout - start.elapsed().as_secs_f64();
        if remaining <= 0.0 {
            report_error(
                &format!("{} not found within {}s", describe, spec.timeout),
                "timeout",
                global,
            );
            return ExitCode::from(1);
        }
        // Cap the per-probe read timeout at the remaining budget (and the 150s
        // ceiling). send_to_daemon_bounded shrinks each transient retry's
        // timeout to what's left of this budget, so the whole probe — retries
        // included — stays bounded by `remaining`.
        let probe_timeout_ms = (remaining * 1000.0) as u32;
        let probe_timeout_ms = probe_timeout_ms.min(win32::READ_TIMEOUT_MS);

        let req = ipc::build_request(
            "wait_check",
            &json!({ probe_key: probe_val }),
        );
        match ipc::send_to_daemon_bounded(&req, probe_timeout_ms) {
            Ok(resp) => {
                if let Ok(parsed) = serde_json::from_str::<serde_json::Value>(&resp) {
                    if parsed.get("error").is_none() {
                        if let Some(result) = parsed.get("result") {
                            if result.get("found").and_then(|f| f.as_bool()).unwrap_or(false) {
                                let elapsed = start.elapsed().as_secs_f64();
                                let element = result
                                    .get("element")
                                    .or_else(|| result.get("matched_element"))
                                    .and_then(|e| e.as_str())
                                    .unwrap_or("");
                                let output = format!(
                                    "## Wait\n{} appeared after {:.1}s",
                                    describe, elapsed
                                );
                                let elapsed_rounded = (elapsed * 10.0).round() / 10.0;
                                let mut out = serde_json::Map::new();
                                out.insert("output".into(), output.into());
                                out.insert("elapsed".into(), serde_json::json!(elapsed_rounded));
                                if probe_key == "selector" {
                                    out.insert("selector".into(), probe_val.clone().into());
                                    out.insert("element".into(), element.into());
                                } else {
                                    out.insert("text".into(), probe_val.clone().into());
                                    out.insert("matched_element".into(), element.into());
                                }
                                output::print_result(&serde_json::Value::Object(out), global.format_json);
                                return ExitCode::SUCCESS;
                            }
                        }
                    } else if let Some(err) = parsed.get("error") {
                        let msg = err.get("message").and_then(|m| m.as_str()).unwrap_or("error");
                        report_error(&format!("wait_check failed: {}", msg), "daemon", global);
                        return ExitCode::from(1);
                    }
                }
            }
            Err(e) => {
                report_error(&format!("IPC error during wait: {}", e), "ipc", global);
                return ExitCode::from(1);
            }
        }

        std::thread::sleep(std::time::Duration::from_secs_f64(poll));
    }
}

/// Run a local action (daemon start/stop/status). These never hit the IPC pipe
/// for start/status (they manage the process directly); stop sends a shutdown
/// request.
fn run_local(local: LocalAction, global: &GlobalFlags) -> ExitCode {
    match local {
        LocalAction::DaemonStart => {
            let python = resolve_python_exe();
            match daemon_spawn_and_wait(&python, global.debug) {
                Ok(msg) => {
                    output::print_text(&msg);
                    ExitCode::SUCCESS
                }
                Err(e) => {
                    report_error(&e, "daemon", global);
                    ExitCode::from(1)
                }
            }
        }
        LocalAction::DaemonStop => {
            // Delegate to the shared non-blocking stop path: fire-and-forget
            // shutdown + bounded PID-gone poll (no 150s reply wait, no silent
            // force-kill). Mirrors kill_daemon's transport but reports honestly.
            match daemon::stop_daemon() {
                Ok(msg) => {
                    output::print_text(&msg);
                    ExitCode::SUCCESS
                }
                Err(e) => {
                    report_error(&e, "daemon", global);
                    ExitCode::from(1)
                }
            }
        }
        LocalAction::DaemonStatus => {
            let pipe_up = ipc::daemon_pipe_exists();
            let pid = daemon::read_pid_from_file();
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
                        "⚠ Version mismatch — the next command will restart the daemon".into(),
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
/// Shares the same spawn + early-exit-poll + stderr-surfacing loop as
/// `ensure_daemon_running`, so a broken-Python install fails fast (~one poll
/// interval) with the real traceback instead of hanging 5s on the generic
/// "did not become ready" message. The earlier asymmetry (#9 was only applied
/// to the cold-start path) is closed here.
fn daemon_spawn_and_wait(python: &str, debug: bool) -> Result<String, String> {
    if ipc::daemon_pipe_exists() {
        return Ok("Daemon already running".into());
    }
    match daemon::spawn_and_wait_for_pipe(python, debug) {
        Ok(pid) => Ok(format!("Daemon started (PID {})", pid)),
        Err(e) => Err(e),
    }
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

/// Locate the Python interpreter that will run the daemon.
///
/// Resolution order:
/// 1. `$AGENT_WINDOWS_PYTHON` — explicit override (highest priority).
/// 2. `python` on PATH — the common case; uv tool installs and venvs put the
///    right interpreter first on PATH.
///
/// We deliberately do NOT verify the interpreter here: importing the module
/// would cost a process spawn (~230ms) and defeat the launcher's fast-startup
/// goal. If this `python` lacks `agent_windows` installed, the spawn still
/// fails fast — `ensure_daemon_running` detects the early exit and surfaces
/// the daemon's real stderr traceback instead.
fn resolve_python_exe() -> String {
    if let Ok(p) = std::env::var("AGENT_WINDOWS_PYTHON") {
        if !p.is_empty() {
            return p;
        }
    }
    // Default — the daemon spawn will fail with a clear message if this python
    // doesn't have agent_windows installed (ensure_daemon_running detects the
    // early exit and surfaces the daemon.log hint).
    "python".into()
}

/// Map a JSON-RPC error code to a process exit code. Protocol errors
/// (the JSON-RPC 2.0 reserved range `-32700..=-32000`, e.g. PARSE_ERROR /
/// INVALID_REQUEST / METHOD_NOT_FOUND / INVALID_PARAMS) exit 2; everything
/// else — the application range `-1001..=-1999` or any other code — exits 1.
/// See `src/agent_windows/ipc/protocol.py` for the authoritative code list.
fn exit_code_for(code: i64) -> u8 {
    if (-32700..=-32000).contains(&code) {
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
  shell                Run a shell command (subject to the daemon's blacklist)

  daemon start/stop/status         Daemon management
  ping                             Daemon health check (round-trip)

Native launcher (fast startup). For --no-daemon or config commands, use
agent-windows-python (the Python CLI).
"#;
    output::print_text(help);
}

#[cfg(test)]
mod tests {
    use super::exit_code_for;

    /// The JSON-RPC 2.0 reserved range (-32700..=-32000) maps to exit 2
    /// (protocol error). Locks the boundary so a future edit to the range is
    /// caught. The Python codes live in `ipc/protocol.py`: PARSE_ERROR=-32700,
    /// INVALID_REQUEST=-32600, METHOD_NOT_FOUND=-32601, INVALID_PARAMS=-32602,
    /// and the application range ELEMENT_NOT_FOUND=-1001 .. DAEMON_INTERNAL=-1999.
    #[test]
    fn exit_code_protocol_range_is_2() {
        // Endpoints of the reserved range.
        assert_eq!(exit_code_for(-32700), 2);
        assert_eq!(exit_code_for(-32000), 2);
        // The four standard protocol codes all map to 2.
        assert_eq!(exit_code_for(-32700), 2); // PARSE_ERROR
        assert_eq!(exit_code_for(-32600), 2); // INVALID_REQUEST
        assert_eq!(exit_code_for(-32601), 2); // METHOD_NOT_FOUND
        assert_eq!(exit_code_for(-32602), 2); // INVALID_PARAMS
    }

    #[test]
    fn exit_code_application_range_is_1() {
        // Application-specific codes (ELEMENT_NOT_FOUND .. DAEMON_INTERNAL_ERROR).
        assert_eq!(exit_code_for(-1001), 1);
        assert_eq!(exit_code_for(-1003), 1); // WAIT_TIMEOUT
        assert_eq!(exit_code_for(-1999), 1); // DAEMON_INTERNAL_ERROR
    }

    #[test]
    fn exit_code_just_outside_reserved_range_is_1() {
        // One step below the reserved range is NOT a protocol error.
        // (This is the regression guard: the old code used `-3999` as the lower
        // bound, which would have classified -3500 / -3999 as exit 2.)
        assert_eq!(exit_code_for(-32701), 1);
        assert_eq!(exit_code_for(-31999), 1);
        assert_eq!(exit_code_for(-3500), 1);
        assert_eq!(exit_code_for(-3999), 1);
        assert_eq!(exit_code_for(0), 1);
    }
}
