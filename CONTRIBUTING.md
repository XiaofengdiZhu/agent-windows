# Contributing to agent-windows

Thanks for your interest in contributing! This guide covers setup, workflow, style, testing, and releasing.

## Code of Conduct

Be respectful and constructive. We welcome newcomers and accept criticism gracefully. Focus on what's best for the project and community.

## Getting Started

### Prerequisites

- **Windows 8.1 / 10 / 11** (the tool drives the Windows desktop via UIA/COM)
- **Python 3.13+**
- **[uv](https://docs.astral.sh/uv/)** — the package runner used throughout
- **Git**
- **Rust + MSVC + Windows SDK** — *only* if you'll change the native launcher (`src/launcher/`)

### Setup

```bash
git clone https://github.com/XiaofengdiZhu/agent-windows.git
cd agent-windows
uv sync                  # core deps
uv sync --extra mcp      # MCP server (fastmcp)
uv sync --extra dev      # pytest
```

Verify:

```bash
uv run agent-windows --help
uv run pytest -q
```

## Development Workflow

### Branching

Branch from `main` with a descriptive prefix:

- `feat/<topic>` — new feature
- `fix/<topic>` — bug fix
- `docs/<topic>` — documentation
- `refactor/<topic>` — refactoring

### Commit Messages

This project follows **Conventional Commits** with an optional scope. Examples from the actual history:

```
feat(mcp): add streamable-http transport
fix(config): type-aware set_config_value parsing
test(ipc): isolate daemon-IPC tests from host daemon state
docs: document the MCP server
refactor(mcp): serve() reads typed MCP config view
chore: ignore the recurring 'nul' file
```

The scope is usually a module (`mcp`, `config`, `ipc`, `telemetry`). Keep the subject line under ~72 chars, imperative mood.

## Code Style

- **Type hints** on all public function signatures.
- **Docstrings**: Google style for public functions/classes.
- **Line length**: 100 characters.
- Match the surrounding code's density and naming.

## Testing

```bash
uv run pytest              # full suite (1500+ tests)
uv run pytest tests/test_config.py -v   # one file
```

Guards worth knowing when you touch specific areas:

- **CLI entry / lazy imports** → run `uv run pytest tests/test_import_latency.py`. There's an import-latency guard (the native launcher's ~12ms cold start must not regress because the Python path got heavier).
- **Config schema** → run `uv run pytest tests/test_config.py tests/test_mcp_config.py`. Strict type validation + dataclass-default drift guards (TODO #10).
- **Manifest / version** → run `uv run pytest tests/test_manifest.py`. Asserts version parity across `pyproject.toml`, `__init__.py`, `manifest.json`, `server.json`, and that `manifest.json`'s tool list matches the `@mcp.tool(name=...)` registrations (TODO #11).

When adding a new MCP tool, **also add its entry to `manifest.json`'s `tools[]`** — the drift test will fail until you do.

## Pull Requests

Before submitting:

- [ ] Tests pass (`uv run pytest`)
- [ ] No sensitive data (API keys, secrets) in the diff
- [ ] Docs updated where relevant (README, docstrings)
- [ ] Commit messages follow Conventional Commits

Open the PR against `main` and include: what it does, why, and how it was tested.

## Releasing

agent-windows publishes to [PyPI](https://pypi.org/project/agent-windows/) via the tag-triggered workflow `.github/workflows/publish.yml`, which uses **PyPI Trusted Publisher (OIDC)** — no API token is stored in the repo.

### Prerequisites (one-time, by the repo owner)

1. On PyPI, configure `agent-windows` as a Trusted Publisher pointing at this repo, the workflow file `.github/workflows/publish.yml`, and the environment `pypi`.
2. Create a `pypi` environment in the repo's GitHub settings (Settings → Environments).

### Release steps

1. **Bump the version in all four sources** (kept in sync by `test_manifest.py`):
   - `pyproject.toml` → `version = "X.Y.Z"` (the single source of truth)
   - `src/agent_windows/__init__.py` → `__version__ = "X.Y.Z"`
   - `manifest.json` → `"version": "X.Y.Z"`
   - `server.json` → top-level `"version"` and `packages[0].version`
2. **Verify drift guards pass**: `uv run pytest tests/test_manifest.py`.
3. **Rebuild the launcher if you changed it** (`src/launcher/`): the launcher is built from source on release (the workflow runs `cargo build --release` on `windows-latest` — no binary is committed). For local development/testing, rebuild and the exe is staged automatically:
   ```bash
   cd src/launcher && build.cmd build --release
   # the exe lands in prebuilt/ (gitignored); run this before `uv build`
   # locally, or the wheel won't include the launcher
   ```
4. **Tag and push**:
   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```
   The workflow builds (`uv build`), publishes to PyPI, and creates a GitHub Release with auto-generated notes.

> `skip-existing: true` makes re-running a tag idempotent (PyPI won't reject an already-published version).

## Security Vulnerabilities

**Do not** report security issues via public issues. See [SECURITY.md](SECURITY.md) for private reporting (email / GitHub Security Advisories).

## License

By contributing, you agree your contributions are licensed under the [MIT License](LICENSE). The vendored `uia` module under `src/agent_windows/uia/` is Apache-2.0 (see `src/agent_windows/uia/LICENSE-APACHE.txt`); contributions touching it must respect that license.
