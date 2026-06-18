//! Command dispatch table. Each entry maps a CLI invocation to a JSON-RPC
//! method + params. The launcher parses raw argv here (a tiny hand-rolled
//! parser — no clap, to keep the binary lean and startup instant) and builds
//! the request.
//!
//! Special-cased commands (local, or with non-trivial param shaping) get their
//! own arms; everything else routes through the generic table.

use serde_json::{json, Map, Value};

/// Outcome of dispatching a parsed argv slice.
pub enum Action {
    /// Send this JSON-RPC request to the daemon.
    Call { method: String, params: Value },
    /// Handle entirely locally (no IPC).
    Local(LocalAction),
    /// Print usage and exit.
    Help,
}

pub enum LocalAction {
    DaemonStart,
    DaemonStop,
    DaemonStatus,
}

/// Parse `agent-windows <command> [args...]` into an Action.
///
/// This is the heart of the launcher — it turns raw argv into either a daemon
/// call or a local action. Mirrors the Typer command structure in
/// `cli/commands/*.py`.
pub fn parse(argv: &[String], global: &GlobalFlags) -> Result<Action, String> {
    if argv.is_empty() {
        return Ok(Action::Help);
    }
    let cmd = argv[0].as_str();
    let rest = &argv[1..];

    match cmd {
        // ── Local commands ────────────────────────────────────────────────
        "daemon" => parse_daemon(rest),
        "config" => Err("config commands are not yet supported by the launcher — \
                         use the Python CLI (agent-windows-python)".into()),

        // ── Observation ───────────────────────────────────────────────────
        "observe" => parse_observe(rest),
        "snapshot" => Ok(Action::Call { method: "snapshot".into(), params: json!({}) }),
        "screenshot" => parse_screenshot(rest),
        "ground" => parse_ground(rest),
        "ask-vision" => parse_ask_vision(rest),
        "detect" => parse_detect(rest),
        "get-text" => {
            let selector = require_positional(rest, 0, "selector")?;
            Ok(Action::Call {
                method: "get_text".into(),
                params: json!({ "selector": selector }),
            })
        }
        "highlight" => parse_highlight(rest),
        "diff" => parse_diff(rest),
        "cleanup" => Ok(Action::Call {
            method: "cleanup_screenshots".into(),
            params: json!({}),
        }),

        // ── `is visible/enabled @eN` ──────────────────────────────────────
        "is" => parse_is(rest),

        // ── Input (mouse) ─────────────────────────────────────────────────
        "click" => parse_xy_opts(rest, "click", &["--right", "--double"]),
        "dblclick" => parse_xy_opts(rest, "dblclick", &["--right"]),
        "hover" => parse_xy(rest, "hover"),
        "move" => parse_xy(rest, "move"),
        "mouse-down" => parse_mouse_button(rest, "mouse_down"),
        "mouse-up" => parse_mouse_button(rest, "mouse_up"),
        "drag" => parse_drag(rest),
        "scroll" => parse_scroll(rest),

        // ── Input (keyboard) ──────────────────────────────────────────────
        "type" => parse_type(rest),
        "shortcut" => {
            let keys = require_positional(rest, 0, "keys")?;
            Ok(Action::Call {
                method: "shortcut".into(),
                params: json!({ "keys": keys }),
            })
        }

        // ── System groups ─────────────────────────────────────────────────
        "app" => parse_app(rest),
        "fs" => parse_fs(rest),
        "clipboard" => parse_clipboard(rest),
        "process" => parse_process(rest),

        // ── Control ───────────────────────────────────────────────────────
        "wait" => parse_wait(rest),
        "batch" => parse_batch(rest, global),

        // ── Diagnostic ────────────────────────────────────────────────────
        "ping" => Ok(Action::Call { method: "ping".into(), params: json!({}) }),

        _ => Err(format!("Unknown command: {}. Run 'agent-windows --help'.", cmd)),
    }
}

// ── Global flags parsed before the command ─────────────────────────────────

#[derive(Default, Clone)]
pub struct GlobalFlags {
    pub no_daemon: bool,
    pub format_json: bool,
    pub debug: bool,
}

// ── Helpers: tiny option parser ─────────────────────────────────────────────

/// Split `rest` into (positionals, options-present-set). An option is any arg
/// starting with `--`; we only track presence (values are pulled separately).
fn split_args(rest: &[String]) -> (Vec<&str>, Vec<&str>) {
    let mut pos = Vec::new();
    let mut opts = Vec::new();
    for a in rest {
        if let Some(name) = a.strip_prefix("--") {
            opts.push(name);
        } else {
            pos.push(a.as_str());
        }
    }
    (pos, opts)
}

/// Get a `--opt value` from the slice, or None.
fn opt_value<'a>(rest: &'a [String], name: &str) -> Option<&'a str> {
    let flag = format!("--{}", name);
    let mut iter = rest.iter();
    while let Some(a) = iter.next() {
        if a == &flag {
            return iter.next().map(|s| s.as_str());
        }
        // Handle `--opt=value` form too.
        if let Some(v) = a.strip_prefix(&format!("{}=", flag)) {
            return Some(v);
        }
    }
    None
}

fn has_flag(rest: &[String], name: &str) -> bool {
    rest.iter().any(|a| a == name || a == &format!("--{}", name))
}

fn require_positional(rest: &[String], idx: usize, name: &str) -> Result<String, String> {
    let (pos, _) = split_args(rest);
    pos.get(idx)
        .map(|s| s.to_string())
        .ok_or_else(|| format!("Missing required positional argument: {}", name))
}

fn parse_xy(rest: &[String], method: &str) -> Result<Action, String> {
    let x: i64 = require_positional(rest, 0, "x")?.parse()
        .map_err(|_| "x must be an integer".to_string())?;
    let y: i64 = require_positional(rest, 1, "y")?.parse()
        .map_err(|_| "y must be an integer".to_string())?;
    Ok(Action::Call {
        method: method.into(),
        params: json!({ "x": x, "y": y }),
    })
}

fn parse_xy_opts(rest: &[String], method: &str, flags: &[&str]) -> Result<Action, String> {
    let x: i64 = require_positional(rest, 0, "x")?.parse()
        .map_err(|_| "x must be an integer".to_string())?;
    let y: i64 = require_positional(rest, 1, "y")?.parse()
        .map_err(|_| "y must be an integer".to_string())?;
    let mut params = Map::new();
    params.insert("x".into(), x.into());
    params.insert("y".into(), y.into());
    for f in flags {
        let key = f.trim_start_matches("--");
        params.insert(key.into(), has_flag(rest, f).into());
    }
    Ok(Action::Call { method: method.into(), params: Value::Object(params) })
}

fn parse_mouse_button(rest: &[String], method: &str) -> Result<Action, String> {
    let x: i64 = require_positional(rest, 0, "x")?.parse()
        .map_err(|_| "x must be an integer".to_string())?;
    let y: i64 = require_positional(rest, 1, "y")?.parse()
        .map_err(|_| "y must be an integer".to_string())?;
    let button = opt_value(rest, "button").unwrap_or("left");
    Ok(Action::Call {
        method: method.into(),
        params: json!({ "x": x, "y": y, "button": button }),
    })
}

fn parse_drag(rest: &[String]) -> Result<Action, String> {
    let x1: i64 = require_positional(rest, 0, "x1")?.parse()
        .map_err(|_| "x1 must be an integer".to_string())?;
    let y1: i64 = require_positional(rest, 1, "y1")?.parse()
        .map_err(|_| "y1 must be an integer".to_string())?;
    let x2: i64 = require_positional(rest, 2, "x2")?.parse()
        .map_err(|_| "x2 must be an integer".to_string())?;
    let y2: i64 = require_positional(rest, 3, "y2")?.parse()
        .map_err(|_| "y2 must be an integer".to_string())?;
    let mut params = Map::new();
    params.insert("x1".into(), x1.into());
    params.insert("y1".into(), y1.into());
    params.insert("x2".into(), x2.into());
    params.insert("y2".into(), y2.into());
    params.insert("right".into(), has_flag(rest, "--right").into());
    Ok(Action::Call { method: "drag".into(), params: Value::Object(params) })
}

fn parse_scroll(rest: &[String]) -> Result<Action, String> {
    let x: i64 = require_positional(rest, 0, "x")?.parse()
        .map_err(|_| "x must be an integer".to_string())?;
    let y: i64 = require_positional(rest, 1, "y")?.parse()
        .map_err(|_| "y must be an integer".to_string())?;
    let direction = opt_value(rest, "direction").unwrap_or("down");
    let amount: i64 = opt_value(rest, "amount")
        .map(|v| v.parse().unwrap_or(3))
        .unwrap_or(3);
    Ok(Action::Call {
        method: "scroll".into(),
        params: json!({ "x": x, "y": y, "direction": direction, "amount": amount }),
    })
}

fn parse_observe(rest: &[String]) -> Result<Action, String> {
    let snapshot = has_flag(rest, "--snapshot");
    let screenshot = has_flag(rest, "--screenshot");
    let ground = opt_value(rest, "ground").unwrap_or("");
    // Default: snapshot + screenshot when no flags.
    let (snap, shot) = if !snapshot && !screenshot && ground.is_empty() {
        (true, true)
    } else {
        (snapshot, screenshot)
    };
    let mut params = Map::new();
    params.insert("snapshot".into(), snap.into());
    params.insert("screenshot".into(), shot.into());
    params.insert("ground".into(), ground.into());
    Ok(Action::Call { method: "observe".into(), params: Value::Object(params) })
}

fn parse_screenshot(rest: &[String]) -> Result<Action, String> {
    let output = opt_value(rest, "output").or_else(|| opt_value(rest, "o")).unwrap_or("");
    let monitor: i64 = opt_value(rest, "monitor")
        .map(|v| v.parse().unwrap_or(-1))
        .unwrap_or(-1);
    let mut params = Map::new();
    params.insert("output".into(), output.into());
    params.insert("monitor".into(), monitor.into());
    if let Some(sel) = opt_value(rest, "selector").or_else(|| opt_value(rest, "s")) {
        params.insert("selector".into(), sel.into());
    }
    if let Some(bbox) = opt_value(rest, "bbox") {
        let nums: Result<Vec<i64>, _> = bbox
            .split(',')
            .map(|n| n.parse().map_err(|_| format!("invalid bbox value: {}", n)))
            .collect();
        params.insert("bbox".into(), Value::Array(nums?.into_iter().map(Value::from).collect()));
    }
    Ok(Action::Call { method: "screenshot".into(), params: Value::Object(params) })
}

fn parse_ground(rest: &[String]) -> Result<Action, String> {
    let description = require_positional(rest, 0, "description")?;
    let mut params = Map::new();
    params.insert("description".into(), description.into());
    if has_flag(rest, "--single-pass") {
        params.insert("single_pass".into(), true.into());
    }
    Ok(Action::Call { method: "ground".into(), params: Value::Object(params) })
}

fn parse_ask_vision(rest: &[String]) -> Result<Action, String> {
    let prompt = require_positional(rest, 0, "prompt")?;
    let mut params = Map::new();
    params.insert("prompt".into(), prompt.into());
    if let Some(v) = opt_value(rest, "image").or_else(|| opt_value(rest, "i")) {
        params.insert("image".into(), v.into());
    }
    if let Some(v) = opt_value(rest, "model").or_else(|| opt_value(rest, "m")) {
        params.insert("model".into(), v.into());
    }
    if let Some(v) = opt_value(rest, "output").or_else(|| opt_value(rest, "o")) {
        params.insert("output".into(), v.into());
    }
    Ok(Action::Call { method: "ask-vision".into(), params: Value::Object(params) })
}

fn parse_detect(rest: &[String]) -> Result<Action, String> {
    let description = require_positional(rest, 0, "description")?;
    let max_targets: i64 = opt_value(rest, "max-targets")
        .map(|v| v.parse().unwrap_or(20))
        .unwrap_or(20);
    let mut params = Map::new();
    params.insert("description".into(), description.into());
    params.insert("max_targets".into(), max_targets.into());
    if let Some(v) = opt_value(rest, "image").or_else(|| opt_value(rest, "i")) {
        params.insert("image".into(), v.into());
    }
    if let Some(v) = opt_value(rest, "model").or_else(|| opt_value(rest, "m")) {
        params.insert("model".into(), v.into());
    }
    if let Some(v) = opt_value(rest, "output").or_else(|| opt_value(rest, "o")) {
        params.insert("output".into(), v.into());
    }
    Ok(Action::Call { method: "detect".into(), params: Value::Object(params) })
}

fn parse_highlight(rest: &[String]) -> Result<Action, String> {
    let x1: i64 = require_positional(rest, 0, "x1")?.parse()
        .map_err(|_| "x1 must be an integer".to_string())?;
    let y1: i64 = require_positional(rest, 1, "y1")?.parse()
        .map_err(|_| "y1 must be an integer".to_string())?;
    let x2: i64 = require_positional(rest, 2, "x2")?.parse()
        .map_err(|_| "x2 must be an integer".to_string())?;
    let y2: i64 = require_positional(rest, 3, "y2")?.parse()
        .map_err(|_| "y2 must be an integer".to_string())?;
    let mut params = Map::new();
    params.insert("x1".into(), x1.into());
    params.insert("y1".into(), y1.into());
    params.insert("x2".into(), x2.into());
    params.insert("y2".into(), y2.into());
    if let Some(v) = opt_value(rest, "label").or_else(|| opt_value(rest, "l")) {
        params.insert("label".into(), v.into());
    }
    let color = opt_value(rest, "color").or_else(|| opt_value(rest, "c")).unwrap_or("red");
    params.insert("color".into(), color.into());
    if let Some(v) = opt_value(rest, "image").or_else(|| opt_value(rest, "i")) {
        params.insert("image".into(), v.into());
    }
    if let Some(v) = opt_value(rest, "output").or_else(|| opt_value(rest, "o")) {
        params.insert("output".into(), v.into());
    }
    Ok(Action::Call { method: "highlight".into(), params: Value::Object(params) })
}

fn parse_diff(rest: &[String]) -> Result<Action, String> {
    let baseline = require_positional(rest, 0, "baseline")?;
    let threshold: i64 = opt_value(rest, "threshold")
        .or_else(|| opt_value(rest, "t"))
        .map(|v| v.parse().unwrap_or(30))
        .unwrap_or(30);
    let mut params = Map::new();
    params.insert("baseline".into(), baseline.into());
    params.insert("threshold".into(), threshold.into());
    if let Some(v) = opt_value(rest, "compare").or_else(|| opt_value(rest, "c")) {
        params.insert("compare".into(), v.into());
    }
    if let Some(v) = opt_value(rest, "output").or_else(|| opt_value(rest, "o")) {
        params.insert("output".into(), v.into());
    }
    Ok(Action::Call { method: "diff".into(), params: Value::Object(params) })
}

fn parse_is(rest: &[String]) -> Result<Action, String> {
    let sub = rest.first().ok_or("Missing 'visible' or 'enabled'")?;
    let check = match sub.as_str() {
        "visible" => "visible",
        "enabled" => "enabled",
        _ => return Err(format!("`is` expects visible/enabled, got: {}", sub)),
    };
    let selector = require_positional(&rest[1..], 0, "selector")?;
    Ok(Action::Call {
        method: "element_state".into(),
        params: json!({ "selector": selector, "check": check }),
    })
}

fn parse_type(rest: &[String]) -> Result<Action, String> {
    let text = require_positional(rest, 0, "text")?;
    let mut params = Map::new();
    params.insert("text".into(), text.into());
    if let Some(at) = opt_value(rest, "at") {
        // `--at X,Y` → at_x, at_y (mirrors input.py:32-36)
        let parts: Vec<&str> = at.split(',').collect();
        if parts.len() != 2 {
            return Err(format!("--at expects X,Y, got: {}", at));
        }
        let x: i64 = parts[0].parse().map_err(|_| "--at x must be integer")?;
        let y: i64 = parts[1].parse().map_err(|_| "--at y must be integer")?;
        params.insert("at_x".into(), x.into());
        params.insert("at_y".into(), y.into());
    }
    params.insert("enter".into(), has_flag(rest, "--enter").into());
    params.insert("clear".into(), has_flag(rest, "--clear").into());
    Ok(Action::Call { method: "type".into(), params: Value::Object(params) })
}

fn parse_app(rest: &[String]) -> Result<Action, String> {
    let sub = rest.first().ok_or("Missing app subcommand (launch/switch/list/resize)")?;
    let args = &rest[1..];
    match sub.as_str() {
        "launch" => {
            let name_or_path = require_positional(args, 0, "name_or_path")?;
            // remaining positionals become app args
            let (pos, _) = split_args(args);
            let app_args: Vec<&str> = pos.iter().skip(1).copied().collect();
            Ok(Action::Call {
                method: "app_launch".into(),
                params: json!({ "name_or_path": name_or_path, "args": app_args }),
            })
        }
        "switch" => {
            let name = require_positional(args, 0, "name_or_title")?;
            Ok(Action::Call {
                method: "app_switch".into(),
                params: json!({ "name_or_title": name }),
            })
        }
        "list" => Ok(Action::Call { method: "app_list".into(), params: json!({}) }),
        "resize" => {
            let name = require_positional(args, 0, "name_or_title")?;
            let width: i64 = opt_value(args, "width")
                .ok_or("--width required")?
                .parse().map_err(|_| "--width must be integer")?;
            let height: i64 = opt_value(args, "height")
                .ok_or("--height required")?
                .parse().map_err(|_| "--height must be integer")?;
            let x: i64 = opt_value(args, "x").map(|v| v.parse().unwrap_or(-1)).unwrap_or(-1);
            let y: i64 = opt_value(args, "y").map(|v| v.parse().unwrap_or(-1)).unwrap_or(-1);
            Ok(Action::Call {
                method: "app_resize".into(),
                params: json!({ "name_or_title": name, "width": width, "height": height, "x": x, "y": y }),
            })
        }
        _ => Err(format!("Unknown app subcommand: {}", sub)),
    }
}

fn parse_fs(rest: &[String]) -> Result<Action, String> {
    let sub = rest.first().ok_or("Missing fs subcommand (read/write/list)")?;
    let args = &rest[1..];
    match sub.as_str() {
        "read" => {
            let path = require_positional(args, 0, "path")?;
            Ok(Action::Call { method: "fs_read".into(), params: json!({ "path": path }) })
        }
        "write" => {
            let path = require_positional(args, 0, "path")?;
            let content = require_positional(args, 1, "content")?;
            Ok(Action::Call {
                method: "fs_write".into(),
                params: json!({ "path": path, "content": content }),
            })
        }
        "list" => {
            let path = require_positional(args, 0, "path")?;
            Ok(Action::Call { method: "fs_list".into(), params: json!({ "path": path }) })
        }
        _ => Err(format!("Unknown fs subcommand: {}", sub)),
    }
}

fn parse_clipboard(rest: &[String]) -> Result<Action, String> {
    let sub = rest.first().ok_or("Missing clipboard subcommand (get/set)")?;
    let args = &rest[1..];
    match sub.as_str() {
        "get" => {
            let mut params = Map::new();
            if has_flag(args, "--keyboard") || has_flag(args, "-k") {
                params.insert("keyboard".into(), true.into());
            }
            Ok(Action::Call { method: "clipboard_get".into(), params: Value::Object(params) })
        }
        "set" => {
            let text = require_positional(args, 0, "text")?;
            let mut params = Map::new();
            params.insert("text".into(), text.into());
            if has_flag(args, "--keyboard") || has_flag(args, "-k") {
                params.insert("keyboard".into(), true.into());
            }
            Ok(Action::Call { method: "clipboard_set".into(), params: Value::Object(params) })
        }
        _ => Err(format!("Unknown clipboard subcommand: {}", sub)),
    }
}

fn parse_process(rest: &[String]) -> Result<Action, String> {
    let sub = rest.first().ok_or("Missing process subcommand (list/kill)")?;
    let args = &rest[1..];
    match sub.as_str() {
        "list" => Ok(Action::Call { method: "process_list".into(), params: json!({}) }),
        "kill" => {
            let pid_or_name = require_positional(args, 0, "pid_or_name")?;
            Ok(Action::Call {
                method: "process_kill".into(),
                params: json!({ "pid_or_name": pid_or_name }),
            })
        }
        _ => Err(format!("Unknown process subcommand: {}", sub)),
    }
}

fn parse_wait(rest: &[String]) -> Result<Action, String> {
    // Three modes (mirrors cli/commands/wait.py):
    //   wait <seconds>     → if seconds <= 0, error; else LOCAL sleep (no IPC)
    //   wait @eN [...]     → client-side polling via wait_check (not yet impl)
    //   wait --text "..."  → client-side polling via wait_check (not yet impl)
    //
    // The local-sleep mode is implemented here (no reason to round-trip to the
    // daemon just to block its STA worker). The polling modes need a config
    // read for poll_interval; defer those to the Python CLI for now.
    let text_mode = opt_value(rest, "text").is_some();

    let (pos, _) = split_args(rest);
    let t0 = pos.first().copied().unwrap_or("");

    // Selector or text mode → needs client-side polling (not yet in launcher).
    if text_mode || t0.starts_with("@e") {
        return Err(
            "wait with @eN selector or --text requires client-side polling, which the \
             native launcher doesn't implement yet. Use the Python CLI: \
             agent-windows-python wait ...".into(),
        );
    }

    // Numeric sleep — do it locally, no IPC.
    let secs: f64 = t0.parse().map_err(|_| {
        format!("invalid target {:?} (expected seconds, @eN selector, or --text)", t0)
    })?;
    if secs <= 0.0 {
        return Err("provide a positive number of seconds, a selector, or --text".into());
    }
    std::thread::sleep(Duration::from_secs_f64(secs));
    // Emit the same output the Python CLI would.
    print_text(&format!("## Wait\nSlept {}s", secs));
    std::process::exit(0);
}

fn parse_batch(rest: &[String], _global: &GlobalFlags) -> Result<Action, String> {
    // --file / stdin / positional JSON
    let raw = if let Some(path) = opt_value(rest, "file").or_else(|| opt_value(rest, "f")) {
        std::fs::read_to_string(path).map_err(|e| format!("cannot read --file: {}", e))?
    } else {
        let (pos, _) = split_args(rest);
        match pos.first().copied() {
            Some("-") => {
                use std::io::Read;
                let mut s = String::new();
                std::io::stdin().read_to_string(&mut s).map_err(|e| e.to_string())?;
                s
            }
            Some(json) => json.to_string(),
            None => {
                return Err("provide commands as argument, --file, or stdin (-)".into());
            }
        }
    };
    let commands: Value = serde_json::from_str(&raw).map_err(|e| format!("invalid JSON: {}", e))?;
    if !commands.is_array() || commands.as_array().unwrap().is_empty() {
        return Err("commands must be a non-empty JSON array".into());
    }
    let stop_on_error = !has_flag(rest, "--continue-on-error");
    let delay: f64 = opt_value(rest, "delay")
        .or_else(|| opt_value(rest, "d"))
        .map(|v| v.parse().unwrap_or(0.0))
        .unwrap_or(0.0);
    Ok(Action::Call {
        method: "batch".into(),
        params: json!({ "commands": commands, "stop_on_error": stop_on_error, "delay": delay }),
    })
}

fn parse_daemon(rest: &[String]) -> Result<Action, String> {
    let sub = rest.first().map(|s| s.as_str()).unwrap_or("");
    match sub {
        "start" => Ok(Action::Local(LocalAction::DaemonStart)),
        "stop" => Ok(Action::Local(LocalAction::DaemonStop)),
        "status" => Ok(Action::Local(LocalAction::DaemonStatus)),
        _ => Err(format!("`daemon` expects start/stop/status, got: {}", sub)),
    }
}

use std::time::Duration;
use crate::output::print_text;
