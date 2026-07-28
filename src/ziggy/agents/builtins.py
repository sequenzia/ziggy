"""Pinned launch metadata for built-in agents (REQ-002, docs/phase0/capability-matrix.md).

Launch commands are exact reviewed pins; ``npx --no-install`` enforces the
no-silent-download rule — if the pinned package is absent, launch fails with
an ``AgentLaunchError`` carrying the matching ``INSTALL_HINTS`` entry, and the
package is never fetched at run time. Treat every mapping in this module as a
constant: :class:`ziggy.agents.registry.AgentRegistry` deep-copies entries
before applying trusted user-config overrides.
"""

from __future__ import annotations

from ziggy.models.agent import AgentConfig

#: Exact reviewed adapter pins (spec §12.1 + ACP registry review; never "latest").
CLAUDE_ADAPTER_PIN = "claude-agent-acp@0.63.0"
CODEX_ADAPTER_PIN = "codex-acp@1.1.7"

#: Built-in agents (v0.1: claude + codex). ``direct_tools_assumed=True`` and
#: ``orchestration_eligible=False`` are the conservative defaults required
#: while the capability-matrix live probes remain deferred: mediation stays
#: advisory and planning with these agents requires the explicit
#: ``allow_uncontained_planner`` acknowledgement.
BUILTIN_AGENTS: dict[str, AgentConfig] = {
    "claude": AgentConfig(
        name="claude",
        builtin=True,
        command="npx",
        args=["--no-install", CLAUDE_ADAPTER_PIN],
        provider="anthropic",
        api_key_env=None,  # adapter-managed login via HOME by default
        orchestration_eligible=False,
        direct_tools_assumed=True,
    ),
    "codex": AgentConfig(
        name="codex",
        builtin=True,
        command="npx",
        args=["--no-install", CODEX_ADAPTER_PIN],
        provider="openai",
        api_key_env=None,  # ChatGPT login state by default
        orchestration_eligible=False,
        direct_tools_assumed=True,
    ),
}

#: Per-agent known behavioral degradations surfaced by ``ziggy doctor`` and
#: ``ziggy agents list``. Intentionally empty until the deferred live probes
#: run — see docs/phase0/capability-matrix.md ("Deferred live checklist",
#: step 7 updates this mapping). Never guess entries from vendor docs.
KNOWN_DEGRADATIONS: dict[str, list[str]] = {
    "claude": [],
    "codex": [],
}

#: Exact install hints for the AgentLaunchError path and ``ziggy doctor``
#: (REQ-002: installs are deliberate; Ziggy never downloads during a run).
INSTALL_HINTS: dict[str, str] = {
    "claude": f"npm install -g {CLAUDE_ADAPTER_PIN}",
    "codex": f"npm install -g {CODEX_ADAPTER_PIN}",
}
