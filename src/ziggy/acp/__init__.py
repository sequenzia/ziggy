"""ACP adapter — the ONLY package that imports the ``acp`` SDK (REQ-001).

This module re-exports the Ziggy-native surface exclusively; no SDK name ever
appears here. Everything outside ``ziggy.acp`` builds against these types.
"""

from ziggy.acp.client import AgentProcessClient
from ziggy.acp.server import ServerConnection, ZiggyAgentProtocol, serve_stdio
from ziggy.acp.types import (
    AgentEvent,
    ClientPermissionUnsupported,
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
    RouteState,
    ServerHandshake,
    ServerNotice,
    ServerStopInfo,
    ServerUpdate,
    SessionOpened,
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
    "ClientPermissionUnsupported",
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
    "RouteState",
    "ServerConnection",
    "ServerHandshake",
    "ServerNotice",
    "ServerStopInfo",
    "ServerUpdate",
    "SessionOpened",
    "StopInfo",
    "TerminalReply",
    "TerminalRequestN",
    "ToolCallEvent",
    "UnknownUpdateEvent",
    "UnsupportedByPolicy",
    "UsageEvent",
    "ZiggyAgentProtocol",
    "serve_stdio",
]
