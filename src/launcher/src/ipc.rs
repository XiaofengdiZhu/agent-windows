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

/// Connect to the daemon pipe, send `request` (newline appended), return the
/// raw response string. Retries the connect on `ERROR_FILE_NOT_FOUND`.
///
/// Errors surface as human-readable strings (the caller decides exit code).
pub fn send_to_daemon(request: &str) -> Result<String, String> {
    let pipe_name_w = widen(PIPE_NAME);
    let mut last_err = String::from("could not connect to daemon pipe");

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
                last_err = format!("daemon pipe not available (winerror {})", err);
                continue;
            }
            return Err(format!("Cannot connect to daemon pipe (winerror {})", err));
        }

        // Got a handle — send/receive, then close.
        let result = transact(handle, request);
        unsafe { CloseHandle(handle) };
        return result;
    }

    Err(last_err)
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
