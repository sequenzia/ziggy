"""ACP adapter — the ONLY package that imports the ``acp`` SDK (REQ-001).

This module re-exports the Ziggy-native surface exclusively; no SDK name ever
appears here. Everything outside ``ziggy.acp`` builds against these types.
"""

from ziggy.acp.client import AgentProcessClient
from ziggy.acp.types import (
    AgentEvent,
    FsReadRequestN,
    FsWriteRequestN,
    HandshakeInfo,
    MediationHooks,
    MessageChunkEvent,
    ModeEvent,
    PermissionOptionN,
    PermissionReply,
    PermissionRequestN,
    PlanEvent,
    PolicyDenied,
    StopInfo,
    TerminalReply,
    TerminalRequestN,
    ToolCallEvent,
    UnknownUpdateEvent,
    UnsupportedByPolicy,
    UsageEvent,
)

__all__ = [
    "AgentEvent",
    "AgentProcessClient",
    "FsReadRequestN",
    "FsWriteRequestN",
    "HandshakeInfo",
    "MediationHooks",
    "MessageChunkEvent",
    "ModeEvent",
    "PermissionOptionN",
    "PermissionReply",
    "PermissionRequestN",
    "PlanEvent",
    "PolicyDenied",
    "StopInfo",
    "TerminalReply",
    "TerminalRequestN",
    "ToolCallEvent",
    "UnknownUpdateEvent",
    "UnsupportedByPolicy",
    "UsageEvent",
]
