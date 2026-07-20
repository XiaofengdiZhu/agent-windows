# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`agent-windows` — Windows Computer-Use CLI for AI agents. Drives the real desktop via the UIA accessibility tree, screenshots, optional vision-model grounding, and mouse/keyboard automation. Ships a CLI, a daemon, and a FastMCP server that exposes the same commands as MCP tools.

Windows-only by design (UIA/COM, `SendInput`, Named Pipes). Tests run on Windows; the `reference/` directory holds third-party repos (Windows-MCP, agent-browser, open-apa) and is **not** part of this project — gitignored, do not edit.

## Commands

Package manager is **uv** (Python ≥3.13).

```bash
uv sync                      # core deps
uv sync --extra mcp          # MCP server (fastmcp) — needed for mcp serve
uv sync --extra dev          # pytest
uv sync --all-extras         # everything (mcp + dev + dxcam/anthropic/gemini/numpy)

uv run agent-windows --help  # run the CLI from a checkout
uv run pytest -q             # full suite
uv run pytest tests/test_config.py -v              # one file
uv run pytest tests/test_config.py::test_name       # one test
AGENT_WINDOWS_E2E=1 uv run pytest -m e2e            # end-to-end daemon smoke tests (opt-in)
```

Rebuild the native Rust launcher (only if you change `src/launcher/`; needs Rust + MSVC + Windows SDK):

```bash
cd src/launcher && build.cmd build --release
# stages target/release/agent-windows.exe into prebuilt/ (gitignored); run before `uv build`
```

## Architecture

### Two-process design: native launcher + persistent Python daemon

```
agent-windows.exe  (Rust launcher, ~12ms cold start: src/launcher/)
        │  argv parse, --config/--debug handling, delegates `config`/`mcp` to Python CLI
        ▼  Named Pipe IPC, JSON-RPC 2.0 (per-user DACL)
Python daemon (persistent: src/agent_windows/daemon/)  ──holds heavy imports (comtypes/uia/Pillow)
        │  single COM STA worker thread
        ▼
UIA / SendInput / filesystem / shell / registry
```

- The daemon is **long-lived**, auto-started on first CLI/MCP call, idles out after 30min. The launcher's fast path is just IPC forwarding; it does *not* import Python.
- `agent-windows-python` (the Python entry point `cli.main:main`) is the fallback for `--no-daemon` and owns `config`/`mcp` subcommands (the launcher delegates those verbatim).
- Entry routing: `python -m agent_windows` (`__main__.py`) checks `--daemon` in argv → `daemon.server.run_daemon()`, else `cli.main.main()`.

### Every command flows through four layers

A new command touches all four — keep them in sync:

1. **CLI command** (`cli/commands/*.py`) — Typer function, builds a `params` dict, calls `_cmd(ctx, "<method>", **params)`.
2. **Dispatcher** (`cli/dispatcher.py`) — `dispatch()` wraps in telemetry + error boundary; either `call_daemon()` (IPC) or `_dispatch_no_daemon()` (in-process, still routes through `route_request` so error codes match).
3. **IPC** (`ipc/`) — `client.call_daemon()` auto-starts the daemon (serialized by `DaemonSpawnLock`), `protocol.py` is the JSON-RPC 2.0 encode/decode + error-code constants, `pipe.py` is the Named Pipe transport.
4. **Daemon handler** (`daemon/handlers/*.py`) — registered via `@handler("<method>")` into `router.HANDLERS`; calls into `core/*` for the real work.

The **MCP server** is a parallel frontend: `mcp/tools/*` decorate `*_fn` callables as FastMCP tools, and each forwards to the daemon over the same named pipe (`mcp/_call.py`). It owns no automation logic.

### Why the daemon is serial (important)

The daemon runs **one command at a time**: single named-pipe instance + one COM STA worker thread + blocking I/O loop. This is load-bearing — UIA/COM objects are apartment-bound to the STA thread and `SendInput` mutates global GUI state, so concurrent commands would race. Don't "fix" the serialization. Isolation between agents = one user account per daemon (pipe path is per-user). See `COMWorkerThread` / `_main_pipe_loop` docstrings.

Results are keyed by a per-submit monotonic `seq`, not `request.id` (the single client reuses id=1) — an orphaned (timed-out) command's late write must not collide with a later request's slot. The `STA_SUBMIT_TIMEOUT_S` (300s) is advisory; it reports `WAIT_TIMEOUT` and disconnects but **cannot cancel** an in-flight UIA/COM call.

### Error codes

Defined in `ipc/protocol.py`. `route_request` maps exception types → codes:
- `-32700..=-32000` JSON-RPC protocol range (PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, **INVALID_PARAMS** from `ValueError`/`IndexError`).
- App range: `ELEMENT_NOT_FOUND` (-1001, also `FileNotFoundError`/dead UIA element), `COORDINATE_OUT_OF_RANGE` (-1002), `WAIT_TIMEOUT` (-1003), `PERMISSION_DENIED` (-1004), `VISION_API_ERROR` (-1005), `DAEMON_INTERNAL_ERROR` (-1999).

CLI exit codes mirror this: protocol errors → exit 2, app errors → exit 1 (`_exit_code_for_daemon_error`, matched by the Rust launcher). Use `router.require(params, key, expected_type)` for required params so a missing one becomes `INVALID_PARAMS` not a `KeyError`→`DAEMON_INTERNAL_ERROR`.

## Conventions that will bite you if ignored

- **Lazy imports inside functions.** Heavy modules (comtypes, uia, PIL, config, telemetry) are imported *inside* the function body, never at module top — especially in `cli/dispatcher.py` and command modules. There's an import-latency guard test (`tests/test_import_latency.py`) enforcing the launcher's ~12ms cold start. Adding a top-level import to the CLI path will regress it.
- **Version is kept in sync across four files** by `tests/test_manifest.py`: `pyproject.toml` (source of truth), `src/agent_windows/__init__.py` (`__version__`), `manifest.json`, `server.json`. Bump all four or the drift test fails. Same test asserts `manifest.json`'s `tools[]` matches the `@mcp tool(name=...)` registrations — **when adding an MCP tool, also add its `manifest.json` entry**.
- **`--config <path>` propagates everywhere.** Set once at the entry point (`set_config_override`), it flows to the spawned daemon via argv scan, applies to reads *and* writes, but state files (pid/lock/version/logs/screenshots) always stay in `~/.agent-windows/`.
- **Config precedence is flag > env > config everywhere.** Daemon re-reads `config.toml` on every command (mtime-checked), so `config set` applies to the next command — except `daemon.idle_timeout_minutes`, read once at startup (needs restart).
- **Debug via env var, not flags threaded through layers.** `--debug` calls `enable_debug()` which sets `AGENT_WINDOWS_DEBUG`; the spawned daemon inherits it and any module reads `is_debug()`. Don't add debug bool parameters.
- **Filesystem blacklist.** User-supplied read/write paths go through `core/filesystem.validate_read_path` / `validate_write_path` (handlers screenshot/highlight/diff/fs). Don't open user paths directly.
- **Tool policy is enforced in two layers.** The MCP server's `--tools`/`--exclude-tools` trims discovery + gates `batch` sub-calls; the daemon separately reads `[tools] include/exclude` from config at startup and rejects filtered methods at `route_request` (`PERMISSION_DENIED`) — so even a direct named-pipe client is bound. `ping`/`shutdown` are always exempt.
- **Conventional Commits** with a module scope (`feat(mcp):`, `fix(config):`, `test(ipc):`). Type hints on all public signatures, Google-style docstrings, 100-char lines.

## Config / secrets

`~/.agent-windows/config.toml` (owner-only ACL). `vlm.api_key` and `[mcp] auth_key` are **DPAPI-encrypted at rest** — always set via `config set` (or `AGENT_WINDOWS_VLM_API_KEY` env var, which takes priority and is never written to disk). Keys are masked in `config show`. Never log or echo raw keys.

## When touching specific areas, run these guards

- CLI entry / lazy imports → `uv run pytest tests/test_import_latency.py`
- Config schema → `uv run pytest tests/test_config.py tests/test_mcp_config.py`
- Manifest / version parity → `uv run pytest tests/test_manifest.py`

The project ships its own agent skill at `skills/agent-windows/SKILL.md` (with `references/`) — the user-facing automation guide and `@eN` selector system. Read it when the task is about *using* the tool to automate the desktop, not about changing its code.
