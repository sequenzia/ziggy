"""Integration regressions for the direct-run engine (FIX #3/#2/#10/#24).

- #3  the cross-process workspace lease guards ``execute_run`` exactly like
      ``execute_workflow``: a busy lease fails the run with ``WorkspaceBusyError``
      and launches nothing; a normal run acquires and releases the lease.
- #2  a secret split across two adjacent message chunks is stream-redacted, so
      concatenating the persisted ``message_chunk`` texts never reassembles it.
- #10 a wedged agent that stops reading stdin is still torn down within a
      bounded time and its whole process group is reaped.
- #24 a turn that ends ``refusal`` after a policy-denied permission surfaces as
      a typed ``PermissionDeniedError`` on the step.
"""

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
from ziggy.engine.lease import LeaseManager  # noqa: E402
from ziggy.ids import new_run_id  # noqa: E402
from ziggy.models.common import RunStatus, StepStatus  # noqa: E402
from ziggy.models.events import EventEnvelope  # noqa: E402
from ziggy.policy import MediationPolicy  # noqa: E402
from ziggy.store import RunStore  # noqa: E402

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


def lease_files(home: Path) -> list[Path]:
    leases = home / "leases"
    return sorted(leases.glob("*.json")) if leases.is_dir() else []


def read_envelopes(events_path: Path) -> list[EventEnvelope]:
    return [
        EventEnvelope.model_validate_json(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]


# --------------------------------------------------------------------- FIX #3


async def test_busy_lease_fails_run_without_launching(tmp_path: Path) -> None:
    """A held workspace lease makes a direct run fail busy with no launch."""
    home = tmp_path / "home"
    spec = make_spec(tmp_path, scenarios.HELLO)
    workspace = Path(spec.cwd)

    # A second, independent LeaseManager (distinct run_id) holds W's lease for
    # the duration of the direct run — a live, provable holder in this process.
    holder_store = RunStore(home)
    holder = LeaseManager().acquire(holder_store, workspace, new_run_id())
    try:
        result = await execute_run(spec)

        assert result.status is RunStatus.FAILED
        assert [e.code for e in result.errors] == ["WorkspaceBusyError"]
        # The main step never ran: no attempt, no handshake, no agent info.
        step = result.steps["main"]
        assert step.status is StepStatus.FAILED
        assert step.attempts == []
        assert step.agent_info is None

        # Nothing was launched and no step ever started for the losing run.
        assert result.events_path is not None
        types = [e.event_type for e in read_envelopes(Path(result.events_path))]
        assert "agent_launching" not in types
        assert "agent_launched" not in types
        assert "step_started" not in types
        assert "lease_acquired" not in types
        # The holder still owns the single lease file for W (the loser released
        # nothing because it never acquired).
        [held] = lease_files(home)
        assert json.loads(held.read_text(encoding="utf-8"))["run_id"] == holder.lease.run_id
    finally:
        holder.release()

    assert lease_files(home) == []  # holder's lease gone once released


async def test_normal_run_acquires_and_releases_lease(tmp_path: Path) -> None:
    """A normal direct run acquires the lease and releases it on completion."""
    home = tmp_path / "home"
    result = await execute_run(make_spec(tmp_path, scenarios.HELLO))

    assert result.status is RunStatus.SUCCESS
    assert lease_files(home) == []  # released after completion (file gone)

    assert result.events_path is not None
    types = [e.event_type for e in read_envelopes(Path(result.events_path))]
    assert "lease_acquired" in types
    assert "lease_released" in types
    assert types.index("lease_acquired") < types.index("agent_launching")


# --------------------------------------------------------------------- FIX #2


async def test_split_secret_not_reassemblable_from_events(tmp_path: Path) -> None:
    """Consecutive persisted message_chunk texts never reassemble the split key."""
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec = RunSpec(
        agent_name="mock-raw",
        command=sys.executable,
        args=[str(RAW_AGENT_PATH), scenarios.SECRET_LEAK],
        env={"PATH": os.environ.get("PATH", "")},
        cwd=str(workspace),
        prompt="leak",
        no_save=False,
        step_timeout_seconds=15.0,
        store_root=home,
    )
    result = await execute_run(spec)
    assert result.status is RunStatus.SUCCESS
    assert result.events_path is not None

    # Reassemble the agent transcript exactly as an auditor would: concatenate
    # the text of every persisted message_chunk in seq order.
    reassembled = "".join(
        e.payload.get("text", "")
        for e in read_envelopes(Path(result.events_path))
        if e.event_type == "message_chunk" and isinstance(e.payload.get("text"), str)
    )
    openai = scenarios.SEEDED_SECRETS["openai_api_key"]
    assert openai not in reassembled, "split OpenAI key reassembled from events.jsonl"
    assert scenarios.SEEDED_SECRETS["anthropic_api_key"] not in reassembled
    assert "[REDACTED:openai_api_key]" in reassembled

    # The split key is caught during streaming (not only at output assembly).
    assert result.redaction.by_kind.get("openai_api_key", 0) >= 1

    # And the raw split bytes never touched any persisted file under the store.
    for path in home.rglob("*"):
        if path.is_file():
            assert openai.encode("utf-8") not in path.read_bytes()


# -------------------------------------------------------------------- FIX #10


async def test_wedged_agent_is_torn_down_within_bounds(tmp_path: Path) -> None:
    """An agent that stops reading stdin mid-turn is reaped group-wide, bounded."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # A large mediated-read target: Ziggy's write of the response fills the pipe
    # so the rung-1 session/cancel drain would block forever without the bound.
    (workspace / scenarios.WEDGE_READ_FILE_NAME).write_bytes(b"a" * (1024 * 1024))
    policy = MediationPolicy.guarded(workspace=workspace, step_dir=workspace)
    spec = make_spec(
        tmp_path,
        scenarios.WEDGE_STDIN,
        step_timeout_seconds=1.5,
        cancel_grace_seconds=0.5,
        policy=policy,
    )

    child_pid: int | None = None

    def on_render(envelope: EventEnvelope) -> None:
        nonlocal child_pid
        if envelope.event_type != "message_chunk":
            return
        text = envelope.payload.get("text", "")
        if isinstance(text, str) and text.startswith(scenarios.CHILD_PID_PREFIX):
            child_pid = int(text[len(scenarios.CHILD_PID_PREFIX) :])

    start = time.monotonic()
    # A regression (unbounded rung 1) would hang here; the outer bound converts
    # that into a fast failure instead of wedging the suite.
    result = await asyncio.wait_for(execute_run(spec, render_cb=on_render), timeout=45.0)
    elapsed = time.monotonic() - start

    assert result.status is RunStatus.FAILED
    assert [e.code for e in result.steps["main"].errors] == ["StepTimeoutError"]
    assert elapsed < 30.0, f"teardown took {elapsed:.1f}s (rung 1 should be bounded)"
    assert child_pid is not None, "agent never announced its child pid"

    deadline = time.monotonic() + 5.0
    while True:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        assert time.monotonic() < deadline, f"child {child_pid} survived group teardown"
        await asyncio.sleep(0.05)


# -------------------------------------------------------------------- FIX #24


async def test_refusal_after_denied_permission_is_typed(tmp_path: Path) -> None:
    """stop_reason='refusal' after a denied permission → PermissionDeniedError."""
    result = await execute_run(make_spec(tmp_path, scenarios.PERMISSION_REFUSAL))

    assert result.status is RunStatus.FAILED
    step = result.steps["main"]
    assert step.status is StepStatus.FAILED
    [error] = step.errors
    assert error.code == "PermissionDeniedError"
    assert error.details["stop_reason"] == "refusal"
    assert error.details["rule_id"] == "phase1-default-deny"
    # The denial that drove the refusal was actually recorded on the step.
    assert len(step.permission_decisions) == 1
    assert step.permission_decisions[0].rule_id == "phase1-default-deny"
