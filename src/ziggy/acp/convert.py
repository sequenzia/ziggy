"""SDK <-> native conversions (pure functions, no I/O).

Every ``session/update`` variant of the pinned SDK (schema-v1.16.0, 13 models)
maps to exactly one native ``AgentEvent``; anything unmodeled becomes
``UnknownUpdateEvent`` — conversion never raises on agent-supplied shapes.
"""

from __future__ import annotations

from typing import Any, get_args

from acp.schema import (
    AgentMessageChunk,
    AgentPlanContentUpdate,
    AgentPlanRemovedUpdate,
    AgentPlanUpdate,
    AgentThoughtChunk,
    AllowedOutcome,
    AvailableCommandsUpdate,
    BaseModel,
    ConfigOptionUpdate,
    Cost,
    CurrentModeUpdate,
    DeniedOutcome,
    InitializeResponse,
    PermissionOption,
    PlanEntry,
    PlanEntryPriority,
    PlanEntryStatus,
    PlanUpdateItems,
    RequestPermissionResponse,
    SessionInfoUpdate,
    TextContentBlock,
    ToolCallLocation,
    ToolCallProgress,
    ToolCallStart,
    ToolCallStatus,
    ToolCallUpdate,
    ToolKind,
    UsageUpdate,
    UserMessageChunk,
)

from ziggy.acp.types import (
    AgentEvent,
    HandshakeInfo,
    MessageChunkEvent,
    ModeEvent,
    PermissionOptionN,
    PermissionReply,
    PermissionRequestN,
    PlanEvent,
    ServerNotice,
    ServerUpdate,
    ToolCallEvent,
    UnknownUpdateEvent,
    UsageEvent,
)


def _dump(model: BaseModel) -> dict[str, Any]:
    """Native-side plain dict: snake_case keys, JSON-safe values.

    The true wire shape (camelCase) is available separately via raw_frame_cb.
    ``warnings=False`` because generated SDK models carry loosely-typed default
    values that trip pydantic's serializer warnings; the dump itself is fine.
    """
    return model.model_dump(mode="json", exclude_none=True, warnings=False)


def content_block_text(block: Any) -> str:
    """Text of a content block; non-text blocks become a typed marker, never a crash."""
    if isinstance(block, TextContentBlock):
        return block.text
    block_type = getattr(block, "type", None) or type(block).__name__
    return f"[{block_type}]"


def _plan_entries(entries: list[PlanEntry]) -> list[dict[str, Any]]:
    return [{"content": e.content, "priority": e.priority, "status": e.status} for e in entries]


def _locations(update: ToolCallStart | ToolCallProgress) -> list[str]:
    return [loc.path for loc in (update.locations or [])]


def to_agent_event(update: Any) -> AgentEvent:
    """Normalize one SDK ``session/update`` variant to a native AgentEvent."""
    if isinstance(update, UserMessageChunk):
        return MessageChunkEvent(role="user", text=content_block_text(update.content))
    if isinstance(update, AgentMessageChunk):
        return MessageChunkEvent(role="agent", text=content_block_text(update.content))
    if isinstance(update, AgentThoughtChunk):
        return MessageChunkEvent(
            role="agent", text=content_block_text(update.content), thought=True
        )
    if isinstance(update, ToolCallStart):
        return ToolCallEvent(
            tool_call_id=update.tool_call_id,
            phase="start",
            title=update.title,
            kind=update.kind,
            status=update.status,
            locations=_locations(update),
            has_content=bool(update.content),
            raw=_dump(update),
        )
    if isinstance(update, ToolCallProgress):
        return ToolCallEvent(
            tool_call_id=update.tool_call_id,
            phase="update",
            title=update.title,
            kind=update.kind,
            status=update.status,
            locations=_locations(update),
            has_content=bool(update.content),
            raw=_dump(update),
        )
    if isinstance(update, AgentPlanUpdate):
        return PlanEvent(entries=_plan_entries(update.entries))
    if isinstance(update, AgentPlanContentUpdate):
        # v2-ish plan surface: item lists normalize to the generic PlanEvent;
        # file/markdown plans have no entry shape, so preserve them unmodeled.
        if isinstance(update.plan, PlanUpdateItems):
            return PlanEvent(entries=_plan_entries(update.plan.entries))
        return UnknownUpdateEvent(update_type=update.session_update, payload=_dump(update))
    if isinstance(update, AgentPlanRemovedUpdate):
        # The client replaces the whole plan on every update; removal == empty plan.
        return PlanEvent(entries=[])
    if isinstance(update, UsageUpdate):
        return UsageEvent(
            used=update.used,
            size=update.size,
            cost=update.cost.amount if update.cost else None,
            currency=update.cost.currency if update.cost else None,
            raw=_dump(update),
        )
    if isinstance(update, CurrentModeUpdate):
        return ModeEvent(kind="current_mode", payload=_dump(update))
    if isinstance(update, ConfigOptionUpdate):
        return ModeEvent(kind="config_option", payload=_dump(update))
    if isinstance(update, SessionInfoUpdate):
        return ModeEvent(kind="session_info", payload=_dump(update))
    if isinstance(update, AvailableCommandsUpdate):
        return ModeEvent(kind="available_commands", payload=_dump(update))

    update_type = getattr(update, "session_update", None) or type(update).__name__
    if isinstance(update, BaseModel):
        payload = _dump(update)
    else:
        payload = {"repr": repr(update)}
    return UnknownUpdateEvent(update_type=str(update_type), payload=payload)


def to_permission_request(
    session_id: str, tool_call: ToolCallUpdate, options: list[PermissionOption]
) -> PermissionRequestN:
    return PermissionRequestN(
        session_id=session_id,
        tool_call=_dump(tool_call),
        options=[
            PermissionOptionN(option_id=o.option_id, name=o.name, kind=o.kind) for o in options
        ],
    )


def to_permission_response(reply: PermissionReply) -> RequestPermissionResponse:
    """Native reply -> wire outcome.

    A 'selected' reply without an option_id is a caller bug; it degrades to the
    denied outcome because failing toward denial is the only safe direction.
    """
    if reply.kind == "selected" and reply.option_id is not None:
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=reply.option_id)
        )
    return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))


def to_handshake_info(resp: InitializeResponse) -> HandshakeInfo:
    info = resp.agent_info
    return HandshakeInfo(
        protocol_version=resp.protocol_version,
        agent_name=info.name if info is not None else "unknown",
        agent_version=info.version if info is not None else None,
        agent_title=info.title if info is not None else None,
        capabilities=_dump(resp.agent_capabilities) if resp.agent_capabilities else {},
        auth_methods=[_dump(m) for m in resp.auth_methods or []],
    )


# --- serving direction (native -> SDK), used only by ziggy/acp/server.py ---

#: SDK ``session/update`` variants Ziggy emits when serving a client.
ServedSessionUpdate = (
    AgentMessageChunk
    | AgentThoughtChunk
    | ToolCallStart
    | ToolCallProgress
    | AgentPlanUpdate
    | UsageUpdate
)

_TOOL_KINDS = frozenset(get_args(ToolKind))
_TOOL_CALL_STATUSES = frozenset(get_args(ToolCallStatus))
_PLAN_PRIORITIES = frozenset(get_args(PlanEntryPriority))
_PLAN_STATUSES = frozenset(get_args(PlanEntryStatus))


def _served_text_chunk(
    text: str, *, thought: bool = False
) -> AgentMessageChunk | AgentThoughtChunk:
    content = TextContentBlock(type="text", text=text)
    if thought:
        return AgentThoughtChunk(session_update="agent_thought_chunk", content=content)
    return AgentMessageChunk(session_update="agent_message_chunk", content=content)


def _served_plan_entry(entry: dict[str, Any]) -> PlanEntry:
    """Native plan entry dict -> PlanEntry; unknown vocabulary degrades to the
    neutral defaults instead of crashing the re-emission stream."""
    priority = entry.get("priority")
    status = entry.get("status")
    return PlanEntry(
        content=str(entry.get("content", "")),
        priority=priority if priority in _PLAN_PRIORITIES else "medium",
        status=status if status in _PLAN_STATUSES else "pending",
    )


def from_agent_event(ev: ServerUpdate) -> ServedSessionUpdate | None:
    """Map one native event to its v1 ``session/update`` model, or None to skip.

    Mapping (phase45-contracts): MessageChunkEvent -> agent_message_chunk
    (thought -> agent_thought_chunk); ToolCallEvent start/update ->
    ToolCallStart/ToolCallProgress; PlanEvent -> AgentPlanUpdate; UsageEvent ->
    UsageUpdate only when used+size are present; ServerNotice ->
    agent_message_chunk. ModeEvent/UnknownUpdateEvent have no v1 serving-side
    mapping and are skipped. Never raises on native shapes: vocabulary the SDK
    does not model degrades (tool kind/status -> unspecified).
    """
    if isinstance(ev, ServerNotice):
        return _served_text_chunk(ev.text)
    if isinstance(ev, MessageChunkEvent):
        return _served_text_chunk(ev.text, thought=ev.thought)
    if isinstance(ev, ToolCallEvent):
        kind = ev.kind if ev.kind in _TOOL_KINDS else None
        status = ev.status if ev.status in _TOOL_CALL_STATUSES else None
        locations = [ToolCallLocation(path=path) for path in ev.locations] or None
        if ev.phase == "start":
            return ToolCallStart(
                session_update="tool_call",
                tool_call_id=ev.tool_call_id,
                # SDK requires a title on tool_call start; fall back to the id.
                title=ev.title or ev.tool_call_id,
                kind=kind,
                status=status,
                locations=locations,
            )
        return ToolCallProgress(
            session_update="tool_call_update",
            tool_call_id=ev.tool_call_id,
            title=ev.title,
            kind=kind,
            status=status,
            locations=locations,
        )
    if isinstance(ev, PlanEvent):
        return AgentPlanUpdate(
            session_update="plan", entries=[_served_plan_entry(e) for e in ev.entries]
        )
    if isinstance(ev, UsageEvent):
        if ev.used is None or ev.size is None:
            return None
        cost = (
            Cost(amount=ev.cost, currency=ev.currency)
            if ev.cost is not None and ev.currency is not None
            else None
        )
        return UsageUpdate(session_update="usage_update", used=ev.used, size=ev.size, cost=cost)
    return None


def from_permission_request(
    req: PermissionRequestN, context_label: str
) -> tuple[ToolCallUpdate, list[PermissionOption]]:
    """Native permission request -> SDK request parts for forwarding.

    The tool-call title is prefixed with ``[context_label] `` so the connecting
    client sees which downstream agent/step is asking.
    """
    payload = dict(req.tool_call)
    title = payload.get("title")
    payload["title"] = f"[{context_label}] {title}" if title else f"[{context_label}]"
    tool_call = ToolCallUpdate.model_validate(payload)
    options = [
        PermissionOption(option_id=o.option_id, name=o.name, kind=o.kind) for o in req.options
    ]
    return tool_call, options


def from_permission_outcome(resp: RequestPermissionResponse) -> PermissionReply:
    """SDK permission response -> native reply ('selected' picks an option,
    including reject options; anything else means cancelled/denied)."""
    outcome = resp.outcome
    if isinstance(outcome, AllowedOutcome):
        return PermissionReply(kind="selected", option_id=outcome.option_id)
    return PermissionReply(kind="cancelled")
