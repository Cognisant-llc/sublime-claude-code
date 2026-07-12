"""One-shot dev loop: unit tests -> plugin reload -> live smoke -> state dump.

Usage:
    uv run python scripts/dev_check.py            # full loop
    uv run python scripts/dev_check.py --no-live  # unit tests only

Requires `subl` on PATH and a running Sublime Text for the live part.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_JSON = os.path.join(tempfile.gettempdir(), "claude_ide_state.json")


def run(cmd, **kw):
    print("$ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=REPO, **kw)


def subl_command(name):
    # `subl --command` returns immediately; the command runs inside Sublime.
    run(["subl", "--command", name], check=True)


def dump_state(max_wait=5.0):
    try:
        os.remove(STATE_JSON)
    except OSError:
        pass
    subl_command("claude_ide_dump_state")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if os.path.exists(STATE_JSON):
            with open(STATE_JSON, encoding="utf-8") as fh:
                return json.load(fh)
        time.sleep(0.2)
    raise SystemExit("dev_check: state dump did not appear (is Sublime running?)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-live", action="store_true", help="unit tests only")
    args = ap.parse_args()

    if run([sys.executable, "-m", "pytest", "-q"]).returncode != 0:
        raise SystemExit("dev_check: unit tests failed")

    if args.no_live:
        print("dev_check: OK (unit only)")
        return

    subl_command("claude_ide_dev_reload")
    time.sleep(1.0)  # let the server restart and write its lock file

    state = dump_state()
    if state.get("error") or not state.get("running"):
        raise SystemExit(f"dev_check: plugin not healthy after reload: {state!r}")
    port, clients = state.get("port"), state.get("clients")
    print(f"dev_check: reloaded, server on port {port} ({clients} client(s))")

    if run([sys.executable, os.path.join("scripts", "smoke.py")]).returncode != 0:
        raise SystemExit("dev_check: live smoke failed")

    print("dev_check: OK (unit + live)")


if __name__ == "__main__":
    main()
