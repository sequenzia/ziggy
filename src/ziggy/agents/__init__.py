"""Agent registration: pinned builtin metadata + trusted-config registry (REQ-002)."""

from ziggy.agents.builtins import (
    BUILTIN_AGENTS,
    DEFAULT_PROBED_AGENTS,
    INSTALL_HINTS,
    KNOWN_DEGRADATIONS,
    VENDOR_CLI_AGENTS,
)
from ziggy.agents.registry import AgentRegistry

__all__ = [
    "BUILTIN_AGENTS",
    "DEFAULT_PROBED_AGENTS",
    "INSTALL_HINTS",
    "KNOWN_DEGRADATIONS",
    "VENDOR_CLI_AGENTS",
    "AgentRegistry",
]
