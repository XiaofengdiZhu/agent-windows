//! Thin Win32 FFI wrappers used by the IPC client and the daemon-lifecycle
//! logic. Keeps raw `unsafe` + `windows_sys` calls in one place.
//!
//! Mirrors the overlapped-I/O pattern from `agent_windows.ipc.pipe`:
//! write the request, then read newline-delimited chunks until the full
//! response arrives — bounded by a timeout so a hung daemon can't block us
//! forever (H-02 equivalent).

use std::time::Duration;

use windows_sys::Win32::{
    Foundation::{
        CloseHandle, GetLastError, ERROR_BROKEN_PIPE, ERROR_IO_PENDING, ERROR_NO_DATA,
        FALSE, HANDLE, WAIT_TIMEOUT,
    },
    Storage::FileSystem::{ReadFile, WriteFile},
    System::{
        IO::{CancelIo, GetOverlappedResult, OVERLAPPED},
        Threading::{CreateEventW, WaitForSingleObject},
    },
};

use crate::ipc::widen;

/// Max bytes we'll buffer for a single response (matches Python's 10 MB guard).
const MAX_MESSAGE_SIZE: usize = 10 * 1024 * 1024;
/// Read/write timeout in ms (matches config `ipc.read_timeout_seconds` = 150s).
const READ_TIMEOUT_MS: u32 = 150_000;

/// Overlapped write of the full payload, completing within the read timeout.
/// Mirrors `_overlapped_write_all`.
pub fn write_pipe(handle: HANDLE, data: &[u8]) -> Result<(), String> {
    let event = new_event()?;
    let mut overlapped: OVERLAPPED = unsafe { std::mem::zeroed() };
    overlapped.hEvent = event;
    let ovl_ptr = &mut overlapped as *mut _;

    let result = unsafe {
        let mut written: u32 = 0;
        let ok = WriteFile(
            handle,
            data.as_ptr(),
            data.len() as u32,
            &mut written,
            ovl_ptr,
        );
        complete_overlapped(handle, event, ovl_ptr, ok != FALSE, READ_TIMEOUT_MS, written as usize)
    };
    unsafe { CloseHandle(event) };
    let transferred = result?;
    if transferred < data.len() {
        return Err(format!("short write: {} of {} bytes", transferred, data.len()));
    }
    Ok(())
}

/// Read newline-delimited chunks until `\n` or EOF. Mirrors the read loop in
/// `send_to_daemon`. Returns the raw bytes (caller decodes + trims).
pub fn read_pipe_until_newline(handle: HANDLE) -> Result<Vec<u8>, String> {
    let mut out = Vec::with_capacity(4096);
    loop {
        let chunk = overlapped_read_chunk(handle)?;
        if chunk.is_empty() {
            break; // EOF
        }
        out.extend_from_slice(&chunk);
        if out.len() > MAX_MESSAGE_SIZE {
            return Err("Message exceeds maximum size".into());
        }
        if out.contains(&b'\n') {
            break;
        }
    }
    Ok(out)
}

/// One overlapped ReadFile of up to 64 KiB, bounded by the read timeout.
/// Mirrors `_overlapped_read_chunk`. Returns the bytes actually read.
fn overlapped_read_chunk(handle: HANDLE) -> Result<Vec<u8>, String> {
    let event = new_event()?;
    let mut overlapped: OVERLAPPED = unsafe { std::mem::zeroed() };
    overlapped.hEvent = event;
    let ovl_ptr = &mut overlapped as *mut _;

    let mut buf = [0u8; 65536];
    let result = unsafe {
        let mut read: u32 = 0;
        let ok = ReadFile(
            handle,
            buf.as_mut_ptr(),
            buf.len() as u32,
            &mut read,
            ovl_ptr,
        );
        complete_overlapped(handle, event, ovl_ptr, ok != FALSE, READ_TIMEOUT_MS, read as usize)
    };
    unsafe { CloseHandle(event) };
    let n = result?;
    Ok(buf[..n].to_vec())
}

/// Create a manual-reset event for overlapped I/O signalling.
fn new_event() -> Result<HANDLE, String> {
    let event = unsafe { CreateEventW(std::ptr::null(), 1, 0, std::ptr::null()) };
    if event.is_null() {
        Err(format!("CreateEventW failed (winerror {})", unsafe { GetLastError() }))
    } else {
        Ok(event)
    }
}

/// Resolve an overlapped I/O result: if pending, wait on the event; if timed
/// out, error; otherwise return the byte count transferred.
///
/// `overlapped` must be the *same* pointer passed to ReadFile/WriteFile — the
/// async operation writes the result into that struct. Passing it by value
/// (a copy) would lose the kernel's result (observed as ERROR_IO_DEVICE).
fn complete_overlapped(
    handle: HANDLE,
    event: HANDLE,
    overlapped_ptr: *mut OVERLAPPED,
    ok: bool,
    timeout_ms: u32,
    immediate_bytes: usize,
) -> Result<usize, String> {
    // Capture LastError immediately — must be read right after the API call,
    // before anything else clobbers it.
    let err = unsafe { GetLastError() };
    if ok || err == ERROR_IO_PENDING {
        if err == ERROR_IO_PENDING {
            let rc = unsafe { WaitForSingleObject(event, timeout_ms) };
            if rc == WAIT_TIMEOUT {
                // Cancel the in-flight async op before returning — otherwise it
                // keeps referencing the handle/overlapped after we close them.
                // (Mirrors pipe.py's CancelIo on timeout.)
                unsafe { CancelIo(handle) };
                return Err(format!("Timed out waiting {}ms for daemon", timeout_ms));
            }
            let mut transferred: u32 = 0;
            if unsafe { GetOverlappedResult(handle, overlapped_ptr, &mut transferred, 0) } == 0 {
                let e = unsafe { GetLastError() };
                return Err(pipe_error(e));
            }
            Ok(transferred as usize)
        } else {
            // Completed synchronously.
            Ok(immediate_bytes)
        }
    } else {
        Err(pipe_error(err))
    }
}

/// Map pipe-related Win32 errors to the same ConnectionError strings the
/// Python client raises.
fn pipe_error(err: u32) -> String {
    match err {
        ERROR_BROKEN_PIPE | ERROR_NO_DATA => {
            "Daemon closed the connection without responding".into()
        }
        _ => format!("pipe I/O error (winerror {})", err),
    }
}

/// `WaitNamedPipeW`: true if the pipe is available within the timeout.
/// Mirrors `pipe_exists`'s underlying call (Python uses 100ms).
pub fn wait_named_pipe(pipe_name: &str, timeout: Duration) -> bool {
    use windows_sys::Win32::System::Pipes::WaitNamedPipeW;
    let name_w = widen(pipe_name);
    let ms = timeout.as_millis().min(u32::MAX as u128) as u32;
    unsafe { WaitNamedPipeW(name_w.as_ptr(), ms) != 0 }
}

/// Convenience: does the daemon pipe currently exist?
pub fn pipe_exists(pipe_name: &str) -> bool {
    wait_named_pipe(pipe_name, Duration::from_millis(100))
}

// ── Process / daemon lifecycle ────────────────────────────────────────────

use windows_sys::Win32::{
    System::Threading::{
        CreateProcessW, GetExitCodeProcess, OpenProcess, PROCESS_INFORMATION,
        PROCESS_QUERY_LIMITED_INFORMATION, STARTUPINFOW,
    },
};

/// STILL_ACTIVE sentinel from Win32 (GetExitCodeProcess).
const STILL_ACTIVE: u32 = 259;

/// True if a process with the given PID is still running.
/// Mirrors `is_process_alive` in lifecycle.py.
pub fn is_process_alive(pid: u32) -> bool {
    unsafe {
        let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if h.is_null() {
            return false;
        }
        let mut code: u32 = 0;
        let ok = GetExitCodeProcess(h, &mut code);
        CloseHandle(h);
        ok != 0 && code == STILL_ACTIVE
    }
}

/// Spawn the daemon as a detached subprocess: `<python> -m agent_windows --daemon`.
/// Returns the process handle + pid (caller owns the handle).
///
/// Mirrors `_start_daemon_process`:
/// - CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
/// - stdio → DEVNULL
pub fn spawn_daemon(python_exe: &str, debug: bool) -> Result<(HANDLE, u32), String> {
    let mut args = format!("-m agent_windows --daemon");
    if debug {
        args.push_str(" --debug");
    }
    // CreateProcessW wants a mutable command line; the app path is quoted.
    let cmdline = format!("\"{}\" {}", python_exe, args);
    let mut cmdline_w: Vec<u16> = cmdline.encode_utf16().chain(std::iter::once(0)).collect();

    // DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    const DETACHED_PROCESS: u32 = 0x0000_0008;
    const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;

    // Redirect stdio to NUL so the detached daemon has no console handles.
    let mut startup: STARTUPINFOW = unsafe { std::mem::zeroed() };
    startup.cb = std::mem::size_of::<STARTUPINFOW>() as u32;
    // hStdInput/Output/Error point at NUL — set via dwFlags.
    const STARTF_USESTDHANDLES: u32 = 0x0000_0100;
    startup.dwFlags = STARTF_USESTDHANDLES;

    let nul_in = open_nul(true);  // read
    let nul_out = open_nul(false); // write
    if !nul_in.is_null() {
        startup.hStdInput = nul_in;
    }
    if !nul_out.is_null() {
        startup.hStdOutput = nul_out;
        startup.hStdError = nul_out;
    }

    let mut info: PROCESS_INFORMATION = unsafe { std::mem::zeroed() };

    let ok = unsafe {
        CreateProcessW(
            std::ptr::null(),
            cmdline_w.as_mut_ptr(),
            std::ptr::null(),
            std::ptr::null(),
            0,
            DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
            std::ptr::null(),
            std::ptr::null(),
            &mut startup,
            &mut info,
        )
    };

    if !nul_in.is_null() {
        unsafe { CloseHandle(nul_in) };
    }
    if !nul_out.is_null() {
        unsafe { CloseHandle(nul_out) };
    }

    if ok == 0 {
        return Err(format!(
            "Failed to spawn daemon (winerror {})",
            unsafe { GetLastError() }
        ));
    }

    // We don't need the thread handle or the primary thread; close it. Keep
    // the process handle so we can poll for early exit (M-31 equivalent).
    unsafe { CloseHandle(info.hThread) };
    Ok((info.hProcess, info.dwProcessId))
}

/// Poll whether a spawned daemon process has exited (and with what code).
/// `Some(code)` = exited; `None` = still starting.
pub fn poll_process(handle: HANDLE) -> Option<u32> {
    unsafe {
        let mut code: u32 = 0;
        if GetExitCodeProcess(handle, &mut code) != 0 && code != STILL_ACTIVE {
            Some(code)
        } else {
            None
        }
    }
}

/// Open a handle to `NUL` for stdio redirection. `read=true` → read access.
fn open_nul(read: bool) -> HANDLE {
    use windows_sys::Win32::Storage::FileSystem::{
        CreateFileW, FILE_ATTRIBUTE_NORMAL, FILE_SHARE_READ, FILE_SHARE_WRITE,
    };
    let access = if read { 0x8000_0000 } else { 0x4000_0000 }; // GENERIC_READ / WRITE
    let nul_w = widen("NUL");
    unsafe {
        CreateFileW(
            nul_w.as_ptr(),
            access,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            std::ptr::null(),
            3, // OPEN_EXISTING
            FILE_ATTRIBUTE_NORMAL,
            std::ptr::null_mut(),
        )
    }
}
