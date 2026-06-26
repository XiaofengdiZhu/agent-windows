# MCP Server Guide

agent-windows ships a complete [MCP](https://modelcontextprotocol.io/) server (built with [FastMCP](https://github.com/jlowin/fastmcp)), exposing every CLI command as MCP tools. Any MCP-capable client — Claude Desktop, Cursor, Gemini CLI, etc. — can drive Windows through it.

> Requires the `mcp` extra: `uv tool install agent-windows` installs it by default; from source run `uv sync --extra mcp`.

The server auto-starts the daemon on the first tool call — no separate `daemon start` needed. Use `--debug` for verbose logging.

```bash
agent-windows mcp serve          # stdio (default) — for local clients
```

> **Alternate config (`--config`):** `mcp serve` accepts the global `--config <path>` flag to run against an alternate `config.toml` — useful for CI, multiple profiles, or testing without touching your real config. The flag propagates to the daemon the server auto-starts, so tool calls use the same config as the server process. State files (pid/lock/logs) stay in the default location. See the Configuration reference.

> **Per-call screenshot resolution:** the `screenshot` tool accepts optional `scale` (e.g. `0.5` for half-resolution), `max_width`, and `max_height` params that override the config defaults for that one capture — handy for a quick low-res peek without changing config. Omit them to use the configured `[screenshot]` values. The returned metadata reports a `Coordinate Scale` factor to apply to image-pixel coords before clicking when a downscale occurred.

## Install in a client

The stdio snippet is the common case — a command + `mcp serve`. Client-specific notes below. `agent-windows` must be on PATH for the client — if it isn't (e.g. an MSIX-sandboxed or WSL client), use the **full absolute path** to the executable (`where agent-windows` to find it; typically `C:\Users\<user>\.local\bin\agent-windows.exe` after `uv tool install`).

### Claude Desktop

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "agent-windows": { "command": "agent-windows", "args": ["mcp", "serve"] }
  }
}
```

#### Claude Desktop — MSIX / Microsoft Store version

The Store-packaged Claude Desktop virtualizes `%APPDATA%`, so its config is **not** at the usual `%APPDATA%\Claude\`. Find it at:

```
%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json
```

MSIX sandboxed apps also don't inherit the system `PATH`, so use the full path to `agent-windows.exe`:

```json
{
  "mcpServers": {
    "agent-windows": {
      "command": "C:\\Users\\<user>\\.local\\bin\\agent-windows.exe",
      "args": ["mcp", "serve"]
    }
  }
}
```

Fully quit Claude Desktop (tray → Quit) and reopen after saving.

### Cursor

Add to `~/.cursor/mcp.json` (or project `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "agent-windows": { "command": "agent-windows", "args": ["mcp", "serve"] }
  }
}
```

### Perplexity Desktop

In Settings → Connectors, add a new MCP server with this JSON (Perplexity uses the standard `mcpServers` shape):

```json
{
  "mcpServers": {
    "agent-windows": { "command": "agent-windows", "args": ["mcp", "serve"] }
  }
}
```

### Gemini CLI

Use the `gemini mcp add` command, or edit `~/.gemini/settings.json` under `mcpServers`:

```bash
gemini mcp add agent-windows -- agent-windows mcp serve
```

```json
{
  "mcpServers": {
    "agent-windows": { "command": "agent-windows", "args": ["mcp", "serve"] }
  }
}
```

### Qwen Code

Use the `qwen mcp add` command (writes to `~/.qwen/settings.json` under `mcpServers`), or edit the file directly:

```bash
qwen mcp add agent-windows agent-windows mcp serve
```

```json
{
  "mcpServers": {
    "agent-windows": { "command": "agent-windows", "args": ["mcp", "serve"] }
  }
}
```

### Codex CLI

Codex reads `~/.codex/config.toml`. Add under `[mcp_servers]` (stdio only):

```toml
[mcp_servers.agent-windows]
command = "agent-windows"
args = ["mcp", "serve"]
```

### Claude Code

Add as a stdio MCP server:

```bash
claude mcp add agent-windows -- agent-windows mcp serve
```

<details>
<summary><b>Claude Code from WSL / Linux driving Windows</b></summary>

Claude Code running **inside WSL** can't run the Windows `agent-windows.exe` directly, but it can shell out to it via `powershell.exe`. Point the MCP server command at PowerShell, which runs the Windows binary:

```bash
claude mcp add agent-windows -- powershell.exe -NoProfile -Command "agent-windows mcp serve"
```

If `agent-windows` isn't on the Windows PATH seen from WSL, use the full path:

```bash
claude mcp add agent-windows -- powershell.exe -NoProfile -Command "& 'C:\Users\<user>\.local\bin\agent-windows.exe' mcp serve"
```

This adds a small per-call overhead (PowerShell startup) but works from any WSL distro. The daemon still runs as a Windows process driving the real desktop.

</details>

## Remote access (HTTP) with authentication

stdio is local-only. For remote/LAN clients use an HTTP transport **behind an auth key** — a bare, unauthenticated bind to a non-loopback address is insecure. `mcp auth` generates a bearer token (DPAPI-encrypted at rest in `[mcp] auth_key`), optionally a TLS cert, and prints the ready-to-paste client config:

```bash
# 1. Generate an auth key + TLS cert, save to ~/.agent-windows/config.toml
agent-windows mcp auth --transport streamable-http --host 0.0.0.0 --port 8000 --with-tls

# 2. (Optional) Auto-start the server at every login as a Scheduled Task
agent-windows mcp install
agent-windows mcp uninstall     # stop + remove the auto-start task
```

The printed client snippet already includes the `Authorization: Bearer <key>` header. The server rejects any HTTP request that doesn't carry it (401). Regenerate with `--force`.

> **Security:** `auth_key` is **DPAPI-encrypted at rest** (same protection as `vlm.api_key`) and masked in `config show`. TLS keys are written to `~/.agent-windows/` under an owner-only ACL. For stdio none of this applies — there's no network surface.

## HTTP security & scaling options

A bare `mcp serve --transport streamable-http --host 0.0.0.0` with no `auth_key` is refused — it would expose the full desktop toolset to the LAN. Set an auth key (`mcp auth`) or pass `--allow-insecure-remote` to opt in. Additional HTTP-only controls (all settable via flag, `AGENT_WINDOWS_*` env var, or `[security]`/`[mcp]`/`[tools]` config; flag > env > config):

| Option | Purpose |
|---|---|
| `--allow-insecure-remote` | Allow an unauthenticated non-loopback bind (prints a warning; not recommended). |
| `--ip-allowlist 10.0.0.0/8,192.168.1.5` | Restrict HTTP clients to the given IPs/CIDRs (IPv4+IPv6); others get 403. `/health` is exempt. |
| `--cors-origins https://my-client.example.com` | Allow cross-origin browser access. **Default: none** — no CORS headers are emitted, so Same-Origin Policy blocks cross-origin requests. |
| `--tools screenshot,click,snapshot` | Enable only these tools (whitelist); removes all others. Overrides `--exclude-tools`. |
| `--exclude-tools shell,registry` | Remove these tools from the active set. |
| `--stateless-http` | Run streamable-http without `Mcp-Session-Id` / per-connection state — reconnecting clients survive a server restart, and the server scales behind a load balancer. Streamable-http only. |

Config equivalents (in `~/.agent-windows/config.toml`):

```toml
[mcp]
allow_insecure_remote = false   # opt into an insecure non-loopback bind
stateless_http = false          # stateless streamable-http

[security]
ip_allowlist = ["10.0.0.0/8"]   # CIDR/IP list; empty = no restriction
cors_origins = ["https://my-client.example.com"]   # empty = no CORS headers

[tools]
include = ["screenshot", "snapshot"]   # whitelist (overrides exclude)
exclude = ["shell", "registry"]        # blacklist
```

## Transports

| Transport | Command | Use case |
|---|---|---|
| stdio (default) | `mcp serve` | Local clients (Claude Desktop, Cursor) |
| streamable-http | `mcp serve --transport streamable-http` | Remote clients (modern MCP) |
| sse | `mcp serve --transport sse` | Remote clients (legacy SSE) |

## Tools

The server exposes ~48 tools mapping 1:1 to the CLI commands: `screenshot`, `snapshot`, `observe`, `ground`, `detect`, `ask_vision`, `highlight`, `diff`, `get_text`, `element_state`, `cleanup_screenshots`, `click`, `dblclick`, `hover`, `move`, `mouse_down`, `mouse_up`, `drag`, `scroll`, `type`, `shortcut`, `expand`, `collapse`, `app_launch`, `app_switch`, `app_resize`, `app_list`, `shell`, `fs_read/write/list/copy/move/delete/search/info`, `clipboard_get/set`, `process_list/kill`, `notify`, `registry`, `batch`, `multi_select`, `multi_edit`, `ping`, `wait`, `wait_for`. See the README [Commands](README.md#commands) table for what each does; the MCP tool names match the daemon method names.

Same usage tips apply: call `snapshot` first for pixel-exact coordinates, prefer it over `ground`/`detect` for named controls, and always `app switch` before input.
