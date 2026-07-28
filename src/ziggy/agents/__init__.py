"""Agent registration: pinned builtin metadata + trusted-config registry (REQ-002)."""

from ziggy.agents.builtins import BUILTIN_AGENTS, INSTALL_HINTS, KNOWN_DEGRADATIONS
from ziggy.agents.registry import AgentRegistry

__all__ = ["BUILTIN_AGENTS", "INSTALL_HINTS", "KNOWN_DEGRADATIONS", "AgentRegistry"]
