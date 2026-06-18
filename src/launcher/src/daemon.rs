//! Daemon lifecycle: read the PID file, decide whether to spawn a fresh
//! daemon, and wait for the pipe to come up.
//!
//! Mirrors `agent_windows.ipc.client.ensure_daemon_running` *including the
//! race fix*: if the pipe is momentarily gone but the daemon process is alive,
//! don't spawn a rival — let the IPC connect-retry handle the gap.

use std::path::PathBuf;
use std::time::Duration;

use crate::ipc::{daemon_pipe_exists, wait_for_pipe};
use crate::win32::{is_process_alive, poll_process, spawn_daemon};

/// Max retries × interval while waiting for a freshly-spawned daemon to create
/// its pipe (matches Python's MAX_CONNECT_RETRIES × RETRY_INTERVAL_S).
const MAX_CONNECT_RETRIES: u32 = 25;
const RETRY_INTERVAL: Duration = Duration::from_millis(200);

/// Where the daemon writes its state. Mirrors `utils/config.py`:
/// `~/.agent-windows/daemon.pid` (line 1 = pid, line 2 = start time).
pub fn pid_file_path() -> PathBuf {
    let mut p = home_dir();
    p.push(".agent-windows");
    p.push("daemon.pid");
    p
}

/// `~/.agent-windows/daemon.version` — the daemon writes its package version
/// here so the launcher can compare against its own (version-mismatch check).
pub fn version_file_path() -> PathBuf {
    let mut p = home_dir();
    p.push(".agent-windows");
    p.push("daemon.version");
    p
}

/// Equivalent of `ensure_daemon_running(debug)`:
/// - pipe exists → return immediately
/// - pipe gone but daemon alive → return (IPC connect-retry handles the gap)
/// - pipe gone and daemon dead → spawn, wait for pipe (fail fast if it exits)
pub fn ensure_daemon_running(python_exe: &str, debug: bool) -> Result<(), String> {
    if daemon_pipe_exists() {
        return Ok(());
    }

    if daemon_process_alive() {
        // Live daemon cycling its pipe — don't spawn a rival.
        return Ok(());
    }

    // Genuinely gone — spawn a fresh one.
    let (handle, _pid) = spawn_daemon(python_exe, debug)
        .map_err(|e| format!("Daemon not running, failed to start: {}", e))?;

    // Retry until the pipe appears, or the spawned process exits (fail fast —
    // a healthy daemon blocks in its main loop after creating the pipe and
    // never exits; an early exit means a config/dependency error).
    for _ in 0..MAX_CONNECT_RETRIES {
        std::thread::sleep(RETRY_INTERVAL);
        if daemon_pipe_exists() {
            unsafe { windows_sys::Win32::Foundation::CloseHandle(handle) };
            return Ok(());
        }
        if let Some(code) = poll_process(handle) {
            unsafe { windows_sys::Win32::Foundation::CloseHandle(handle) };
            return Err(format!(
                "Daemon process exited during startup (code {}) — likely a configuration or \
                 environment error. Check ~/.agent-windows/logs/daemon.log, or run \
                 `{} -m agent_windows --daemon --debug` for console output.",
                code, python_exe
            ));
        }
    }

    unsafe { windows_sys::Win32::Foundation::CloseHandle(handle) };
    Err(format!(
        "Daemon did not become ready after {:.1}s",
        MAX_CONNECT_RETRIES as f64 * RETRY_INTERVAL.as_secs_f64()
    ))
}

/// Read the PID file and check liveness. Mirrors `_daemon_process_alive`.
fn daemon_process_alive() -> bool {
    let Some(pid) = read_pid_from_file() else {
        return false;
    };
    is_process_alive(pid)
}

/// Parse line 1 of the PID file as the daemon PID.
fn read_pid_from_file() -> Option<u32> {
    let content = std::fs::read_to_string(pid_file_path()).ok()?;
    let first_line = content.lines().next()?;
    first_line.trim().parse::<u32>().ok()
}

/// Resolve `~`. On Windows that's `%USERPROFILE%`.
pub fn home_dir() -> PathBuf {
    std::env::var_os("USERPROFILE")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

/// Block until the daemon pipe is ready, polling up to `timeout`. Used by the
/// `daemon start` subcommand.
pub fn wait_daemon_ready(timeout: Duration) -> bool {
    wait_for_pipe(timeout)
}
