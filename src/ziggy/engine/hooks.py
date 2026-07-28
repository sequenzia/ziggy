"""Mediation hooks for the direct-run engine.

``RecordingHooks`` (Phase 1) implements the :class:`ziggy.acp.MediationHooks`
protocol with a fixed default deny: permission requests select a reject option
(or return the denied outcome when none is offered), mediated filesystem
operations raise ``PolicyDenied``, and the terminal surface is declared
unsupported. Every mediated interaction is recorded through the canonical
event pipeline so the audit trail is complete even though the policy is
trivial.

``PolicyHooks`` (Phase 2) extends it with the real guarded mediation policy:
:class:`ziggy.policy.MediationPolicy` makes every decision, allowed
filesystem requests are actually served by Ziggy, and permission decisions
additionally emit metadata-log lines. Denials stay deny-shaped
(``PolicyDenied``) and the terminal surface remains ``UnsupportedByPolicy``
even when allowlisted — v0.1 does not implement client-side terminal
execution and never pretends otherwise.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
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
from ziggy.errors import PersistenceError
from ziggy.events import RunRecorder
from ziggy.ids import utc_now_iso
from ziggy.models.common import EnforcementScope, PermissionDecisionKind
from ziggy.policy import Decision, MediationPolicy, PathAmbiguity, resolve_contained
from ziggy.store.logs import MetadataLogger, NullLogger

DEFAULT_DENY_RULE_ID = "phase1-default-deny"
DEFAULT_DENY_POLICY_NAME = "default-deny"

#: Byte ceiling for serving one mediated file read. An in-policy read of a
#: larger file is refused deny-shaped (``PolicyDenied``) with a resource
#: reason — the ACP fs surface has no partial-content contract to lean on.
MAX_MEDIATED_READ_BYTES = 5 * 1024 * 1024

#: Rule ids for engine-level (non-policy) mediation failures. Recorded so a
#: refused service is never attributed to the policy rule that allowed it.
RULE_MEDIATED_READ_LIMIT = "mediated-read-limit"
RULE_MEDIATED_IO_ERROR = "mediated-io-error"

#: Either metadata logger implementation (``NullLogger`` for --no-save runs).
MetadataLoggerLike = MetadataLogger | NullLogger

#: Prefer the narrowest reject option so a denial never registers a
#: standing "always" preference with the agent.
_REJECT_KIND_ORDER = ("reject_once", "reject_always")
#: Same principle for approvals: never volunteer a standing allow_always.
_ALLOW_KIND_ORDER = ("allow_once", "allow_always")

PermissionDecider = Callable[
    [PermissionRequestN], Awaitable[tuple[PermissionReply, dict[str, Any]]]
]


def _select_option(
    options: Sequence[PermissionOptionN], kind_order: Sequence[str]
) -> PermissionReply:
    """First option matching ``kind_order`` preference; else the cancelled
    outcome (which the wire treats as denied — failing toward denial)."""
    for kind in kind_order:
        for option in options:
            if option.kind == kind:
                return PermissionReply(kind="selected", option_id=option.option_id)
    return PermissionReply(kind="cancelled")


def _default_deny_reply(options: list[PermissionOptionN]) -> PermissionReply:
    return _select_option(options, _REJECT_KIND_ORDER)


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


class PolicyHooks(RecordingHooks):
    """Phase-2 mediation hooks: the guarded policy decides, the hooks serve.

    Permission requests are resolved by ``MediationPolicy.decide_permission``
    and mapped to the narrowest matching wire option (``allow_once`` before
    ``allow_always``; ``reject_once`` before ``reject_always``; the cancelled
    outcome when no matching option is offered — failing toward denial on the
    wire). Mediated filesystem requests the policy allows are actually served
    by Ziggy: reads return the file text (bounded by
    :data:`MAX_MEDIATED_READ_BYTES`), writes create parent directories inside
    the proven containment and land the content on disk. Denials raise
    :class:`PolicyDenied` carrying the decision's reason.

    The terminal surface records the policy decision but always raises
    :class:`UnsupportedByPolicy`: v0.1 does not implement client-side terminal
    execution, so even an allowlist-allowed ``create`` is honestly reported as
    unsupported rather than pretending anything ran.

    Serving re-canonicalizes the path at write/read time; a path whose
    resolution changes between decision and service (e.g. a symlink swapped
    underneath) is re-checked by ``resolve_contained`` and denied when it can
    no longer be proven contained. This narrows, but cannot fully close, the
    inherent decide/serve window — ACP mediation is observable governance
    over ACP-routed requests, never an OS sandbox.

    Every decision is recorded through the canonical event pipeline with the
    rule id / policy name / policy source from the :class:`Decision`.
    Permission decisions additionally emit metadata-log lines (rule_id and
    decision only) when a logger is provided; metadata-log write failures are
    swallowed (best effort), allowlist misuse is not.
    """

    def __init__(
        self,
        *,
        recorder: RunRecorder,
        policy: MediationPolicy,
        logger: MetadataLoggerLike | None = None,
        step_id: str = "main",
        attempt_no: int = 1,
    ) -> None:
        super().__init__(recorder=recorder, step_id=step_id, attempt_no=attempt_no)
        self._policy = policy
        self._logger = logger

    # --------------------------------------------------------------- helpers

    def _decision_fields(self, decision: Decision) -> dict[str, Any]:
        return {
            "rule_id": decision.rule_id,
            "policy_name": decision.policy_name,
            "policy_source": decision.policy_source,
            "reason": decision.reason,
        }

    def _log_metadata(self, event: str, **detail: Any) -> None:
        if self._logger is None:
            return
        with suppress(PersistenceError):
            self._logger.log(event, run_id=self._recorder.run_id, step_id=self._step_id, **detail)

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
        decision = self._policy.decide_permission(req)
        if decision.allowed:
            reply = _select_option(req.options, _ALLOW_KIND_ORDER)
        else:
            reply = _select_option(req.options, _REJECT_KIND_ORDER)
        if decision.allowed:
            kind = PermissionDecisionKind.APPROVED
        else:
            kind = PermissionDecisionKind.DENIED
        self._emit(
            "permission_decided",
            {
                "request_summary": summary,
                "options_offered": [o.kind for o in req.options],
                "decision": kind.value,
                "enforcement_scope": decision.enforcement_scope.value,
                "ts": utc_now_iso(),
                "selected_option_id": reply.option_id,
                **self._decision_fields(decision),
            },
        )
        self._log_metadata("permission_decided", rule_id=decision.rule_id, decision=kind.value)
        return reply

    # ----------------------------------------------------------- fs mediation

    async def read_text_file(self, req: FsReadRequestN) -> str:
        base_payload: dict[str, Any] = {"path": req.path, "line": req.line, "limit": req.limit}
        decision = self._policy.decide_fs_read(req.path)
        if not decision.allowed:
            self._emit(
                "fs_read",
                {**base_payload, "decision": "denied", **self._decision_fields(decision)},
            )
            raise PolicyDenied(decision.reason)
        try:
            resolved = resolve_contained(self._policy.workspace, req.path)
        except PathAmbiguity as exc:
            self._emit(
                "fs_read",
                {
                    **base_payload,
                    "decision": "denied",
                    "rule_id": "path-ambiguity-deny",
                    "policy_name": decision.policy_name,
                    "policy_source": "default",
                    "reason": str(exc),
                },
            )
            raise PolicyDenied(f"read of {req.path!r} failed re-canonicalization: {exc}") from exc
        try:
            data = resolved.read_bytes()
        except OSError as exc:
            self._emit(
                "fs_read",
                {
                    **base_payload,
                    "decision": "denied",
                    "rule_id": RULE_MEDIATED_IO_ERROR,
                    "policy_name": decision.policy_name,
                    "policy_source": "default",
                    "reason": f"mediated read failed: {exc.__class__.__name__}",
                },
            )
            raise PolicyDenied(f"cannot read {req.path!r}: {exc.__class__.__name__}") from exc
        if len(data) > MAX_MEDIATED_READ_BYTES:
            self._emit(
                "fs_read",
                {
                    **base_payload,
                    "decision": "denied",
                    "rule_id": RULE_MEDIATED_READ_LIMIT,
                    "policy_name": decision.policy_name,
                    "policy_source": "default",
                    "reason": (
                        f"file is {len(data)} bytes; mediated reads are capped at "
                        f"{MAX_MEDIATED_READ_BYTES} bytes"
                    ),
                },
            )
            raise PolicyDenied(
                f"mediated read of {req.path!r} exceeds the "
                f"{MAX_MEDIATED_READ_BYTES}-byte resource cap"
            )
        text = data.decode("utf-8", errors="replace")
        if req.line is not None or req.limit is not None:
            lines = text.splitlines(keepends=True)
            start = max((req.line or 1) - 1, 0)  # ACP line numbers are 1-based
            end = start + req.limit if req.limit is not None else None
            text = "".join(lines[start:end])
        self._emit(
            "fs_read",
            {
                **base_payload,
                "path": str(resolved),
                "decision": "allowed",
                "content_bytes": len(text.encode("utf-8")),
                **self._decision_fields(decision),
            },
        )
        return text

    async def write_text_file(self, req: FsWriteRequestN) -> None:
        """Serve an in-policy mediated write; deny everything else.

        Denied/failed writes keep the ``requested_path`` payload key (never
        ``path``): the recorder aggregates a ``path``-keyed ``fs_write`` event
        into an applied FileChange, and nothing changed on disk here.
        """
        content_bytes = len(req.content.encode("utf-8"))
        base_payload: dict[str, Any] = {
            "requested_path": req.path,
            "content_bytes": content_bytes,
        }
        decision = self._policy.decide_fs_write(req.path)
        if not decision.allowed:
            self._emit(
                "fs_write",
                {**base_payload, "decision": "denied", **self._decision_fields(decision)},
            )
            raise PolicyDenied(decision.reason)
        try:
            resolved = resolve_contained(self._policy.workspace, req.path)
        except PathAmbiguity as exc:
            self._emit(
                "fs_write",
                {
                    **base_payload,
                    "decision": "denied",
                    "rule_id": "path-ambiguity-deny",
                    "policy_name": decision.policy_name,
                    "policy_source": "default",
                    "reason": str(exc),
                },
            )
            raise PolicyDenied(f"write of {req.path!r} failed re-canonicalization: {exc}") from exc
        change_type = "modified" if resolved.exists() else "created"
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(req.content, encoding="utf-8")
        except OSError as exc:
            self._emit(
                "fs_write",
                {
                    **base_payload,
                    "decision": "denied",
                    "rule_id": RULE_MEDIATED_IO_ERROR,
                    "policy_name": decision.policy_name,
                    "policy_source": "default",
                    "reason": f"mediated write failed: {exc.__class__.__name__}",
                },
            )
            raise PolicyDenied(f"cannot write {req.path!r}: {exc.__class__.__name__}") from exc
        self._emit(
            "fs_write",
            {
                "path": str(resolved),
                "content_bytes": content_bytes,
                "change_type": change_type,
                "decision": "allowed",
                **self._decision_fields(decision),
            },
        )

    # ------------------------------------------------------------- terminal

    async def handle_terminal(self, req: TerminalRequestN) -> TerminalReply:
        decision = self._policy.decide_terminal(req)
        payload: dict[str, Any] = {
            "op": req.op,
            "decision": "allowed-unsupported" if decision.allowed else "denied",
            **self._decision_fields(decision),
        }
        command = req.payload.get("command")
        if isinstance(command, str):
            payload["command"] = command
        self._emit("terminal_op", payload)
        if decision.allowed:
            raise UnsupportedByPolicy(
                "terminal execution matched the trusted user allowlist, but v0.1 "
                "does not implement client-side terminal execution"
            )
        raise PolicyDenied(decision.reason)
