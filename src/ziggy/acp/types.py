"""Native ACP domain types (REQ-001, ARCHITECTURE.md "Native domain types").

Frozen dataclasses — not pydantic — because these are hot-path internal values
created once per protocol frame. They are the ONLY shapes that cross the
``ziggy.acp`` boundary; SDK models never leave this package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True, slots=True)
class HandshakeInfo:
    """Recorded ``initialize`` outcome: identity + capabilities per run/step."""

    protocol_version: int
    agent_name: str
    agent_version: str | None
    agent_title: str | None
    capabilities: dict[str, Any]
    auth_methods: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class MessageChunkEvent:
    role: Literal["agent", "user"]
    text: str
    thought: bool = False


@dataclass(frozen=True, slots=True)
class ToolCallEvent:
    tool_call_id: str
    phase: Literal["start", "update"]
    title: str | None
    kind: str | None
    status: str | None
    locations: list[str]
    has_content: bool
    #: full normalized update payload; kept only for debug capture
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PlanEvent:
    #: each entry: {"content": str, "priority": str, "status": str}
    entries: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class UsageEvent:
    used: int | None
    size: int | None
    cost: float | None
    currency: str | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModeEvent:
    """Session-surface updates: current_mode / config_option / session_info /
    available_commands."""

    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class UnknownUpdateEvent:
    """Any ``session/update`` variant Ziggy does not model; never dropped."""

    update_type: str
    payload: dict[str, Any]


AgentEvent = (
    MessageChunkEvent | ToolCallEvent | PlanEvent | UsageEvent | ModeEvent | UnknownUpdateEvent
)


@dataclass(frozen=True, slots=True)
class StopInfo:
    #: end_turn | max_tokens | max_turn_requests | refusal | cancelled
    stop_reason: str


@dataclass(frozen=True, slots=True)
class PermissionOptionN:
    option_id: str
    name: str
    #: allow_once | allow_always | reject_once | reject_always
    kind: str


@dataclass(frozen=True, slots=True)
class PermissionRequestN:
    session_id: str
    tool_call: dict[str, Any]
    options: list[PermissionOptionN]


@dataclass(frozen=True, slots=True)
class PermissionReply:
    """'selected' picks an option (including reject options); 'cancelled' means
    no option applies (denied/cancelled outcome on the wire)."""

    kind: Literal["selected", "cancelled"]
    option_id: str | None = None


@dataclass(frozen=True, slots=True)
class FsReadRequestN:
    path: str
    line: int | None = None
    limit: int | None = None


@dataclass(frozen=True, slots=True)
class FsWriteRequestN:
    path: str
    content: str


@dataclass(frozen=True, slots=True)
class TerminalRequestN:
    #: create | output | release | wait_for_exit | kill
    op: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TerminalReply:
    """Op-shaped result payload for a served terminal request.

    Expected keys by op: ``create`` -> {"terminal_id"}; ``output`` ->
    {"output", "truncated", "exit_status"?}; ``wait_for_exit`` ->
    {"exit_code"?, "signal"?}; ``release``/``kill`` -> {}.
    """

    payload: dict[str, Any] = field(default_factory=dict)


class PolicyDenied(Exception):
    """Raised by hooks when policy denies a mediated fs/terminal operation."""

    def __init__(self, reason: str = "denied by policy") -> None:
        super().__init__(reason)
        self.reason = reason


class UnsupportedByPolicy(Exception):
    """Raised by hooks for surfaces Ziggy declares unsupported (e.g. terminal
    ops when no allowlist matches the whole surface)."""

    def __init__(self, reason: str = "unsupported") -> None:
        super().__init__(reason)
        self.reason = reason


class MediationHooks(Protocol):
    """Engine-implemented callbacks the adapter invokes while a turn is live."""

    async def on_event(self, ev: AgentEvent) -> None: ...

    async def resolve_permission(self, req: PermissionRequestN) -> PermissionReply: ...

    async def read_text_file(self, req: FsReadRequestN) -> str:
        """Return file content; raises PolicyDenied when out of policy."""
        ...

    async def write_text_file(self, req: FsWriteRequestN) -> None:
        """Write content; raises PolicyDenied when out of policy."""
        ...

    async def handle_terminal(self, req: TerminalRequestN) -> TerminalReply:
        """Serve a terminal op; raises PolicyDenied or UnsupportedByPolicy."""
        ...
