//! Output formatting. Mirrors `agent_windows.cli.dispatcher.print_result`:
//! - `text` mode: print `result["output"]`; if absent, print `key: value` lines
//! - `json` mode: pretty-print the whole result object
//!
//! Both write UTF-8 directly to stdout (bypassing the Windows GBK console
//! codepage — the L-15 equivalent).

use serde_json::Value;
use windows_sys::Win32::Foundation::HANDLE;

/// Print a result object according to the requested format.
pub fn print_result(result: &Value, json_mode: bool) {
    if json_mode {
        let text = serde_json::to_string_pretty(result).unwrap_or_else(|_| "{}".into());
        write_stdout(&text);
        return;
    }
    // text mode
    if let Some(output) = result.get("output").and_then(|v| v.as_str()) {
        if !output.is_empty() {
            write_stdout(output);
            return;
        }
    }
    // Fallback: key: value lines for every non-`output` field.
    if let Some(obj) = result.as_object() {
        let mut lines = Vec::new();
        for (k, v) in obj {
            if k == "output" {
                continue;
            }
            lines.push(format!("{}: {}", k, value_to_text(v)));
        }
        if !lines.is_empty() {
            write_stdout(&lines.join("\n"));
        }
    }
}

/// Print bare text (used by `wait`'s local-sleep mode + error messages).
pub fn print_text(text: &str) {
    write_stdout(text);
}

/// Print to stderr (errors). Text mode only — `--format json` callers use
/// `print_error_json` so errors stay on stdout as parseable JSON.
pub fn print_error(text: &str) {
    let mut out = text.to_string();
    if !out.ends_with('\n') {
        out.push('\n');
    }
    let bytes = out.as_bytes();
    write_handle(stderr_handle(), bytes);
}

/// Print an error as JSON to **stdout**, for `--format json` mode. Keeps the
/// success/error contract uniform: every line on stdout is valid JSON, so an
/// agent consumer never has to read stderr to detect a failure.
///
/// Produces `{"success":false,"error":"<msg>","type":"<error_type>"}`.
///
/// `error_type` is one of a closed set. See `report_error` in `main.rs` for
/// the authoritative list and how callers choose it.
pub fn print_error_json(msg: &str, error_type: &str) {
    let escaped = json_escape(msg);
    let line = format!(
        "{{\"success\":false,\"error\":\"{}\",\"type\":\"{}\"}}",
        escaped, error_type
    );
    write_stdout(&line);
}

/// Minimal JSON string escaper (avoids pulling in serde just to encode one
/// message — the rest of this module writes pre-built JSON via serde_json).
fn json_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 8);
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

/// Render a JSON value as a flat string (mirrors Python's str() on the value).
fn value_to_text(v: &Value) -> String {
    match v {
        Value::Null => String::new(),
        Value::Bool(b) => b.to_string(),
        Value::Number(n) => n.to_string(),
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

/// Write `text` + newline to stdout as UTF-8, bypassing the console codepage.
fn write_stdout(text: &str) {
    let mut buf = text.as_bytes().to_vec();
    if !buf.ends_with(b"\n") {
        buf.push(b'\n');
    }
    write_handle(stdout_handle(), &buf);
}

fn stdout_handle() -> HANDLE {
    use windows_sys::Win32::System::Console::GetStdHandle;
    // STD_OUTPUT_HANDLE = -11 (pseudo-handle)
    unsafe { GetStdHandle(-11i32 as u32) }
}

fn stderr_handle() -> HANDLE {
    use windows_sys::Win32::System::Console::GetStdHandle;
    // STD_ERROR_HANDLE = -12 (pseudo-handle)
    unsafe { GetStdHandle(-12i32 as u32) }
}

/// Write bytes to a console handle, falling back to WriteFile.
fn write_handle(handle: HANDLE, bytes: &[u8]) {
    use windows_sys::Win32::System::Console::WriteConsoleW;
    use windows_sys::Win32::Storage::FileSystem::WriteFile;

    // Encode to UTF-16 for WriteConsoleW (handles Unicode on the console).
    let utf16: Vec<u16> = String::from_utf8_lossy(bytes).encode_utf16().collect();
    let mut written: u32 = 0;
    // Lengths here are bounded by console output strings (tiny), but guard the
    // casts anyway for symmetry with write_pipe and to stay correct if a future
    // caller passes a large buffer.
    let n16 = u32::try_from(utf16.len()).unwrap_or(u32::MAX);
    let n8 = u32::try_from(bytes.len()).unwrap_or(u32::MAX);
    let ok = unsafe {
        WriteConsoleW(handle, utf16.as_ptr().cast(), n16, &mut written, std::ptr::null())
    };
    if ok == 0 {
        // Fallback: raw bytes via WriteFile (redirected streams, pipes).
        unsafe { WriteFile(handle, bytes.as_ptr(), n8, &mut written, std::ptr::null_mut()) };
    }
}
