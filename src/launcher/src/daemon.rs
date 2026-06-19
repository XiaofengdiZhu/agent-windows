//! Daemon lifecycle: read the PID file, decide whether to spawn a fresh
//! daemon, and wait for the pipe to come up.
//!
//! Mirrors `agent_windows.ipc.client.ensure_daemon_running` *including the
//! race fix*: if the pipe is momentarily gone but the daemon process is alive,
//! don't spawn a rival — let the IPC connect-retry handle the gap.

use std::path::PathBuf;
use std::time::{Duration, Instant};

use crate::ipc::{daemon_pipe_exists, send_no_wait, wait_for_pipe};
use crate::win32::{is_process_alive, poll_process, read_stderr_to_string, spawn_daemon, truncate_for_error_msg};
use windows_sys::Win32::Foundation::HANDLE;

/// Max retries × interval while waiting for a freshly-spawned daemon to create
/// its pipe (matches Python's MAX_CONNECT_RETRIES × RETRY_INTERVAL_S).
const MAX_CONNECT_RETRIES: u32 = 25;
const RETRY_INTERVAL: Duration = Duration::from_millis(200);
/// How long to wait for a graceful shutdown RPC to take effect before forcing.
const GRACEFUL_SHUTDOWN_WAIT: Duration = Duration::from_secs(2);
/// Poll interval while waiting for a killed daemon to actually exit.
const SHUTDOWN_POLL: Duration = Duration::from_millis(100);

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
/// - pipe up + version matches → return immediately
/// - pipe up but version mismatches → kill the stale daemon, fall through
/// - pipe gone but daemon alive + version matches → return (IPC connect-retry
///   handles the gap while the daemon recreates its single-instance pipe)
/// - pipe gone and daemon dead (or version-mismatched) → spawn, wait for pipe
///
/// The version check prevents silent reuse of a daemon from a different CLI
/// build after an upgrade — mirrors `agent-browser`'s `daemon_version_matches`
/// + `kill_stale_daemon`. A missing version file also counts as a mismatch:
/// reusing an unversioned daemon is exactly the stale-leftover bug the check
/// exists to prevent.
pub fn ensure_daemon_running(python_exe: &str, debug: bool) -> Result<(), String> {
    // If the daemon process is alive, decide on its version *before* relying on
    // the pipe — a version mismatch needs to kill+respawn regardless of whether
    // the pipe is momentarily up or in its recreate gap.
    if daemon_process_alive() {
        if daemon_version_matches() {
            // Healthy and current — let the IPC connect-retry absorb any
            // transient pipe gap rather than spawning a rival.
            return Ok(());
        }
        // Version mismatch (or unversioned): kill it and fall through to spawn
        // a fresh daemon. This is the auto-restart the `daemon status` warning
        // promises.
        let _ = kill_daemon();
    } else {
        // Daemon process is dead but may have left a stale PID/version file
        // (it crashed or was killed externally). Remove them so the fresh
        // spawn below starts clean and `daemon status` never reports a dead
        // PID. Mirrors `cleanup_stale_daemon` in lifecycle.py.
        cleanup_stale_files();
    }

    // Genuinely gone — spawn a fresh one. `stderr_read` is the read end of an
    // anonymous pipe wired to the daemon's stderr, so if it exits early we can
    // surface the real traceback (import failure, etc.) instead of a generic
    // "check the logs" message (#9).
    let (handle, _pid, stderr_read) = spawn_daemon(python_exe, debug)
        .map_err(|e| format!("Daemon not running, failed to start: {}", e))?;

    // Retry until the pipe appears, or the spawned process exits (fail fast —
    // a healthy daemon blocks in its main loop after creating the pipe and
    // never exits; an early exit means a config/dependency error).
    for _ in 0..MAX_CONNECT_RETRIES {
        std::thread::sleep(RETRY_INTERVAL);
        if daemon_pipe_exists() {
            close_handle(handle);
            close_stderr(stderr_read);
            return Ok(());
        }
        if let Some(code) = poll_process(handle) {
            let stderr = stderr_read
                .map(read_stderr_to_string)
                .unwrap_or_default();
            close_handle(handle);
            close_stderr(stderr_read);
            return Err(format_daemon_startup_error(code, &stderr, python_exe));
        }
    }

    close_handle(handle);
    close_stderr(stderr_read);
    Err(format!(
        "Daemon did not become ready after {:.1}s",
        MAX_CONNECT_RETRIES as f64 * RETRY_INTERVAL.as_secs_f64()
    ))
}

/// Build the early-exit error message. If the daemon wrote anything to stderr
/// (e.g. a Python traceback from a failed import), embed a trimmed, char-safe
/// excerpt; otherwise fall back to pointing at the log file / --debug.
/// Mirrors agent-browser's `connection.rs:705-744` surfacing of stderr.
fn format_daemon_startup_error(code: u32, stderr: &str, python_exe: &str) -> String {
    let stderr_trimmed = stderr.trim();
    if stderr_trimmed.is_empty() {
        format!(
            "Daemon process exited during startup (code {}) — likely a configuration or \
             environment error. Check ~/.agent-windows/logs/daemon.log, or run \
             `{} -m agent_windows --daemon --debug` for console output.",
            code, python_exe
        )
    } else {
        let snippet = truncate_for_error_msg(stderr_trimmed, 500);
        format!(
            "Daemon process exited during startup (code {}):\n{}",
            code, snippet
        )
    }
}

/// Close a process handle (best-effort; non-null only).
fn close_handle(handle: HANDLE) {
    if !handle.is_null() {
        unsafe { windows_sys::Win32::Foundation::CloseHandle(handle) };
    }
}

/// Close an optional stderr-read handle (best-effort).
fn close_stderr(stderr: Option<HANDLE>) {
    if let Some(h) = stderr {
        close_handle(h);
    }
}

/// True if the running daemon's self-reported version matches this binary.
///
/// Mirrors agent-browser's `daemon_version_matches`: a missing version file is
/// treated as a mismatch (don't silently reuse an unversioned stale daemon).
/// To avoid killing a daemon that is merely mid-startup (PID file written,
/// version file not yet), re-check a few times before declaring mismatch —
/// the daemon writes both files within milliseconds of each other.
fn daemon_version_matches() -> bool {
    // Fast path: file present.
    if let Ok(v) = std::fs::read_to_string(version_file_path()) {
        return v.trim() == env!("CARGO_PKG_VERSION");
    }
    // File missing. If a daemon process is alive, it may still be starting
    // (older daemons write PID before version). Re-check briefly before
    // deciding this is a stale/mismatched daemon worth killing.
    for _ in 0..10 {
        if !daemon_process_alive() {
            break; // No live process → genuinely stale, mismatch is correct.
        }
        if let Ok(v) = std::fs::read_to_string(version_file_path()) {
            return v.trim() == env!("CARGO_PKG_VERSION");
        }
        std::thread::sleep(SHUTDOWN_POLL);
    }
    false
}

/// Stop the running daemon and remove its PID/version files.
///
/// Strategy: fire-and-forget the `shutdown` JSON-RPC method (we don't read the
/// response — the daemon processes it when its main loop is idle, and waiting
/// for the reply could block up to 150s if the daemon is busy with another
/// client). We then poll the PID until it's gone. If the graceful path stalls,
/// fall back to `taskkill /F` so we never hang the caller. Stale files are
/// removed either way so the next spawn starts clean.
fn kill_daemon() -> Result<(), String> {
    // Fire-and-forget: write shutdown, don't block on the response.
    let req = "{\"jsonrpc\":\"2.0\",\"method\":\"shutdown\",\"id\":1}";
    let _ = send_no_wait(req);

    let deadline = Instant::now() + GRACEFUL_SHUTDOWN_WAIT;
    while Instant::now() < deadline {
        if !daemon_process_alive() {
            break;
        }
        std::thread::sleep(SHUTDOWN_POLL);
    }

    // Force-kill fallback — but only if the *current* PID file still names a
    // live process. Re-reading avoids a PID-recycle hazard: if the daemon
    // exited and cleaned up its PID file, or the OS recycled the PID to an
    // unrelated process, we must not taskkill the wrong process.
    if let Some(pid) = read_pid_from_file() {
        if pid_matches_live_daemon(pid) {
            force_kill(pid);
        }
    }

    cleanup_stale_files();
    Ok(())
}

/// True if `pid` is still the running daemon (the PID file still names it AND
/// it's alive). Guards the force-kill path against PID recycling: a recycled
/// PID would pass `is_process_alive` but wouldn't be named by the PID file we
/// just (re-)read, or the file would be gone (clean shutdown).
fn pid_matches_live_daemon(pid: u32) -> bool {
    match read_pid_from_file() {
        Some(current) if current == pid => is_process_alive(pid),
        _ => false,
    }
}

/// `taskkill /F /PID <pid>` — the hard-kill fallback when graceful shutdown
/// stalls. Output is discarded; failure is non-fatal (the caller proceeds to
/// spawn, which will itself fail loudly if the old daemon still holds the pipe).
fn force_kill(pid: u32) {
    let _ = std::process::Command::new("taskkill")
        .args(["/PID", &pid.to_string(), "/F"])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status();
    // Give the OS a moment to reap it before the caller spawns a replacement
    // that wants to bind the single-instance pipe.
    std::thread::sleep(Duration::from_millis(200));
}

/// Remove leftover PID + version files (the daemon writes both on startup).
/// Mirrors `cleanup_stale_daemon` in lifecycle.py. Safe to call when the files
/// don't exist.
fn cleanup_stale_files() {
    let _ = std::fs::remove_file(pid_file_path());
    let _ = std::fs::remove_file(version_file_path());
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

#[cfg(test)]
mod tests {
    use super::format_daemon_startup_error;

    #[test]
    fn startup_error_with_stderr_embeds_snippet() {
        // A Python traceback on stderr should be surfaced verbatim (trimmed),
        // so the user sees the real failure instead of "check the logs".
        let stderr = "Traceback (most recent call last):\n  ModuleNotFoundError: No module named 'pywintypes'\n\n";
        let msg = format_daemon_startup_error(1, stderr, "python");
        assert!(msg.starts_with("Daemon process exited during startup (code 1):\n"));
        // Trimmed: trailing blank line dropped.
        assert!(msg.contains("ModuleNotFoundError: No module named 'pywintypes'"));
        assert!(!msg.ends_with("\n\n"));
        // Must not show the generic fallback when stderr is present.
        assert!(!msg.contains("Check ~/.agent-windows/logs/daemon.log"));
    }

    #[test]
    fn startup_error_without_stderr_falls_back_to_log_hint() {
        let msg = format_daemon_startup_error(1, "", "python");
        assert!(msg.contains("code 1"));
        assert!(msg.contains("daemon.log"));
        assert!(msg.contains("--debug"));
    }

    #[test]
    fn startup_error_whitespace_only_stderr_falls_back() {
        // Whitespace-only stderr is effectively empty.
        let msg = format_daemon_startup_error(2, "   \n  \n", "python");
        assert!(msg.contains("daemon.log"));
        assert!(!msg.contains(":\n"));
    }

    #[test]
    fn startup_error_truncates_long_stderr() {
        let stderr = "E".repeat(2000);
        let msg = format_daemon_startup_error(1, &stderr, "python");
        // Body is "code 1):\n" + ≤500 chars.
        let body = msg.strip_prefix("Daemon process exited during startup (code 1):\n").unwrap();
        assert!(body.len() <= 500);
    }
}
