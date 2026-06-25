# Security Policy

## Overview

agent-windows is a Windows Computer-Use CLI that drives the **real Windows desktop** on behalf of an AI agent: it captures screenshots, reads the UIA accessibility tree, locates elements by vision, and automates mouse/keyboard, applications, files, the clipboard, processes, and the registry. It is **NOT sandboxed** — every tool call performs a real action on the machine running the server.

## ⚠️ Critical Warning — Read Before Deploying

### Direct Operating System Interaction

agent-windows interacts **directly with your actual Windows operating system**. This means:

- **Real System Actions**: every tool call executes a real action — a real click, real keystrokes, a real PowerShell command, a real screenshot.
- **No Safety Net**: there is no simulation, preview, or rollback layer.
- **User Permissions**: the server runs with the permissions of the user that started it.

### Irreversible and Destructive Changes

Many operations **cannot be undone**:

- **File Deletion** (`fs_delete`) and registry deletion (`registry delete`) — permanent.
- **Text Overwrite** (`type` with `clear=True`) — replaces existing content without recovery.
- **PowerShell** (`shell`) — can modify the registry, services, and system configuration.
- **UI Confirmations** (`click`, `shortcut`) — clicking "Delete"/"Yes"/"Confirm" or pressing `Alt+F4` has real consequences.

### Where NOT to Deploy

**Do not** deploy agent-windows where you cannot tolerate:

- Accidental data loss or corruption
- Unintended system configuration changes
- Exposure of sensitive information through screenshots
- Execution of malicious commands if the driving LLM is compromised

**Never** deploy on production servers, systems with irreplaceable data, compliance-regulated environments (healthcare, finance, government), or any machine you don't fully control.

### Recommended Safe Deployment

For safer use, **strongly consider**:

- ✅ **Virtual Machines** (Hyper-V, VMware, VirtualBox) with snapshots before each session
- ✅ **Windows Sandbox** (built into Windows 10/11 Pro/Enterprise)
- ✅ Dedicated test machines with regular backups

## Transport & Authentication Security Posture

This is the most important section: how the network surface is (or isn't) exposed.

### stdio (default) — local only

`agent-windows mcp serve` defaults to **stdio** (stdin/stdout). There is **no network surface** — it's for local clients like Claude Desktop or Cursor. Use this for any single-machine setup. None of the HTTP/security options below apply to stdio.

### HTTP transports (sse / streamable-http) — authentication enforced

For remote/LAN clients an HTTP transport is used, and **a bare, unauthenticated bind to a non-loopback address is refused at startup**. `mcp serve --host 0.0.0.0` with no auth configured exits with an error rather than exposing the full toolset to the LAN. You must opt in to one of the following:

| Mechanism | What it does |
|---|---|
| **Auth key** (`mcp auth`, or `[mcp] auth_key`) | A bearer token (`Authorization: Bearer <key>`) required on every request, compared in constant time (`secrets.compare_digest`). The key is **DPAPI-encrypted at rest** and masked in `config show`. |
| **OAuth 2.0 + PKCE** (`--oauth-client-id/--oauth-client-secret`) | Full authorization-code + PKCE flow; the four OAuth endpoints are mounted and an OAuth access token is a valid credential. |
| **`--allow-insecure-remote`** | Explicit opt-out of the auth requirement (prints a warning). Not recommended. |

### Additional HTTP-only controls

| Control | Default | Purpose |
|---|---|---|
| `--ip-allowlist 10.0.0.0/8,...` | empty (no restriction) | Restrict HTTP clients to the given IPs/CIDRs (IPv4+IPv6); others get 403. `/health` is exempt. |
| `--cors-origins https://...` | **empty — no CORS headers emitted** | Cross-origin browser access is blocked by Same-Origin Policy unless explicitly allowed. |
| DNS-rebinding protection (TrustedHost) | **on** (loopback/named-host binds) | Validates the `Host` header against the bind address so a rebinding attack can't reach a localhost-bound server. Opt out only via `--allow-insecure-remote`. |
| `--ssl-certfile` / `--ssl-keyfile` | off | Enable HTTPS for HTTP transports. |
| `--tools` / `--exclude-tools` | all tools | Whitelist or blacklist tools at startup — e.g. `--exclude-tools shell,registry` to remove the most destructive tools. |

For full option reference see the [README MCP Server section](README.md#mcp-server).

## Tool Risk Classification

Based on the tools' security annotations (`readOnlyHint` / `destructiveHint`):

### High-Risk (potentially destructive)

| Tool | Risk | Why |
|---|---|---|
| `shell` | Critical | Arbitrary PowerShell — system modifications, file deletion, network operations |
| `registry` (set/delete) | Critical | Persistent system configuration changes |
| `fs_delete` | High | Permanent file/directory deletion |
| `click`, `shortcut` | High | Can trigger destructive UI actions (delete confirmations, `Alt+F4`) |
| `type` (`clear=True`) | High | Overwrites existing text without recovery |

### Medium-Risk (modifying, generally non-destructive)

`app_launch`, `process_kill`, `clipboard_set`, `fs_write`, `fs_move`, `multi_*`.

### Low-Risk (read-only)

`snapshot`, `screenshot`, `get_text`, `element_state`, `ping`, `wait`, `wait_for`, `fs_read`, `fs_list`, `fs_info`, `process_list`, `app_list`.

## Telemetry & Data Privacy

agent-windows collects **optional, local-only** usage data — and it is **off by default**:

- **When enabled** (`AGENT_WINDOWS_TELEMETRY=true` or `[telemetry] enabled=true`), each tool call's **name, success/failure, and wall-clock duration** are appended as one line to `~/.agent-windows/logs/telemetry.log`. **Zero network.**
- **We do NOT collect**: tool arguments (typed text, coordinates, file paths), tool outputs (screenshots, command results, page content), or personal identifiers beyond a locally-generated session id.
- Screenshots and captured state remain strictly on the local machine.

## Best Practices

1. **Run with least privilege** — use a standard user account, not an administrator.
2. **Connect only trusted MCP clients** — review the client's security practices.
3. **Prefer stdio** — no network surface; use HTTP only when you need remote access, and always behind an auth key or OAuth.
4. **Disable high-risk tools** for untrusted workflows: `--exclude-tools shell,registry`.
5. **Keep regular, verified backups**.
6. **Monitor** the driving agent's actions in real time.

## Vulnerability Reporting

**Do NOT** open a public GitHub issue for security vulnerabilities.

Instead, please:

1. Email the maintainer at **523084467@qq.com**, or
2. Use [GitHub Security Advisories](https://github.com/XiaofengdiZhu/agent-windows/security/advisories) (preferred).

Please include: a description, steps to reproduce, potential impact, and a suggested fix if available. We aim to acknowledge within 48 hours and provide a status update within 7 days.

## Disclaimer

**USE AT YOUR OWN RISK.** agent-windows is provided "as is" without warranty of any kind. The maintainer is not responsible for data loss, system damage, security breaches, or the actions performed by LLM agents using this server. Users are solely responsible for appropriate use, implementing security controls, and complying with applicable laws.

## License

This security policy is part of the agent-windows project, licensed under the MIT License. See [LICENSE](LICENSE).
