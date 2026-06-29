//! Command dispatch table. Each entry maps a CLI invocation to a JSON-RPC
//! method + params. The launcher parses raw argv here (a tiny hand-rolled
//! parser — no clap, to keep the binary lean and startup instant) and builds
//! the request.
//!
//! Special-cased commands (local, or with non-trivial param shaping) get their
//! own arms; everything else routes through the generic table.

use std::collections::HashMap;

use serde_json::{json, Map, Value};


/// A typed parse error. Carries both a machine-readable `type` (stable string
/// for the JSON error contract, so an agent consumer can pick a retry strategy
/// without parsing free text) and a human-readable message.
///
/// Mirrors agent-browser's `ParseError` enum. Variants:
/// - `UnknownCommand`: the top-level command name wasn't recognized.
/// - `UnknownSubcommand`: a group command's sub wasn't recognized; carries the
///   list of valid subcommands so we can list them (#5).
/// - `MissingArgument`: a required positional/value was absent.
/// - `InvalidValue`: a value failed to parse (non-integer, bad format, …).
#[derive(Debug)]
pub struct ParseError {
    kind: ErrorKind,
    message: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ErrorKind {
    UnknownCommand,
    UnknownSubcommand,
    MissingArgument,
    InvalidValue,
}

impl ParseError {
    /// Stable machine-readable type tag for the JSON error contract.
    pub fn type_name(&self) -> &'static str {
        match self.kind {
            ErrorKind::UnknownCommand => "unknown_command",
            ErrorKind::UnknownSubcommand => "unknown_subcommand",
            ErrorKind::MissingArgument => "missing_argument",
            ErrorKind::InvalidValue => "invalid_value",
        }
    }

    /// Human-readable message (text mode).
    pub fn message(&self) -> &str {
        &self.message
    }
}

impl std::fmt::Display for ParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

fn unknown_command(cmd: &str) -> ParseError {
    ParseError {
        kind: ErrorKind::UnknownCommand,
        message: format!("Unknown command: {}. Run 'agent-windows --help'.", cmd),
    }
}

fn unknown_subcommand(group: &str, sub: &str, valid: &[&str]) -> ParseError {
    ParseError {
        kind: ErrorKind::UnknownSubcommand,
        message: format!(
            "Unknown {} subcommand: {} (valid: {})",
            group,
            sub,
            valid.join(", ")
        ),
    }
}

fn missing(message: impl Into<String>) -> ParseError {
    ParseError {
        kind: ErrorKind::MissingArgument,
        message: message.into(),
    }
}

fn invalid(message: impl Into<String>) -> ParseError {
    ParseError {
        kind: ErrorKind::InvalidValue,
        message: message.into(),
    }
}

/// Outcome of dispatching a parsed argv slice.
#[derive(Debug)]
pub enum Action {
    /// Send this JSON-RPC request to the daemon.
    Call { method: String, params: Value },
    /// Handle entirely locally (no IPC).
    Local(LocalAction),
    /// `wait <seconds>`: sleep locally (no IPC) and print a confirmation.
    /// Separate from `Local` (daemon lifecycle) so main() can honor
    /// --format json — the Python CLI prints `{"output": "## Wait\nSlept Ns"}`
    /// in json mode, and the old code hard-coded a text-only print + exit
    /// inside parse(), which had no access to the format flag.
    LocalSleep(f64),
    /// Client-side polling loop: repeatedly call `wait_check` until found or
    /// timeout. Can't be a single Call because it needs multiple IPC round-
    /// trips with local sleeps between them (mirrors cli/commands/wait.py).
    Wait(WaitSpec),
    /// Client-side polling loop for `wait-for`: repeatedly call
    /// `wait_for_check` with a multi-condition predicate until satisfied or
    /// timeout (mirrors cli/commands/wait.py:wait_for). Separate from `Wait`
    /// because the probe carries three fields (condition/text/window) and the
    /// success output names the condition rather than a selector/text.
    WaitFor(WaitForSpec),
    /// Print usage and exit.
    Help,
}

/// Parameters for the `wait` polling modes (selector or text).
#[derive(Debug)]
pub struct WaitSpec {
    pub probe: WaitProbe,
    pub timeout: f64,
    pub interval: f64,
}

#[derive(Debug)]
pub enum WaitProbe {
    Selector(String),
    Text(String),
}

/// Parameters for the `wait-for` polling loop.
#[derive(Debug)]
pub struct WaitForSpec {
    pub condition: String,
    pub text: String,
    pub window: String,
    pub timeout: f64,
    pub interval: f64,
}

#[derive(Debug)]
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
pub fn parse(argv: &[String], global: &GlobalFlags) -> Result<Action, ParseError> {
    if argv.is_empty() {
        return Ok(Action::Help);
    }
    let cmd = argv[0].as_str();
    // `--help` / `-h` anywhere as the first token → print help.
    if cmd == "--help" || cmd == "-h" {
        return Ok(Action::Help);
    }
    let rest = &argv[1..];

    match cmd {
        // ── Local commands ────────────────────────────────────────────────
        // `config` is NOT listed here: it is intercepted in main.rs (before
        // `parse` runs) and delegated to the Python CLI. Reaching this match
        // arm with `config` would mean that early intercept was bypassed.
        "daemon" => parse_daemon(rest),

        // ── Observation ───────────────────────────────────────────────────
        "observe" => parse_observe(rest),
        "snapshot" => parse_snapshot(rest),
        "screenshot" => parse_screenshot(rest),
        "ground" => parse_ground(rest),
        "ask-vision" => parse_ask_vision(rest),
        "detect" => parse_detect(rest),
        "get-text" => {
            let p = ParsedArgs::parse(rest, &[]);
            let selector = p.pos(0, "selector")?;
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
        "click" => parse_xy_opts(rest, "click", &["right", "double"]),
        "dblclick" => parse_xy_opts(rest, "dblclick", &["right"]),
        "hover" => parse_xy(rest, "hover"),
        "move" => parse_xy(rest, "move"),
        "mouse-down" => parse_mouse_button(rest, "mouse_down"),
        "mouse-up" => parse_mouse_button(rest, "mouse_up"),
        "drag" => parse_drag(rest),
        "scroll" => parse_scroll(rest),

        // ── Input (keyboard) ──────────────────────────────────────────────
        "type" => parse_type(rest),
        "shortcut" => {
            let p = ParsedArgs::parse(rest, &[]);
            let keys = p.pos(0, "keys")?;
            Ok(Action::Call {
                method: "shortcut".into(),
                params: json!({ "keys": keys }),
            })
        }
        // `expand`/`collapse` drive a live UIA ExpandCollapsePattern at @eN (no
        // coordinates — the element is reverse-looked-up from the snapshot). They
        // were registered in the Python CLI (input.py:154-169) and the daemon
        // (handlers/input.py:160,178) but missing from this table, so under the
        // recommended `uv tool install` path they failed with "Unknown command".
        "expand" => parse_selector_only(rest, "expand"),
        "collapse" => parse_selector_only(rest, "collapse"),

        // ── System groups ─────────────────────────────────────────────────
        "app" => parse_app(rest),
        "shell" => {
            // --timeout/-t (seconds) overrides the shell.timeout config default.
            // Forwarded only when > 0 (Python system.py:66-67); 0 means "use the
            // config default" (the daemon treats a 0/None timeout that way).
            let p = ParsedArgs::parse(rest, &["timeout", "t"]);
            let command = p.pos(0, "command")?;
            let timeout: f64 = opt_float(&p, &["timeout", "t"], 0.0)?;
            let mut params = Map::new();
            params.insert("command".into(), command.into());
            if timeout > 0.0 {
                params.insert("timeout".into(), timeout.into());
            }
            Ok(Action::Call {
                method: "shell".into(),
                params: Value::Object(params),
            })
        }
        "fs" => parse_fs(rest),
        "clipboard" => parse_clipboard(rest),
        "process" => parse_process(rest),

        // ── Control ───────────────────────────────────────────────────────
        "wait" => parse_wait(rest),
        "wait-for" => parse_wait_for(rest),
        "batch" => parse_batch(rest, global),

        // ── Batch interaction ─────────────────────────────────────────────
        "multi-select" => parse_multi_select(rest),
        "multi-edit" => parse_multi_edit(rest),

        // ── System extras ─────────────────────────────────────────────────
        // `notify` and `registry` route to daemon handlers; they were missing
        // from the launcher, so the skill's documented commands failed with
        // "Unknown command" under the recommended `uv tool install` path.
        "notify" => parse_notify(rest),
        "registry" => parse_registry(rest),

        // ── Diagnostic ────────────────────────────────────────────────────
        "ping" => Ok(Action::Call { method: "ping".into(), params: json!({}) }),

        _ => Err(unknown_command(cmd)),
    }
}

// ── Global flags parsed before the command ─────────────────────────────────

#[derive(Default, Clone)]
pub struct GlobalFlags {
    pub no_daemon: bool,
    pub format_json: bool,
    pub debug: bool,
    /// `--config <path>` in the global-flag prefix: an alternate config.toml.
    /// None when absent (use the default ~/.agent-windows/config.toml). When
    /// set, the launcher appends `--config <path>` when spawning the daemon so
    /// the daemon reads the same file.
    pub config: Option<String>,
    /// `--no-daemon` appeared AFTER the command name (tail position). The
    /// launcher only honors --no-daemon in the prefix (where it triggers
    /// Python-CLI delegation); in the tail it's a usage error — the Python CLI
    /// rejects a post-command --no-daemon as "No such option", and silently
    /// running via the daemon would ignore the user's intent. `main()` checks
    /// this and emits a clear error.
    pub no_daemon_in_tail: bool,
}

// ── Helpers: tiny option parser ─────────────────────────────────────────────
//
// A single pass turns `rest` into one consistent view of positionals vs.
// options. The old design had THREE independent passes (split_args,
// opt_value, require_positional) that disagreed about what counts as a
// positional — a flag's value (e.g. the `800` in `--width 800`) isn't a
// positional, but split_args counted it as one, so `require_positional`
// could grab the wrong token. `ParsedArgs` fixes that by knowing, in
// advance, which flags take a value and consuming it as part of the option.

/// One pass over `rest`, producing positionals + option values + boolean flags.
///
/// `value_opts` lists every option name (long, and single-char short) that
/// consumes the following token as its value, e.g. `["output", "o", "monitor"]`.
/// A name appears once regardless of long/short form; both `--output x` and
/// `-o x` work, as do `--output=x` / `-o=x`. Options not in `value_opts` are
/// treated as boolean flags (presence only). Anything not recognized as an
/// option is a positional.
///
/// Known limitations vs. Typer (intentional — none of our commands need them):
/// - No `--` end-of-options terminator (Typer supports it). Our positionals
///   never start with `-`-plus-letter in a way that would need escaping.
/// - A repeated value-option takes the last occurrence (last-writer-wins).
struct ParsedArgs {
    positionals: Vec<String>,
    /// Canonical (first-listed) name → value, for value-taking options.
    values: HashMap<String, String>,
    /// Canonical names of present boolean flags.
    flags: Vec<String>,
}

impl ParsedArgs {
    fn parse(rest: &[String], value_opts: &[&str]) -> ParsedArgs {
        // Map every accepted spelling → canonical name (first in the group).
        // A group like ["output", "o"] means both --output and -o map to "output".
        let mut spelling_to_canon: HashMap<String, String> = Default::default();
        for canon in value_opts {
            spelling_to_canon.insert(format!("--{}", canon), (*canon).into());
            if canon.len() == 1 {
                spelling_to_canon.insert(format!("-{}", canon), (*canon).into());
            }
        }

        let mut positionals = Vec::new();
        let mut values: HashMap<String, String> = Default::default();
        let mut flags = Vec::new();

        let mut i = 0;
        while i < rest.len() {
            let a = &rest[i];

            // `--name=value` / `-n=value` form. Check against every value-opt
            // spelling so an unknown `--x=y` falls through to positional/flag.
            if let Some(eq) = a.find('=') {
                let (lhs, rhs) = a.split_at(eq);
                let rhs = &rhs[1..]; // drop '='
                if let Some(canon) = spelling_to_canon.get(lhs) {
                    values.insert(canon.clone(), rhs.into());
                    i += 1;
                    continue;
                }
            }

            // Exact `--name` / `-n` form.
            if let Some(canon) = spelling_to_canon.get(a) {
                // Value-taking option → consume the next token as its value.
                // Missing value is recorded as empty-string (the command may
                // still surface a clear error); we don't panic.
                let v = rest.get(i + 1).map(|s| s.clone()).unwrap_or_default();
                values.insert(canon.clone(), v);
                i += 2;
                continue;
            }

            // Boolean flag: `--name` where name isn't a value-opt. We still
            // accept the short `-n` form. Record under the bare name.
            if let Some(name) = a.strip_prefix("--") {
                flags.push(name.into());
                i += 1;
                continue;
            }
            // Short `-n` form. Exclude a leading digit so negative numbers
            // like `-1` (a valid positional, e.g. `click -1 -1` on a multi-
            // monitor desktop) aren't swallowed as a flag. No legal short
            // option here is a digit (they're all letters: o/s/i/m/…).
            if a.starts_with('-')
                && a.len() == 2
                && a != "-"
                && !a.as_bytes()[1].is_ascii_digit()
            {
                flags.push(a[1..].into());
                i += 1;
                continue;
            }

            // Otherwise it's a positional.
            positionals.push(a.clone());
            i += 1;
        }

        ParsedArgs { positionals, values, flags }
    }

    /// The positional at `idx`, or a MissingArgument error.
    fn pos(&self, idx: usize, name: &str) -> Result<String, ParseError> {
        self.positionals.get(idx).cloned().ok_or_else(|| {
            missing(format!("Missing required positional argument: {}", name))
        })
    }

    /// First present value-option among `names` (long/short treated equal).
    fn value(&self, names: &[&str]) -> Option<&str> {
        names.iter().find_map(|n| self.values.get(*n).map(|s| s.as_str()))
    }

    /// True if any of the boolean flag names is present (long/short equal).
    fn flag(&self, names: &[&str]) -> bool {
        names.iter().any(|n| self.flags.iter().any(|f| f == n))
    }
}

/// An optional integer option: return `default` when absent, but reject a
/// *present but malformed* value with `invalid_value` (so an agent consumer
/// can rely on the typed error contract to detect a bad argument, instead of
/// silently getting the default). `name` is the first spelling, used for the
/// error message.
///
/// Note: a trailing value-option with no following token (e.g. `scroll 5 5
/// --amount`) is recorded by `ParsedArgs` as the empty string, not as absent.
/// We treat empty as "use the default" (the user wrote the flag but supplied
/// no value) and only reject a *non-empty* unparseable value.
fn opt_int(p: &ParsedArgs, names: &[&str], default: i64) -> Result<i64, ParseError> {
    match p.value(names) {
        None | Some("") => Ok(default),
        Some(v) => v.parse().map_err(|_| {
            invalid(format!("--{} must be an integer", names[0]))
        }),
    }
}

/// Same as `opt_int` but for floating-point options. Rejects non-finite
/// values (NaN/±Infinity): Rust's `str::parse::<f64>()` accepts `"nan"` and
/// `"inf"`, and a NaN `--timeout` would make `start.elapsed() >= timeout`
/// always false (an infinite poll loop in `run_wait`).
fn opt_float(p: &ParsedArgs, names: &[&str], default: f64) -> Result<f64, ParseError> {
    match p.value(names) {
        None | Some("") => Ok(default),
        Some(v) => v.parse::<f64>().map_err(|_| {
            invalid(format!("--{} must be a number", names[0]))
        }).and_then(|f| {
            if f.is_finite() {
                Ok(f)
            } else {
                Err(invalid(format!(
                    "--{} must be a finite number (not nan/inf)", names[0]
                )))
            }
        }),
    }
}

fn parse_xy(rest: &[String], method: &str) -> Result<Action, ParseError> {
    // `--selector/-s @eN` overrides X Y (mirrors input.py:_xy_or_selector). The
    // selector must start with "@e"; an empty/bare value is treated as a bad
    // selector so the daemon surfaces a clear ELEMENT_NOT_FOUND rather than the
    // launcher silently sending an empty string.
    let p = ParsedArgs::parse(rest, &["selector", "s"]);
    if let Some(sel) = p.value(&["selector", "s"]) {
        if sel.is_empty() {
            return Err(invalid("--selector/-s requires an @eN ref"));
        }
        return Ok(Action::Call {
            method: method.into(),
            params: json!({ "selector": sel }),
        });
    }
    let x: i64 = p.pos(0, "x")?.parse().map_err(|_| invalid("x must be an integer"))?;
    let y: i64 = p.pos(1, "y")?.parse().map_err(|_| invalid("y must be an integer"))?;
    Ok(Action::Call {
        method: method.into(),
        params: json!({ "x": x, "y": y }),
    })
}

fn parse_xy_opts(rest: &[String], method: &str, flags: &[&str]) -> Result<Action, ParseError> {
    let p = ParsedArgs::parse(rest, &["selector", "s"]);
    let mut params = Map::new();
    if let Some(sel) = p.value(&["selector", "s"]) {
        if sel.is_empty() {
            return Err(invalid("--selector/-s requires an @eN ref"));
        }
        params.insert("selector".into(), sel.into());
    } else {
        let x: i64 = p.pos(0, "x")?.parse().map_err(|_| invalid("x must be an integer"))?;
        let y: i64 = p.pos(1, "y")?.parse().map_err(|_| invalid("y must be an integer"))?;
        params.insert("x".into(), x.into());
        params.insert("y".into(), y.into());
    }
    for f in flags {
        params.insert((*f).into(), p.flag(&[f]).into());
    }
    Ok(Action::Call { method: method.into(), params: Value::Object(params) })
}

fn parse_mouse_button(rest: &[String], method: &str) -> Result<Action, ParseError> {
    let p = ParsedArgs::parse(rest, &["button", "selector", "s"]);
    let mut params = Map::new();
    if let Some(sel) = p.value(&["selector", "s"]) {
        if sel.is_empty() {
            return Err(invalid("--selector/-s requires an @eN ref"));
        }
        params.insert("selector".into(), sel.into());
    } else {
        let x: i64 = p.pos(0, "x")?.parse().map_err(|_| invalid("x must be an integer"))?;
        let y: i64 = p.pos(1, "y")?.parse().map_err(|_| invalid("y must be an integer"))?;
        params.insert("x".into(), x.into());
        params.insert("y".into(), y.into());
    }
    let button = p.value(&["button"]).unwrap_or("left").to_string();
    params.insert("button".into(), button.into());
    Ok(Action::Call { method: method.into(), params: Value::Object(params) })
}

fn parse_drag(rest: &[String]) -> Result<Action, ParseError> {
    let p = ParsedArgs::parse(rest, &[]);
    let x1: i64 = p.pos(0, "x1")?.parse().map_err(|_| invalid("x1 must be an integer"))?;
    let y1: i64 = p.pos(1, "y1")?.parse().map_err(|_| invalid("y1 must be an integer"))?;
    let x2: i64 = p.pos(2, "x2")?.parse().map_err(|_| invalid("x2 must be an integer"))?;
    let y2: i64 = p.pos(3, "y2")?.parse().map_err(|_| invalid("y2 must be an integer"))?;
    let mut params = Map::new();
    params.insert("x1".into(), x1.into());
    params.insert("y1".into(), y1.into());
    params.insert("x2".into(), x2.into());
    params.insert("y2".into(), y2.into());
    params.insert("right".into(), p.flag(&["right"]).into());
    Ok(Action::Call { method: "drag".into(), params: Value::Object(params) })
}

fn parse_scroll(rest: &[String]) -> Result<Action, ParseError> {
    let p = ParsedArgs::parse(rest, &["direction", "amount", "selector", "s"]);
    let direction = p.value(&["direction"]).unwrap_or("down").to_string();
    let amount: i64 = opt_int(&p, &["amount"], 3)?;
    if let Some(sel) = p.value(&["selector", "s"]) {
        if sel.is_empty() {
            return Err(invalid("--selector/-s requires an @eN ref"));
        }
        return Ok(Action::Call {
            method: "scroll".into(),
            params: json!({ "selector": sel, "direction": direction, "amount": amount }),
        });
    }
    let x: i64 = p.pos(0, "x")?.parse().map_err(|_| invalid("x must be an integer"))?;
    let y: i64 = p.pos(1, "y")?.parse().map_err(|_| invalid("y must be an integer"))?;
    Ok(Action::Call {
        method: "scroll".into(),
        params: json!({ "x": x, "y": y, "direction": direction, "amount": amount }),
    })
}

fn parse_observe(rest: &[String]) -> Result<Action, ParseError> {
    let p = ParsedArgs::parse(rest, &["ground", "display", "d"]);
    let snapshot = p.flag(&["snapshot"]);
    let screenshot = p.flag(&["screenshot"]);
    let ground = p.value(&["ground"]).unwrap_or("").to_string();
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
    if let Some(disp) = collect_display(rest)? {
        params.insert("display".into(), disp);
    }
    Ok(Action::Call { method: "observe".into(), params: Value::Object(params) })
}

/// `snapshot` used to forward an empty params object; now it also forwards the
/// multi-display filter (`--display`/`-d`, repeatable) so the launcher matches
/// the Python CLI (TODO #9-B). The daemon's `parse_display_selection` accepts a
/// single int or a list; we always send a list (a single `-d 0` → `[0]`).
fn parse_snapshot(rest: &[String]) -> Result<Action, ParseError> {
    let mut params = Map::new();
    if let Some(disp) = collect_display(rest)? {
        params.insert("display".into(), disp);
    }
    Ok(Action::Call { method: "snapshot".into(), params: Value::Object(params) })
}

/// Collect every `--display N` / `-d N` occurrence into a de-duplicated JSON
/// array of non-negative ints. `ParsedArgs` is last-writer-wins, so it can't
/// represent the repeatable form; we walk the raw argv here. `-d=0` and
/// `--display=0` work too. Returns None when no display flag was given (the
/// daemon treats a missing `display` as "no filter" — backward compatible).
///
/// The daemon's `parse_display_selection` also accepts a single int, but
/// sending a list uniformly is simpler and accepted in all cases.
fn collect_display(rest: &[String]) -> Result<Option<Value>, ParseError> {
    let mut indices: Vec<i64> = Vec::new();
    let mut i = 0;
    while i < rest.len() {
        let a = &rest[i];
        // `--display=N` / `-d=N`
        if let Some(rhs) = a.strip_prefix("--display=").or_else(|| a.strip_prefix("-d=")) {
            push_display(&mut indices, rhs)?;
            i += 1;
            continue;
        }
        // `--display N` / `-d N` (two tokens)
        if a == "--display" || a == "-d" {
            let v = rest.get(i + 1).map(|s| s.as_str()).unwrap_or("");
            if v.is_empty() {
                return Err(invalid("--display/-d requires a non-negative integer"));
            }
            push_display(&mut indices, v)?;
            i += 2;
            continue;
        }
        i += 1;
    }
    if indices.is_empty() {
        Ok(None)
    } else {
        Ok(Some(Value::Array(indices.into_iter().map(Value::from).collect())))
    }
}

/// Parse one display index and push it if not already present (the daemon
/// de-duplicates too, but doing it here keeps the forwarded payload minimal).
fn push_display(indices: &mut Vec<i64>, raw: &str) -> Result<(), ParseError> {
    let n: i64 = raw.parse().map_err(|_| {
        invalid(format!("--display/-d must be a non-negative integer, got {:?}", raw))
    })?;
    if n < 0 {
        return Err(invalid(format!("--display/-d must be non-negative, got {}", n)));
    }
    if !indices.contains(&n) {
        indices.push(n);
    }
    Ok(())
}

fn parse_screenshot(rest: &[String]) -> Result<Action, ParseError> {
    // value opts: --annotate is a boolean flag (presence only); --grid-w/--grid-h
    // take integer values. Mirrors observe.py:52-61. Param-name mapping: the
    // Python CLI renames --annotate→use_annotation, --grid-w→width_reference_line,
    // --grid-h→height_reference_line (observe.py:69-74); the daemon handler reads
    // those exact keys (handlers/screenshot.py:74-77,101-113).
    let p = ParsedArgs::parse(rest, &["output", "o", "monitor", "selector", "s", "bbox", "grid-w", "grid-h"]);
    let output = p.value(&["output", "o"]).unwrap_or("").to_string();
    let monitor: i64 = opt_int(&p, &["monitor"], -1)?;
    let mut params = Map::new();
    params.insert("output".into(), output.into());
    params.insert("monitor".into(), monitor.into());
    if let Some(sel) = p.value(&["selector", "s"]) {
        params.insert("selector".into(), sel.into());
    }
    if let Some(bbox) = p.value(&["bbox"]) {
        let nums: Result<Vec<i64>, ParseError> = bbox
            .split(',')
            .map(|n| n.parse().map_err(|_| invalid(format!("invalid bbox value: {}", n))))
            .collect();
        params.insert("bbox".into(), Value::Array(nums?.into_iter().map(Value::from).collect()));
    }
    // --annotate → use_annotation. Only forwarded when present (observe.py:69-70).
    // grid lines forward only when their value is non-zero, and only alongside
    // --annotate (the daemon ignores them otherwise; observe.py:71-74 gates them
    // on `if annotate`).
    if p.flag(&["annotate"]) {
        params.insert("use_annotation".into(), true.into());
        if let Some(gw) = p.value(&["grid-w"]) {
            let gw: i64 = gw.parse().map_err(|_| invalid("--grid-w must be an integer"))?;
            if gw > 0 {
                params.insert("width_reference_line".into(), gw.into());
            }
        }
        if let Some(gh) = p.value(&["grid-h"]) {
            let gh: i64 = gh.parse().map_err(|_| invalid("--grid-h must be an integer"))?;
            if gh > 0 {
                params.insert("height_reference_line".into(), gh.into());
            }
        }
    }
    Ok(Action::Call { method: "screenshot".into(), params: Value::Object(params) })
}

fn parse_ground(rest: &[String]) -> Result<Action, ParseError> {
    let p = ParsedArgs::parse(rest, &[]);
    let description = p.pos(0, "description")?;
    let mut params = Map::new();
    params.insert("description".into(), description.into());
    if p.flag(&["single-pass"]) {
        params.insert("single_pass".into(), true.into());
    }
    Ok(Action::Call { method: "ground".into(), params: Value::Object(params) })
}

fn parse_ask_vision(rest: &[String]) -> Result<Action, ParseError> {
    let p = ParsedArgs::parse(rest, &["image", "i", "model", "m", "output", "o"]);
    let prompt = p.pos(0, "prompt")?;
    let mut params = Map::new();
    params.insert("prompt".into(), prompt.into());
    if let Some(v) = p.value(&["image", "i"]) { params.insert("image".into(), v.into()); }
    if let Some(v) = p.value(&["model", "m"]) { params.insert("model".into(), v.into()); }
    if let Some(v) = p.value(&["output", "o"]) { params.insert("output".into(), v.into()); }
    Ok(Action::Call { method: "ask-vision".into(), params: Value::Object(params) })
}

fn parse_detect(rest: &[String]) -> Result<Action, ParseError> {
    let p = ParsedArgs::parse(rest, &["max-targets", "image", "i", "model", "m", "output", "o"]);
    let description = p.pos(0, "description")?;
    let max_targets: i64 = opt_int(&p, &["max-targets"], 20)?;
    let mut params = Map::new();
    params.insert("description".into(), description.into());
    params.insert("max_targets".into(), max_targets.into());
    if let Some(v) = p.value(&["image", "i"]) { params.insert("image".into(), v.into()); }
    if let Some(v) = p.value(&["model", "m"]) { params.insert("model".into(), v.into()); }
    if let Some(v) = p.value(&["output", "o"]) { params.insert("output".into(), v.into()); }
    Ok(Action::Call { method: "detect".into(), params: Value::Object(params) })
}

fn parse_highlight(rest: &[String]) -> Result<Action, ParseError> {
    let p = ParsedArgs::parse(rest, &["label", "l", "color", "c", "image", "i", "output", "o"]);
    let x1: i64 = p.pos(0, "x1")?.parse().map_err(|_| invalid("x1 must be an integer"))?;
    let y1: i64 = p.pos(1, "y1")?.parse().map_err(|_| invalid("y1 must be an integer"))?;
    let x2: i64 = p.pos(2, "x2")?.parse().map_err(|_| invalid("x2 must be an integer"))?;
    let y2: i64 = p.pos(3, "y2")?.parse().map_err(|_| invalid("y2 must be an integer"))?;
    let mut params = Map::new();
    params.insert("x1".into(), x1.into());
    params.insert("y1".into(), y1.into());
    params.insert("x2".into(), x2.into());
    params.insert("y2".into(), y2.into());
    if let Some(v) = p.value(&["label", "l"]) { params.insert("label".into(), v.into()); }
    // Python only sends color when it differs from the default ("red"); mirror
    // that so the daemon's own default applies when the user omits --color.
    if let Some(color) = p.value(&["color", "c"]) {
        if color != "red" { params.insert("color".into(), color.into()); }
    }
    if let Some(v) = p.value(&["image", "i"]) { params.insert("image".into(), v.into()); }
    if let Some(v) = p.value(&["output", "o"]) { params.insert("output".into(), v.into()); }
    Ok(Action::Call { method: "highlight".into(), params: Value::Object(params) })
}

fn parse_diff(rest: &[String]) -> Result<Action, ParseError> {
    let p = ParsedArgs::parse(rest, &["threshold", "t", "compare", "c", "output", "o"]);
    let baseline = p.pos(0, "baseline")?;
    let threshold: i64 = opt_int(&p, &["threshold", "t"], 30)?;
    let mut params = Map::new();
    params.insert("baseline".into(), baseline.into());
    params.insert("threshold".into(), threshold.into());
    if let Some(v) = p.value(&["compare", "c"]) { params.insert("compare".into(), v.into()); }
    if let Some(v) = p.value(&["output", "o"]) { params.insert("output".into(), v.into()); }
    Ok(Action::Call { method: "diff".into(), params: Value::Object(params) })
}

fn parse_is(rest: &[String]) -> Result<Action, ParseError> {
    let sub = rest.first().ok_or_else(|| missing("Missing 'visible' or 'enabled'"))?;
    let check = match sub.as_str() {
        "visible" => "visible",
        "enabled" => "enabled",
        _ => return Err(unknown_subcommand("is", sub, &["visible", "enabled"])),
    };
    let p = ParsedArgs::parse(&rest[1..], &[]);
    let selector = p.pos(0, "selector")?;
    Ok(Action::Call {
        method: "element_state".into(),
        params: json!({ "selector": selector, "check": check }),
    })
}

fn parse_type(rest: &[String]) -> Result<Action, ParseError> {
    // value opts: --at (x,y), --at-selector/--ats (@eN, overrides --at), --caret
    // (start|idle|end). The daemon's `type` handler reads at_x/at_y OR
    // at_selector, and caret_position (input.py:55).
    let p = ParsedArgs::parse(rest, &["at", "at-selector", "ats", "caret"]);
    let text = p.pos(0, "text")?;
    let mut params = Map::new();
    params.insert("text".into(), text.into());
    if let Some(sel) = p.value(&["at-selector", "ats"]) {
        if sel.is_empty() {
            return Err(invalid("--at-selector requires an @eN ref"));
        }
        params.insert("at_selector".into(), sel.into());
    } else if let Some(at) = p.value(&["at"]) {
        // `--at X,Y` → at_x, at_y (mirrors input.py:59-62)
        let parts: Vec<&str> = at.split(',').collect();
        if parts.len() != 2 {
            return Err(invalid(format!("--at expects X,Y, got: {}", at)));
        }
        let x: i64 = parts[0].parse().map_err(|_| invalid("--at x must be integer"))?;
        let y: i64 = parts[1].parse().map_err(|_| invalid("--at y must be integer"))?;
        params.insert("at_x".into(), x.into());
        params.insert("at_y".into(), y.into());
    }
    params.insert("enter".into(), p.flag(&["enter"]).into());
    params.insert("clear".into(), p.flag(&["clear"]).into());
    // caret defaults to "idle"; the daemon maps the value through unchanged.
    let caret = p.value(&["caret"]).unwrap_or("idle").to_string();
    params.insert("caret_position".into(), caret.into());
    Ok(Action::Call { method: "type".into(), params: Value::Object(params) })
}

/// `expand`/`collapse @eN`: forward `{"selector": sel}`. Like `get-text`, these
/// take a single positional `selector` and nothing else (no coordinates — the
/// daemon's ExpandCollapsePattern handler reverse-looks-up the element from the
/// snapshot). Mirrors input.py:154-169.
fn parse_selector_only(rest: &[String], method: &str) -> Result<Action, ParseError> {
    let p = ParsedArgs::parse(rest, &[]);
    let selector = p.pos(0, "selector")?;
    Ok(Action::Call {
        method: method.into(),
        params: json!({ "selector": selector }),
    })
}

fn parse_app(rest: &[String]) -> Result<Action, ParseError> {
    const VALID: &[&str] = &["launch", "switch", "list", "resize"];
    let sub = rest.first()
        .ok_or_else(|| missing("Missing app subcommand (launch/switch/list/resize)"))?;
    let args = &rest[1..];
    match sub.as_str() {
        "launch" => {
            let p = ParsedArgs::parse(args, &[]);
            let name_or_path = p.pos(0, "name_or_path")?;
            // remaining positionals become app args
            let app_args: Vec<String> = p.positionals.iter().skip(1).cloned().collect();
            Ok(Action::Call {
                method: "app_launch".into(),
                params: json!({ "name_or_path": name_or_path, "args": app_args }),
            })
        }
        "switch" => {
            let p = ParsedArgs::parse(args, &[]);
            let name = p.pos(0, "name_or_title")?;
            Ok(Action::Call {
                method: "app_switch".into(),
                params: json!({ "name_or_title": name }),
            })
        }
        "list" => Ok(Action::Call { method: "app_list".into(), params: json!({}) }),
        "resize" => {
            let p = ParsedArgs::parse(args, &["width", "height", "x", "y"]);
            let name = p.pos(0, "name_or_title")?;
            let width: i64 = p.value(&["width"]).ok_or_else(|| missing("--width required"))?
                .parse().map_err(|_| invalid("--width must be integer"))?;
            let height: i64 = p.value(&["height"]).ok_or_else(|| missing("--height required"))?
                .parse().map_err(|_| invalid("--height must be integer"))?;
            let x: i64 = opt_int(&p, &["x"], -1)?;
            let y: i64 = opt_int(&p, &["y"], -1)?;
            Ok(Action::Call {
                method: "app_resize".into(),
                params: json!({ "name_or_title": name, "width": width, "height": height, "x": x, "y": y }),
            })
        }
        _ => Err(unknown_subcommand("app", sub, VALID)),
    }
}

fn parse_fs(rest: &[String]) -> Result<Action, ParseError> {
    // Eight subcommands; mirrors the Python `fs` Typer group (system.py:76-187).
    // read/write/list existed first; copy/move/delete/search/info were added for
    // CLI/launcher parity (the daemon already implemented them — handlers/
    // system.py:120-167 — but the launcher rejected them as unknown_subcommand).
    const VALID: &[&str] = &["read", "write", "list", "copy", "move", "delete", "search", "info"];
    let sub = rest.first()
        .ok_or_else(|| missing("Missing fs subcommand (read/write/list/copy/move/delete/search/info)"))?;
    let args = &rest[1..];
    match sub.as_str() {
        "read" => {
            // offset/limit/encoding forward only when non-default/non-empty
            // (Python system.py:86-91): offset>0, limit>0, encoding != "".
            let p = ParsedArgs::parse(args, &["offset", "limit", "encoding"]);
            let path = p.pos(0, "path")?;
            let mut params = Map::new();
            params.insert("path".into(), path.into());
            let offset: i64 = opt_int(&p, &["offset"], 0)?;
            if offset > 0 {
                params.insert("offset".into(), offset.into());
            }
            let limit: i64 = opt_int(&p, &["limit"], 0)?;
            if limit > 0 {
                params.insert("limit".into(), limit.into());
            }
            if let Some(enc) = p.value(&["encoding"]) {
                if !enc.is_empty() {
                    params.insert("encoding".into(), enc.into());
                }
            }
            Ok(Action::Call { method: "fs_read".into(), params: Value::Object(params) })
        }
        "write" => {
            // --append forwards only when present (Python system.py:104-105).
            let p = ParsedArgs::parse(args, &[]);
            let path = p.pos(0, "path")?;
            let content = p.pos(1, "content")?;
            let mut params = Map::new();
            params.insert("path".into(), path.into());
            params.insert("content".into(), content.into());
            if p.flag(&["append"]) {
                params.insert("append".into(), true.into());
            }
            Ok(Action::Call {
                method: "fs_write".into(),
                params: Value::Object(params),
            })
        }
        "list" => {
            // pattern/recursive/show-hidden forward only when non-default
            // (Python system.py:119-124): pattern != "", flags present.
            let p = ParsedArgs::parse(args, &["pattern"]);
            let path = p.pos(0, "path")?;
            let mut params = Map::new();
            params.insert("path".into(), path.into());
            if let Some(pat) = p.value(&["pattern"]) {
                if !pat.is_empty() {
                    params.insert("pattern".into(), pat.into());
                }
            }
            if p.flag(&["recursive"]) {
                params.insert("recursive".into(), true.into());
            }
            if p.flag(&["show-hidden"]) {
                params.insert("show_hidden".into(), true.into());
            }
            Ok(Action::Call { method: "fs_list".into(), params: Value::Object(params) })
        }
        // `copy`/`move SRC DEST [--overwrite]`. overwrite forwards only when the
        // flag is present (Python system.py:136-138, 150-152).
        "copy" | "move" => {
            let method = if sub == "copy" { "fs_copy" } else { "fs_move" };
            let p = ParsedArgs::parse(args, &[]);
            let path = p.pos(0, "path")?;
            let destination = p.pos(1, "destination")?;
            let mut params = Map::new();
            params.insert("path".into(), path.into());
            params.insert("destination".into(), destination.into());
            if p.flag(&["overwrite"]) {
                params.insert("overwrite".into(), true.into());
            }
            Ok(Action::Call { method: method.into(), params: Value::Object(params) })
        }
        // `delete PATH [--recursive]` (non-empty dirs need recursive).
        "delete" => {
            let p = ParsedArgs::parse(args, &[]);
            let path = p.pos(0, "path")?;
            let mut params = Map::new();
            params.insert("path".into(), path.into());
            if p.flag(&["recursive"]) {
                params.insert("recursive".into(), true.into());
            }
            Ok(Action::Call { method: "fs_delete".into(), params: Value::Object(params) })
        }
        // `search PATH PATTERN [--no-recursive]`. Inverted vs. the others: the
        // daemon defaults recursive=True (handlers/system.py:159), so we forward
        // `recursive=false` only when --no-recursive is given, and forward
        // nothing otherwise (letting the daemon's True default apply). Python
        // always sends `recursive = not no_recursive` (system.py:177); we mirror
        // its omission of the default to avoid clobbering a config default.
        "search" => {
            let p = ParsedArgs::parse(args, &[]);
            let path = p.pos(0, "path")?;
            let pattern = p.pos(1, "pattern")?;
            let mut params = Map::new();
            params.insert("path".into(), path.into());
            params.insert("pattern".into(), pattern.into());
            if p.flag(&["no-recursive"]) {
                params.insert("recursive".into(), false.into());
            }
            Ok(Action::Call { method: "fs_search".into(), params: Value::Object(params) })
        }
        // `info PATH` — metadata only, no options.
        "info" => {
            let p = ParsedArgs::parse(args, &[]);
            let path = p.pos(0, "path")?;
            Ok(Action::Call { method: "fs_info".into(), params: json!({ "path": path }) })
        }
        _ => Err(unknown_subcommand("fs", sub, VALID)),
    }
}

fn parse_clipboard(rest: &[String]) -> Result<Action, ParseError> {
    const VALID: &[&str] = &["get", "set"];
    let sub = rest.first()
        .ok_or_else(|| missing("Missing clipboard subcommand (get/set)"))?;
    let args = &rest[1..];
    match sub.as_str() {
        "get" => {
            let p = ParsedArgs::parse(args, &[]);
            let mut params = Map::new();
            if p.flag(&["keyboard", "k"]) {
                params.insert("keyboard".into(), true.into());
            }
            Ok(Action::Call { method: "clipboard_get".into(), params: Value::Object(params) })
        }
        "set" => {
            let p = ParsedArgs::parse(args, &[]);
            let text = p.pos(0, "text")?;
            let mut params = Map::new();
            params.insert("text".into(), text.into());
            if p.flag(&["keyboard", "k"]) {
                params.insert("keyboard".into(), true.into());
            }
            Ok(Action::Call { method: "clipboard_set".into(), params: Value::Object(params) })
        }
        _ => Err(unknown_subcommand("clipboard", sub, VALID)),
    }
}

fn parse_process(rest: &[String]) -> Result<Action, ParseError> {
    const VALID: &[&str] = &["list", "kill"];
    let sub = rest.first()
        .ok_or_else(|| missing("Missing process subcommand (list/kill)"))?;
    let args = &rest[1..];
    match sub.as_str() {
        // sort_by/limit/name forward only when they differ from the defaults
        // (Python system.py:304-309): sort_by != "memory", limit != 0, name != "".
        // Omitting the defaults lets a config default (if any) apply instead of
        // being clobbered by the launcher.
        "list" => {
            let p = ParsedArgs::parse(args, &["sort-by", "limit", "name"]);
            let mut params = Map::new();
            let sort_by = p.value(&["sort-by"]).unwrap_or("memory").to_string();
            if sort_by != "memory" {
                params.insert("sort_by".into(), sort_by.into());
            }
            let limit: i64 = opt_int(&p, &["limit"], 0)?;
            if limit != 0 {
                params.insert("limit".into(), limit.into());
            }
            if let Some(name) = p.value(&["name"]) {
                if !name.is_empty() {
                    params.insert("name".into(), name.into());
                }
            }
            Ok(Action::Call { method: "process_list".into(), params: Value::Object(params) })
        }
        "kill" => {
            let p = ParsedArgs::parse(args, &[]);
            let pid_or_name = p.pos(0, "pid_or_name")?;
            let mut params = Map::new();
            params.insert("pid_or_name".into(), pid_or_name.into());
            if p.flag(&["force"]) {
                params.insert("force".into(), true.into());
            }
            Ok(Action::Call { method: "process_kill".into(), params: Value::Object(params) })
        }
        _ => Err(unknown_subcommand("process", sub, VALID)),
    }
}

fn parse_wait(rest: &[String]) -> Result<Action, ParseError> {
    // Three modes (mirrors cli/commands/wait.py):
    //   wait <seconds>     → if seconds <= 0, error; else LOCAL sleep (no IPC)
    //   wait @eN [...]     → client-side polling via wait_check
    //   wait --text "..."  → client-side polling via wait_check
    let p = ParsedArgs::parse(rest, &["text", "timeout", "t", "interval"]);
    let text_mode = p.value(&["text"]).map(|t| WaitProbe::Text(t.into()));
    let timeout: f64 = opt_float(&p, &["timeout", "t"], 30.0)?;
    let interval: f64 = opt_float(&p, &["interval"], 0.5)?;

    let t0 = p.positionals.first().map(|s| s.as_str()).unwrap_or("");

    // Selector mode: wait @eN
    if let Some(sel) = t0.strip_prefix("@e") {
        if !sel.is_empty() {
            return Ok(Action::Wait(WaitSpec {
                probe: WaitProbe::Selector(t0.into()),
                timeout,
                interval,
            }));
        }
    }

    // Text mode: wait --text "..."
    if let Some(text) = text_mode {
        if !t0.is_empty() {
            return Err(invalid("cannot specify both a target and --text"));
        }
        return Ok(Action::Wait(WaitSpec {
            probe: text,
            timeout,
            interval,
        }));
    }

    // Numeric sleep — return a local-sleep action. main() performs the actual
    // sleep and formats the output honoring --format json (the Python CLI prints
    // `{"output": "## Wait\nSlept Ns"}` in json mode). We don't sleep/print here
    // because parse() has no access to the format flag, and a print+exit inside
    // parse would bypass json mode (the original bug).
    let secs: f64 = t0.parse().map_err(|_| {
        invalid(format!("invalid target {:?} (expected seconds, @eN selector, or --text)", t0))
    })?;
    if secs <= 0.0 {
        return Err(invalid("provide a positive number of seconds, a selector, or --text"));
    }
    Ok(Action::LocalSleep(secs))
}

/// `wait-for <condition> [--text S] [--window W] [--timeout N] [--interval N]`.
/// Polls `wait_for_check` client-side (mirrors cli/commands/wait.py:wait_for).
/// The condition is validated server-side on the first probe; we forward it
/// verbatim rather than duplicating the alias table here (normalize_condition
/// in core/wait_for.py lowercases, maps aliases like `enabled`→
/// `element_enabled`, and raises INVALID_PARAMS for an unknown one).
fn parse_wait_for(rest: &[String]) -> Result<Action, ParseError> {
    let p = ParsedArgs::parse(rest, &["text", "window", "timeout", "t", "interval"]);
    let condition = p.pos(0, "condition")?;
    let text = p.value(&["text"]).unwrap_or("").to_string();
    let window = p.value(&["window"]).unwrap_or("").to_string();
    let timeout: f64 = opt_float(&p, &["timeout", "t"], 30.0)?;
    let interval: f64 = opt_float(&p, &["interval"], 0.5)?;
    Ok(Action::WaitFor(WaitForSpec { condition, text, window, timeout, interval }))
}

fn parse_batch(rest: &[String], _global: &GlobalFlags) -> Result<Action, ParseError> {
    let p = ParsedArgs::parse(rest, &["file", "f", "delay", "d"]);
    // --file / stdin / positional JSON
    let raw = if let Some(path) = p.value(&["file", "f"]) {
        std::fs::read_to_string(path).map_err(|e| invalid(format!("cannot read --file: {}", e)))?
    } else {
        match p.positionals.first() {
            Some(s) if s == "-" => {
                use std::io::Read;
                let mut buf = String::new();
                std::io::stdin().read_to_string(&mut buf)
                    .map_err(|e| invalid(format!("cannot read stdin: {}", e)))?;
                buf
            }
            Some(json) => json.clone(),
            None => {
                return Err(missing("provide commands as argument, --file, or stdin (-)"));
            }
        }
    };
    let commands: Value = serde_json::from_str(&raw)
        .map_err(|e| invalid(format!("invalid JSON: {}", e)))?;
    if !commands.is_array() || commands.as_array().unwrap().is_empty() {
        return Err(invalid("commands must be a non-empty JSON array"));
    }
    let stop_on_error = !p.flag(&["continue-on-error"]);
    let delay: f64 = opt_float(&p, &["delay", "d"], 0.0)?;
    Ok(Action::Call {
        method: "batch".into(),
        params: json!({ "commands": commands, "stop_on_error": stop_on_error, "delay": delay }),
    })
}

fn parse_daemon(rest: &[String]) -> Result<Action, ParseError> {
    const VALID: &[&str] = &["start", "stop", "status"];
    let sub = rest.first().map(|s| s.as_str()).unwrap_or("");
    match sub {
        "start" => Ok(Action::Local(LocalAction::DaemonStart)),
        "stop" => Ok(Action::Local(LocalAction::DaemonStop)),
        "status" => Ok(Action::Local(LocalAction::DaemonStatus)),
        _ => Err(unknown_subcommand("daemon", sub, VALID)),
    }
}

/// `notify "title" "message" [--app-id ID]`. The daemon handler requires
/// title+message and defaults app_id to PowerShell's AUMID when absent, so we
/// forward app_id only when the user gave one (system.py:296-310).
fn parse_notify(rest: &[String]) -> Result<Action, ParseError> {
    let p = ParsedArgs::parse(rest, &["app-id"]);
    let title = p.pos(0, "title")?;
    let message = p.pos(1, "message")?;
    let mut params = Map::new();
    params.insert("title".into(), title.into());
    params.insert("message".into(), message.into());
    if let Some(app_id) = p.value(&["app-id"]) {
        if !app_id.is_empty() {
            params.insert("app_id".into(), app_id.into());
        }
    }
    Ok(Action::Call { method: "notify".into(), params: Value::Object(params) })
}

/// `registry {get|set|delete|list} ...`. All four sub-commands route to the
/// single `registry` method with a `mode` field (system.py:318-386).
///   get    PATH NAME
///   set    PATH NAME VALUE [--type/-t T]
///   delete PATH [NAME]   (NAME omitted → delete the whole key)
///   list   PATH
fn parse_registry(rest: &[String]) -> Result<Action, ParseError> {
    const VALID: &[&str] = &["get", "set", "delete", "list"];
    let sub = rest.first()
        .ok_or_else(|| missing("Missing registry subcommand (get/set/delete/list)"))?;
    let args = &rest[1..];
    match sub.as_str() {
        "get" => {
            let p = ParsedArgs::parse(args, &[]);
            let path = p.pos(0, "path")?;
            let name = p.pos(1, "name")?;
            Ok(Action::Call {
                method: "registry".into(),
                params: json!({ "mode": "get", "path": path, "name": name }),
            })
        }
        "set" => {
            let p = ParsedArgs::parse(args, &["type", "t"]);
            let path = p.pos(0, "path")?;
            let name = p.pos(1, "name")?;
            let value = p.pos(2, "value")?;
            let reg_type = p.value(&["type", "t"]).unwrap_or("String").to_string();
            // MultiString takes a list; the Python CLI JSON-parses the string
            // value into a list before forwarding (system.py:344-354). The
            // daemon's JSON-RPC path accepts either a native list or a string,
            // so for MultiString we parse the JSON here and forward a list to
            // match the CLI contract; for Binary we forward the hex string as-is.
            let value_param: Value = if reg_type == "MultiString" {
                serde_json::from_str(&value).map_err(|e| {
                    invalid(format!("--type MultiString requires a JSON array: {}", e))
                }).and_then(|parsed: Value| {
                    if parsed.is_array() {
                        Ok(parsed)
                    } else {
                        Err(invalid("--type MultiString requires a JSON array"))
                    }
                })?
            } else {
                Value::String(value)
            };
            Ok(Action::Call {
                method: "registry".into(),
                params: json!({ "mode": "set", "path": path, "name": name, "value": value_param, "type": reg_type }),
            })
        }
        "delete" => {
            let p = ParsedArgs::parse(args, &[]);
            let path = p.pos(0, "path")?;
            let mut params = Map::new();
            params.insert("mode".into(), "delete".into());
            params.insert("path".into(), path.into());
            // name omitted → delete the whole key (forward nothing). An explicit
            // empty string targets the registry's unnamed default value — but we
            // can't distinguish "no positional" from "empty positional" here, so
            // we follow the Python CLI's lead: a provided NAME (incl. "") is
            // forwarded; an absent NAME isn't. (BUG-B in system.py:358-377.)
            if let Some(name) = p.positionals.get(1) {
                params.insert("name".into(), name.clone().into());
            }
            Ok(Action::Call { method: "registry".into(), params: Value::Object(params) })
        }
        "list" => {
            let p = ParsedArgs::parse(args, &[]);
            let path = p.pos(0, "path")?;
            Ok(Action::Call {
                method: "registry".into(),
                params: json!({ "mode": "list", "path": path }),
            })
        }
        _ => Err(unknown_subcommand("registry", sub, VALID)),
    }
}

/// Read the JSON payload for multi-select/multi-edit: `--file` > stdin (`-`) >
/// positional argument. Mirrors multi.py:_read_json and batch's source
/// precedence. Returns the parsed JSON array (shape validated by the daemon).
fn read_multi_json(p: &ParsedArgs) -> Result<Value, ParseError> {
    let raw = if let Some(path) = p.value(&["file", "f"]) {
        std::fs::read_to_string(path)
            .map_err(|e| invalid(format!("cannot read --file: {}", e)))?
    } else {
        match p.positionals.first() {
            Some(s) if s == "-" => {
                use std::io::Read;
                let mut buf = String::new();
                std::io::stdin().read_to_string(&mut buf)
                    .map_err(|e| invalid(format!("cannot read stdin: {}", e)))?;
                buf
            }
            Some(json) => json.clone(),
            None => {
                return Err(missing("provide JSON as argument, --file, or stdin (-)"));
            }
        }
    };
    let data: Value = serde_json::from_str(&raw)
        .map_err(|e| invalid(format!("invalid JSON: {}", e)))?;
    if !data.is_array() {
        return Err(invalid("JSON must be an array"));
    }
    Ok(data)
}

/// `multi-select '[[x,y],...]' --selectors` or with @eN refs.
/// `--press-ctrl/--no-ctrl` is a Typer boolean pair (default press-ctrl=true).
/// The daemon wants `locs` (coords) OR `selectors` (@eN) — never both
/// (multi.py:42-77, handler multi.py:129).
fn parse_multi_select(rest: &[String]) -> Result<Action, ParseError> {
    let p = ParsedArgs::parse(rest, &["file", "f"]);
    let data = read_multi_json(&p)?;
    let mut params = Map::new();
    // --no-ctrl present → press_ctrl false; --press-ctrl present → true; else default true.
    let press_ctrl = !p.flag(&["no-ctrl"]);
    params.insert("press_ctrl".into(), press_ctrl.into());
    if p.flag(&["selectors"]) {
        params.insert("selectors".into(), data);
    } else {
        params.insert("locs".into(), data);
    }
    Ok(Action::Call { method: "multi_select".into(), params: Value::Object(params) })
}

/// `multi-edit '[[x,y,text],...]' --selectors` or with @eN+text pairs.
/// Same locs/selectors split as multi-select; no press-ctrl (multi.py:80-108).
fn parse_multi_edit(rest: &[String]) -> Result<Action, ParseError> {
    let p = ParsedArgs::parse(rest, &["file", "f"]);
    let data = read_multi_json(&p)?;
    let mut params = Map::new();
    if p.flag(&["selectors"]) {
        params.insert("selectors".into(), data);
    } else {
        params.insert("locs".into(), data);
    }
    Ok(Action::Call { method: "multi_edit".into(), params: Value::Object(params) })
}

#[cfg(test)]
mod tests {
    use super::{parse, Action, GlobalFlags};

    fn argv(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    /// Parse must classify each failure with a stable JSON `type` so an agent
    /// consumer can pick a retry strategy. These lock the contract.
    #[test]
    fn unknown_command_typed() {
        let g = GlobalFlags::default();
        let e = parse(&argv(&["boguscmd"]), &g).unwrap_err();
        assert_eq!(e.type_name(), "unknown_command");
        assert!(e.message().contains("boguscmd"));
    }

    #[test]
    fn missing_argument_typed() {
        let g = GlobalFlags::default();
        // `click` needs x/y positionals.
        let e = parse(&argv(&["click"]), &g).unwrap_err();
        assert_eq!(e.type_name(), "missing_argument");
        assert!(e.message().contains("x"));
    }

    #[test]
    fn invalid_value_typed() {
        let g = GlobalFlags::default();
        let e = parse(&argv(&["click", "abc", "def"]), &g).unwrap_err();
        assert_eq!(e.type_name(), "invalid_value");
    }

    #[test]
    fn unknown_subcommand_lists_valid_options() {
        let g = GlobalFlags::default();
        let e = parse(&argv(&["app", "frobnicate"]), &g).unwrap_err();
        assert_eq!(e.type_name(), "unknown_subcommand");
        // #5: the message must name the valid choices so the caller can recover.
        assert!(e.message().contains("launch"));
        assert!(e.message().contains("resize"));
    }

    #[test]
    fn daemon_subcommand_valid_options() {
        let g = GlobalFlags::default();
        let e = parse(&argv(&["daemon", "reboot"]), &g).unwrap_err();
        assert_eq!(e.type_name(), "unknown_subcommand");
        assert!(e.message().contains("start"));
        assert!(e.message().contains("stop"));
        assert!(e.message().contains("status"));
    }

    #[test]
    fn valid_commands_parse_to_call() {
        // Sanity: a known-good command isn't misclassified as an error.
        let g = GlobalFlags::default();
        assert!(parse(&argv(&["ping"]), &g).is_ok());
        assert!(parse(&argv(&["app", "list"]), &g).is_ok());
    }

    // ── New command mappings (parity with the Python CLI) ──────────────────
    //
    // These were missing from the launcher, so the documented commands failed
    // with "Unknown command" under the recommended `uv tool install` path.
    // Each test locks both that the command is recognized AND the exact JSON
    // the daemon expects (method name + param keys), since a method/param
    // mismatch surfaces only at runtime as a JSON-RPC error.

    #[test]
    fn notify_forwards_title_message_and_app_id() {
        let g = GlobalFlags::default();
        match parse(&argv(&["notify", "Build done", "all green", "--app-id", "my.app"]), &g) {
            Ok(Action::Call { method, params }) => {
                assert_eq!(method, "notify");
                assert_eq!(params["title"], "Build done");
                assert_eq!(params["message"], "all green");
                assert_eq!(params["app_id"], "my.app");
            }
            other => panic!("expected Call, got {:?}", other),
        }
        // No --app-id → app_id absent (daemon defaults to PowerShell's AUMID).
        match parse(&argv(&["notify", "t", "m"]), &g) {
            Ok(Action::Call { params, .. }) => assert!(params.get("app_id").is_none()),
            other => panic!("expected Call, got {:?}", other),
        }
    }

    #[test]
    fn registry_modes_map_to_single_method_with_mode() {
        let g = GlobalFlags::default();
        // get
        match parse(&argv(&["registry", "get", "HKCU:\\S", "Version"]), &g) {
            Ok(Action::Call { method, params }) => {
                assert_eq!(method, "registry");
                assert_eq!(params["mode"], "get");
                assert_eq!(params["path"], "HKCU:\\S");
                assert_eq!(params["name"], "Version");
            }
            other => panic!("got {:?}", other),
        }
        // set with default type String
        match parse(&argv(&["registry", "set", "HKCU:\\S", "Count", "42"]), &g) {
            Ok(Action::Call { params, .. }) => {
                assert_eq!(params["mode"], "set");
                assert_eq!(params["value"], "42");
                assert_eq!(params["type"], "String");
            }
            other => panic!("got {:?}", other),
        }
        // set with --type DWord
        match parse(&argv(&["registry", "set", "HKCU:\\S", "Count", "42", "--type", "DWord"]), &g) {
            Ok(Action::Call { params, .. }) => assert_eq!(params["type"], "DWord"),
            other => panic!("got {:?}", other),
        }
        // set -t short form
        match parse(&argv(&["registry", "set", "HKCU:\\S", "Count", "42", "-t", "QWord"]), &g) {
            Ok(Action::Call { params, .. }) => assert_eq!(params["type"], "QWord"),
            other => panic!("got {:?}", other),
        }
        // set MultiString → forwarded as a JSON array
        match parse(&argv(&["registry", "set", "HKCU:\\S", "Multi", "[\"a\",\"b\"]", "-t", "MultiString"]), &g) {
            Ok(Action::Call { params, .. }) => {
                assert_eq!(params["value"], serde_json::json!(["a", "b"]));
            }
            other => panic!("got {:?}", other),
        }
        // set MultiString with a non-array → invalid_value
        let e = parse(&argv(&["registry", "set", "HKCU:\\S", "Multi", "notarray", "-t", "MultiString"]), &g).unwrap_err();
        assert_eq!(e.type_name(), "invalid_value");
        // delete with name
        match parse(&argv(&["registry", "delete", "HKCU:\\S", "Version"]), &g) {
            Ok(Action::Call { params, .. }) => {
                assert_eq!(params["mode"], "delete");
                assert_eq!(params["name"], "Version");
            }
            other => panic!("got {:?}", other),
        }
        // delete whole key (name omitted → name absent)
        match parse(&argv(&["registry", "delete", "HKCU:\\S"]), &g) {
            Ok(Action::Call { params, .. }) => assert!(params.get("name").is_none()),
            other => panic!("got {:?}", other),
        }
        // list
        match parse(&argv(&["registry", "list", "HKCU:\\S"]), &g) {
            Ok(Action::Call { params, .. }) => assert_eq!(params["mode"], "list"),
            other => panic!("got {:?}", other),
        }
        // unknown sub → lists valid choices
        let e = parse(&argv(&["registry", "frob"]), &g).unwrap_err();
        assert_eq!(e.type_name(), "unknown_subcommand");
        assert!(e.message().contains("get") && e.message().contains("list"));
    }

    #[test]
    fn expand_collapse_forward_selector() {
        let g = GlobalFlags::default();
        // expand @e5 → method expand, {selector}. These drove a live UIA pattern
        // but were missing from the launcher, failing with "Unknown command".
        match parse(&argv(&["expand", "@e5"]), &g) {
            Ok(Action::Call { method, params }) => {
                assert_eq!(method, "expand");
                assert_eq!(params["selector"], "@e5");
            }
            other => panic!("got {:?}", other),
        }
        match parse(&argv(&["collapse", "@e5"]), &g) {
            Ok(Action::Call { method, params }) => {
                assert_eq!(method, "collapse");
                assert_eq!(params["selector"], "@e5");
            }
            other => panic!("got {:?}", other),
        }
        // Missing selector → missing_argument.
        let e = parse(&argv(&["expand"]), &g).unwrap_err();
        assert_eq!(e.type_name(), "missing_argument");
    }

    #[test]
    fn fs_extra_subcommands_and_unknown_lists_all() {
        let g = GlobalFlags::default();
        // copy/move SRC DEST [--overwrite]: overwrite forwards only when present.
        match parse(&argv(&["fs", "copy", "a", "b"]), &g) {
            Ok(Action::Call { method, params }) => {
                assert_eq!(method, "fs_copy");
                assert_eq!(params["path"], "a");
                assert_eq!(params["destination"], "b");
                assert!(params.get("overwrite").is_none());
            }
            other => panic!("got {:?}", other),
        }
        match parse(&argv(&["fs", "copy", "a", "b", "--overwrite"]), &g) {
            Ok(Action::Call { params, .. }) => assert_eq!(params["overwrite"], true),
            other => panic!("got {:?}", other),
        }
        match parse(&argv(&["fs", "move", "a", "b", "--overwrite"]), &g) {
            Ok(Action::Call { method, params }) => {
                assert_eq!(method, "fs_move");
                assert_eq!(params["overwrite"], true);
            }
            other => panic!("got {:?}", other),
        }
        // delete PATH [--recursive]
        match parse(&argv(&["fs", "delete", "d"]), &g) {
            Ok(Action::Call { method, params }) => {
                assert_eq!(method, "fs_delete");
                assert_eq!(params["path"], "d");
                assert!(params.get("recursive").is_none());
            }
            other => panic!("got {:?}", other),
        }
        match parse(&argv(&["fs", "delete", "d", "--recursive"]), &g) {
            Ok(Action::Call { params, .. }) => assert_eq!(params["recursive"], true),
            other => panic!("got {:?}", other),
        }
        // search PATH PATTERN [--no-recursive]: inverted — recursive=false only
        // with the flag; absent forwards nothing (daemon default is True).
        match parse(&argv(&["fs", "search", ".", "*.py"]), &g) {
            Ok(Action::Call { method, params }) => {
                assert_eq!(method, "fs_search");
                assert_eq!(params["path"], ".");
                assert_eq!(params["pattern"], "*.py");
                assert!(params.get("recursive").is_none());
            }
            other => panic!("got {:?}", other),
        }
        match parse(&argv(&["fs", "search", ".", "*.py", "--no-recursive"]), &g) {
            Ok(Action::Call { params, .. }) => assert_eq!(params["recursive"], false),
            other => panic!("got {:?}", other),
        }
        // info PATH
        match parse(&argv(&["fs", "info", "f"]), &g) {
            Ok(Action::Call { method, params }) => {
                assert_eq!(method, "fs_info");
                assert_eq!(params["path"], "f");
            }
            other => panic!("got {:?}", other),
        }
        // Missing positionals → missing_argument.
        let e = parse(&argv(&["fs", "info"]), &g).unwrap_err();
        assert_eq!(e.type_name(), "missing_argument");
        // unknown sub → lists all EIGHT valid choices (regression: the old VALID
        // only had read/write/list, so copy/move/... were misclassified).
        let e = parse(&argv(&["fs", "frob"]), &g).unwrap_err();
        assert_eq!(e.type_name(), "unknown_subcommand");
        for sub in &["read", "write", "list", "copy", "move", "delete", "search", "info"] {
            assert!(e.message().contains(sub), "message must list {:?}", sub);
        }
    }

    #[test]
    fn dropped_options_forwarded_with_default_omission() {
        let g = GlobalFlags::default();

        // ── screenshot --annotate --grid-w/--grid-h ───────────────────────
        // Param-name remap: --annotate→use_annotation, --grid-w→
        // width_reference_line, --grid-h→height_reference_line.
        // No flags → none forwarded.
        match parse(&argv(&["screenshot"]), &g) {
            Ok(Action::Call { params, .. }) => {
                assert!(params.get("use_annotation").is_none());
                assert!(params.get("width_reference_line").is_none());
                assert!(params.get("height_reference_line").is_none());
            }
            other => panic!("got {:?}", other),
        }
        match parse(&argv(&["screenshot", "--annotate"]), &g) {
            Ok(Action::Call { params, .. }) => {
                assert_eq!(params["use_annotation"], true);
                assert!(params.get("width_reference_line").is_none());
                assert!(params.get("height_reference_line").is_none());
            }
            other => panic!("got {:?}", other),
        }
        // grid lines forward only when non-zero, alongside --annotate.
        match parse(&argv(&["screenshot", "--annotate", "--grid-w", "10", "--grid-h", "8"]), &g) {
            Ok(Action::Call { params, .. }) => {
                assert_eq!(params["use_annotation"], true);
                assert_eq!(params["width_reference_line"], 10);
                assert_eq!(params["height_reference_line"], 8);
            }
            other => panic!("got {:?}", other),
        }
        // zero grid value → not forwarded.
        match parse(&argv(&["screenshot", "--annotate", "--grid-w", "0"]), &g) {
            Ok(Action::Call { params, .. }) => {
                assert_eq!(params["use_annotation"], true);
                assert!(params.get("width_reference_line").is_none());
            }
            other => panic!("got {:?}", other),
        }

        // ── fs read --offset/--limit/--encoding (non-default only) ─────────
        match parse(&argv(&["fs", "read", "f", "--offset", "100", "--limit", "5", "--encoding", "gbk"]), &g) {
            Ok(Action::Call { params, .. }) => {
                assert_eq!(params["offset"], 100);
                assert_eq!(params["limit"], 5);
                assert_eq!(params["encoding"], "gbk");
            }
            other => panic!("got {:?}", other),
        }
        match parse(&argv(&["fs", "read", "f"]), &g) {
            Ok(Action::Call { params, .. }) => {
                assert!(params.get("offset").is_none());
                assert!(params.get("limit").is_none());
                assert!(params.get("encoding").is_none());
            }
            other => panic!("got {:?}", other),
        }

        // ── fs write --append ────────────────────────────────────────────
        match parse(&argv(&["fs", "write", "f", "c", "--append"]), &g) {
            Ok(Action::Call { params, .. }) => assert_eq!(params["append"], true),
            other => panic!("got {:?}", other),
        }
        match parse(&argv(&["fs", "write", "f", "c"]), &g) {
            Ok(Action::Call { params, .. }) => assert!(params.get("append").is_none()),
            other => panic!("got {:?}", other),
        }

        // ── fs list --pattern/--recursive/--show-hidden ───────────────────
        match parse(&argv(&["fs", "list", "d", "--pattern", "*.py", "--recursive", "--show-hidden"]), &g) {
            Ok(Action::Call { params, .. }) => {
                assert_eq!(params["pattern"], "*.py");
                assert_eq!(params["recursive"], true);
                assert_eq!(params["show_hidden"], true);
            }
            other => panic!("got {:?}", other),
        }
        match parse(&argv(&["fs", "list", "d"]), &g) {
            Ok(Action::Call { params, .. }) => {
                assert!(params.get("pattern").is_none());
                assert!(params.get("recursive").is_none());
                assert!(params.get("show_hidden").is_none());
            }
            other => panic!("got {:?}", other),
        }

        // ── shell --timeout/-t (forward only when > 0) ────────────────────
        match parse(&argv(&["shell", "echo hi", "--timeout", "120"]), &g) {
            Ok(Action::Call { params, .. }) => assert_eq!(params["timeout"], 120.0),
            other => panic!("got {:?}", other),
        }
        match parse(&argv(&["shell", "echo hi", "-t", "60"]), &g) {
            Ok(Action::Call { params, .. }) => assert_eq!(params["timeout"], 60.0),
            other => panic!("got {:?}", other),
        }
        // timeout 0 → not forwarded (use config default).
        match parse(&argv(&["shell", "echo hi", "--timeout", "0"]), &g) {
            Ok(Action::Call { params, .. }) => assert!(params.get("timeout").is_none()),
            other => panic!("got {:?}", other),
        }
        match parse(&argv(&["shell", "echo hi"]), &g) {
            Ok(Action::Call { params, .. }) => assert!(params.get("timeout").is_none()),
            other => panic!("got {:?}", other),
        }

        // ── process list --sort-by/--limit/--name (non-default only) ──────
        match parse(&argv(&["process", "list", "--sort-by", "cpu", "--limit", "10", "--name", "chrome"]), &g) {
            Ok(Action::Call { params, .. }) => {
                assert_eq!(params["sort_by"], "cpu");
                assert_eq!(params["limit"], 10);
                assert_eq!(params["name"], "chrome");
            }
            other => panic!("got {:?}", other),
        }
        // defaults (memory/0/"") → nothing forwarded.
        match parse(&argv(&["process", "list"]), &g) {
            Ok(Action::Call { params, .. }) => {
                assert!(params.get("sort_by").is_none());
                assert!(params.get("limit").is_none());
                assert!(params.get("name").is_none());
            }
            other => panic!("got {:?}", other),
        }

        // ── process kill --force ──────────────────────────────────────────
        match parse(&argv(&["process", "kill", "notepad", "--force"]), &g) {
            Ok(Action::Call { params, .. }) => assert_eq!(params["force"], true),
            other => panic!("got {:?}", other),
        }
        match parse(&argv(&["process", "kill", "notepad"]), &g) {
            Ok(Action::Call { params, .. }) => assert!(params.get("force").is_none()),
            other => panic!("got {:?}", other),
        }
    }

    #[test]
    fn malformed_dropped_option_is_invalid_value() {
        // The malformed-numeric→invalid_value contract (L-05) must hold for the
        // newly-wired value opts too, so a bad arg surfaces rather than silently
        // defaulting.
        let g = GlobalFlags::default();
        // shell --timeout abc
        let e = parse(&argv(&["shell", "echo", "--timeout", "abc"]), &g).unwrap_err();
        assert_eq!(e.type_name(), "invalid_value");
        assert!(e.message().contains("timeout"));
        // shell --timeout nan (opt_float rejects non-finite)
        let e = parse(&argv(&["shell", "echo", "--timeout", "nan"]), &g).unwrap_err();
        assert_eq!(e.type_name(), "invalid_value");
        // screenshot --grid-w notanumber
        let e = parse(&argv(&["screenshot", "--annotate", "--grid-w", "x"]), &g).unwrap_err();
        assert_eq!(e.type_name(), "invalid_value");
        // process list --limit nope
        let e = parse(&argv(&["process", "list", "--limit", "nope"]), &g).unwrap_err();
        assert_eq!(e.type_name(), "invalid_value");
    }

    #[test]
    fn multi_select_and_edit_split_locs_and_selectors() {
        let g = GlobalFlags::default();
        // multi-select coords (default press-ctrl true)
        match parse(&argv(&["multi-select", "[[100,200],[300,400]]"]), &g) {
            Ok(Action::Call { method, params }) => {
                assert_eq!(method, "multi_select");
                assert_eq!(params["locs"], serde_json::json!([[100,200],[300,400]]));
                assert_eq!(params["press_ctrl"], true);
                assert!(params.get("selectors").is_none());
            }
            other => panic!("got {:?}", other),
        }
        // multi-select selectors
        match parse(&argv(&["multi-select", "[\"@e1\",\"@e3\"]", "--selectors"]), &g) {
            Ok(Action::Call { method, params }) => {
                assert_eq!(method, "multi_select");
                assert_eq!(params["selectors"], serde_json::json!(["@e1","@e3"]));
                assert!(params.get("locs").is_none());
            }
            other => panic!("got {:?}", other),
        }
        // --no-ctrl flips press_ctrl
        match parse(&argv(&["multi-select", "[[1,2]]", "--no-ctrl"]), &g) {
            Ok(Action::Call { params, .. }) => assert_eq!(params["press_ctrl"], false),
            other => panic!("got {:?}", other),
        }
        // multi-edit coords → locs
        match parse(&argv(&["multi-edit", "[[100,200,\"hi\"]]"]), &g) {
            Ok(Action::Call { method, params }) => {
                assert_eq!(method, "multi_edit");
                assert_eq!(params["locs"], serde_json::json!([[100,200,"hi"]]));
            }
            other => panic!("got {:?}", other),
        }
        // multi-edit selectors
        match parse(&argv(&["multi-edit", "[[\"@e1\",\"Alice\"]]", "--selectors"]), &g) {
            Ok(Action::Call { params, .. }) => assert!(params.get("selectors").is_some() && params.get("locs").is_none()),
            other => panic!("got {:?}", other),
        }
        // non-array JSON → invalid_value
        let e = parse(&argv(&["multi-select", "{}"]), &g).unwrap_err();
        assert_eq!(e.type_name(), "invalid_value");
        // missing payload → missing_argument
        let e = parse(&argv(&["multi-select"]), &g).unwrap_err();
        assert_eq!(e.type_name(), "missing_argument");
    }

    #[test]
    fn wait_for_parses_to_wait_for_action() {
        let g = GlobalFlags::default();
        match parse(&argv(&["wait-for", "text_exists", "--text", "Done", "--timeout", "5"]), &g) {
            Ok(Action::WaitFor(spec)) => {
                assert_eq!(spec.condition, "text_exists");
                assert_eq!(spec.text, "Done");
                assert_eq!(spec.timeout, 5.0);
            }
            other => panic!("expected WaitFor, got {:?}", other),
        }
        // alias + window scoping + short timeout
        match parse(&argv(&["wait-for", "enabled", "--text", "OK", "--window", "Notepad", "-t", "10"]), &g) {
            Ok(Action::WaitFor(spec)) => {
                assert_eq!(spec.condition, "enabled");
                assert_eq!(spec.window, "Notepad");
            }
            other => panic!("got {:?}", other),
        }
        // missing condition → missing_argument
        let e = parse(&argv(&["wait-for"]), &g).unwrap_err();
        assert_eq!(e.type_name(), "missing_argument");
    }

    #[test]
    fn wait_numeric_sleep_returns_local_sleep_action() {
        let g = GlobalFlags::default();
        // `wait 3` → LocalSleep(3.0). Regression: the old code did the sleep
        // and a text-only print INSIDE parse() then process::exit(0), which
        // (a) couldn't honor --format json and (b) made parse() do I/O. Now it
        // returns an action so main() formats the output correctly.
        match parse(&argv(&["wait", "3"]), &g) {
            Ok(Action::LocalSleep(secs)) => assert_eq!(secs, 3.0),
            other => panic!("expected LocalSleep, got {:?}", other),
        }
        // fractional seconds
        match parse(&argv(&["wait", "0.5"]), &g) {
            Ok(Action::LocalSleep(secs)) => assert_eq!(secs, 0.5),
            other => panic!("got {:?}", other),
        }
        // zero/negative → invalid_value (no sleep)
        for bad in &["0", "-1", "0.0"] {
            let e = parse(&argv(&["wait", bad]), &g).unwrap_err();
            assert_eq!(e.type_name(), "invalid_value", "wait {:?} should be invalid_value", bad);
        }
        // non-numeric → invalid_value
        let e = parse(&argv(&["wait", "abc"]), &g).unwrap_err();
        assert_eq!(e.type_name(), "invalid_value");
    }

    #[test]
    fn mouse_commands_accept_selector_override() {
        let g = GlobalFlags::default();
        // click -s @e3 → selector, no x/y
        match parse(&argv(&["click", "-s", "@e3"]), &g) {
            Ok(Action::Call { method, params }) => {
                assert_eq!(method, "click");
                assert_eq!(params["selector"], "@e3");
                assert!(params.get("x").is_none());
            }
            other => panic!("got {:?}", other),
        }
        // click --selector @e3 --right → selector + right flag
        match parse(&argv(&["click", "--selector", "@e3", "--right"]), &g) {
            Ok(Action::Call { params, .. }) => {
                assert_eq!(params["selector"], "@e3");
                assert_eq!(params["right"], true);
            }
            other => panic!("got {:?}", other),
        }
        // coordinates still work when no selector
        match parse(&argv(&["click", "10", "20"]), &g) {
            Ok(Action::Call { params, .. }) => {
                assert_eq!(params["x"], 10);
                assert_eq!(params["y"], 20);
                assert!(params.get("selector").is_none());
            }
            other => panic!("got {:?}", other),
        }
        // mouse-down -s @e3 --button right
        match parse(&argv(&["mouse-down", "-s", "@e3", "--button", "right"]), &g) {
            Ok(Action::Call { params, .. }) => {
                assert_eq!(params["selector"], "@e3");
                assert_eq!(params["button"], "right");
            }
            other => panic!("got {:?}", other),
        }
        // scroll -s @e3 --direction up
        match parse(&argv(&["scroll", "-s", "@e3", "--direction", "up"]), &g) {
            Ok(Action::Call { params, .. }) => {
                assert_eq!(params["selector"], "@e3");
                assert_eq!(params["direction"], "up");
            }
            other => panic!("got {:?}", other),
        }
        // hover / move / dblclick / mouse-up also take selector
        for cmd in &["hover", "move", "dblclick", "mouse-up"] {
            match parse(&argv(&[cmd, "-s", "@e1"]), &g) {
                Ok(Action::Call { params, .. }) => assert_eq!(params["selector"], "@e1"),
                other => panic!("{}: got {:?}", cmd, other),
            }
        }
        // empty selector → invalid_value
        let e = parse(&argv(&["click", "-s", ""]), &g).unwrap_err();
        assert_eq!(e.type_name(), "invalid_value");
    }

    #[test]
    fn type_at_selector_and_caret_forwarded() {
        let g = GlobalFlags::default();
        // --at-selector overrides --at, maps to at_selector
        match parse(&argv(&["type", "hi", "--at-selector", "@e3"]), &g) {
            Ok(Action::Call { method, params }) => {
                assert_eq!(method, "type");
                assert_eq!(params["text"], "hi");
                assert_eq!(params["at_selector"], "@e3");
                assert!(params.get("at_x").is_none());
                // caret defaults to idle
                assert_eq!(params["caret_position"], "idle");
            }
            other => panic!("got {:?}", other),
        }
        // --ats short alias
        match parse(&argv(&["type", "hi", "--ats", "@e3"]), &g) {
            Ok(Action::Call { params, .. }) => assert_eq!(params["at_selector"], "@e3"),
            other => panic!("got {:?}", other),
        }
        // --at X,Y still works when no at-selector
        match parse(&argv(&["type", "hi", "--at", "100,200"]), &g) {
            Ok(Action::Call { params, .. }) => {
                assert_eq!(params["at_x"], 100);
                assert_eq!(params["at_y"], 200);
                assert!(params.get("at_selector").is_none());
            }
            other => panic!("got {:?}", other),
        }
        // --caret start
        match parse(&argv(&["type", "hi", "--caret", "start"]), &g) {
            Ok(Action::Call { params, .. }) => assert_eq!(params["caret_position"], "start"),
            other => panic!("got {:?}", other),
        }
    }

    #[test]
    fn observe_and_snapshot_accept_display_filter() {
        let g = GlobalFlags::default();
        // snapshot -d 0 → display [0]
        match parse(&argv(&["snapshot", "-d", "0"]), &g) {
            Ok(Action::Call { method, params }) => {
                assert_eq!(method, "snapshot");
                assert_eq!(params["display"], serde_json::json!([0]));
            }
            other => panic!("got {:?}", other),
        }
        // snapshot with no -d → no display key (backward compatible)
        match parse(&argv(&["snapshot"]), &g) {
            Ok(Action::Call { params, .. }) => assert!(params.get("display").is_none()),
            other => panic!("got {:?}", other),
        }
        // repeated -d de-duplicated into one array
        match parse(&argv(&["snapshot", "-d", "0", "-d", "1", "-d", "0"]), &g) {
            Ok(Action::Call { params, .. }) => assert_eq!(params["display"], serde_json::json!([0, 1])),
            other => panic!("got {:?}", other),
        }
        // --display= form
        match parse(&argv(&["snapshot", "--display=2"]), &g) {
            Ok(Action::Call { params, .. }) => assert_eq!(params["display"], serde_json::json!([2])),
            other => panic!("got {:?}", other),
        }
        // negative index → invalid_value
        let e = parse(&argv(&["snapshot", "-d", "-1"]), &g).unwrap_err();
        assert_eq!(e.type_name(), "invalid_value");
        // non-int → invalid_value
        let e = parse(&argv(&["snapshot", "-d", "abc"]), &g).unwrap_err();
        assert_eq!(e.type_name(), "invalid_value");
        // observe also forwards display
        match parse(&argv(&["observe", "-d", "1"]), &g) {
            Ok(Action::Call { params, .. }) => assert_eq!(params["display"], serde_json::json!([1])),
            other => panic!("got {:?}", other),
        }
    }

    // ── ParsedArgs: the regression suite ───────────────────────────────────
    //
    // These lock the behavior that motivated the single-pass rewrite. The old
    // split_args/opt_value/require_positional trio each walked the args
    // independently and disagreed about what counts as a positional — a value-
    // taking option's argument (e.g. the `800` in `--width 800`) was counted
    // as a positional, so a later positional could grab the wrong token.

    /// Regression: a flag's value must NOT be mistaken for a positional.
    /// `app resize NAME --width 800` → name=NAME, width=800, not width=NAME.
    #[test]
    fn value_opt_not_counted_as_positional() {
        let g = GlobalFlags::default();
        match parse(&argv(&["app", "resize", "notepad", "--width", "800", "--height", "600"]), &g) {
            Ok(Action::Call { method, params }) => {
                assert_eq!(method, "app_resize");
                assert_eq!(params["name_or_title"], "notepad");
                assert_eq!(params["width"], 800);
                assert_eq!(params["height"], 600);
            }
            other => panic!("expected Call, got {:?}", other),
        }
    }

    /// Regression: the same when the value comes BEFORE the positionals.
    /// Order must not matter; `--output` here takes the next token as its value.
    #[test]
    fn value_opt_before_positional() {
        let g = GlobalFlags::default();
        // screenshot --output shot.png has no positionals; verify it doesn't
        // swallow a stray token. `--bbox` is value-taking too.
        match parse(&argv(&["screenshot", "--output", "out.png", "--bbox", "0,0,10,10"]), &g) {
            Ok(Action::Call { method, params }) => {
                assert_eq!(method, "screenshot");
                assert_eq!(params["output"], "out.png");
                assert_eq!(params["bbox"], serde_json::json!([0, 0, 10, 10]));
            }
            other => panic!("expected Call, got {:?}", other),
        }
    }

    /// `--name=value` form must bind to the option, and `=` must not leak
    /// into positionals.
    #[test]
    fn equals_form_binds_value() {
        let g = GlobalFlags::default();
        match parse(&argv(&["app", "resize", "notepad", "--width=900", "--height=700"]), &g) {
            Ok(Action::Call { params, .. }) => {
                assert_eq!(params["width"], 900);
                assert_eq!(params["height"], 700);
                assert_eq!(params["name_or_title"], "notepad");
            }
            other => panic!("expected Call, got {:?}", other),
        }
    }

    /// Short option (`-o`) must behave like its long form, including `=`.
    #[test]
    fn short_option_aliases_long() {
        let g = GlobalFlags::default();
        let a = parse(&argv(&["ask-vision", "describe this", "-o", "r.json"]), &g).unwrap();
        let b = parse(&argv(&["ask-vision", "describe this", "--output", "r.json"]), &g).unwrap();
        let c = parse(&argv(&["ask-vision", "describe this", "-o=r.json"]), &g).unwrap();
        match (a, b, c) {
            (Action::Call { params: pa, .. },
             Action::Call { params: pb, .. },
             Action::Call { params: pc, .. }) => {
                assert_eq!(pa["output"], "r.json");
                assert_eq!(pb["output"], "r.json");
                assert_eq!(pc["output"], "r.json");
                assert_eq!(pa["prompt"], "describe this");
            }
            other => panic!("expected Call x3, got {:?}", other),
        }
    }

    /// Boolean flags (presence-only) must still be recognized in any slot,
    /// and must NOT consume the following token.
    #[test]
    fn boolean_flag_does_not_consume_next() {
        let g = GlobalFlags::default();
        // `type hello --enter` → enter=true, text=hello. The `--enter` flag
        // must not eat a following positional (there is none here, but the
        // negative case below proves it doesn't consume).
        match parse(&argv(&["type", "hello", "--enter"]), &g) {
            Ok(Action::Call { params, .. }) => {
                assert_eq!(params["text"], "hello");
                assert_eq!(params["enter"], true);
            }
            other => panic!("expected Call, got {:?}", other),
        }
        // Flag sandwiched between positionals: must not consume pos[1].
        match parse(&argv(&["click", "10", "--right", "20"]), &g) {
            Ok(Action::Call { params, .. }) => {
                assert_eq!(params["x"], 10);
                assert_eq!(params["y"], 20);
                assert_eq!(params["right"], true);
            }
            other => panic!("expected Call, got {:?}", other),
        }
    }

    /// Regression: negative integer positionals must stay positionals, not be
    /// mistaken for a short `-<digit>` flag. Multi-monitor coordinates go
    /// negative (e.g. a window at x=-1920), so `click -1 -1` must give
    /// x=-1, y=-1. The old `split_args` handled this (it only split on `--`);
    /// a naive single-pass parser that treats any `-c` as a flag regresses it.
    #[test]
    fn negative_positional_not_treated_as_flag() {
        let g = GlobalFlags::default();
        match parse(&argv(&["click", "-1", "-1"]), &g) {
            Ok(Action::Call { params, .. }) => {
                assert_eq!(params["x"], -1);
                assert_eq!(params["y"], -1);
            }
            other => panic!("expected Call with x=-1,y=-1, got {:?}", other),
        }
        // drag with negative start coords on a multi-monitor setup.
        match parse(&argv(&["drag", "-1920", "0", "-100", "100"]), &g) {
            Ok(Action::Call { params, .. }) => {
                assert_eq!(params["x1"], -1920);
                assert_eq!(params["y1"], 0);
                assert_eq!(params["x2"], -100);
                assert_eq!(params["y2"], 100);
            }
            other => panic!("expected Call, got {:?}", other),
        }
    }

    /// A trailing value-taking option with no following token is recorded as
    /// present-but-empty, never panics, and leaves the real positionals
    /// untouched. We use `scroll` (x/y are positionals, amount is a value-opt)
    /// so a leaked "value" would corrupt x or y — this is the case the old
    /// split_args/require_positional trio actually got wrong (it counted an
    /// option's value as a positional). `scroll 5 5 --amount` must keep
    /// x=5, y=5 and fall back amount to its default.
    #[test]
    fn trailing_value_opt_records_empty_not_positional() {
        let g = GlobalFlags::default();
        match parse(&argv(&["scroll", "5", "5", "--amount"]), &g) {
            Ok(Action::Call { params, .. }) => {
                // x/y survive intact — the missing --amount value did not
                // leak into the positional list.
                assert_eq!(params["x"], 5);
                assert_eq!(params["y"], 5);
                // amount=Some("") → opt_int treats empty as "use default" (3),
                // so a trailing `--amount` with no value still yields the default.
                assert_eq!(params["amount"], 3);
            }
            other => panic!("expected Call, got {:?}", other),
        }
    }

    /// A repeated value-option takes the last occurrence (last-writer-wins),
    /// matching Typer. Locks the `values.insert` overwrite contract so a future
    /// change to e.g. first-wins or error-on-duplicate is caught.
    #[test]
    fn repeated_value_option_last_wins() {
        let g = GlobalFlags::default();
        match parse(&argv(&["app", "resize", "n", "--height", "9", "--width", "1", "--width", "2"]), &g) {
            Ok(Action::Call { params, .. }) => {
                assert_eq!(params["width"], 2);
                assert_eq!(params["height"], 9);
                assert_eq!(params["name_or_title"], "n");
            }
            other => panic!("expected Call, got {:?}", other),
        }
    }

    // ── L-05: malformed optional numeric → invalid_value (not silent default) ─
    //
    // The typed-error contract promises `invalid_value` when "a value failed
    // to parse", so an agent consumer can detect a bad argument and retry with
    // a fix. The old `.map(|v| v.parse().unwrap_or(DEFAULT)).unwrap_or(DEFAULT)`
    // shape silently returned the default for a *present but malformed* value,
    // defeating that signal. These lock the new behavior: malformed → error;
    // absent → default; empty (trailing flag with no value) → default.

    #[test]
    fn malformed_int_option_is_invalid_value() {
        let g = GlobalFlags::default();
        // `scroll x y --amount abc` must surface invalid_value, not silently
        // produce amount=3.
        let e = parse(&argv(&["scroll", "1", "2", "--amount", "abc"]), &g).unwrap_err();
        assert_eq!(e.type_name(), "invalid_value");
        assert!(e.message().contains("amount"));
        // Same for an app-resize optional (x/y) — earlier these silently
        // defaulted to -1, inconsistent with width/height which already errored.
        let e = parse(&argv(&["app", "resize", "n", "--width", "10", "--height", "10", "--x", "nope"]), &g)
            .unwrap_err();
        assert_eq!(e.type_name(), "invalid_value");
        assert!(e.message().contains("x"));
    }

    #[test]
    fn malformed_float_option_is_invalid_value() {
        let g = GlobalFlags::default();
        // wait --timeout abc (f64 path via opt_float).
        let e = parse(&argv(&["wait", "--text", "hi", "--timeout", "abc"]), &g).unwrap_err();
        assert_eq!(e.type_name(), "invalid_value");
        assert!(e.message().contains("timeout"));
        // batch --delay notanumber (f64 path).
        let e = parse(&argv(&["batch", "--delay", "fast", "[{}]"]), &g).unwrap_err();
        assert_eq!(e.type_name(), "invalid_value");
        assert!(e.message().contains("delay"));
    }

    #[test]
    fn nonfinite_float_option_is_invalid_value() {
        // Rust's f64 parsing accepts "nan"/"inf", but a NaN --timeout would make
        // `start.elapsed() >= timeout` always false → an infinite poll loop in
        // run_wait. opt_float must reject non-finite values.
        let g = GlobalFlags::default();
        for bad in &["nan", "inf", "-inf", "infinity"] {
            let e = parse(&argv(&["wait", "--text", "hi", "--timeout", bad]), &g).unwrap_err();
            assert_eq!(e.type_name(), "invalid_value", "nan/inf {:?} must be rejected", bad);
            assert!(e.message().contains("finite"), "msg for {:?}: {}", bad, e.message());
        }
    }

    #[test]
    fn absent_numeric_option_uses_default() {
        let g = GlobalFlags::default();
        // No --amount → default 3 still applies (regression guard: the
        // malformed→error change must not break the absent case).
        match parse(&argv(&["scroll", "1", "2"]), &g) {
            Ok(Action::Call { params, .. }) => assert_eq!(params["amount"], 3),
            other => panic!("expected Call, got {:?}", other),
        }
    }
}
