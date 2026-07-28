"""End-to-end SIGINT cancellation: ``ziggy run`` exits 130 (REQ-003, §6.4).

A real ``python -m ziggy run`` subprocess streams the slow mock scenario;
SIGINT is delivered once the first chunk appears on stdout (proving the
engine is mid-turn and the CLI signal handler is installed). The CLI must
cancel through the teardown ladder, persist the run as ``cancelled``, and
exit with code 130.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.mocks import RAW_AGENT_PATH, scenarios  # noqa: E402

pytestmark = pytest.mark.slow

_STARTUP_DEADLINE_SECONDS = 30.0
_EXIT_DEADLINE_SECONDS = 30.0


def _read_until(proc: subprocess.Popen[bytes], needle: bytes, deadline: float) -> bytes:
    """Accumulate stdout bytes until ``needle`` appears or the deadline passes."""
    assert proc.stdout is not None
    os.set_blocking(proc.stdout.fileno(), False)
    seen = b""
    while time.monotonic() < deadline:
        chunk = proc.stdout.read()
        if chunk:
            seen += chunk
            if needle in seen:
                return seen
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    return seen


def test_sigint_mid_run_exits_130_and_persists_cancelled(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (home / "config.toml").write_text(
        "schema_version = 1\n"
        "[engine]\ndefault_step_timeout_seconds = 25\ncancel_grace_seconds = 5.0\n"
        "[agents.mock-slow]\n"
        f"command = {json.dumps(sys.executable)}\n"
        f"args = [{json.dumps(str(RAW_AGENT_PATH))}, {json.dumps(scenarios.SLOW_STREAM)}]\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["ZIGGY_HOME"] = str(home)
    env["NO_COLOR"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-m", "ziggy", "run", "mock-slow", "go"],
        cwd=workspace,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        tick = scenarios.SLOW_STREAM_TICK_PREFIX.encode("utf-8")
        seen = _read_until(proc, tick, time.monotonic() + _STARTUP_DEADLINE_SECONDS)
        assert tick in seen, f"agent never started streaming; stdout so far: {seen!r}"

        proc.send_signal(signal.SIGINT)
        returncode = proc.wait(timeout=_EXIT_DEADLINE_SECONDS)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    assert returncode == 130, proc.stderr.read() if proc.stderr else b""

    # the cancelled run persisted with a durable manifest
    run_dirs = [
        entry
        for entry in (home / "runs").iterdir()
        if entry.is_dir() and (entry / "result.json").is_file()
    ]
    assert len(run_dirs) == 1
    manifest = json.loads((run_dirs[0] / "result.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "cancelled"
