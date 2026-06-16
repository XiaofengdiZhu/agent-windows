"""End-to-end daemon smoke tests.

These spawn the REAL daemon as a subprocess (``python -m agent_windows --daemon``
under the hood), send a ``ping`` over the real named pipe, and assert the full
round-trip — with zero mocking of the IPC, the server loop, or the COM worker.

Isolation (no collision with a real daemon, no polluting ``~/.agent-windows/``):
- a UNIQUE per-test pipe name (PID + suffix) is injected into the daemon
  subprocess by wrapping ``agent_windows.daemon.server.create_pipe_server``
  (the binding the main loop calls at server.py:350) to force our name — the
  original is preserved so the owner-only security descriptor (M-02) still
  builds. The client-side ``send_to_daemon`` gets the same name explicitly;
- a tmp data dir is injected by monkeypatching
  ``agent_windows.utils.config.get_data_dir``, redirecting PID/version/log
  files away from the user's real ``~/.agent-windows/``.

NOTE: a leftover ``AGENT_WINDOWS_E2E=1`` in the shell silently enables these in
a plain ``pytest tests/`` run — unset it to restore the side-effect-free default.

These tests are OPT-IN: they skip unless ``AGENT_WINDOWS_E2E=1`` is set, so the
default ``pytest`` run stays side-effect-free. The pipe name + data dir are
fully isolated, so running them is safe even with a real daemon up — but
keeping them off by default avoids spawning a subprocess in every local run.
(TEST3-07)
"""
import json
import os
import subprocess
import sys
import time

import pytest

from agent_windows.ipc.protocol import JSONRPCRequest, JSONRPCResponse
from agent_windows.ipc.pipe import send_to_daemon, pipe_exists

pytestmark = pytest.mark.e2e


def _e2e_enabled() -> bool:
    return bool(os.environ.get("AGENT_WINDOWS_E2E"))


# Bootstrap that runs INSIDE the daemon subprocess. It monkeypatches the
# call-time-resolved bindings the server reads — BEFORE run_daemon() binds the
# pipe — so no source edits are needed.
#
# Why patch server.create_pipe_server and not pipe.PIPE_NAME:
#   server.py:38 does `from agent_windows.ipc.pipe import create_pipe_server`,
#   binding the name into the server module's namespace. server.py:350 then
#   calls `create_pipe_server()` with no args, using the default that was bound
#   at function-definition time in pipe.py (the PIPE_NAME value). So we must
#   replace the binding the server actually calls. We wrap the original so we
#   keep the security-descriptor construction (M-02) intact and only force the
#   name. (TEST3-07)
_BOOTSTRAP = r"""
import os
from pathlib import Path

data_dir = os.environ["AGENT_WINDOWS_E2E_DATA_DIR"]
pipe_name = os.environ["AGENT_WINDOWS_E2E_PIPE_NAME"]

# Redirect the daemon's pipe to our isolated name, keeping the real
# create_pipe_server (which builds the owner-only security descriptor).
import agent_windows.ipc.pipe as _pipe
_orig_create = _pipe.create_pipe_server
def _create_named(_buffer_size=65536):
    return _orig_create(pipe_name)
import agent_windows.daemon.server as _server
_server.create_pipe_server = _create_named

# Redirect PID/version/log paths to the tmp data dir instead of the user's real
# ~/.agent-windows/ (all of get_pid_path/get_version_path/get_log_dir derive
# from get_data_dir()).
import agent_windows.utils.config as _cfg
_cfg.get_data_dir = lambda: Path(data_dir)

# Launch the real daemon main loop (same code path as `python -m agent_windows
# --daemon`, minus the __main__ argv check).
from agent_windows.daemon.server import run_daemon
run_daemon()
"""


@pytest.fixture
def e2e_env(tmp_path):
    """Compute an isolated pipe name + data dir for one test run."""
    pipe_name = rf"\\.\pipe\agent-windows-e2e-{os.getpid()}-{int(time.time() * 1000) % 100000}"
    data_dir = str(tmp_path / "aw-data")
    os.makedirs(data_dir, exist_ok=True)
    return {"pipe_name": pipe_name, "data_dir": data_dir}


def _spawn_daemon(env):
    """Spawn the real daemon subprocess, isolated to env's pipe + data dir."""
    child_env = dict(os.environ)
    child_env["AGENT_WINDOWS_E2E_DATA_DIR"] = env["data_dir"]
    child_env["AGENT_WINDOWS_E2E_PIPE_NAME"] = env["pipe_name"]
    proc = subprocess.Popen(
        [sys.executable, "-c", _BOOTSTRAP],
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


def _wait_for_pipe(pipe_name, timeout=20.0, proc=None):
    """Poll until the daemon's named pipe appears (or proc dies / timeout)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            out, err = proc.communicate(timeout=2)
            raise RuntimeError(
                f"Daemon exited early (code={proc.returncode}) before pipe appeared.\n"
                f"STDOUT: {out.decode(errors='replace')}\n"
                f"STDERR: {err.decode(errors='replace')}")
        if pipe_exists(pipe_name):
            return
        time.sleep(0.2)
    raise TimeoutError(f"Daemon pipe {pipe_name} did not appear within {timeout}s")


def _rpc(method, pipe_name, params=None, timeout=10.0):
    """Send a JSON-RPC request to the daemon and return the parsed result."""
    req = JSONRPCRequest(id=1, method=method, params=params)
    resp_str = send_to_daemon(req.to_json(), pipe_name=pipe_name, timeout=timeout)
    return JSONRPCResponse.from_json(resp_str)


def _shutdown(proc, pipe_name):
    """Cleanly shut the daemon down; kill as a fallback."""
    try:
        _rpc("shutdown", pipe_name, timeout=5.0)
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
        proc.wait(timeout=5)


class TestE2EDaemon:
    """Real subprocess daemon + real named-pipe client round-trip."""

    def test_ping_roundtrip(self, e2e_env):
        if not _e2e_enabled():
            pytest.skip("Set AGENT_WINDOWS_E2E=1 to run end-to-end daemon tests")

        env = e2e_env
        proc = _spawn_daemon(env)
        try:
            _wait_for_pipe(env["pipe_name"], proc=proc)
            resp = _rpc("ping", env["pipe_name"])
            assert resp.error is None, f"ping returned error: {resp.error}"
            assert resp.result == {"status": "ok", "message": "pong"}
        finally:
            _shutdown(proc, env["pipe_name"])

    def test_shutdown_cleans_pid_file(self, e2e_env):
        """After a clean shutdown, the PID file is removed (lifecycle cleanup)."""
        if not _e2e_enabled():
            pytest.skip("Set AGENT_WINDOWS_E2E=1 to run end-to-end daemon tests")

        env = e2e_env
        proc = _spawn_daemon(env)
        try:
            _wait_for_pipe(env["pipe_name"], proc=proc)
            pid_file = os.path.join(env["data_dir"], "daemon.pid")
            # The daemon writes the PID file at startup; confirm it exists.
            assert os.path.exists(pid_file), "PID file not written at startup"
            _shutdown(proc, env["pipe_name"])
            # After clean shutdown, the PID file must be gone.
            assert not os.path.exists(pid_file), "PID file not removed after shutdown"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
