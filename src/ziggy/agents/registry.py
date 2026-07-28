"""Agent registry: builtins merged with trusted user config (REQ-002 registration).

The registry is built from a :class:`ziggy.config.ResolvedConfig`, whose
``[agents.*]`` table is USER_ONLY — every entry here already carries trusted
user authority. A user entry naming a builtin overrides only the fields it
explicitly sets (REQ-002: trusted user config may override command, args, and
explicit environment); custom agents must provide a command.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError as PydanticValidationError

from ziggy.agents.builtins import BUILTIN_AGENTS
from ziggy.errors import ConfigError
from ziggy.models.agent import AgentConfig

if TYPE_CHECKING:
    from ziggy.config import ResolvedConfig

#: Fields a trusted user entry may override on a builtin agent. ``name`` and
#: ``builtin`` are never overridable; ``direct_tools_assumed`` stays at the
#: conservative default until live probes update the capability matrix.
BUILTIN_OVERRIDABLE_FIELDS = frozenset(
    {
        "command",
        "args",
        "env",
        "inherit_env",
        "working_dir",
        "api_key_env",
        "provider",
        "orchestration_eligible",
    }
)

#: Fallback "was this field explicitly set?" tests for entries that do not
#: expose pydantic's ``model_fields_set`` (duck-typed stand-ins).
_OPTIONAL_SCALARS = ("command", "working_dir", "api_key_env", "provider")


def _explicit_overrides(entry: Any) -> dict[str, Any]:
    """Overridable fields the user actually set on ``entry``.

    Pydantic entries report explicit fields via ``model_fields_set``; other
    objects fall back to "non-default value means set". An explicit
    ``command = None`` never overrides (a builtin command cannot be unset).
    """
    fields_set = getattr(entry, "model_fields_set", None)
    if fields_set is not None:
        names = set(fields_set) & BUILTIN_OVERRIDABLE_FIELDS
    else:
        names = set()
        for field in BUILTIN_OVERRIDABLE_FIELDS:
            value = getattr(entry, field, None)
            if field in _OPTIONAL_SCALARS:
                explicit = value is not None
            elif field == "orchestration_eligible":
                explicit = bool(value)
            else:  # args / env / inherit_env: non-empty container
                explicit = bool(value)
            if explicit:
                names.add(field)
    overrides: dict[str, Any] = {}
    for field in names:
        value = getattr(entry, field)
        if field == "command" and value is None:
            continue
        overrides[field] = value
    return overrides


def _validated(name: str, data: dict[str, Any]) -> AgentConfig:
    try:
        return AgentConfig.model_validate(data)
    except PydanticValidationError as exc:
        raise ConfigError(
            f"invalid agent config for '{name}': {exc}", details={"agent": name}
        ) from exc


def _merge_builtin(name: str, entry: Any) -> AgentConfig:
    base = BUILTIN_AGENTS[name]
    return _validated(name, {**base.model_dump(), **_explicit_overrides(entry)})


def _custom_agent(name: str, entry: Any) -> AgentConfig:
    command = getattr(entry, "command", None)
    if not command:
        raise ConfigError(
            f"custom agent '{name}' requires a command (only builtin agents may omit it)",
            details={"agent": name},
        )
    return _validated(
        name,
        {
            "name": name,
            "builtin": False,
            "command": command,
            "args": list(getattr(entry, "args", []) or []),
            "env": dict(getattr(entry, "env", {}) or {}),
            "inherit_env": list(getattr(entry, "inherit_env", []) or []),
            "working_dir": getattr(entry, "working_dir", None),
            "api_key_env": getattr(entry, "api_key_env", None),
            "provider": getattr(entry, "provider", None),
            "orchestration_eligible": bool(getattr(entry, "orchestration_eligible", False)),
            # conservative until proven otherwise; custom agents are never probed
            "direct_tools_assumed": True,
        },
    )


class AgentRegistry:
    """Immutable-by-convention lookup of launchable agents for one run/session."""

    def __init__(self, agents: Mapping[str, AgentConfig]) -> None:
        self._agents: dict[str, AgentConfig] = dict(agents)

    @classmethod
    def from_config(cls, resolved: ResolvedConfig) -> AgentRegistry:
        """Merge builtin defaults with the trusted user ``[agents.*]`` table."""
        merged: dict[str, AgentConfig] = {
            name: cfg.model_copy(deep=True) for name, cfg in BUILTIN_AGENTS.items()
        }
        entries: Mapping[str, Any] = getattr(resolved.config, "agents", {}) or {}
        for name, entry in entries.items():
            if name in BUILTIN_AGENTS:
                merged[name] = _merge_builtin(name, entry)
            else:
                merged[name] = _custom_agent(name, entry)
        return cls(merged)

    def get(self, name: str) -> AgentConfig:
        try:
            return self._agents[name]
        except KeyError:
            registered = self.names()
            raise ConfigError(
                f"unknown agent '{name}'; registered agents: {', '.join(registered)}",
                details={"agent": name, "registered": registered},
            ) from None

    def list(self) -> list[AgentConfig]:
        """All agents, deterministically ordered: builtins first (canonical
        order), then custom agents alphabetically."""
        builtins = [self._agents[name] for name in BUILTIN_AGENTS if name in self._agents]
        customs = sorted(
            (cfg for name, cfg in self._agents.items() if name not in BUILTIN_AGENTS),
            key=lambda cfg: cfg.name,
        )
        return [*builtins, *customs]

    def names(self) -> list[str]:
        return [cfg.name for cfg in self.list()]

    def is_builtin(self, name: str) -> bool:
        return name in BUILTIN_AGENTS
