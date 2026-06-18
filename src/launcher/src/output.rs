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

/// Print to stderr (errors).
pub fn print_error(text: &str) {
    let mut out = text.to_string();
    if !out.ends_with('\n') {
        out.push('\n');
    }
    let bytes = out.as_bytes();
    write_handle(stderr_handle(), bytes);
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
    let ok = unsafe {
        WriteConsoleW(handle, utf16.as_ptr().cast(), utf16.len() as u32, &mut written, std::ptr::null())
    };
    if ok == 0 {
        // Fallback: raw bytes via WriteFile (redirected streams, pipes).
        unsafe { WriteFile(handle, bytes.as_ptr(), bytes.len() as u32, &mut written, std::ptr::null_mut()) };
    }
}
