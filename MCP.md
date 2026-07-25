# MCP Server Guide

agent-windows ships a complete [MCP](https://modelcontextprotocol.io/) server (built with [FastMCP](https://github.com/jlowin/fastmcp)), exposing every CLI command as MCP tools. Any MCP-capable client — Claude Desktop, Cursor, Gemini CLI, etc. — can drive Windows through it.

> Requires the `mcp` extra — FastMCP is an *opt-in* dependency (`[project.optional-dependencies] mcp = ["fastmcp>=3.0"]` in `pyproject.toml`, with no default extras), so a bare `uv tool install agent-windows` does **not** pull it in. Install with `uv tool install "agent-windows[mcp]"`, or from source run `uv sync --extra mcp`.

The server auto-starts the daemon on the first tool call — no separate `daemon start` needed. Use `--debug` for verbose logging (`--debug` sets the `AGENT_WINDOWS_DEBUG` env var via `enable_debug()`; the auto-started daemon inherits it automatically, and any module reads it with `is_debug()` instead of receiving a flag).

```bash
agent-windows mcp serve          # stdio (default) — for local clients
```

> **Alternate config (`--config`):** `mcp serve` accepts the global `--config <path>` flag to run against an alternate `config.toml` — useful for CI, multiple profiles, or testing without touching your real config. The flag propagates to the daemon the server auto-starts, so tool calls use the same config as the server process. State files (pid/lock/logs) stay in the default location. See the Configuration reference.

> **Per-call screenshot resolution:** the `screenshot` tool accepts optional `scale` (e.g. `0.5` for half-resolution), `max_width`, and `max_height` params that override the config defaults for that one capture — handy for a quick low-res peek without changing config. Omit them to use the configured `[screenshot]` values. The returned metadata reports a `Coordinate Scale` factor to apply to image-pixel coords before clicking when a downscale occurred.

## Install in a client

The stdio snippet is the common case — a command + `mcp serve`. Client-specific notes below. `agent-windows` must be on PATH for the client — if it isn't (e.g. an MSIX-sandboxed or WSL client), use the **full absolute path** to the executable (`where agent-windows` to find it; typically `C:\Users\<user>\.local\bin\agent-windows.exe` after `uv tool install`).

### Claude Desktop

Run `agent-windows mcp auth` — it auto-detects your Claude Desktop install (MSIX Store version or standard) and prints the exact path to your `claude_desktop_config.json`, plus a ready-to-paste snippet. The standard `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "agent-windows": { "command": "agent-windows", "args": ["mcp", "serve"] }
  }
}
```

#### Claude Desktop — MSIX / Microsoft Store version

The Store-packaged Claude Desktop virtualizes `%APPDATA%`, so its config is **not** at the usual `%APPDATA%\Claude\`. `agent-windows mcp auth` detects this automatically and prints the real path (it probes `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\`). The printed stdio snippet already uses the absolute path to `agent-windows.exe`, because MSIX sandboxes don't inherit the system PATH:

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

> **`mcp auth --transport stdio`** is also supported — useful to write an `auth_key` to config now (with a stdio client snippet printed) so a later switch to HTTP just needs `mcp serve`. It rejects `--with-tls` (TLS has no effect on stdio) and writes `[mcp] transport = "stdio"` alongside the key. The stdio transport itself ignores `auth_key` (no network surface), so the stored key is dormant until you switch transports.

> **Security:** `auth_key` is **DPAPI-encrypted at rest** (same protection as `vlm.api_key`) and masked in `config show`. TLS keys are written to `~/.agent-windows/` under an owner-only ACL. For stdio none of this applies — there's no network surface.

> **TLS paths:** if you hand-edit `[mcp] ssl_certfile`/`ssl_keyfile` to a relative path, it resolves against the **config file's directory** (so a config under `--config <path>` finds its cert next to itself), not the process CWD. `mcp auth --with-tls` writes absolute paths, so this only matters for hand-edited relative paths. A configured file that's missing makes `mcp serve` fail fast with the resolved path before startup.

### OAuth 2.0 (authorization code + PKCE) — alternative to the static key

As an alternative to a single long-lived bearer `auth_key`, the server can run a real OAuth 2.0 authorization-code flow with PKCE (S256). Configure a pre-provisioned confidential client:

```bash
agent-windows mcp serve --transport streamable-http \
  --host 0.0.0.0 --port 8000 --with-tls \
  --oauth-client-id <id> --oauth-client-secret <secret>
```

Both credentials must be set together — one without the other is rejected. Precedence for each is **flag > env > config** (mirroring the rest of the server):

| Source | Name |
|---|---|
| flag | `--oauth-client-id` / `--oauth-client-secret` |
| env  | `AGENT_WINDOWS_OAUTH_CLIENT_ID` / `AGENT_WINDOWS_OAUTH_CLIENT_SECRET` |
| config | `[security] oauth_client_id` / `oauth_client_secret` |

When both are set, the server mounts four OAuth endpoints (all on the same listener as `/mcp`, riding the same TLS/IP/CORS stack) and accepts a valid OAuth access token as a credential:

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/.well-known/oauth-authorization-server` | RFC 8414 metadata (issuer, endpoints, `code_challenge_methods_supported: ["S256"]`). |
| `POST` | `/oauth/register` | RFC 7591 dynamic registration — **disabled** (`403`, "Configure --oauth-client-id/--oauth-client-secret on the server."). Clients are pre-provisioned. |
| `GET`  | `/oauth/authorize` | Authorization endpoint. Issues a 5-minute single-use code to a caller supplying the configured `client_id`, an S256 PKCE challenge, and a **loopback** `redirect_uri`. `state` is reflected, not bound. |
| `POST` | `/oauth/token` | Code → 1-hour bearer token. Checks `client_id`, `redirect_uri` match, code expiry, PKCE verifier, and `client_secret` (`secrets.compare_digest`). Tokens are validated via `validate_oauth_token`. |

These four paths (plus `/health`) are reachable **before** auth — `_PUBLIC_PATHS` in `mcp/server.py` (shared by the auth, OAuth, **and IP-allowlist** middlewares so they never disagree on what's public) lets a client discover metadata and start the flow unauthenticated; everything else still requires a token. Two middlewares compose the gate:

- **`OAuthOnlyMiddleware`** — used when OAuth is configured but no static `auth_key` is. A request with any valid (non-expired) OAuth access token passes; everything else gets `401`.
- **`AuthKeyMiddleware` with `oauth_validator` fallback** — used when *both* OAuth and an `auth_key` are configured. The static key is checked first; on a miss, a valid OAuth token is accepted as a fallback.

> **Threat model — read before exposing this remotely.** Per the long caveat at the top of `src/agent_windows/mcp/oauth.py`, this OAuth server is a **transport-credentials convenience for single-user / local-agent deployments**, NOT an authorization-decision layer and NOT safe for multi-tenant use. Concretely: the authorization endpoint issues a code to *any* caller that knows the configured `client_id` and supplies a loopback `redirect_uri` + PKCE challenge — there is **no resource-owner consent screen and no session binding**, and `state` is reflected rather than verified. Any local process that can reach `127.0.0.1:<port>` (trivially, via a browser-driven `fetch` to localhost) can complete the flow on its own loopback redirect, read the secret from the same user context, exchange the code, and receive a 1-hour token granting the *full* MCP toolset (`shell`, `registry`, file I/O). PKCE is end-to-end attacker-controlled in this local-drive scenario, so it authenticates nothing here.
>
> The hardening that *does* apply: `redirect_uri` is constrained to loopback and re-checked at `/oauth/token`; `client_secret` is compared in constant time; the HTTP bind still refuses non-loopback addresses without an `auth_key`/OAuth client or `--allow-insecure-remote`; and tokens/codes live only in process memory. **State is not persisted** — a server restart invalidates every token and code; clients must re-authenticate. If you need real authorization boundaries across mutually-distrusting clients, put a real auth/consent front-end (with persistence and consent) in front of this server; do not rely on this module for that.

## HTTP security & scaling options

A bare `mcp serve --transport streamable-http --host 0.0.0.0` with no `auth_key` is refused — it would expose the full desktop toolset to the LAN. Set an auth key (`mcp auth`) or pass `--allow-insecure-remote` to opt in. Additional HTTP-only controls (all settable via flag, `AGENT_WINDOWS_*` env var, or `[security]`/`[mcp]`/`[tools]` config; flag > env > config):

| Option | Purpose |
|---|---|
| `--allow-insecure-remote` | Allow an unauthenticated non-loopback bind (prints a warning; not recommended). |
| `--ip-allowlist 10.0.0.0/8,192.168.1.5` | Restrict HTTP clients to the given IPs/CIDRs (IPv4+IPv6); others get 403. `/health` **and the four OAuth endpoints** are exempt (so the OAuth flow can start unauthenticated — see below). |
| `--cors-origins https://my-client.example.com` | Allow cross-origin browser access. **Default: none** — no CORS headers are emitted, so Same-Origin Policy blocks cross-origin requests. |
| `--tools screenshot,click,snapshot` | Restrict to these tools (whitelist); removes all others. Overrides `--exclude-tools`. |
| `--exclude-tools shell,registry` | Remove these tools from the active set. |

> **These filters are enforced, not just hidden.** A filtered-out tool is genuinely blocked — the `batch` tool applies the same filter to its `commands[].method` sub-calls, so excluding `shell` also blocks `batch({"method":"shell"})` (the whole batch aborts with the offenders named). Enforcement runs in two layers: this MCP server process (discovery trim + the `batch` gate), **and** the daemon itself — at startup the daemon reads `[tools] include/exclude` from config and rejects any filtered method at the router (`PERMISSION_DENIED`), so even a process connecting *directly* to the named pipe (bypassing this server) is bound by the policy. Note the daemon honors the **config** `[tools]` section, not the `--tools`/`--exclude-tools` flags (those are process-local to this MCP server and never written to config); to lock the daemon, set `[tools]` in `config.toml`. The daemon's two **control-plane** methods — `ping` (health check) and `shutdown` (daemon-stop lifecycle, reached via `agent-windows daemon stop`) — are always exempt from the daemon `[tools]` policy (`_POLICY_EXEMPT = {"ping","shutdown"}` in `daemon/router.py`), so an over-restrictive typo can't brick the daemon into something unprobeable or unkillable. That exemption does **not** extend to the MCP `shutdown` **power tool** (system power-off) — that tool calls daemon method `system_shutdown`, a normal automation method, and IS excludable at both layers: `--exclude-tools shutdown` (MCP) and `[tools] exclude=["system_shutdown"]` (daemon). The MCP `restart` power tool is the same shape (daemon method `system_restart`), excludable via `--exclude-tools restart` / `[tools] exclude=["system_restart"]`. Likewise the MCP `ping` tool is removable via `--exclude-tools ping` (it only trims MCP discovery; the daemon method stays reachable to the daemon's own clients). See [SECURITY.md](SECURITY.md). Use the valid tool names from [manifest.json](https://github.com/XiaofengdiZhu/agent-windows/blob/main/manifest.json) (`tools[].name`) or the catalog below.
| `--stateless-http` | Run streamable-http without `Mcp-Session-Id` / per-connection state — reconnecting clients survive a server restart, and the server scales behind a load balancer. Streamable-http only. |
| `--file-transfer` / `--no-file-transfer` | Enable or kill-switch the HTTP `/files` file-bridge endpoints (host↔Windows). Default on; `--no-file-transfer` unmounts them (`/files` → 404). HTTP only. |
| `--max-upload-mb 512` | Max `/files` PUT body size in MiB (`0` = unlimited); oversize → 413. Default 512. |

> **DNS-rebinding protection (automatic, no flag).** On an HTTP transport the server installs starlette's `TrustedHostMiddleware` and validates every request's `Host` header against the addresses it actually bound to (`compute_allowed_hosts` in `mcp/server.py`, called from `mcp_cmd.py`): loopback bind → `["localhost","127.0.0.1","[::1]"]`; a named-host bind (not a wildcard) → a one-element allowlist of that host; `0.0.0.0`/`::`/`""` → no check (can't name a finite trusted set). A mismatched `Host` returns `400`. This closes the standard DNS-rebinding attack (attacker points a domain they control at your loopback/LAN address so cross-origin browser requests carry *their* Host, bypassing SOP) — and it also validates websocket `Host` headers, a tighter check than the bearer/IP gates. Disabled only when you opt into `--allow-insecure-remote`. Never applies to `stdio`.

> **Local-only telemetry (`--telemetry`).** Off by default. When enabled, every tool call (its source `mcp`/`cli`, name, success/failure, and wall-clock duration) is appended as one line to `~/.agent-windows/logs/telemetry.log`. Zero network, zero new deps. Applies to **all** transports (including stdio) — each surviving tool (post-filter) is wrapped with a timing decorator. Set with `--telemetry`, `AGENT_WINDOWS_TELEMETRY=1`, or `[telemetry] enabled = true` (flag > env > config). CLI invocations are logged too, but only via `[telemetry] enabled` or `AGENT_WINDOWS_TELEMETRY=1` — the `--telemetry` flag is `mcp serve`-only (the CLI has no per-subcommand telemetry flag).

Config equivalents (in `~/.agent-windows/config.toml`):

```toml
[mcp]
transport = "stdio"             # stdio | sse | streamable-http
host = "127.0.0.1"              # bind address (HTTP transports)
port = 8000                     # bind port
auth_key = ""                   # HTTP bearer token (DPAPI-encrypted at rest; set via `mcp auth`)
ssl_certfile = ""               # HTTPS cert path (set via `mcp auth --with-tls`; relative resolves against this file's dir)
ssl_keyfile = ""                # HTTPS key path (likewise)
allow_insecure_remote = false   # opt into an insecure non-loopback bind
stateless_http = false          # stateless streamable-http
file_transfer = true            # enable the HTTP /files file-bridge endpoints
max_upload_mb = 512             # /files PUT size cap in MiB (0 = unlimited)

[security]
ip_allowlist = ["10.0.0.0/8"]   # CIDR/IP list; empty = no restriction
cors_origins = ["https://my-client.example.com"]   # empty = no CORS headers
oauth_client_id = ""            # OAuth 2.0 confidential client (flag/env only — NOT `config set`-able; hand-edit config.toml if needed)
oauth_client_secret = ""        # paired secret (NOT DPAPI-encrypted; flag/env only — NOT `config set`-able; hand-edit config.toml if needed)

[tools]
include = ["screenshot", "snapshot"]   # whitelist (overrides exclude)
exclude = ["shell", "registry"]        # blacklist

[telemetry]
enabled = false                 # append each tool call's name/result/duration to logs/telemetry.log
```

> The daemon honors this `[tools]` section using **daemon method names** (write it as a TOML array — `config set` only writes scalars, so a comma-joined value matches nothing). The names match the MCP tool names above except for `ask-vision`/`wait_check`/`wait_for_check` (the daemon uses the hyphenated/poll names); the high-risk names (`shell`, `registry`, `fs_delete`, `process_kill`, …) are identical.

## File transfer over HTTP (`/files`)

On an HTTP transport the server also exposes three **file-transfer** endpoints on the *same listener* — the host↔Windows file bridge. They are plain HTTP (not MCP/JSON-RPC): an agent running elsewhere uses `curl` (or any HTTP client) to push/pull files to/from the Windows box. This fills a gap the `fs_*` MCP tools can't — those are text-only and local-only, so without `/files` you'd shell out to `scp`/`Invoke-WebRequest` by hand to move a binary blob.

The endpoints ride the **entire `/mcp` security stack** — bearer auth, IP allowlist, TLS, DNS-rebinding. They are intentionally *not* in the public-path set, so every request is authenticated exactly like a tool call. Set an auth key first (`mcp auth`).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/files?path=<abs>` | Stream a file **down** (Windows → host). |
| `HEAD` | `/files?path=<abs>` | Headers only — size + `X-SHA256` for integrity checking, no body. |
| `PUT` | `/files?path=<abs>&overwrite=true` | Stream a file **up** (host → Windows); raw octet-stream body. |

`path` is a URL-encoded absolute path. Directories return `400` (zip first, or use `fs_list`). Uploads create parent dirs as needed (`mkdir -p`, like `fs_write`). The path blacklist is the same `fs_*` one (`.ssh`, install trees, …) via `validate_read_path` / `validate_write_path`.

**Download + verify** (HEAD carries `X-SHA256`; GET does not, to avoid reading a large file twice):

```bash
# HEAD first to get the size + sha256, then download
curl -sI -H "Authorization: Bearer $KEY" \
  "http://host:8000/files?path=C%3A%5CUsers%5Cme%5Cbig.bin"
# → x-size: 10485760 ; x-sha256: 9f86d081…

curl -fsS -H "Authorization: Bearer $KEY" \
  "http://host:8000/files?path=C%3A%5CUsers%5Cme%5Cbig.bin" -o big.bin
sha256sum big.bin   # must match x-sha256
```

**Upload:**

```bash
curl -fsS -X PUT -H "Authorization: Bearer $KEY" \
  --data-binary @local.bin \
  "http://host:8000/files?path=C%3A%5CUsers%5Cme%5Cdest.bin&overwrite=true"
# → {"path":"C:\\Users\\me\\dest.bin","bytes":10485760,"sha256":"9f86d081…"}
```

Status codes: `200` (GET body / PUT JSON `{path, bytes, sha256}`), `400` (missing/relative path, directory), `403` (path blacklist / IP allowlist), `404` (not found), `409` (PUT target exists without `overwrite=true`), `413` (body exceeds `max_upload_mb`).

> **Concurrency:** concurrent PUTs to the *same* path serialize — the first creates/overwrites, a later one without `overwrite=true` gets `409`. PUTs to *different* paths run in parallel.

> **Kill-switch:** `--no-file-transfer` (or `[mcp] file_transfer = false`) leaves the routes unmounted — `/files` returns `404`. Never mounted on `stdio` (no listener). `max_upload_mb = 0` removes the upload cap.

## Transports

| Transport | Command | Use case |
|---|---|---|
| stdio (default) | `mcp serve` | Local clients (Claude Desktop, Cursor) |
| streamable-http | `mcp serve --transport streamable-http` | Remote clients (modern MCP) |
| sse | `mcp serve --transport sse` | Remote clients (legacy SSE) |

## Tools

The server exposes **64 tools** mapping 1:1 to the CLI commands: `screenshot`, `snapshot`, `snapshot_diff`, `observe`, `ground`, `detect`, `ask_vision`, `highlight`, `diff`, `find_image`, `pixel_at`, `get_text`, `element_state`, `cleanup_screenshots`, `list_displays`, `click`, `dblclick`, `hover`, `move`, `mouse_down`, `mouse_up`, `drag`, `scroll`, `type`, `shortcut`, `key_down`, `key_up`, `expand`, `collapse`, `app_launch`, `app_switch`, `app_resize`, `app_list`, `window_close`, `window_enable`, `window_focus`, `window_maximize`, `window_minimize`, `window_restore`, `shell`, `fs_read/write/list/copy/move/delete/search/info`, `clipboard_get/set`, `process_list/kill`, `notify`, `registry`, `lock`, `sleep`, `shutdown`, `restart`, `batch`, `multi_select`, `multi_edit`, `ping`, `wait`, `wait_for`. See the README [Commands](README.md#commands) table for what each does; the MCP tool names match the daemon method names (except `shutdown`/`restart` → daemon `system_shutdown`/`system_restart`, and `ask_vision`/`wait`/`wait_for` → daemon `ask-vision`/`wait_check`/`wait_for_check`). These exact MCP tool names are the valid values for `--tools`/`--exclude-tools` (authoritative list in [manifest.json](https://github.com/XiaofengdiZhu/agent-windows/blob/main/manifest.json) `tools[].name`). `batch` is itself a tool, so it is removable the same way — but when present it enforces the active filter on its own sub-commands (see the note under [HTTP security & scaling options](#http-security--scaling-options)).

Same usage tips apply: call `snapshot` first for pixel-exact coordinates, prefer it over `ground`/`detect` for named controls, and always `app switch` before input. `snapshot` additionally takes `window` (HWND / title substring / process name) and `active` (bool) to scope the tree to a single top-level window — `@eN` refs restart at `@e1` within that subtree and stay aligned with the 2s cache slot. `list_displays` enumerates monitors (index, primary, resolution, rect, work area, device name) so you know which N to pass to the `display` arg instead of guessing.

> **Vision tools need a VLM key.** `ground`, `detect`, and `ask_vision` call a multimodal model and require an API key. Set it with `config set vlm.api_key <key>` (DPAPI-encrypted at rest, masked in `config show`), or via the `AGENT_WINDOWS_VLM_API_KEY` env var — which **takes priority** over the on-disk value and is **never written to disk** (handy for CI/secrets stores). Without a key configured those three tools fail; the other 61 tools work without one.

### Per-tool limits (bound worker memory & blocking time)

The single worker thread caps a few inputs so one oversized call can't exhaust memory or block the desktop for minutes. These are enforced at the daemon router (→ `INVALID_PARAMS` if exceeded), not just advisory:

| Tool | Limit | Notes |
|---|---|---|
| `type` | **10000 chars** of `text` | Typing is char-by-char with a delay; a longer string would block the worker for hours. Write the text to a file (`fs write`) and paste it, or type in chunks, for longer content. |
| `fs_read` | **~5 MiB** returned; a line range (`offset`/`limit`) is also byte-bounded | `offset`/`limit` are 1-based (0 or omitted = whole file); `limit` is capped at 100000 lines. A multi-GB file is never read whole — large files are streamed. |
| `fs_list` | **5000 entries** | A huge directory returns the first 5000 (sorted) + a `[Truncated: ...]` notice; memory stays bounded regardless of tree size. |
| `fs_search` | **1000 matches** | Stops and reports truncation after 1000 matches. |

These caps exist because the worker is single-threaded and shared — an unbounded read/list/type would stall every other command behind it.
