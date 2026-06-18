"""Compare the native launcher against the Python CLI, command by command.

Runs each command through both binaries and diffs stdout + exit code. Assumes
the daemon is already running and warm.

Usage (from repo root):
    python src/launcher/verify.py
    python src/launcher/verify.py --bin src/launcher/target/release/agent-windows.exe
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
PY_CLI = REPO / ".venv/Scripts/agent-windows.exe"
DEFAULT_RUST = REPO / "src/launcher/target/release/agent-windows.exe"


def run(bin_path: Path, args: list[str]) -> tuple[str, int, float]:
    t0 = time.perf_counter()
    p = subprocess.run(
        [str(bin_path), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    elapsed = time.perf_counter() - t0
    return p.stdout, p.returncode, elapsed


# Commands to compare. Each is (label, argv). Chosen to be safe + non-mutating.
CASES: list[tuple[str, list[str]]] = [
    ("snapshot",            ["snapshot"]),
    ("app list",            ["app", "list"]),
    ("process list",        ["process", "list"]),
    ("clipboard get",       ["clipboard", "get"]),
    ("daemon status",       ["daemon", "status"]),
    ("observe --snapshot",  ["observe", "--snapshot"]),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", type=Path, default=DEFAULT_RUST,
                    help="path to the rust launcher exe")
    args = ap.parse_args()

    if not args.bin.exists():
        print(f"ERROR: launcher not found at {args.bin}")
        print("Build it first:  cd src/launcher && build.cmd build --release")
        return 2
    if not PY_CLI.exists():
        print(f"ERROR: python CLI not found at {PY_CLI}")
        return 2

    # Warm the daemon.
    run(PY_CLI, ["ping"])

    print(f"{'command':<22} {'py ms':>7} {'rust ms':>8}  status")
    print("-" * 60)
    failures = 0
    total_py = 0.0
    total_rust = 0.0
    for label, argv in CASES:
        py_out, py_rc, py_t = run(PY_CLI, argv)
        rs_out, rs_rc, rs_t = run(args.bin, argv)
        total_py += py_t
        total_rust += rs_t
        # Normalize: strip trailing whitespace per line, since both may pad
        # slightly differently. Compare structure, not exact bytes.
        py_norm = "\n".join(l.rstrip() for l in py_out.splitlines())
        rs_norm = "\n".join(l.rstrip() for l in rs_out.splitlines())
        match = py_norm == rs_norm and py_rc == rs_rc
        status = "OK" if match else "DIFF"
        if not match:
            failures += 1
        print(f"{label:<22} {py_t*1000:7.1f} {rs_t*1000:8.1f}  {status}")
        if not match:
            print(f"    py  (rc={py_rc}): {py_norm[:200]!r}")
            print(f"    rust(rc={rs_rc}): {rs_norm[:200]!r}")

    print("-" * 60)
    if total_rust > 0:
        print(f"{'TOTAL':<22} {total_py*1000:7.1f} {total_rust*1000:8.1f}  "
              f"({total_py/total_rust:.1f}x faster)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
