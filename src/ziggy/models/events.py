"""Canonical event envelope and per-artifact records (§7.3 supporting types).

``events.jsonl`` is the append-only redacted source of truth; one
``EventEnvelope`` per line. Everything else (result.json, index, logs,
terminal output) is a derived view.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from ziggy.models.common import CaptureStatus, EnforcementScope, PermissionDecisionKind


class RedactionMark(BaseModel):
    """Per-event redaction metadata: counts only, never matched text."""

    model_config = ConfigDict(extra="forbid")

    applied: bool = False
    counts: dict[str, int] = {}  # kind -> occurrences


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    seq: int
    ts: str  # UTC ISO-8601 Z
    monotonic_offset_ms: int
    run_id: str
    step_id: str | None = None
    attempt_no: int | None = None  # pre-launch run errors have no attempt
    session_id: str | None = None
    event_type: str
    payload: dict[str, Any] = {}
    protocol_payload_ref: str | None = None  # debug capture only
    capture_status: CaptureStatus = CaptureStatus.COMPLETE
    redaction: RedactionMark = RedactionMark()


class ToolCallRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_call_id: str
    kind: str | None = None
    title: str | None = None
    status: str | None = None  # pending|in_progress|completed|failed (agent-reported)
    locations: list[str] = []
    capture_status: CaptureStatus = CaptureStatus.COMPLETE
    protocol_payload_ref: str | None = None


class FileChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    change_type: str  # created|modified|deleted|renamed|unknown
    capture_method: str  # acp_tool_call|acp_fs_write|workspace_diff|unknown
    attribution: str = "unknown"  # step|run|unknown
    patch_ref: str | None = None
    binary: bool = False
    capture_status: CaptureStatus = CaptureStatus.DERIVED


class PermissionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_summary: str
    options_offered: list[str] = []
    decision: PermissionDecisionKind
    rule_id: str
    policy_name: str
    policy_source: str  # config provenance of the winning rule
    enforcement_scope: EnforcementScope = EnforcementScope.ACP_MEDIATED
    ts: str
    client_response: str | None = None  # server mode: what the upstream client answered


class CaptureSummaryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: CaptureStatus
    source: str
    event_count: int = 0
    byte_count: int = 0
    truncated: bool = False
    path: str | None = None


class RedactionSummary(BaseModel):
    """Aggregate pattern application counts for a run (never matched text)."""

    model_config = ConfigDict(extra="forbid")

    total_redactions: int = 0
    by_kind: dict[str, int] = {}
    warnings: list[str] = []
