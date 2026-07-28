"""Phase-1 mediation hooks: record everything, allow nothing.

``RecordingHooks`` implements the :class:`ziggy.acp.MediationHooks` protocol
for the direct-run engine. Phase 1 has no policy engine, so the stance is a
fixed default deny: permission requests select a reject option (or return the
denied outcome when none is offered), mediated filesystem operations raise
``PolicyDenied``, and the terminal surface is declared unsupported. Every
mediated interaction is recorded through the canonical event pipeline so the
audit trail is complete even though the policy is trivial.

Phase 2 swaps in the real policy engine through the ``decide_permission``
constructor seam: a decider returns both the wire reply and the
``permission_decided`` payload (PermissionDecision-shaped), and the hooks keep
owning event emission so recording stays in one place.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ziggy.acp import (
    AgentEvent,
    FsReadRequestN,
    FsWriteRequestN,
    MessageChunkEvent,
    ModeEvent,
    PermissionOptionN,
    PermissionReply,
    PermissionRequestN,
    PlanEvent,
    PolicyDenied,
    TerminalReply,
    TerminalRequestN,
    ToolCallEvent,
    UnknownUpdateEvent,
    UnsupportedByPolicy,
    UsageEvent,
)
from ziggy.events import RunRecorder
from ziggy.ids import utc_now_iso
from ziggy.models.common import EnforcementScope, PermissionDecisionKind

DEFAULT_DENY_RULE_ID = "phase1-default-deny"
DEFAULT_DENY_POLICY_NAME = "default-deny"

#: Prefer the narrowest reject option so a Phase-1 denial never registers a
#: standing "always" preference with the agent.
_REJECT_KIND_ORDER = ("reject_once", "reject_always")

PermissionDecider = Callable[
    [PermissionRequestN], Awaitable[tuple[PermissionReply, dict[str, Any]]]
]


def _default_deny_reply(options: list[PermissionOptionN]) -> PermissionReply:
    for kind in _REJECT_KIND_ORDER:
        for option in options:
            if option.kind == kind:
                return PermissionReply(kind="selected", option_id=option.option_id)
    return PermissionReply(kind="cancelled")


def _request_summary(tool_call: dict[str, Any]) -> str:
    title = tool_call.get("title")
    if isinstance(title, str) and title:
        return title
    tool_call_id = tool_call.get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id:
        return tool_call_id
    return "permission request"


class RecordingHooks:
    """MediationHooks implementation for the Phase-1 direct-run engine.

    ``session_id`` starts as ``None`` and is set by the runner once
    ``session/new`` succeeds; events emitted before that carry no session.
    """

    def __init__(
        self,
        *,
        recorder: RunRecorder,
        step_id: str = "main",
        attempt_no: int = 1,
        decide_permission: PermissionDecider | None = None,
    ) -> None:
        self._recorder = recorder
        self._step_id = step_id
        self._attempt_no = attempt_no
        self._decide_permission = decide_permission
        self.session_id: str | None = None

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self._recorder.emit(
            event_type=event_type,
            step_id=self._step_id,
            attempt_no=self._attempt_no,
            session_id=self.session_id,
            payload=payload,
        )

    # ------------------------------------------------------------ streaming

    async def on_event(self, ev: AgentEvent) -> None:
        if isinstance(ev, MessageChunkEvent):
            event_type = "thought_chunk" if ev.thought else "message_chunk"
            self._emit(event_type, {"role": ev.role, "text": ev.text})
        elif isinstance(ev, ToolCallEvent):
            event_type = "tool_call" if ev.phase == "start" else "tool_call_update"
            self._emit(
                event_type,
                {
                    "tool_call_id": ev.tool_call_id,
                    "phase": ev.phase,
                    "title": ev.title,
                    "kind": ev.kind,
                    "status": ev.status,
                    "locations": list(ev.locations),
                    "has_content": ev.has_content,
                    "raw": ev.raw,
                },
            )
        elif isinstance(ev, PlanEvent):
            self._emit("plan", {"entries": ev.entries})
        elif isinstance(ev, UsageEvent):
            self._emit(
                "usage",
                {
                    "used": ev.used,
                    "size": ev.size,
                    "cost": ev.cost,
                    "currency": ev.currency,
                    "raw": ev.raw,
                },
            )
        elif isinstance(ev, ModeEvent):
            self._emit("mode", {"kind": ev.kind, "payload": ev.payload})
        elif isinstance(ev, UnknownUpdateEvent):
            self._emit("unknown_update", {"update_type": ev.update_type, "payload": ev.payload})

    # ---------------------------------------------------------- permissions

    async def resolve_permission(self, req: PermissionRequestN) -> PermissionReply:
        summary = _request_summary(req.tool_call)
        self._emit(
            "permission_requested",
            {
                "request_summary": summary,
                "tool_call": req.tool_call,
                "options": [
                    {"option_id": o.option_id, "name": o.name, "kind": o.kind} for o in req.options
                ],
            },
        )
        if self._decide_permission is not None:
            reply, decision_payload = await self._decide_permission(req)
        else:
            reply = _default_deny_reply(req.options)
            decision_payload = {
                "request_summary": summary,
                "options_offered": [o.kind for o in req.options],
                "decision": PermissionDecisionKind.DENIED.value,
                "rule_id": DEFAULT_DENY_RULE_ID,
                "policy_name": DEFAULT_DENY_POLICY_NAME,
                "policy_source": "default",
                "enforcement_scope": EnforcementScope.ACP_MEDIATED.value,
                "ts": utc_now_iso(),
            }
        self._emit(
            "permission_decided",
            {**decision_payload, "selected_option_id": reply.option_id},
        )
        return reply

    # ----------------------------------------------------------- fs mediation

    async def read_text_file(self, req: FsReadRequestN) -> str:
        self._emit(
            "fs_read",
            {
                "path": req.path,
                "line": req.line,
                "limit": req.limit,
                "decision": "denied",
                "rule_id": DEFAULT_DENY_RULE_ID,
            },
        )
        raise PolicyDenied("phase1 default-deny: mediated file read is not permitted")

    async def write_text_file(self, req: FsWriteRequestN) -> None:
        """Deny and record. The ``path`` key is deliberately absent from the
        payload: the recorder counts a ``path``-keyed ``fs_write`` event as an
        applied FileChange, and this write was denied, so nothing changed."""
        self._emit(
            "fs_write",
            {
                "requested_path": req.path,
                "content_bytes": len(req.content.encode("utf-8")),
                "decision": "denied",
                "rule_id": DEFAULT_DENY_RULE_ID,
            },
        )
        raise PolicyDenied("phase1 default-deny: mediated file write is not permitted")

    # ------------------------------------------------------------- terminal

    async def handle_terminal(self, req: TerminalRequestN) -> TerminalReply:
        payload: dict[str, Any] = {
            "op": req.op,
            "decision": "unsupported",
            "rule_id": DEFAULT_DENY_RULE_ID,
        }
        command = req.payload.get("command")
        if isinstance(command, str):
            payload["command"] = command
        self._emit("terminal_op", payload)
        raise UnsupportedByPolicy("phase1: the terminal surface is not supported")
