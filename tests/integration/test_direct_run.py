"""Integration: Phase-1 direct-run engine against the raw mock agent (REQ-004/005)."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.mocks import RAW_AGENT_PATH, scenarios  # noqa: E402

from ziggy.engine import RunSpec, execute_run  # noqa: E402
from ziggy.models.common import (  # noqa: E402
    CaptureStatus,
    EnforcementScope,
    PermissionDecisionKind,
    RunStatus,
    StepStatus,
)
from ziggy.models.events import EventEnvelope  # noqa: E402
from ziggy.models.result import RunResult  # noqa: E402
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


def read_envelopes(events_path: Path) -> list[EventEnvelope]:
    lines = events_path.read_text(encoding="utf-8").splitlines()
    return [EventEnvelope.model_validate_json(line) for line in lines]


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


async def test_hello_success_persists_full_contract(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, scenarios.HELLO)
    result = await execute_run(spec)

    assert result.status is RunStatus.SUCCESS
    step = result.steps["main"]
    assert step.status is StepStatus.SUCCESS
    assert step.outputs["text"] == "".join(scenarios.HELLO_CHUNKS)
    assert step.agent_info is not None
    assert step.agent_info.agent_name == scenarios.RAW_AGENT_NAME
    assert step.agent_info.agent_version == scenarios.AGENT_VERSION
    assert step.agent_info.protocol_version == scenarios.PROTOCOL_VERSION
    assert len(step.attempts) == 1
    assert step.attempts[0].stop_reason == "end_turn"
    assert step.attempts[0].status is StepStatus.SUCCESS

    assert result.persisted is True
    assert result.result_path is not None and result.events_path is not None
    result_path = Path(result.result_path)
    events_path = Path(result.events_path)
    assert result_path.is_file() and events_path.is_file()

    envelopes = read_envelopes(events_path)
    assert [e.seq for e in envelopes] == list(range(len(envelopes)))
    assert all(e.run_id == result.run_id for e in envelopes)
    types = [e.event_type for e in envelopes]
    assert types[0] == "run_started"
    assert types[-1] == "run_finished"
    assert types.count("message_chunk") == len(scenarios.HELLO_CHUNKS)
    for expected in ("handshake", "session_created", "prompt_started", "step_finished"):
        assert expected in types

    on_disk = RunResult.model_validate(json.loads(result_path.read_text(encoding="utf-8")))
    assert on_disk.run_id == result.run_id
    assert on_disk.status is RunStatus.SUCCESS
    assert on_disk.persisted is True

    store = RunStore(tmp_path / "home")
    with RunIndex(store.index_db_path) as index:
        row = index.get(result.run_id)
    assert row is not None
    assert row.status == "success"
    assert row.target == "mock-raw"
    assert row.workspace == spec.cwd
    assert row.result_path == str(result_path)

    assert mode_of(store.runs_dir) == 0o700
    assert mode_of(result_path.parent) == 0o700
    assert mode_of(result_path) == 0o600
    assert mode_of(events_path) == 0o600


async def test_tool_calls_merged_and_diff_file_change(tmp_path: Path) -> None:
    result = await execute_run(make_spec(tmp_path, scenarios.TOOL_CALLS))

    assert result.status is RunStatus.SUCCESS
    step = result.steps["main"]
    assert step.outputs["text"] == scenarios.TOOL_CALLS_INTRO
    by_id = {tc.tool_call_id: tc for tc in step.tool_calls}
    assert set(by_id) == {scenarios.EXEC_TOOL_CALL_ID, scenarios.DIFF_TOOL_CALL_ID}
    exec_tc = by_id[scenarios.EXEC_TOOL_CALL_ID]
    assert exec_tc.title == scenarios.EXEC_TOOL_TITLE
    assert exec_tc.kind == "execute"
    assert exec_tc.status == "completed"  # merged in from the tool_call_update frame
    diff_tc = by_id[scenarios.DIFF_TOOL_CALL_ID]
    assert diff_tc.status == "completed"
    assert diff_tc.locations == [scenarios.DIFF_PATH]

    assert len(step.file_changes) == 1
    change = step.file_changes[0]
    assert change.path == scenarios.DIFF_PATH
    assert change.change_type == "modified"
    assert change.capture_method == "acp_tool_call"
    assert change.capture_status is CaptureStatus.DERIVED


async def test_permission_request_default_denied(tmp_path: Path) -> None:
    result = await execute_run(make_spec(tmp_path, scenarios.PERMISSION))

    assert result.status is RunStatus.SUCCESS
    step = result.steps["main"]
    assert step.outputs["text"] == scenarios.PERMISSION_DENIED_TEXT
    assert len(step.permission_decisions) == 1
    decision = step.permission_decisions[0]
    assert decision.decision is PermissionDecisionKind.DENIED
    assert decision.rule_id == "phase1-default-deny"
    assert decision.policy_name == "default-deny"
    assert decision.enforcement_scope is EnforcementScope.ACP_MEDIATED
    assert decision.request_summary == scenarios.PERMISSION_TOOL_TITLE

    assert result.events_path is not None
    types = [e.event_type for e in read_envelopes(Path(result.events_path))]
    assert "permission_requested" in types
    assert "permission_decided" in types


async def test_fs_ops_denied_and_capture_honest(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, scenarios.FS_OPS)
    file_content = "fs-ops-secret-canary\n"
    (Path(spec.cwd) / scenarios.FS_READ_NAME).write_text(file_content, encoding="utf-8")
    result = await execute_run(spec)

    assert result.status is RunStatus.SUCCESS
    step = result.steps["main"]
    assert step.outputs["text"] == scenarios.FS_DENIED_TEXT
    assert step.file_changes == []

    assert result.events_path is not None
    envelopes = read_envelopes(Path(result.events_path))
    fs_reads = [e for e in envelopes if e.event_type == "fs_read"]
    assert len(fs_reads) == 1
    assert fs_reads[0].payload["decision"] == "denied"

    assert result.capture["file_changes"].event_count == 0
    assert result.capture["transcript"].status is CaptureStatus.COMPLETE
    # the denied read must not have leaked the file's content into the record
    assert file_content not in Path(result.events_path).read_text(encoding="utf-8")


async def test_malformed_frame_skipped_run_succeeds(tmp_path: Path) -> None:
    result = await execute_run(make_spec(tmp_path, scenarios.MALFORMED))

    assert result.status is RunStatus.SUCCESS
    assert result.steps["main"].outputs["text"] == scenarios.MALFORMED_FIRST_TEXT
    assert result.steps["main"].errors == []


async def test_no_save_leaves_no_trace(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, scenarios.HELLO, no_save=True)
    result = await execute_run(spec)

    assert result.status is RunStatus.SUCCESS
    assert result.persisted is False
    assert result.result_path is None
    assert result.events_path is None
    assert not (tmp_path / "home").exists()

    step = result.steps["main"]
    assert step.outputs["text"] == "".join(scenarios.HELLO_CHUNKS)
    assert step.attempts[0].stop_reason == "end_turn"
    assert step.agent_info is not None
    assert result.capture["transcript"].event_count == len(scenarios.HELLO_CHUNKS)
    assert result.capture["transcript"].path is None
