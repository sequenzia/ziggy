"""Child-process environment composition (REQ-007, spec §6.2 Authentication).

Ziggy never passes the parent environment through wholesale. The documented
minimal baseline is ``HOME`` and ``PATH`` (plus ``TERM`` and ``LANG`` when the
parent has them); everything else must be named explicitly — either in the
agent's ``inherit_env`` list, its literal ``env`` table, or its single
``api_key_env`` credential reference. A named-but-unset credential fails with
``ConfigError`` before any subprocess is launched (spec §5.1 error table).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from ziggy.errors import ConfigError
from ziggy.models.agent import AgentConfig

#: Documented minimal baseline: forwarded when present in the parent env.
#: HOME additionally carries adapter-managed login state (spec §6.2).
BASELINE_ENV_VARS: tuple[str, ...] = ("HOME", "PATH", "TERM", "LANG")


def compose_child_env(
    agent: AgentConfig,
    base_env: Mapping[str, str] = os.environ,
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Compose the complete explicit environment for one agent subprocess.

    Layers, later wins: baseline (present-in-parent only) -> ``inherit_env``
    names present in the parent -> ``env`` literals -> the ``api_key_env``
    value. Inherited names absent from the parent are skipped silently; a
    named ``api_key_env`` that is missing or empty raises ``ConfigError``
    BEFORE launch with the spec §5.1 message shape.

    Returns ``(env, secret_values)`` where ``secret_values`` is the
    ``[("env:VAR", value)]`` list for the credential variable, shaped for
    :class:`ziggy.redact.Redactor` exact-value matching (replacing the
    Phase-1 name-heuristic).
    """
    env: dict[str, str] = {}
    for name in BASELINE_ENV_VARS:
        if name in base_env:
            env[name] = base_env[name]
    for name in agent.inherit_env:
        if name in base_env:
            env[name] = base_env[name]
    env.update(agent.env)

    secret_values: list[tuple[str, str]] = []
    if agent.api_key_env is not None:
        value = base_env.get(agent.api_key_env, "")
        if not value:
            raise ConfigError(
                f"Agent '{agent.name}' requires env var {agent.api_key_env} (not set).",
                details={"agent": agent.name, "env_var": agent.api_key_env},
            )
        env[agent.api_key_env] = value
        secret_values.append((f"env:{agent.api_key_env}", value))
    return env, secret_values
