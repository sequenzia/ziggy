"""Integration: policy-driven direct runs against the raw mock agent (Phase 2).

Covers the config -> prepare_run -> execute_run pipeline with the guarded
mediation policy actually serving mediated filesystem requests, permission
requests resolved by policy, the config fingerprint + policy provenance in
the persisted RunResult, and metadata-only lifecycle logs.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.mocks import MOCKS_DIR, RAW_AGENT_PATH, scenarios  # noqa: E402

from ziggy.config import ResolvedConfig, load_config  # noqa: E402
from ziggy.engine import PreparedRun, RunOverrides, execute_run, prepare_run  # noqa: E402
from ziggy.models.common import (  # noqa: E402
    CaptureStatus,
    EnforcementScope,
    PermissionDecisionKind,
    RunStatus,
    StepStatus,
)
from ziggy.models.events import EventEnvelope  # noqa: E402
from ziggy.store.logs import ALLOWED_DETAIL_FIELDS  # noqa: E402

pytestmark = pytest.mark.slow

#: Wrapper agent reusing the raw mock's wire plumbing to request permission
#: for a READ-kind tool call with a workspace location (the stock permission
#: scenario requests an execute kind, which guarded policy default-denies).
PERM_READ_AGENT = """\
import asyncio
import json
import os
import sys

sys.path.insert(0, ___MOCKS_DIR___)

import raw_agent  # noqa: E402
import scenarios  # noqa: E402


class PermReadAgent(raw_agent.MockAgent):
    def __init__(self):
        super().__init__(scenarios.PERMISSION)
        self._handlers[scenarios.PERMISSION] = self._scenario_perm_read

    async def _scenario_perm_read(self, session_id):
        cwd = self._session_cwd.get(session_id, "")
        response = await self._request(
            "session/request_permission",
            {
                "sessionId": session_id,
                "toolCall": {
                    "toolCallId": "call-read-perm",
                    "title": "read hello.txt",
                    "kind": "read",
                    "status": "pending",
                    "locations": [{"path": os.path.join(cwd, "hello.txt")}],
                },
                "options": [
                    {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "reject_once", "name": "Reject once", "kind": "reject_once"},
                ],
            },
        )
        outcome = (response.get("result") or {}).get("outcome") or {}
        approved = (
            outcome.get("outcome") == "selected"
            and outcome.get("optionId") == "allow_once"
        )
        self._chunk(session_id, "perm-approved" if approved else "perm-denied")
        return "end_turn"


async def _serve():
    agent = PermReadAgent()
    reader = await raw_agent._stdin_reader()
    while True:
        line = await reader.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            frame = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(frame, dict):
            await agent.dispatch(frame)


asyncio.run(_serve())
"""


def build_prepared(
    tmp_path: Path, *, agent_args: list[str]
) -> tuple[ResolvedConfig, PreparedRun, Path, Path]:
    """User config registering one mock agent; returns (resolved, prepared, home, ws)."""
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    args_toml = ", ".join(json.dumps(a) for a in agent_args)
    user_file = tmp_path / "user-config.toml"
    user_file.write_text(
        "schema_version = 1\n\n"
        "[engine]\ndefault_step_timeout_seconds = 15\n\n"
        f"[results]\nstore_path = {json.dumps(str(home))}\n\n"
        f"[agents.mock-agent]\ncommand = {json.dumps(sys.executable)}\n"
        f"args = [{args_toml}]\n",
        encoding="utf-8",
    )
    resolved = load_config(None, user_path=user_file, env={})
    prepared = prepare_run(
        resolved,
        agent_name="mock-agent",
        prompt="go",
        workspace=workspace,
        overrides=RunOverrides(),
    )
    return resolved, prepared, home, workspace


def read_envelopes(events_path: str) -> list[EventEnvelope]:
    lines = Path(events_path).read_text(encoding="utf-8").splitlines()
    return [EventEnvelope.model_validate_json(line) for line in lines]


def read_log_records(home: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((home / "logs").glob("ziggy-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            records.append(json.loads(line))
    return records


def assert_metadata_only(records: list[dict[str, Any]]) -> None:
    """Every log line carries only the REQ-016 shape and allowlisted detail."""
    assert records, "expected metadata log lines"
    for record in records:
        assert set(record) <= {"ts", "level", "event", "run_id", "step_id", "agent", "detail"}
        assert set(record.get("detail", {})) <= ALLOWED_DETAIL_FIELDS


async def test_fs_ops_served_by_policy(tmp_path: Path) -> None:
    resolved, prepared, home, workspace = build_prepared(
        tmp_path, agent_args=[str(RAW_AGENT_PATH), scenarios.FS_OPS]
    )
    file_content = "hello from the workspace\n"
    (workspace / scenarios.FS_READ_NAME).write_text(file_content, encoding="utf-8")

    result = await execute_run(prepared.spec)
    prepared.logger.close()

    assert result.status is RunStatus.SUCCESS
    step = result.steps["main"]
    assert step.status is StepStatus.SUCCESS
    # read was served (content returned to the agent), then the write succeeded
    assert step.outputs["text"] == scenarios.FS_READ_PREFIX + file_content

    workspace_real = Path(os.path.realpath(workspace))
    written = workspace / scenarios.FS_WRITE_NAME
    assert written.read_text(encoding="utf-8") == scenarios.FS_WRITE_CONTENT

    [change] = step.file_changes
    assert change.path == str(workspace_real / scenarios.FS_WRITE_NAME)
    assert change.change_type == "created"
    assert change.capture_method == "acp_fs_write"
    assert change.capture_status is CaptureStatus.COMPLETE

    assert result.events_path is not None
    envelopes = read_envelopes(result.events_path)
    by_type = {e.event_type: e for e in envelopes}
    assert by_type["config_resolved"].payload == {"fingerprint": resolved.fingerprint}
    assert by_type["policy_resolved"].payload["policy_name"] == "guarded"

    fs_read = by_type["fs_read"]
    assert fs_read.payload["decision"] == "allowed"
    assert fs_read.payload["rule_id"] == "read-in-workspace-allow"
    assert fs_read.payload["path"] == str(workspace_real / scenarios.FS_READ_NAME)
    fs_write = by_type["fs_write"]
    assert fs_write.payload["decision"] == "allowed"
    assert fs_write.payload["rule_id"] == "write-in-stepdir-allow"
    assert fs_write.payload["path"] == str(workspace_real / scenarios.FS_WRITE_NAME)

    # config fingerprint + policy provenance are in the persisted manifest
    assert result.config_fingerprint == resolved.fingerprint
    assert result.policy is not None
    assert result.policy.policy_name == "guarded"
    assert result.result_path is not None
    on_disk = json.loads(Path(result.result_path).read_text(encoding="utf-8"))
    assert on_disk["config_fingerprint"] == resolved.fingerprint
    assert on_disk["policy"]["policy_name"] == "guarded"
    assert on_disk["policy"]["enforcement"] == "advisory"

    # metadata log: lifecycle events, allowlisted fields only, RunResult path ref
    records = read_log_records(home)
    assert_metadata_only(records)
    events = {r["event"] for r in records}
    assert {
        "run_started",
        "agent_launched",
        "handshake",
        "session_created",
        "prompt_started",
        "terminated",
        "step_finished",
        "run_finished",
        "run_persisted",
    } <= events
    assert all(r["run_id"] == result.run_id for r in records if "run_id" in r)
    [persisted] = [r for r in records if r["event"] == "run_persisted"]
    assert persisted["detail"]["path_ref"] == result.result_path
    # the prompt and workspace file contents never reach the metadata log
    log_text = json.dumps(records)
    assert file_content.strip() not in log_text
    assert str(workspace) not in log_text


async def test_sensitive_path_read_denied(tmp_path: Path) -> None:
    _resolved, prepared, home, workspace = build_prepared(
        tmp_path, agent_args=[str(RAW_AGENT_PATH), scenarios.FS_OPS]
    )
    canary = "DOTENV-CANARY-VALUE-99887766\n"
    (workspace / ".env").write_text(canary, encoding="utf-8")
    # the fixed fs_ops scenario reads hello.txt; route it into .env via symlink
    (workspace / scenarios.FS_READ_NAME).symlink_to(".env")

    result = await execute_run(prepared.spec)
    prepared.logger.close()

    assert result.status is RunStatus.SUCCESS
    step = result.steps["main"]
    assert step.outputs["text"] == scenarios.FS_DENIED_TEXT
    assert step.file_changes == []
    assert not (workspace / scenarios.FS_WRITE_NAME).exists()

    assert result.events_path is not None
    envelopes = read_envelopes(result.events_path)
    [fs_read] = [e for e in envelopes if e.event_type == "fs_read"]
    assert fs_read.payload["decision"] == "denied"
    assert fs_read.payload["rule_id"] == "sensitive-path-deny"
    # the denied read never leaked the sensitive file's content anywhere
    assert canary.strip() not in Path(result.events_path).read_text(encoding="utf-8")
    assert canary.strip() not in json.dumps(read_log_records(home))


async def test_permission_execute_without_command_default_denied(tmp_path: Path) -> None:
    _, prepared, home, _ = build_prepared(
        tmp_path, agent_args=[str(RAW_AGENT_PATH), scenarios.PERMISSION]
    )

    result = await execute_run(prepared.spec)
    prepared.logger.close()

    assert result.status is RunStatus.SUCCESS
    step = result.steps["main"]
    assert step.outputs["text"] == scenarios.PERMISSION_DENIED_TEXT
    [decision] = step.permission_decisions
    assert decision.decision is PermissionDecisionKind.DENIED
    assert decision.rule_id == "terminal-default-deny"
    assert decision.policy_name == "guarded"
    assert decision.policy_source == "default"
    assert decision.enforcement_scope is EnforcementScope.ACP_MEDIATED

    records = read_log_records(home)
    assert_metadata_only(records)
    [logged] = [r for r in records if r["event"] == "permission_decided"]
    assert logged["detail"] == {"rule_id": "terminal-default-deny", "decision": "denied"}


async def test_permission_read_in_workspace_allowed_agent_proceeds(tmp_path: Path) -> None:
    script = tmp_path / "perm_read_agent.py"
    script.write_text(
        PERM_READ_AGENT.replace("___MOCKS_DIR___", json.dumps(str(MOCKS_DIR))),
        encoding="utf-8",
    )
    _, prepared, home, workspace = build_prepared(tmp_path, agent_args=[str(script)])
    (workspace / "hello.txt").write_text("readable\n", encoding="utf-8")

    result = await execute_run(prepared.spec)
    prepared.logger.close()

    assert result.status is RunStatus.SUCCESS
    step = result.steps["main"]
    # the agent saw the allow_once selection and proceeded
    assert step.outputs["text"] == "perm-approved"
    [decision] = step.permission_decisions
    assert decision.decision is PermissionDecisionKind.APPROVED
    assert decision.rule_id == "read-in-workspace-allow"
    assert decision.policy_name == "guarded"
    assert decision.policy_source == "default"

    assert result.events_path is not None
    envelopes = read_envelopes(result.events_path)
    [decided] = [e for e in envelopes if e.event_type == "permission_decided"]
    assert decided.payload["selected_option_id"] == "allow_once"

    records = read_log_records(home)
    assert_metadata_only(records)
    [logged] = [r for r in records if r["event"] == "permission_decided"]
    assert logged["detail"] == {"rule_id": "read-in-workspace-allow", "decision": "approved"}
