//! Named-pipe IPC client. Mirrors `agent_windows.ipc.pipe.send_to_daemon`:
//! connect to `\\.\pipe\agent-windows` (overlapped), send a newline-delimited
//! JSON-RPC request, read the newline-delimited response.
//!
//! The connect retries through `ERROR_FILE_NOT_FOUND` (the transient gap while
//! the daemon tears down and recreates its single-instance pipe between
//! connections) — the same race the Python client was fixed for.

use std::time::{Duration, Instant};

use windows_sys::Win32::{
    Foundation::{
        CloseHandle, GetLastError, GENERIC_READ, GENERIC_WRITE, HANDLE,
        INVALID_HANDLE_VALUE,
    },
    Storage::FileSystem::{CreateFileW, FILE_FLAG_OVERLAPPED, OPEN_EXISTING},
};

use crate::win32::{pipe_exists, wait_named_pipe};

/// Win32 error: file not found (the pipe doesn't exist right now).
const ERROR_FILE_NOT_FOUND: u32 = 2;

pub const PIPE_NAME: &str = r"\\.\pipe\agent-windows";
/// Connect retry: 50 × 10ms = up to ~0.5s, matching the Python client.
const CONNECT_RETRIES: u32 = 50;
const CONNECT_BACKOFF: Duration = Duration::from_millis(10);
/// Transient-failure retry: the daemon cycles its single-instance pipe
/// (disconnect → create → ConnectNamedPipe) between connections, so a request
/// that connects into the gap — or lands during a restart — can see a broken
/// pipe mid-transact. Retry the whole connect+send+recv a few times before
/// giving up. Mirrors the `ConnectionError` semantics the Python client
/// already tolerates.
const TRANSIENT_RETRIES: u32 = 3;
const TRANSIENT_BACKOFF: Duration = Duration::from_millis(50);

/// Connect to the daemon pipe, send `request` (newline appended), return the
/// raw response string. Retries the connect on `ERROR_FILE_NOT_FOUND`, and
/// retries the *whole* connect+transact on transient mid-request failures
/// (broken pipe / connection reset) up to `TRANSIENT_RETRIES` times.
///
/// Errors surface as human-readable strings (the caller decides exit code).
pub fn send_to_daemon(request: &str) -> Result<String, String> {
    let mut last_err = String::from("could not connect to daemon pipe");
    for attempt in 0..TRANSIENT_RETRIES {
        match connect_and_transact(request) {
            Ok(resp) => return Ok(resp),
            Err(e) if is_transient(&e) && attempt + 1 < TRANSIENT_RETRIES => {
                // Broken pipe / connection reset — the daemon likely cycled its
                // pipe or is mid-restart. Back off and retry the whole op.
                last_err = e;
                std::thread::sleep(TRANSIENT_BACKOFF);
                continue;
            }
            Err(e) => return Err(e),
        }
    }
    Err(last_err)
}

/// One full connect + send + recv attempt. Returns a string that callers can
/// classify via `is_transient`.
fn connect_and_transact(request: &str) -> Result<String, String> {
    let pipe_name_w = widen(PIPE_NAME);

    for attempt in 0..CONNECT_RETRIES {
        let handle = unsafe {
            CreateFileW(
                pipe_name_w.as_ptr(),
                GENERIC_READ | GENERIC_WRITE,
                0,
                std::ptr::null(),
                OPEN_EXISTING,
                FILE_FLAG_OVERLAPPED,
                std::ptr::null_mut(),
            )
        };

        if handle == INVALID_HANDLE_VALUE {
            let err = unsafe { GetLastError() };
            if err == ERROR_FILE_NOT_FOUND && attempt + 1 < CONNECT_RETRIES {
                // Pipe is in the recreate gap — back off and retry.
                std::thread::sleep(CONNECT_BACKOFF);
                continue;
            }
            // ERROR_FILE_NOT_FOUND at the end of the retry window is also
            // transient (the daemon may be starting); anything else is not.
            return Err(format!("Cannot connect to daemon pipe (winerror {})", err));
        }

        // Got a handle — send/receive, then close.
        let result = transact(handle, request);
        unsafe { CloseHandle(handle) };
        return result;
    }

    Err("daemon pipe not available (winerror 2)".into())
}

/// Classify an IPC error string as transient (retryable) or permanent.
///
/// Transient conditions: the pipe not existing yet (daemon starting), the
/// daemon closing the connection mid-request (broken pipe / no data — happens
/// when our request lands in the recreate gap), or a pipe-not-connected reset.
/// These are expected under the single-instance pipe's connect cycling and
/// warrant a retry rather than a hard failure. Built on stable substrings that
/// `connect_and_transact` / `win32::pipe_error` emit.
fn is_transient(err: &str) -> bool {
    let e = err.to_ascii_lowercase();
    e.contains("winerror 2")
        || e.contains("winerror 109")
        || e.contains("winerror 232")
        || e.contains("winerror 233")
        || e.contains("closed the connection")
}

#[cfg(test)]
mod tests {
    use super::is_transient;

    #[test]
    fn transient_winerror_codes_retry() {
        // ERROR_FILE_NOT_FOUND (2): pipe not up yet (daemon starting).
        assert!(is_transient("Cannot connect to daemon pipe (winerror 2)"));
        assert!(is_transient("daemon pipe not available (winerror 2)"));
        // ERROR_BROKEN_PIPE (109): daemon closed the pipe mid-request.
        assert!(is_transient(
            "Daemon closed the connection without responding (winerror 109)"
        ));
        // ERROR_NO_DATA (232).
        assert!(is_transient(
            "Daemon closed the connection without responding (winerror 232)"
        ));
        // ERROR_PIPE_NOT_CONNECTED (233).
        assert!(is_transient("pipe I/O error (winerror 233)"));
    }

    #[test]
    fn non_transient_errors_do_not_retry() {
        // A genuine I/O failure (e.g. ERROR_INVALID_HANDLE 6) is not retryable.
        assert!(!is_transient("pipe I/O error (winerror 6)"));
        // Timeout is a real failure, not a pipe-cycle blip.
        assert!(!is_transient(
            "Timed out waiting 150000ms for daemon"
        ));
    }

    #[test]
    fn classifier_is_case_insensitive() {
        // Defensive: error strings are built in-house and lower-ASCII, but the
        // classifier lowercases before matching so a future call site that
        // passes mixed case still classifies correctly.
        assert!(is_transient("WINERROR 109"));
    }
}

/// Low-level send + recv on a connected overlapped pipe handle.
fn transact(handle: HANDLE, request: &str) -> Result<String, String> {
    let payload = format!("{}\n", request);
    let bytes = payload.as_bytes();

    crate::win32::write_pipe(handle, bytes)?;
    let resp = crate::win32::read_pipe_until_newline(handle)?;
    // The response is UTF-8; strip the trailing newline.
    Ok(String::from_utf8_lossy(&resp)
        .trim_end_matches('\n')
        .to_string())
}

/// True if the named pipe currently exists (daemon is likely running and
/// accepting connections). Mirrors `pipe_exists` (WaitNamedPipeW).
pub fn daemon_pipe_exists() -> bool {
    pipe_exists(PIPE_NAME)
}

/// Fire-and-forget: connect, write the request, close — never read the
/// response. Used by `daemon stop`/kill paths where we don't want to block on
/// the daemon's (possibly delayed) reply. The daemon processes the request
/// when its main loop is idle regardless of whether we read the response.
///
/// Returns Ok once the request bytes are written; the actual effect happens
/// asynchronously. Best-effort: returns Err if we can't connect/write within
/// a short window (caller shouldn't depend on the result).
pub fn send_no_wait(request: &str) -> Result<(), String> {
    let pipe_name_w = widen(PIPE_NAME);
    // Fewer retries than a normal request — this is best-effort, and if the
    // pipe is mid-recreate the caller's PID poll / force-kill handles it.
    for attempt in 0..20 {
        let handle = unsafe {
            CreateFileW(
                pipe_name_w.as_ptr(),
                GENERIC_READ | GENERIC_WRITE,
                0,
                std::ptr::null(),
                OPEN_EXISTING,
                FILE_FLAG_OVERLAPPED,
                std::ptr::null_mut(),
            )
        };
        if handle == INVALID_HANDLE_VALUE {
            let err = unsafe { GetLastError() };
            if err == ERROR_FILE_NOT_FOUND && attempt + 1 < 20 {
                std::thread::sleep(CONNECT_BACKOFF);
                continue;
            }
            return Err(format!("Cannot connect to daemon pipe (winerror {})", err));
        }
        // Write and close immediately — don't wait for/read the response.
        let result = crate::win32::write_pipe(handle, format!("{}\n", request).as_bytes());
        unsafe { CloseHandle(handle) };
        return result;
    }
    Err("daemon pipe not available".into())
}

/// Wait for the pipe to appear, polling up to `timeout`. Used after spawning a
/// fresh daemon while it initializes.
pub fn wait_for_pipe(timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if wait_named_pipe(PIPE_NAME, Duration::from_millis(100)) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    false
}

/// Encode a `&str` into a NUL-terminated UTF-16 Vec for the W-suffix Win32 APIs.
pub(crate) fn widen(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0u16)).collect()
}
