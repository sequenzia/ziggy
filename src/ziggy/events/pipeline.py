"""Canonical event pipeline: one recorder, every consumer (REQ-004, REQ-011).

``RunRecorder.emit()`` is the single entry point for everything that happens
during a run. Order of operations per event:

1. redact the payload (before persistence, aggregation, or rendering sees it),
2. apply the capture profile (metadata-only reduction of content payloads),
3. enforce byte ceilings (oversized payloads and the per-step event budget),
4. assign ``seq``/``ts``/``monotonic_offset_ms`` and build the envelope,
5. append one compact-JSON line to ``events.jsonl`` (unless ``--no-save``),
6. update in-memory aggregations (tool calls, file changes, permissions, text),
7. fan out to the live render callback.

``events.jsonl`` is the canonical record; the aggregations exist so the engine
can assemble StepResult/RunResult without re-reading it. Byte ceilings compare
serialized envelope line sizes against ``EventLimits.max_event_bytes_per_step``;
once a step crosses the ceiling the recorder emits exactly one ``truncation``
event and later events for that step carry only
``{"truncated": true, "original_bytes": n}`` (metadata-only continuation). That
continuation is itself bounded (:data:`_MAX_TRUNCATED_CONTINUATION_LINES`): once
the budget is spent, further events are no longer persisted at all — they are
counted in memory and the totals are carried on the step's ``step_finished``
event — so a runaway step cannot grow ``events.jsonl`` without bound.

Oversized single payloads are measured *before* redaction/profile reduction so a
hostile multi-megabyte frame is reduced to truncation metadata without first
regex-walking its body (the transport-level frame cap belongs in the SDK layer).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from ziggy.errors import PersistenceError
from ziggy.ids import MonotonicClock, utc_now_iso
from ziggy.models.common import CaptureProfile, CaptureStatus
from ziggy.models.events import (
    CaptureSummaryEntry,
    EventEnvelope,
    FileChange,
    PermissionDecision,
    RedactionMark,
    RedactionSummary,
    ToolCallRecord,
)
from ziggy.models.result import UsageSummary
from ziggy.redact import Redactor
from ziggy.store import RunDirWriter

#: Stable canonical event type names (ARCHITECTURE.md). ``emit`` rejects
#: anything else so typos cannot silently create new event vocabularies.
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "run_started",
        "config_resolved",
        "policy_resolved",
        "agent_launching",
        "agent_launched",
        "handshake",
        "session_created",
        "prompt_started",
        "message_chunk",
        "thought_chunk",
        "tool_call",
        "tool_call_update",
        "plan",
        "usage",
        "mode",
        "unknown_update",
        "permission_requested",
        "permission_decided",
        "fs_read",
        "fs_write",
        "terminal_op",
        "file_change",
        "step_started",
        "step_finished",
        "egress_notice",
        "lease_acquired",
        "lease_released",
        "cancel_requested",
        "terminated",
        "truncation",
        "error",
        "run_finished",
        "raw_frame",
    }
)

TRANSCRIPT = "transcript"
TOOL_CALLS = "tool_calls"
PERMISSIONS = "permissions"
FILE_CHANGES = "file_changes"

#: Artifact classes reported by ``capture_summary()`` (REQ-004).
ARTIFACT_CLASSES: tuple[str, ...] = (TRANSCRIPT, TOOL_CALLS, PERMISSIONS, FILE_CHANGES)

_CLASS_OF: dict[str, str] = {
    "message_chunk": TRANSCRIPT,
    "thought_chunk": TRANSCRIPT,
    "tool_call": TOOL_CALLS,
    "tool_call_update": TOOL_CALLS,
    "permission_requested": PERMISSIONS,
    "permission_decided": PERMISSIONS,
    "fs_write": FILE_CHANGES,
    "file_change": FILE_CHANGES,
}

_CLASS_SOURCES: dict[str, str] = {
    TRANSCRIPT: "acp_session_updates",
    TOOL_CALLS: "acp_tool_calls",
    PERMISSIONS: "policy_engine",
    FILE_CHANGES: "acp_tool_call_inference",
}

#: Payload keys that carry agent content per event type. The metadata capture
#: profile reduces these values to ``{"bytes": n, "type": t}``; the standard
#: profile reduces only ``thought_chunk`` (REQ-004: standard capture records
#: only event metadata for thought updates). Both snake_case (native dumps)
#: and camelCase (wire-shaped payloads) spellings are covered.
_TOOL_CALL_CONTENT_KEYS: frozenset[str] = frozenset(
    {"raw", "raw_input", "raw_output", "rawInput", "rawOutput", "content"}
)

_CONTENT_KEYS: dict[str, frozenset[str]] = {
    "message_chunk": frozenset({"text", "content"}),
    "thought_chunk": frozenset({"text", "content"}),
    "tool_call": _TOOL_CALL_CONTENT_KEYS,
    "tool_call_update": _TOOL_CALL_CONTENT_KEYS,
    "fs_read": frozenset({"content"}),
    "fs_write": frozenset({"content"}),
    # Permission requests embed the full wire tool_call (rawInput/rawOutput/
    # content) and terminal ops carry the full command string. Both must be
    # reduced under the metadata profile so proprietary code / secrets a user
    # chose to keep off disk never land there (FIX #9).
    "permission_requested": frozenset({"tool_call"}),
    "terminal_op": frozenset({"command"}),
}

_SEVERITY: dict[CaptureStatus, int] = {
    CaptureStatus.COMPLETE: 0,
    CaptureStatus.DERIVED: 1,
    CaptureStatus.PARTIAL: 2,
    CaptureStatus.UNAVAILABLE: 3,
}

_MAX_RECORDED_PERSISTENCE_ERRORS = 5

#: Real ``usage`` payload vocabulary emitted by engine/hooks.py from an ACP
#: ``usage_update`` (see ``ziggy.acp.types.UsageEvent`` / the SDK ``UsageUpdate``
#: schema). ``used`` is "tokens currently in context" and ``size`` is the
#: context-window capacity; ``cost`` is the cumulative session cost. This is a
#: context gauge, not a provider input/output token breakdown -- Ziggy never
#: receives that split here, so the derived ``UsageSummary`` can never honestly
#: be reported as ``complete`` (FIX #17).
_USAGE_TOKEN_KEY = "used"
_USAGE_SIZE_KEY = "size"
_USAGE_COST_KEY = "cost"
_USAGE_CURRENCY_KEY = "currency"
#: Units label documenting that the surfaced token count is context occupancy,
#: not an input/output accounting.
_USAGE_UNITS = "context_tokens"

#: After a step crosses its byte ceiling, at most this many metadata-only
#: continuation lines are persisted for it; further events are suppressed
#: (counted in memory, summarized on ``step_finished``) so a runaway step cannot
#: grow ``events.jsonl`` without bound (FIX #22).
_MAX_TRUNCATED_CONTINUATION_LINES = 20
#: Rough per-line envelope overhead, used only to estimate suppressed bytes for
#: the continuation summary (the exact line was never serialized -- that is the
#: work we deliberately avoid).
_CONTINUATION_LINE_OVERHEAD_BYTES = 256


def _worse(a: CaptureStatus, b: CaptureStatus) -> CaptureStatus:
    """Capture statuses only ever degrade; never report better than reality."""
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


def _json_bytes(value: Any) -> int:
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    )


def _truncated_payload(original_bytes: int) -> dict[str, Any]:
    return {"truncated": True, "original_bytes": original_bytes}


@dataclass(frozen=True, slots=True)
class EventLimits:
    """Byte ceilings for the event stream (REQ-011 defaults).

    ``max_artifact_bytes_per_run`` is a passthrough for artifact writers (the
    50 MiB per-run artifact ceiling); the recorder itself does not write
    artifacts and does not enforce it.
    """

    max_event_bytes_per_step: int = 10 * 1024 * 1024
    max_payload_bytes_per_event: int = 1 * 1024 * 1024
    max_artifact_bytes_per_run: int = 50 * 1024 * 1024


DEFAULT_LIMITS = EventLimits()


class RunRecorder:
    """Canonical event recorder for one run (ARCHITECTURE.md contract).

    ``store_writer=None`` is ``--no-save`` mode: nothing touches the
    filesystem, but sequencing, redaction, ceilings, and aggregations behave
    identically. ``events.jsonl`` write failures are collected as
    ``PersistenceError`` (capped list plus a total counter) and degrade
    ``capture_summary()``; they never interrupt the event stream. Render
    callback exceptions are swallowed and counted for the same reason.
    """

    def __init__(
        self,
        *,
        run_id: str,
        store_writer: RunDirWriter | None,
        redactor: Redactor,
        capture_profile: CaptureProfile | str,
        limits: EventLimits = DEFAULT_LIMITS,
        render_cb: Callable[[EventEnvelope], None] | None = None,
        clock: MonotonicClock | None = None,
    ) -> None:
        self.run_id = run_id
        self._writer = store_writer
        self._redactor = redactor
        self._profile = CaptureProfile(capture_profile)
        self._limits = limits
        self._render_cb = render_cb
        self._clock = clock or MonotonicClock()
        self._seq = 0
        self._step_bytes: dict[str | None, int] = {}
        self._truncated_steps: set[str | None] = set()
        self._truncated_continuation: dict[str | None, int] = {}
        self._suppressed_events: dict[str | None, int] = {}
        self._suppressed_bytes: dict[str | None, int] = {}
        self._class_counts: dict[str, int] = dict.fromkeys(ARTIFACT_CLASSES, 0)
        self._class_bytes: dict[str, int] = dict.fromkeys(ARTIFACT_CLASSES, 0)
        self._class_truncated: set[str] = set()
        self._events_written = 0
        self._write_failures = 0
        self._persistence_errors: list[PersistenceError] = []
        self._render_failures = 0
        self._tool_calls: dict[str | None, dict[str, ToolCallRecord]] = {}
        self._file_changes: dict[str | None, list[FileChange]] = {}
        self._fc_seen: dict[str | None, set[tuple[str, str]]] = {}
        self._permissions: dict[str | None, list[PermissionDecision]] = {}
        self._text: dict[str | None, list[str]] = {}
        self._usage_seen = 0
        self._usage_by_step: dict[str | None, dict[str, Any]] = {}

    @property
    def capture_profile(self) -> CaptureProfile:
        return self._profile

    @property
    def limits(self) -> EventLimits:
        return self._limits

    @property
    def event_count(self) -> int:
        return self._seq

    @property
    def events_path(self) -> Path | None:
        return self._writer.events_path if self._writer is not None else None

    @property
    def persistence_errors(self) -> list[PersistenceError]:
        """Collected events-write failures (capped at the first few; see
        ``write_failures`` for the true total)."""
        return list(self._persistence_errors)

    @property
    def write_failures(self) -> int:
        return self._write_failures

    @property
    def render_failures(self) -> int:
        return self._render_failures

    # ------------------------------------------------------------------ emit

    def emit(
        self,
        *,
        event_type: str,
        step_id: str | None = None,
        attempt_no: int | None = None,
        session_id: str | None = None,
        payload: dict[str, Any] | None = None,
        capture_status: CaptureStatus | str = CaptureStatus.COMPLETE,
        protocol_payload_ref: str | None = None,
    ) -> EventEnvelope | None:
        """Record one canonical event; returns the persisted envelope.

        Returns ``None`` when the event is not persisted: the capture profile
        suppresses it entirely (``raw_frame`` outside ``debug``), or the step's
        post-truncation continuation budget is spent (FIX #22). No seq is
        consumed in either case. ``capture_status`` may only be degraded by the
        pipeline (profile reduction and truncation escalate it toward
        ``partial``), never upgraded.
        """
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event_type: {event_type!r}")
        if self._profile is not CaptureProfile.DEBUG:
            if event_type == "raw_frame":
                return None
            protocol_payload_ref = None

        status = CaptureStatus(capture_status)
        raw_payload = dict(payload or {})

        # FIX #19a: measure the RAW payload size BEFORE redaction / profile
        # reduction. A single oversized payload is reduced to truncation
        # metadata without first regex-walking megabytes of hostile content, so
        # per-frame work stays bounded. (A transport-level frame cap in the SDK
        # layer -- ziggy.acp -- is deferred hardening, out of this scope.)
        raw_bytes = _json_bytes(raw_payload)
        replaced = False
        if raw_bytes > self._limits.max_payload_bytes_per_event:
            body: dict[str, Any] = _truncated_payload(raw_bytes)
            mark = RedactionMark()  # nothing was persisted, so nothing was scanned
            status = CaptureStatus.PARTIAL
            original_bytes = raw_bytes
            replaced = True
            self._mark_truncated(event_type, step_id)
        else:
            body, counts = self._redactor.redact_payload(raw_payload)
            mark = RedactionMark(applied=bool(counts), counts=counts)
            if self._reduce_for_profile(event_type):
                body = self._metadata_view(event_type, body)
                status = _worse(status, CaptureStatus.PARTIAL)
            original_bytes = _json_bytes(body)

        if event_type != "truncation":
            if step_id in self._truncated_steps:
                # FIX #22: the step already crossed its byte ceiling. Persist a
                # bounded number of metadata-only continuation lines, then stop
                # writing entirely -- counting suppressed events/bytes in memory
                # -- so a runaway step cannot grow events.jsonl without bound.
                if event_type == "step_finished":
                    # Lifecycle event: always persisted, and it carries the
                    # continuation summary for the step.
                    if not replaced:
                        body = _truncated_payload(original_bytes)
                    body = {
                        **body,
                        "suppressed_events": self._suppressed_events.get(step_id, 0),
                        "suppressed_bytes": self._suppressed_bytes.get(step_id, 0),
                    }
                    status = CaptureStatus.PARTIAL
                    self._mark_truncated(event_type, step_id)
                else:
                    if not replaced:
                        body = _truncated_payload(original_bytes)
                    status = CaptureStatus.PARTIAL
                    self._mark_truncated(event_type, step_id)
                    persisted = self._truncated_continuation.get(step_id, 0)
                    if persisted >= _MAX_TRUNCATED_CONTINUATION_LINES:
                        self._suppressed_events[step_id] = (
                            self._suppressed_events.get(step_id, 0) + 1
                        )
                        self._suppressed_bytes[step_id] = (
                            self._suppressed_bytes.get(step_id, 0)
                            + _json_bytes(body)
                            + _CONTINUATION_LINE_OVERHEAD_BYTES
                        )
                        return None
                    self._truncated_continuation[step_id] = persisted + 1
            else:
                draft = self._make_envelope(
                    seq=self._seq,
                    event_type=event_type,
                    step_id=step_id,
                    attempt_no=attempt_no,
                    session_id=session_id,
                    body=body,
                    status=status,
                    mark=mark,
                    ref=protocol_payload_ref,
                )
                line_len = len(draft.model_dump_json().encode("utf-8")) + 1
                used = self._step_bytes.get(step_id, 0)
                if used + line_len > self._limits.max_event_bytes_per_step:
                    self._truncated_steps.add(step_id)
                    self._record(
                        event_type="truncation",
                        step_id=step_id,
                        attempt_no=attempt_no,
                        session_id=session_id,
                        body={
                            "limit_bytes": self._limits.max_event_bytes_per_step,
                            "bytes_at_truncation": used,
                        },
                        status=CaptureStatus.COMPLETE,
                        mark=RedactionMark(),
                        ref=None,
                    )
                    if not replaced:
                        body = _truncated_payload(original_bytes)
                    status = CaptureStatus.PARTIAL
                    self._mark_truncated(event_type, step_id)

        return self._record(
            event_type=event_type,
            step_id=step_id,
            attempt_no=attempt_no,
            session_id=session_id,
            body=body,
            status=status,
            mark=mark,
            ref=protocol_payload_ref,
        )

    def _make_envelope(
        self,
        *,
        seq: int,
        event_type: str,
        step_id: str | None,
        attempt_no: int | None,
        session_id: str | None,
        body: dict[str, Any],
        status: CaptureStatus,
        mark: RedactionMark,
        ref: str | None,
    ) -> EventEnvelope:
        return EventEnvelope(
            seq=seq,
            ts=utc_now_iso(),
            monotonic_offset_ms=self._clock.elapsed_ms(),
            run_id=self.run_id,
            step_id=step_id,
            attempt_no=attempt_no,
            session_id=session_id,
            event_type=event_type,
            payload=body,
            protocol_payload_ref=ref,
            capture_status=status,
            redaction=mark,
        )

    def _record(
        self,
        *,
        event_type: str,
        step_id: str | None,
        attempt_no: int | None,
        session_id: str | None,
        body: dict[str, Any],
        status: CaptureStatus,
        mark: RedactionMark,
        ref: str | None,
    ) -> EventEnvelope:
        envelope = self._make_envelope(
            seq=self._seq,
            event_type=event_type,
            step_id=step_id,
            attempt_no=attempt_no,
            session_id=session_id,
            body=body,
            status=status,
            mark=mark,
            ref=ref,
        )
        self._seq += 1
        line = envelope.model_dump_json()
        nbytes = len(line.encode("utf-8")) + 1
        self._step_bytes[step_id] = self._step_bytes.get(step_id, 0) + nbytes
        cls = _CLASS_OF.get(event_type)
        if cls is not None:
            self._class_counts[cls] += 1
            self._class_bytes[cls] += nbytes
        if self._writer is not None:
            try:
                self._writer.append_event(line)
                self._events_written += 1
            except PersistenceError as exc:
                self._write_failures += 1
                if len(self._persistence_errors) < _MAX_RECORDED_PERSISTENCE_ERRORS:
                    self._persistence_errors.append(exc)
        self._aggregate(envelope)
        if self._render_cb is not None:
            try:
                self._render_cb(envelope)
            except Exception:
                self._render_failures += 1
        return envelope

    # ------------------------------------------------- profile / truncation

    def _reduce_for_profile(self, event_type: str) -> bool:
        if event_type not in _CONTENT_KEYS:
            return False
        if self._profile is CaptureProfile.METADATA:
            return True
        return self._profile is CaptureProfile.STANDARD and event_type == "thought_chunk"

    def _metadata_view(self, event_type: str, body: dict[str, Any]) -> dict[str, Any]:
        """Replace content values with ``{"bytes": n, "type": t}`` metadata.

        Byte counts describe the redacted content that would otherwise have
        been persisted, so they can be reported without leaking anything. A
        nested ``tool_call`` dict (permission_requested payloads) is *recursed*
        into rather than collapsed whole: identity/status fields stay for
        auditability while only its content-bearing sub-keys are reduced
        (FIX #9).
        """
        content_keys = _CONTENT_KEYS[event_type]
        reduced: dict[str, Any] = {}
        for key, value in body.items():
            if key not in content_keys or value is None:
                reduced[key] = value
            elif key == "tool_call" and isinstance(value, dict):
                reduced[key] = self._reduce_tool_call(value)
            else:
                reduced[key] = {"bytes": _json_bytes(value), "type": type(value).__name__}
        return reduced

    def _reduce_tool_call(self, tool_call: dict[str, Any]) -> dict[str, Any]:
        """Reduce a nested wire tool_call to auditable metadata: keep
        id/kind/title/status (and any other identity keys), replace only the
        content-bearing sub-keys (rawInput/rawOutput/content/raw) with
        ``{"bytes": n, "type": t}`` so proprietary code never lands on disk."""
        reduced: dict[str, Any] = {}
        for key, value in tool_call.items():
            if key in _TOOL_CALL_CONTENT_KEYS and value is not None:
                reduced[key] = {"bytes": _json_bytes(value), "type": type(value).__name__}
            else:
                reduced[key] = value
        return reduced

    def _mark_truncated(self, event_type: str, step_id: str | None) -> None:
        cls = _CLASS_OF.get(event_type)
        if cls is None:
            return
        self._class_truncated.add(cls)
        if cls == TOOL_CALLS:
            for record in self._tool_calls.get(step_id, {}).values():
                record.capture_status = CaptureStatus.PARTIAL

    # ------------------------------------------------------------ aggregates

    def _aggregate(self, envelope: EventEnvelope) -> None:
        event_type = envelope.event_type
        payload = envelope.payload
        step_id = envelope.step_id
        if event_type == "message_chunk":
            text = payload.get("text")
            if isinstance(text, str) and payload.get("role", "agent") == "agent":
                self._text.setdefault(step_id, []).append(text)
        elif event_type in ("tool_call", "tool_call_update"):
            self._merge_tool_call(step_id, payload)
            self._extract_diff_changes(step_id, payload)
        elif event_type == "fs_write":
            path = payload.get("path")
            if isinstance(path, str):
                self._add_file_change(
                    step_id,
                    FileChange(
                        path=path,
                        change_type=str(payload.get("change_type", "unknown")),
                        capture_method="acp_fs_write",
                        attribution="step" if step_id else "run",
                        capture_status=CaptureStatus.COMPLETE,
                    ),
                )
        elif event_type == "file_change":
            data = {k: v for k, v in payload.items() if k in FileChange.model_fields}
            try:
                self._add_file_change(step_id, FileChange(**data))
            except PydanticValidationError:
                return
        elif event_type == "permission_decided":
            data = {k: v for k, v in payload.items() if k in PermissionDecision.model_fields}
            data.setdefault("ts", envelope.ts)
            try:
                decision = PermissionDecision(**data)
            except PydanticValidationError:
                return
            self._permissions.setdefault(step_id, []).append(decision)
        elif event_type == "usage":
            self._usage_seen += 1
            # ``used`` (context tokens) and ``cost`` (cumulative session cost)
            # are current-state gauges: the latest non-None value wins WITHIN a
            # step. Tracking per step means a later step never silently
            # overwrites an earlier step's counts (FIX #17).
            step_usage = self._usage_by_step.setdefault(step_id, {})
            for key in (
                _USAGE_TOKEN_KEY,
                _USAGE_SIZE_KEY,
                _USAGE_COST_KEY,
                _USAGE_CURRENCY_KEY,
            ):
                value = payload.get(key)
                if value is not None:
                    step_usage[key] = value

    def _merge_tool_call(self, step_id: str | None, payload: dict[str, Any]) -> None:
        tool_call_id = payload.get("tool_call_id")
        if not isinstance(tool_call_id, str):
            return
        table = self._tool_calls.setdefault(step_id, {})
        record = table.get(tool_call_id)
        if record is None:
            record = ToolCallRecord(tool_call_id=tool_call_id)
            table[tool_call_id] = record
        for field in ("kind", "title", "status"):
            value = payload.get(field)
            if isinstance(value, str):
                setattr(record, field, value)
        locations = payload.get("locations")
        if isinstance(locations, list) and locations:
            record.locations = [str(loc) for loc in locations]

    def _extract_diff_changes(self, step_id: str | None, payload: dict[str, Any]) -> None:
        """Infer file changes from diff-typed tool-call content (derived capture)."""
        raw = payload.get("raw")
        content = raw.get("content") if isinstance(raw, dict) else None
        if content is None:
            content = payload.get("content")
        if not isinstance(content, list):
            return
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "diff":
                continue
            path = item.get("path")
            if not isinstance(path, str):
                continue
            old_text = item.get("old_text", item.get("oldText"))
            self._add_file_change(
                step_id,
                FileChange(
                    path=path,
                    change_type="modified" if old_text is not None else "created",
                    capture_method="acp_tool_call",
                    attribution="step" if step_id else "run",
                    capture_status=CaptureStatus.DERIVED,
                ),
            )

    def _add_file_change(self, step_id: str | None, change: FileChange) -> None:
        seen = self._fc_seen.setdefault(step_id, set())
        key = (change.path, change.capture_method)
        if key in seen:
            return
        seen.add(key)
        self._file_changes.setdefault(step_id, []).append(change)

    def tool_calls(self, step_id: str | None = None) -> list[ToolCallRecord]:
        """Tool calls for a step, merged by id in first-seen order."""
        return list(self._tool_calls.get(step_id, {}).values())

    def file_changes(self, step_id: str | None = None) -> list[FileChange]:
        return list(self._file_changes.get(step_id, []))

    def permission_decisions(self, step_id: str | None = None) -> list[PermissionDecision]:
        return list(self._permissions.get(step_id, []))

    def text_output(self, step_id: str | None = None) -> str:
        """Concatenated agent message chunks — becomes StepResult.outputs['text']."""
        return "".join(self._text.get(step_id, []))

    def usage_summary(self) -> UsageSummary | None:
        """Provider usage aggregated across steps, honestly labelled (FIX #17).

        The only emitter (engine/hooks.py) forwards an ACP ``usage_update``:
        ``used`` = tokens currently in context, ``size`` = context-window
        capacity, ``cost`` = cumulative session cost. That is a context gauge,
        not an input/output token accounting, so:

        - ``used`` is surfaced as ``input_tokens`` with ``units='context_tokens'``
          (a documented reinterpretation); ``output_tokens`` stays ``None``
          because ACP never decomposes it, and ``size`` (capacity, not
          consumption) has no honest home here -- it stays in the canonical
          ``usage`` events on disk.
        - counts are summed per step, never silently overwritten run-wide.
        - ``capture_status`` is at best ``derived`` (a mapping was needed) and
          ``partial`` when even ``used`` was absent -- never ``complete``, which
          would claim a full, timely provider breakdown Ziggy never received.
        """
        if self._usage_seen == 0:
            return None
        total_used: int | None = None
        total_cost: float | None = None
        currency: str | None = None
        for step_usage in self._usage_by_step.values():
            used = step_usage.get(_USAGE_TOKEN_KEY)
            if isinstance(used, int) and not isinstance(used, bool):
                total_used = (total_used or 0) + used
            cost = step_usage.get(_USAGE_COST_KEY)
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                total_cost = (total_cost or 0.0) + float(cost)
            step_currency = step_usage.get(_USAGE_CURRENCY_KEY)
            if isinstance(step_currency, str):
                currency = step_currency
        saw_tokens = total_used is not None
        status = CaptureStatus.DERIVED if saw_tokens else CaptureStatus.PARTIAL
        return UsageSummary(
            provider=None,
            units=_USAGE_UNITS if saw_tokens else None,
            input_tokens=total_used,
            output_tokens=None,
            cost=total_cost,
            currency=currency,
            capture_status=status.value,
            ceiling_enforceable=False,
        )

    # ------------------------------------------------------------ summaries

    def capture_summary(self) -> dict[str, CaptureSummaryEntry]:
        """Honest per-artifact-class completeness (REQ-004; never optimistic).

        - ``file_changes`` is at best ``derived`` here: this recorder infers
          changes from tool calls and mediated writes, never from a verified
          workspace diff.
        - The metadata profile reports content classes (transcript,
          tool_calls, permissions) as ``partial`` with source
          ``metadata_profile``: it reduces transcript/tool content and the
          tool_call embedded in permission_requested events.
        - Standard-profile thought reduction is that profile's documented
          contract, so it alone does not degrade the transcript class; any
          truncation does.
        - Events-write failures degrade every class: ``partial`` when some
          events reached disk, ``unavailable`` when none did.
        """
        path = str(self._writer.events_path) if self._writer is not None else None
        summary: dict[str, CaptureSummaryEntry] = {}
        for cls in ARTIFACT_CLASSES:
            status = CaptureStatus.DERIVED if cls == FILE_CHANGES else CaptureStatus.COMPLETE
            source = _CLASS_SOURCES[cls]
            if self._profile is CaptureProfile.METADATA and cls in (
                TRANSCRIPT,
                TOOL_CALLS,
                PERMISSIONS,
            ):
                # PERMISSIONS is degraded too: the metadata profile reduces the
                # tool_call embedded in permission_requested events (FIX #9), so
                # this class is no longer fully captured on disk.
                status = _worse(status, CaptureStatus.PARTIAL)
                source = "metadata_profile"
            truncated = cls in self._class_truncated
            if truncated:
                status = _worse(status, CaptureStatus.PARTIAL)
            if self._writer is not None and self._write_failures:
                degraded = (
                    CaptureStatus.PARTIAL if self._events_written else CaptureStatus.UNAVAILABLE
                )
                status = _worse(status, degraded)
            summary[cls] = CaptureSummaryEntry(
                status=status,
                source=source,
                event_count=self._class_counts[cls],
                byte_count=self._class_bytes[cls],
                truncated=truncated,
                path=path,
            )
        return summary

    def redaction_summary(self) -> RedactionSummary:
        return self._redactor.summary()

    async def finalize(self) -> None:
        """Flush + fsync ``events.jsonl``. The writer stays open: the engine
        still needs it to persist ``result.json`` and release the sentinel."""
        if self._writer is None:
            return
        try:
            self._writer.fsync_events()
        except PersistenceError as exc:
            self._write_failures += 1
            if len(self._persistence_errors) < _MAX_RECORDED_PERSISTENCE_ERRORS:
                self._persistence_errors.append(exc)
        except (OSError, ValueError) as exc:
            self._write_failures += 1
            if len(self._persistence_errors) < _MAX_RECORDED_PERSISTENCE_ERRORS:
                self._persistence_errors.append(
                    PersistenceError(f"events fsync failed: {exc}", details={"run_id": self.run_id})
                )
