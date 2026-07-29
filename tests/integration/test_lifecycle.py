"""Integration: crash, cancellation, teardown ladder, and timeout (§6.4, §10.2)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.mocks import RAW_AGENT_PATH, scenarios  # noqa: E402

from ziggy.engine import RunSpec, execute_run  # noqa: E402
from ziggy.models.common import CaptureStatus, RunStatus, StepStatus  # noqa: E402
from ziggy.models.events import EventEnvelope  # noqa: E402
from ziggy.store import RunIndex, RunStore  # noqa: E402

pytestmark = pytest.mark.slow


def make_spec(tmp_path: Path, scenario: str, **overrides: Any) -> RunSpec:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    defaults: dict[str, Any] = {
        "agent_name": "mock-raw",
        "command": sys.executable,
        "args": [str(RAW_AGENT_PATH), scenario],
        "env": {"PATH": os.environ.get("PATH", "")},
        "cwd": str(workspace),
        "prompt": "go",
        "no_save": False,
        "step_timeout_seconds": 15.0,
        "cancel_grace_seconds": 5.0,
        "store_root": tmp_path / "home",
    }
    defaults.update(overrides)
    return RunSpec(**defaults)


def index_row_status(tmp_path: Path, run_id: str) -> str:
    store = RunStore(tmp_path / "home")
    with RunIndex(store.index_db_path) as index:
        row = index.get(run_id)
    assert row is not None
    return row.status


async def test_crash_mid_turn_partial_capture(tmp_path: Path) -> None:
    result = await execute_run(make_spec(tmp_path, scenarios.CRASH_MID_TURN))

    assert result.status is RunStatus.FAILED
    step = result.steps["main"]
    assert step.status is StepStatus.FAILED
    assert [e.code for e in step.errors] == ["ProtocolError"]
    assert step.attempts[0].exit_code == scenarios.CRASH_EXIT_CODE
    assert step.outputs["text"] == scenarios.CRASH_FIRST_TEXT
    assert result.capture["transcript"].status is CaptureStatus.PARTIAL

    assert result.result_path is not None
    on_disk = json.loads(Path(result.result_path).read_text(encoding="utf-8"))
    assert on_disk["status"] == "failed"
    assert index_row_status(tmp_path, result.run_id) == "failed"


async def test_cancel_event_cancels_and_persists(tmp_path: Path) -> None:
    cancel_event = asyncio.Event()

    def on_render(envelope: EventEnvelope) -> None:
        if envelope.event_type == "message_chunk":
            cancel_event.set()

    spec = make_spec(tmp_path, scenarios.SLOW_STREAM)
    result = await execute_run(spec, render_cb=on_render, cancel_event=cancel_event)

    assert result.status is RunStatus.CANCELLED
    step = result.steps["main"]
    assert step.status is StepStatus.CANCELLED
    assert [e.code for e in step.errors] == ["CancelledError"]
    assert step.attempts[0].stop_reason == "cancelled"  # agent honored session/cancel
    assert result.result_path is not None
    assert Path(result.result_path).is_file()
    assert index_row_status(tmp_path, result.run_id) == "cancelled"


async def test_ignore_cancel_kills_full_process_group(tmp_path: Path) -> None:
    """§10.2 cancellation critical path: a hostile agent that ignores
    ``session/cancel`` and has spawned a child is torn down group-wide."""
    cancel_event = asyncio.Event()
    child_pid: int | None = None

    def on_render(envelope: EventEnvelope) -> None:
        nonlocal child_pid
        if envelope.event_type != "message_chunk":
            return
        text = envelope.payload.get("text", "")
        if isinstance(text, str) and text.startswith(scenarios.CHILD_PID_PREFIX):
            child_pid = int(text[len(scenarios.CHILD_PID_PREFIX) :])
            cancel_event.set()

    spec = make_spec(tmp_path, scenarios.IGNORE_CANCEL, cancel_grace_seconds=0.5)
    result = await execute_run(spec, render_cb=on_render, cancel_event=cancel_event)

    assert result.status is RunStatus.CANCELLED
    assert [e.code for e in result.steps["main"].errors] == ["CancelledError"]
    assert child_pid is not None

    deadline = time.monotonic() + 5.0
    while True:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        assert time.monotonic() < deadline, f"child {child_pid} survived group teardown"
        await asyncio.sleep(0.05)

    assert index_row_status(tmp_path, result.run_id) == "cancelled"


async def test_step_timeout_fails_step(tmp_path: Path) -> None:
    spec = make_spec(
        tmp_path, scenarios.SLOW_STREAM, step_timeout_seconds=0.8, cancel_grace_seconds=2.0
    )
    result = await execute_run(spec)

    assert result.status is RunStatus.FAILED
    step = result.steps["main"]
    assert step.status is StepStatus.FAILED
    assert [e.code for e in step.errors] == ["StepTimeoutError"]
    assert result.capture["transcript"].status is CaptureStatus.PARTIAL

    assert result.events_path is not None
    lines = Path(result.events_path).read_text(encoding="utf-8").splitlines()
    types = [EventEnvelope.model_validate_json(line).event_type for line in lines]
    assert "cancel_requested" in types
    assert "terminated" in types
    assert index_row_status(tmp_path, result.run_id) == "failed"


async def test_launch_failure_still_persists_failed_result(tmp_path: Path) -> None:
    """§5.1 edge case: missing agent binary -> AgentLaunchError, run persisted."""
    spec = make_spec(tmp_path, scenarios.HELLO, command=str(tmp_path / "missing-agent"))
    result = await execute_run(spec)

    assert result.status is RunStatus.FAILED
    step = result.steps["main"]
    assert [e.code for e in step.errors] == ["AgentLaunchError"]
    assert step.attempts[0].exit_code is None
    assert step.agent_info is None
    assert result.result_path is not None
    assert Path(result.result_path).is_file()
    assert index_row_status(tmp_path, result.run_id) == "failed"
