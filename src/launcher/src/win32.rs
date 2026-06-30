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
        CloseHandle, FILETIME, GetLastError, ERROR_BROKEN_PIPE, ERROR_HANDLE_EOF, ERROR_IO_PENDING,
        ERROR_NO_DATA, ERROR_PIPE_NOT_CONNECTED, FALSE, HANDLE, WAIT_OBJECT_0, WAIT_TIMEOUT,
    },
    Security::SECURITY_ATTRIBUTES,
    Storage::FileSystem::{ReadFile, WriteFile},
    System::{
        IO::{CancelIo, GetOverlappedResult, OVERLAPPED},
        Pipes::CreatePipe,
        Threading::{CreateEventW, WaitForSingleObject},
    },
};

use crate::ipc::widen;

/// Max bytes we'll buffer for a single response (matches Python's 10 MB guard).
const MAX_MESSAGE_SIZE: usize = 10 * 1024 * 1024;
/// Read/write timeout in ms (matches config `ipc.read_timeout_seconds` = 150s).
/// `pub(crate)` so the IPC layer can use it as the default per-op timeout.
pub(crate) const READ_TIMEOUT_MS: u32 = 150_000;

/// Overlapped write of the full payload, completing within `timeout_ms`.
/// Mirrors `_overlapped_write_all`. The caller supplies the per-write timeout:
/// normal requests use `READ_TIMEOUT_MS`; fire-and-forget paths (`send_no_wait`)
/// pass a short bound so a wedged daemon can't block the kill path for minutes.
pub fn write_pipe(handle: HANDLE, data: &[u8], timeout_ms: u32) -> Result<(), String> {
    // WriteFile takes a u32 length; guard the cast so a >4 GiB payload (not
    // reachable today, but defensive against a future caller) fails cleanly
    // instead of silently truncating to a short write. Done BEFORE allocating
    // the event handle so the error path doesn't leak it.
    let len = u32::try_from(data.len()).map_err(|_| "write payload too large")?;

    let event = new_event()?;
    let mut overlapped: OVERLAPPED = unsafe { std::mem::zeroed() };
    overlapped.hEvent = event;
    let ovl_ptr = &mut overlapped as *mut _;

    let result = unsafe {
        let mut written: u32 = 0;
        let ok = WriteFile(
            handle,
            data.as_ptr(),
            len,
            &mut written,
            ovl_ptr,
        );
        complete_overlapped(handle, event, ovl_ptr, ok != FALSE, timeout_ms, written as usize)
    };
    unsafe { CloseHandle(event) };
    let transferred = result?;
    if transferred < data.len() {
        return Err(format!("short write: {} of {} bytes", transferred, data.len()));
    }
    Ok(())
}

/// Read newline-delimited chunks until `\n` or EOF. Mirrors the read loop in
/// `send_to_daemon`. Each chunk read is bounded by `timeout_ms`. Returns the
/// raw bytes (caller decodes + trims).
pub fn read_pipe_until_newline(handle: HANDLE, timeout_ms: u32) -> Result<Vec<u8>, String> {
    let mut out = Vec::with_capacity(4096);
    // Track how far we've already scanned for `\n`, so each chunk is scanned
    // once rather than re-scanning the whole accumulated buffer (which would be
    // O(n²) up to the 10 MiB cap). `scan_from` is the byte offset in `out` at
    // which the next scan should begin.
    let mut scan_from = 0usize;
    loop {
        let chunk = overlapped_read_chunk(handle, timeout_ms)?;
        if chunk.is_empty() {
            break; // EOF
        }
        out.extend_from_slice(&chunk);
        if out.len() > MAX_MESSAGE_SIZE {
            return Err("Message exceeds maximum size".into());
        }
        if out[scan_from..].contains(&b'\n') {
            break;
        }
        scan_from = out.len().saturating_sub(1);
    }
    Ok(out)
}

/// One overlapped ReadFile of up to 64 KiB, bounded by `timeout_ms`.
/// Mirrors `_overlapped_read_chunk`. Returns the bytes actually read.
fn overlapped_read_chunk(handle: HANDLE, timeout_ms: u32) -> Result<Vec<u8>, String> {
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
        complete_overlapped(handle, event, ovl_ptr, ok != FALSE, timeout_ms, read as usize)
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
                // CancelIo only *queues* a cancellation — the kernel still
                // writes the completion status into `overlapped` (and signals
                // `event`) when the cancel takes effect. We must NOT let the
                // caller close `event` / drop the stack `OVERLAPPED` until
                // GetOverlappedResult returns, or the kernel writes into freed
                // memory (UAF). This is the documented overlapped-cancel
                // contract; the Python client gets away with skipping it only
                // because GC defers its OVERLAPPED/event release.
                unsafe { CancelIo(handle) };
                let mut transferred: u32 = 0;
                // bWait=TRUE: block until the cancel completes. Safe because the
                // handle is overlapped (CreateFileW FILE_FLAG_OVERLAPPED /
                // inheritable anonymous pipe) and CancelIo has already been
                // issued, so this returns promptly with ERROR_OPERATION_ABORTED.
                let _ = unsafe { GetOverlappedResult(handle, overlapped_ptr, &mut transferred, 1) };
                return Err(format!("Timed out waiting {}ms for daemon", timeout_ms));
            }
            // WAIT_OBJECT_0 = signalled (done). Anything else (WAIT_FAILED, an
            // abandoned wait, …) is an error — don't fall through to reading
            // `transferred`, which may be uninitialized in that case. Capture
            // the winerror before GetOverlappedResult clobbers it.
            if rc != WAIT_OBJECT_0 {
                return Err(format!("WaitForSingleObject failed (winerror {})", rc));
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
/// Python client raises. Every message carries the winerror number so the IPC
/// layer's `is_transient` classifier can match it deterministically.
fn pipe_error(err: u32) -> String {
    match err {
        ERROR_BROKEN_PIPE | ERROR_NO_DATA | ERROR_PIPE_NOT_CONNECTED => {
            format!("Daemon closed the connection without responding (winerror {})", err)
        }
        _ => format!("pipe I/O error (winerror {})", err),
    }
}

#[cfg(test)]
mod tests {
    use super::{
        build_daemon_args, create_inheritable_pipe, filetime_to_u64, open_nul, pipe_error,
        read_stderr_to_string, truncate_for_error_msg,
    };
    use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};

    #[test]
    fn broken_pipe_message_carries_winerror() {
        // The IPC transient-retry classifier matches on "winerror <n>", so the
        // human-readable text must embed it.
        assert!(pipe_error(109).contains("winerror 109"));
        assert!(pipe_error(232).contains("winerror 232"));
        assert!(pipe_error(233).contains("winerror 233"));
    }

    #[test]
    fn other_errors_use_generic_form() {
        let m = pipe_error(6);
        assert!(m.contains("winerror 6"));
        assert!(!m.contains("closed the connection"));
    }

    #[test]
    fn truncate_short_input_unchanged() {
        assert_eq!(truncate_for_error_msg("hello", 500), "hello");
        // Exactly at the cap is fine.
        let s = "a".repeat(500);
        assert_eq!(truncate_for_error_msg(&s, 500).len(), 500);
    }

    #[test]
    fn truncate_empty_and_zero_max() {
        // Edge cases the multibyte test doesn't cover: empty input, and a
        // max of 0 (must return "" without underflowing the boundary loop).
        assert_eq!(truncate_for_error_msg("", 500), "");
        assert_eq!(truncate_for_error_msg("hello", 0), "");
        assert_eq!(truncate_for_error_msg("中文", 0), "");
    }

    #[test]
    fn truncate_ascii_caps_at_max() {
        let s = "a".repeat(800);
        assert_eq!(truncate_for_error_msg(&s, 500), &"a".repeat(500));
    }

    #[test]
    fn truncate_respects_multibyte_char_boundary() {
        // 中文 = 6 bytes (3 per char). A cap landing mid-character must back up
        // to the previous boundary so we never emit a half-codepoint (which
        // would be invalid UTF-8 and break the error message).
        // Bytes: 中(0-2) 文(3-5) 测(6-8) 试(9-11) ...
        let s = "中文测试中文测试"; // 18 bytes, boundaries at 0,3,6,9,12,15,18
        // Cap at 7 bytes lands inside 测 (bytes 6,7,8) — not a boundary → back to 6.
        assert_eq!(truncate_for_error_msg(s, 7), "中文");
        // Cap at 9 is exactly the end of 测 → boundary, keeps three chars.
        assert_eq!(truncate_for_error_msg(s, 9), "中文测");
        // Cap inside a 4-byte emoji: "a😀b" → 😀 is 4 bytes at offset 1-4.
        // Boundaries: 0(a) 1(😀) 5(b) 6.
        let s2 = "a😀b";
        assert_eq!(truncate_for_error_msg(s2, 2), "a"); // would split 😀, backs to 1
        assert_eq!(truncate_for_error_msg(s2, 5), "a😀"); // exactly past 😀
    }

    /// `filetime_to_u64` combines the two u32 halves of a FILETIME the way the
    /// Win32 docs specify (dwHighDateTime in the high 32 bits). Locked because
    /// `process_create_time_secs` (and thus the PID-recycle guard in
    /// `is_our_daemon`) depends on this being exactly right — a wrong combine
    /// would silently make every identity check fail-closed.
    #[test]
    fn filetime_to_u64_combines_halves() {
        use windows_sys::Win32::Foundation::FILETIME;
        let ft = FILETIME { dwLowDateTime: 0x1234_5678, dwHighDateTime: 0x0000_9ABC };
        assert_eq!(filetime_to_u64(&ft), 0x0000_9ABC_1234_5678);

        // Zero stays zero.
        let zero = FILETIME { dwLowDateTime: 0, dwHighDateTime: 0 };
        assert_eq!(filetime_to_u64(&zero), 0);

        // A realistic value: FILETIME epoch for 2020-01-01 ~132233352000000000
        // (100ns intervals since 1601). Low/high split sanity.
        let intervals: u64 = 132_233_352_000_000_000;
        let ft = FILETIME {
            dwLowDateTime: (intervals & 0xFFFF_FFFF) as u32,
            dwHighDateTime: (intervals >> 32) as u32,
        };
        assert_eq!(filetime_to_u64(&ft), intervals);
    }

    /// Round-trip a stderr snippet through a real anonymous pipe: write on the
    // write end, close it, read it back. This is the core of #9 — proves the
    // pipe + read path actually captures what a dying daemon would emit.
    #[test]
    fn stderr_pipe_round_trip_captures_written_bytes() {
        let (read, write) = create_inheritable_pipe();
        assert!(!read.is_null() && !write.is_null(), "pipe creation failed");

        let payload = b"Traceback (most recent call last):\n  ModuleNotFoundError: No module named 'pywintypes'\n";
        let n = unsafe {
            windows_sys::Win32::Storage::FileSystem::WriteFile(
                write,
                payload.as_ptr(),
                payload.len() as u32,
                &mut 0u32,
                std::ptr::null_mut(),
            )
        };
        assert_ne!(n, 0, "WriteFile to pipe failed");
        // Close the write end so the read sees EOF — mirrors spawn_daemon
        // closing the parent's write copy right after CreateProcessW.
        unsafe { CloseHandle(write) };

        let got = read_stderr_to_string(read);
        unsafe { CloseHandle(read) };
        assert!(got.contains("ModuleNotFoundError: No module named 'pywintypes'"));
    }

    #[test]
    fn open_nul_succeeds_with_a_valid_non_pseudo_handle() {
        // F12: CreateFileW signals failure with INVALID_HANDLE_VALUE (-1), not
        // null — so open_nul must return None on failure (not a handle that
        // .is_null() would mis-identify as valid). Under normal conditions NUL
        // opens fine, so we get Some(h) where h is neither null nor the -1
        // pseudo value, and CloseHandle(h) succeeds. This locks the
        // Option<HANDLE> contract: a future regression to a bare HANDLE return
        // (with callers using is_null) would compile but this test still
        // documents the expected shape.
        for read in [true, false] {
            match open_nul(read) {
                Some(h) => {
                    assert!(!h.is_null(), "NUL handle was null");
                    assert!(h != INVALID_HANDLE_VALUE, "NUL handle was INVALID_HANDLE_VALUE");
                    unsafe { CloseHandle(h) };
                }
                None => panic!("open_nul({read}) failed under normal conditions"),
            }
        }
    }

    #[test]
    fn build_daemon_args_base_form() {
        assert_eq!(build_daemon_args(false, None), "-m agent_windows --daemon");
    }

    #[test]
    fn build_daemon_args_debug_appended() {
        assert_eq!(
            build_daemon_args(true, None),
            "-m agent_windows --daemon --debug"
        );
    }

    #[test]
    fn build_daemon_args_config_appended() {
        assert_eq!(
            build_daemon_args(false, Some("/tmp/x.toml")),
            "-m agent_windows --daemon --config \"/tmp/x.toml\""
        );
    }

    #[test]
    fn build_daemon_args_debug_and_config() {
        assert_eq!(
            build_daemon_args(true, Some("C:\\Program Files\\a.toml")),
            "-m agent_windows --daemon --debug --config \"C:\\Program Files\\a.toml\""
        );
    }

    #[test]
    fn build_daemon_args_doubles_trailing_backslashes() {
        // A path ending in a backslash (e.g. a tab-completed directory) must
        // have its trailing backslashes doubled, else CommandLineToArgvW reads
        // the closing `\"` as an escaped quote and never terminates the arg.
        assert_eq!(
            build_daemon_args(false, Some("C:\\configs\\")),
            "-m agent_windows --daemon --config \"C:\\configs\\\\\""
        );
        // Two trailing backslashes -> four.
        assert_eq!(
            build_daemon_args(false, Some("C:\\a\\b\\\\")),
            "-m agent_windows --daemon --config \"C:\\a\\b\\\\\\\\\""
        );
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
        CreateProcessW, DeleteProcThreadAttributeList, GetExitCodeProcess, GetProcessTimes,
        InitializeProcThreadAttributeList, OpenProcess, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
        PROCESS_INFORMATION, PROCESS_QUERY_LIMITED_INFORMATION, STARTUPINFOEXW, STARTUPINFOW,
        UpdateProcThreadAttribute, EXTENDED_STARTUPINFO_PRESENT, LPPROC_THREAD_ATTRIBUTE_LIST,
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

/// The process's creation time as a Unix-epoch second (float), for comparison
/// against the start time the daemon writes to its PID file (Python's
/// `time.time()`). Used to defeat PID recycling: a recycled PID would be alive
/// but its creation time wouldn't match the value recorded when the daemon
/// started. Returns None if the process can't be queried (gone, access denied).
///
/// `GetProcessTimes` reports FILETIME (100ns intervals since 1601-01-01 UTC);
/// we convert to Unix epoch seconds by subtracting the 1601→1970 offset
/// (11644473600 s) and dividing by 10_000_000. f64 keeps microsecond
/// resolution, which is finer than the daemon's recorded `time.time()` and
/// the comparison tolerance.
pub fn process_create_time_secs(pid: u32) -> Option<f64> {
    // EPOCH_DIFF_SECS = seconds between 1601-01-01 (FILETIME epoch) and
    // 1970-01-01 (Unix epoch).
    const EPOCH_DIFF_SECS: f64 = 11_644_473_600.0;
    unsafe {
        let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if h.is_null() {
            return None;
        }
        let mut creation: FILETIME = std::mem::zeroed();
        let mut exit: FILETIME = std::mem::zeroed();
        let mut kernel: FILETIME = std::mem::zeroed();
        let mut user: FILETIME = std::mem::zeroed();
        let ok = GetProcessTimes(h, &mut creation, &mut exit, &mut kernel, &mut user);
        CloseHandle(h);
        if ok == 0 {
            return None;
        }
        let intervals = filetime_to_u64(&creation);
        Some(intervals as f64 / 10_000_000.0 - EPOCH_DIFF_SECS)
    }
}

/// FILETIME is two u32 halves (dwLowDateTime, dwHighDateTime) forming a 64-bit
/// count of 100ns intervals. Combine them little-endian as the Win32 docs specify.
fn filetime_to_u64(ft: &FILETIME) -> u64 {
    ((ft.dwHighDateTime as u64) << 32) | (ft.dwLowDateTime as u64)
}

/// Build the daemon's argument string (`-m agent_windows --daemon [--debug]
/// [--config "<path>"]`). Pure/testable — `spawn_daemon` embeds it after the
/// quoted interpreter path. The config path is double-quoted (Windows paths may
/// contain spaces, e.g. `C:\Program Files\...`); embedded double-quotes are
/// stripped as a defensive simplification (a legit path containing `"` is
/// pathological). A trailing run of backslashes is doubled so CreateProcessW's
/// CommandLineToArgvW doesn't read the final `\"` as an escaped quote (which
/// would swallow the closing quote and mangle the argument). (TODO #21-1)
pub fn build_daemon_args(debug: bool, config: Option<&str>) -> String {
    let mut args = String::from("-m agent_windows --daemon");
    if debug {
        args.push_str(" --debug");
    }
    if let Some(c) = config {
        // Strip embedded quotes (pathological), then double trailing
        // backslashes so they survive the closing quote (MSDN argv rules).
        let cleaned = c.replace('"', "");
        let trailing_bs = cleaned.chars().rev().take_while(|&ch| ch == '\\').count();
        let mut value = cleaned;
        if trailing_bs > 0 {
            value.push_str(&"\\".repeat(trailing_bs));
        }
        args.push_str(&format!(" --config \"{}\"", value));
    }
    args
}

/// Spawn the daemon as a detached subprocess: `<python> -m agent_windows --daemon`.
/// Returns (process handle, pid, stderr-read handle). The caller owns all three
/// handles and must close them. `stderr_read` is the read end of an anonymous
/// pipe wired to the daemon's stderr, so an early-exit traceback can be
/// surfaced (None only if the pipe couldn't be created — non-fatal, we just
/// lose stderr capture and fall back to the generic message).
///
/// Mirrors `_start_daemon_process`:
/// - CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
/// - stdin/stdout → NUL; stderr → inheritable anonymous pipe (parent reads it)
///
/// Trade-off vs. the old NUL-redirected stderr: once the launcher closes its
/// read end (after the daemon is up or has exited), any *post-startup* write
/// to the daemon's stderr hits a reader-less pipe and raises BrokenPipeError.
/// In practice this is fine — the daemon logs to daemon.log, not stderr, in
/// non-debug mode (server.py only adds a stderr StreamHandler under --debug),
/// so the only post-startup stderr writes are catastrophic crashes that are
/// already fatal. Keeping the read end open instead would risk the pipe
/// buffer filling and blocking the daemon on a large stderr flood.
pub fn spawn_daemon(
    python_exe: &str,
    debug: bool,
    config: Option<&str>,
) -> Result<(HANDLE, u32, Option<HANDLE>), String> {
    let args = build_daemon_args(debug, config);
    // CreateProcessW wants a mutable command line; the app path is quoted.
    let cmdline = format!("\"{}\" {}", python_exe, args);
    let mut cmdline_w: Vec<u16> = cmdline.encode_utf16().chain(std::iter::once(0)).collect();

    // DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    const DETACHED_PROCESS: u32 = 0x0000_0008;
    const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;

    // Redirect stdin/stdout to NUL. stderr goes to an inheritable anonymous
    // pipe so we can read the daemon's real startup error (import failures,
    // tracebacks) when it exits early — those are written by the interpreter
    // to stderr regardless of the daemon's logging config.
    let mut startup: STARTUPINFOW = unsafe { std::mem::zeroed() };
    startup.cb = std::mem::size_of::<STARTUPINFOW>() as u32;
    const STARTF_USESTDHANDLES: u32 = 0x0000_0100;
    startup.dwFlags = STARTF_USESTDHANDLES;

    let nul_in = open_nul(true);  // read
    let nul_out = open_nul(false); // write
    if let Some(h) = nul_in {
        startup.hStdInput = h;
    }
    if let Some(h) = nul_out {
        startup.hStdOutput = h;
    }

    // Inheritable anonymous pipe for stderr. If creation fails, fall back to
    // NUL for stderr and proceed without capture — better to start the daemon
    // than to fail outright over a missing diagnostic.
    let (stderr_read, stderr_write) = create_inheritable_pipe();
    let stderr_captured = !stderr_read.is_null() && !stderr_write.is_null();
    if stderr_captured {
        // The read end is intentionally NOT added to the inheritable handle
        // list below — only the write end reaches the child. (No need to clear
        // the INHERIT flag: the PROC_THREAD_ATTRIBUTE_HANDLE_LIST is the sole
        // authority on what gets inherited when present.)
        startup.hStdError = stderr_write;
    } else if let Some(h) = nul_out {
        startup.hStdError = h;
    }

    let mut info: PROCESS_INFORMATION = unsafe { std::mem::zeroed() };

    // CRITICAL: we must inherit ONLY the std handles we set (the stderr pipe
    // write end, plus the NUL stdin/stdout), NOT the launcher's own console
    // handles. Blanket bInheritHandles=TRUE would also inherit the parent's
    // console (the shell that launched us), defeating DETACHED_PROCESS — the
    // daemon would stay attached to the parent console and the shell would
    // hang waiting for it to release the console (observed: 5-min deadlock).
    //
    // The fix is STARTUPINFOEXW with a PROC_THREAD_ATTRIBUTE_HANDLE_LIST that
    // names exactly the handles the child may inherit; CreateProcessW then
    // ignores all other inheritable handles. This is how Rust's std
    // Command+Stdio::piped() avoids the same trap.
    // Collect the handles the child should inherit (deduplicated — stderr may
    // alias the NUL stdout handle when capture is disabled). nul_in/nul_out are
    // Option<HANDLE> (None if open_nul failed); stderr_write is a bare HANDLE
    // (null if the pipe failed), so wrap it to unify the filter below.
    let mut inherit_handles: Vec<HANDLE> = Vec::new();
    let stderr_for_inherit = if stderr_captured { Some(stderr_write) } else { None };
    for h in [nul_in, nul_out, stderr_for_inherit].into_iter().flatten() {
        if !inherit_handles.contains(&h) {
            inherit_handles.push(h);
        }
    }

    let attr = HandleList::new(&inherit_handles);
    let mut creation_flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW;

    // When we have a handle-list whitelist, spawn with bInheritHandles=TRUE +
    // STARTUPINFOEXW: the child inherits ONLY the listed handles (NUL stdio +
    // stderr pipe), so it cannot inherit the parent's console (which would
    // defeat DETACHED_PROCESS and deadlock the parent shell).
    //
    // Fallback (whitelist build failed): bInheritHandles=FALSE + plain
    // STARTUPINFOW. This matches the pre-#9 baseline exactly — no handles are
    // inherited at all. Critically, the fallback must NOT keep
    // bInheritHandles=TRUE without a whitelist: that would silently
    // reintroduce the console-inheritance deadlock this whole mechanism
    // exists to prevent. Stderr capture is lost in the fallback, but the
    // daemon still starts.
    //
    // `startup_ex` must outlive CreateProcessW (it holds the attribute-list
    // pointer), so it's declared in the outer scope and populated by branch.
    let mut startup_ex: STARTUPINFOEXW = unsafe { std::mem::zeroed() };
    let inherit: i32;
    let startup_ptr: *mut STARTUPINFOW;
    if let Some(a) = &attr {
        startup_ex.StartupInfo = startup;
        startup_ex.StartupInfo.cb = std::mem::size_of::<STARTUPINFOEXW>() as u32;
        startup_ex.StartupInfo.dwFlags |= STARTF_USESTDHANDLES;
        startup_ex.lpAttributeList = a.ptr();
        creation_flags |= EXTENDED_STARTUPINFO_PRESENT;
        inherit = 1;
        startup_ptr = &mut startup_ex.StartupInfo as *mut STARTUPINFOW;
    } else {
        startup_ex.StartupInfo = startup;
        startup_ex.StartupInfo.cb = std::mem::size_of::<STARTUPINFOW>() as u32;
        startup_ex.StartupInfo.dwFlags |= STARTF_USESTDHANDLES;
        inherit = 0;
        startup_ptr = &mut startup_ex.StartupInfo as *mut STARTUPINFOW;
    }

    let ok = unsafe {
        CreateProcessW(
            std::ptr::null(),
            cmdline_w.as_mut_ptr(),
            std::ptr::null(),
            std::ptr::null(),
            inherit,
            creation_flags,
            std::ptr::null(),
            std::ptr::null(),
            startup_ptr as *mut _,
            &mut info,
        )
    };
    // Capture the spawn-failure code IMMEDIATELY. GetLastError is thread-local
    // and is clobbered by any subsequent Win32 call — the CloseHandle calls and
    // the attribute-list Drop below would overwrite it, so the error message
    // previously reported a meaningless/random code instead of the real reason
    // (e.g. 2 = not found, 5 = access denied, 193 = bad exe format).
    let spawn_err = if ok == 0 { Some(unsafe { GetLastError() }) } else { None };
    // `attr` (and its backing buffer) drops here, AFTER CreateProcessW — but
    // the attribute list must be freed with DeleteProcThreadAttributeList
    // before the buffer is freed; HandleList's Drop does that in order.

    if let Some(h) = nul_in {
        unsafe { CloseHandle(h) };
    }
    if let Some(h) = nul_out {
        unsafe { CloseHandle(h) };
    }
    // Close the write end in the parent immediately: the child holds its own
    // copy, and once the child exits the read end sees EOF (otherwise the read
    // would block forever waiting for a writer that never closes).
    if !stderr_write.is_null() {
        unsafe { CloseHandle(stderr_write) };
    }

    if let Some(err) = spawn_err {
        // Spawn failed — clean up the pipe read end too.
        if !stderr_read.is_null() {
            unsafe { CloseHandle(stderr_read) };
        }
        return Err(format!("Failed to spawn daemon (winerror {})", err));
    }

    // We don't need the thread handle or the primary thread; close it. Keep
    // the process handle so we can poll for early exit (M-31 equivalent).
    unsafe { CloseHandle(info.hThread) };
    let stderr = if stderr_captured { Some(stderr_read) } else { None };
    Ok((info.hProcess, info.dwProcessId, stderr))
}

/// Create an inheritable anonymous pipe (for wiring the daemon's stderr).
/// Returns (read, write); either may be null on failure (caller checks).
fn create_inheritable_pipe() -> (HANDLE, HANDLE) {
    let mut sa: SECURITY_ATTRIBUTES = unsafe { std::mem::zeroed() };
    sa.nLength = std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32;
    sa.bInheritHandle = 1;
    let mut read: HANDLE = std::ptr::null_mut();
    let mut write: HANDLE = std::ptr::null_mut();
    let ok = unsafe { CreatePipe(&mut read, &mut write, &sa, 0) };
    if ok == 0 {
        return (std::ptr::null_mut(), std::ptr::null_mut());
    }
    (read, write)
}

/// RAII wrapper around a `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`, the mechanism
/// that restricts handle inheritance to an explicit set when spawning with
/// `bInheritHandles=TRUE`. Without this, the child would also inherit the
/// parent's console handles, defeating `DETACHED_PROCESS` and deadlocking the
/// parent shell (it waits for all console-attached descendants).
///
/// The backing buffer is a heap allocation aligned to `usize` (the documented
/// requirement is pointer alignment). `Drop` calls `DeleteProcThreadAttributeList`
/// before the buffer is released.
struct HandleList {
    buf: Box<[usize]>,
}

impl HandleList {
    /// Build a handle list permitting the child to inherit exactly `handles`.
    /// Returns None on failure or empty input — the caller MUST then spawn
    /// with bInheritHandles=FALSE (not TRUE) to avoid re-introducing the
    /// console-inheritance deadlock; see `spawn_daemon`'s fallback branch.
    fn new(handles: &[HANDLE]) -> Option<HandleList> {
        if handles.is_empty() {
            return None;
        }
        // First call: discover the required byte size.
        let mut size: usize = 0;
        unsafe {
            InitializeProcThreadAttributeList(std::ptr::null_mut(), 1, 0, &mut size);
        }
        // size is in bytes; round up to usize words for proper alignment.
        let words = size.div_ceil(std::mem::size_of::<usize>());
        let mut buf = vec![0usize; words.max(1)];
        let ptr = buf.as_mut_ptr() as LPPROC_THREAD_ATTRIBUTE_LIST;
        let ok = unsafe {
            InitializeProcThreadAttributeList(ptr, 1, 0, &mut size)
        };
        if ok == 0 {
            return None;
        }
        let ok = unsafe {
            UpdateProcThreadAttribute(
                ptr,
                0,
                PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
                handles.as_ptr() as *const _,
                std::mem::size_of_val(handles),
                std::ptr::null_mut(),
                std::ptr::null(),
            )
        };
        if ok == 0 {
            unsafe { DeleteProcThreadAttributeList(ptr) };
            return None;
        }
        Some(HandleList { buf: buf.into_boxed_slice() })
    }

    /// Raw pointer for STARTUPINFOEXW.lpAttributeList. Valid while `self` lives.
    fn ptr(&self) -> LPPROC_THREAD_ATTRIBUTE_LIST {
        self.buf.as_ptr() as LPPROC_THREAD_ATTRIBUTE_LIST
    }
}

impl Drop for HandleList {
    fn drop(&mut self) {
        unsafe {
            DeleteProcThreadAttributeList(self.buf.as_ptr() as LPPROC_THREAD_ATTRIBUTE_LIST);
        }
    }
}

/// Read all bytes from a stderr pipe handle until EOF, decoding as UTF-8
/// (lossy — Python tracebacks are UTF-8).
///
/// Safety contract: this is only called after `poll_process` reports the child
/// has exited. An exited child has closed its write end, so the (synchronous)
/// pipe yields any buffered bytes then EOF immediately — the call never blocks
/// in the normal early-exit path. The 1s-per-read timeout is a defensive cap
/// for the theoretical race where the OS hasn't yet torn down the child's
/// handles; it is not the primary safety mechanism. Capped at
/// `MAX_STDERR_CAPTURE` bytes. Note: a daemon that writes >4KB (the default
/// pipe buffer) of stderr WITHOUT exiting would block on its own write and
/// never trigger `poll_process` — but the daemon only writes stderr on a fatal
/// startup error (then exits), so this doesn't arise in practice.
pub fn read_stderr_to_string(handle: HANDLE) -> String {
    const STDERR_READ_TIMEOUT_MS: u32 = 1_000;
    let mut out = Vec::new();
    loop {
        match overlapped_read_chunk_timed(handle, STDERR_READ_TIMEOUT_MS) {
            Ok(chunk) if chunk.is_empty() => break, // EOF
            Ok(chunk) => {
                out.extend_from_slice(&chunk);
                if out.len() > MAX_STDERR_CAPTURE {
                    out.truncate(MAX_STDERR_CAPTURE);
                    break;
                }
            }
            Err(_) => break, // timeout or pipe closed — take what we have
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// One overlapped ReadFile bounded by `timeout_ms`, returning raw bytes.
/// Distinguishes EOF (Ok(empty)) from a hard error (Err). Mirrors
/// `overlapped_read_chunk` but signals EOF instead of treating it as data.
fn overlapped_read_chunk_timed(handle: HANDLE, timeout_ms: u32) -> Result<Vec<u8>, String> {
    let event = new_event()?;
    let mut overlapped: OVERLAPPED = unsafe { std::mem::zeroed() };
    overlapped.hEvent = event;
    let ovl_ptr = &mut overlapped as *mut _;

    let mut buf = [0u8; 4096];
    // All Win32 calls below are unsafe; group them in one block. We close
    // `event` on every path (success, EOF, error) to avoid a handle leak.
    let result = unsafe {
        let mut read: u32 = 0;
        let ok = ReadFile(
            handle,
            buf.as_mut_ptr(),
            buf.len() as u32,
            &mut read,
            ovl_ptr,
        );
        let err = GetLastError();
        // Synchronous completion.
        if ok != 0 {
            CloseHandle(event);
            return Ok(buf[..read as usize].to_vec());
        }
        // ERROR_HANDLE_EOF / ERROR_BROKEN_PIPE → the write end is closed: EOF.
        if err == ERROR_HANDLE_EOF || err == ERROR_BROKEN_PIPE {
            CloseHandle(event);
            return Ok(Vec::new());
        }
        if err != ERROR_IO_PENDING {
            CloseHandle(event);
            return Err(format!("ReadFile failed (winerror {})", err));
        }
        // Pending — wait for completion.
        let rc = WaitForSingleObject(event, timeout_ms);
        if rc == WAIT_TIMEOUT {
            // Drain the cancel before closing `event`: CancelIo only queues the
            // cancel; the kernel still writes the completion into `overlapped`
            // and signals `event` when it takes effect. Closing the event first
            // would be a UAF. bWait=TRUE blocks until the cancel completes
            // (prompt, ERROR_OPERATION_ABORTED).
            CancelIo(handle);
            let mut transferred: u32 = 0;
            let _ = GetOverlappedResult(handle, ovl_ptr, &mut transferred, 1);
            CloseHandle(event);
            return Err(format!("Timed out waiting {}ms for stderr", timeout_ms));
        }
        // WAIT_OBJECT_0 = signalled. Anything else (WAIT_FAILED, …) is an error —
        // don't read `transferred`, which may be unset.
        if rc != WAIT_OBJECT_0 {
            CloseHandle(event);
            return Err(format!("WaitForSingleObject failed (winerror {})", rc));
        }
        let mut transferred: u32 = 0;
        let got = GetOverlappedResult(handle, ovl_ptr, &mut transferred, 0);
        let gerr = GetLastError();
        CloseHandle(event);
        if got == 0 && (gerr == ERROR_HANDLE_EOF || gerr == ERROR_BROKEN_PIPE) {
            return Ok(Vec::new());
        }
        if got == 0 {
            return Err(format!("GetOverlappedResult failed (winerror {})", gerr));
        }
        Ok(buf[..transferred as usize].to_vec())
    };
    result
}

/// Cap on how many stderr bytes we buffer before truncating. The daemon's
/// early-exit traceback is small; this guards against a pathological flood.
const MAX_STDERR_CAPTURE: usize = 8 * 1024;

/// Truncate a stderr snippet for inclusion in an error message: cap at `max`
/// chars and back up to a UTF-8 char boundary so we never split a multibyte
/// sequence. Mirrors agent-browser's `connection.rs:729-737` truncation.
pub fn truncate_for_error_msg(s: &str, max: usize) -> &str {
    if s.len() <= max {
        return s;
    }
    let mut end = max;
    while end > 0 && !s.is_char_boundary(end) {
        end -= 1;
    }
    &s[..end]
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
/// The handle is marked inheritable so it can be listed in the spawn handle
/// list (only listed handles reach the child, but listed handles must also
/// carry the INHERIT flag for the child to actually receive them).
///
/// Returns `None` on failure. `CreateFileW` signals failure with
/// `INVALID_HANDLE_VALUE` (i.e. `(HANDLE)-1`), **not** null — so callers cannot
/// test success with `.is_null()` (that's always true for `-1`). Encoding the
/// failure in `Option` makes the success/failure branch unmissable and keeps
/// every consumer honest without re-deriving the `== INVALID_HANDLE_VALUE`
/// check at each site. NUL almost never fails to open, so this is defensive,
/// but the old `is_null` check was wrong for any future failure. (F12)
fn open_nul(read: bool) -> Option<HANDLE> {
    use windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE;
    use windows_sys::Win32::Storage::FileSystem::{
        CreateFileW, FILE_ATTRIBUTE_NORMAL, FILE_SHARE_READ, FILE_SHARE_WRITE,
    };
    let mut sa: SECURITY_ATTRIBUTES = unsafe { std::mem::zeroed() };
    sa.nLength = std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32;
    sa.bInheritHandle = 1;
    let access = if read { 0x8000_0000 } else { 0x4000_0000 }; // GENERIC_READ / WRITE
    let nul_w = widen("NUL");
    let handle = unsafe {
        CreateFileW(
            nul_w.as_ptr(),
            access,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            &sa,
            3, // OPEN_EXISTING
            FILE_ATTRIBUTE_NORMAL,
            std::ptr::null_mut(),
        )
    };
    if handle == INVALID_HANDLE_VALUE {
        None
    } else {
        Some(handle)
    }
}
