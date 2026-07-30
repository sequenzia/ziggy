"""Pinned launch metadata for built-in agents (REQ-002, docs/phase0/capability-matrix.md).

Built-ins come in two launch shapes, and the difference is load-bearing:

*npm-adapter built-ins* (``claude``, ``codex``) launch a reviewed adapter
package through ``npx --no-install``, which enforces the no-silent-download
rule: if the pinned package is absent the launch fails and the package is never
fetched at run time.

*Vendor-CLI built-ins* (``opencode``, ``devin``) speak ACP from the vendor CLI
itself (``opencode acp`` / ``devin acp``), so there is no npm adapter to pin.
Nothing is ever downloaded here either — the command is resolved on ``PATH``
and a missing binary fails the launch — but the launch command **cannot pin a
version**. Their version identity is established only at handshake
(``agentInfo.version``) and must be read from there, never assumed from
``*_REVIEWED_VERSION`` below.

``INSTALL_HINTS`` reaches the user through ``ziggy doctor``, which knows the
agent name. The ``AgentLaunchError`` raised by :mod:`ziggy.acp` sits below this
layer, sees only the command, and so points at ``ziggy doctor`` instead of
carrying the exact install command itself.

Treat every mapping in this module as a constant:
:class:`ziggy.agents.registry.AgentRegistry` deep-copies entries before
applying trusted user-config overrides.
"""

from __future__ import annotations

from ziggy.models.agent import AgentConfig

#: Exact reviewed adapter pins (spec §12.1 + ACP registry review; never "latest").
CLAUDE_ADAPTER_PIN = "claude-agent-acp@0.63.0"
CODEX_ADAPTER_PIN = "codex-acp@1.1.7"

#: ACP subcommand of the vendor CLIs (``opencode acp``, ``devin acp``: JSON-RPC
#: over stdio, meant to be launched by an ACP client — never interactively).
VENDOR_ACP_SUBCOMMAND = "acp"

#: Reviewed vendor-CLI versions, for install hints and docs only — NOT enforced
#: at launch (see the module docstring). OpenCode ships a pinnable npm package;
#: the Devin CLI is distributed by installer/cask only and exposes no pinnable
#: version, hence no constant.
OPENCODE_REVIEWED_VERSION = "1.18.9"

#: Vendor-CLI built-ins: registered and launchable by default, but their CLI is
#: an optional install (unlike the npm adapters the install docs require).
VENDOR_CLI_AGENTS: frozenset[str] = frozenset({"opencode", "devin"})

#: Built-ins a bare ``ziggy doctor`` probes: the release-gating pair whose
#: adapters the install docs require (spec §12.1 checklist). The vendor-CLI
#: built-ins are excluded so a machine that has not installed those optional
#: CLIs — e.g. anyone without a Devin Cloud account — does not see a red bare
#: doctor run; probe them explicitly with ``--agent opencode`` / ``--all``.
DEFAULT_PROBED_AGENTS: frozenset[str] = frozenset({"claude", "codex"})

#: Built-in agents. ``direct_tools_assumed=True`` and
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
    "opencode": AgentConfig(
        name="opencode",
        builtin=True,
        command="opencode",  # resolved on PATH; never downloaded
        args=[VENDOR_ACP_SUBCOMMAND],
        # Not 'anthropic'/'openai': OpenCode is provider-agnostic and routes to
        # whichever model provider the user configured, so naming a vendor would
        # misstate egress. This matches what ``workflows.egress.step_provider``
        # synthesizes for an unlabelled agent, but a shipped built-in declares it:
        # the string lands in persisted RunResults and in users'
        # ``[egress] acknowledged_provider_sets``, so it is an interface, not a
        # derived default to be changed with the fallback.
        provider="custom:opencode",
        api_key_env=None,  # 'opencode auth login' state / per-provider env vars
        orchestration_eligible=False,
        direct_tools_assumed=True,
    ),
    "devin": AgentConfig(
        name="devin",
        builtin=True,
        command="devin",  # resolved on PATH; never downloaded
        args=[VENDOR_ACP_SUBCOMMAND],
        # same reasoning as opencode above: the Devin CLI's model routing is
        # unverified here, so this stays a distinct 'custom:' identity rather
        # than claiming a known vendor, until a live probe records one
        # (capability-matrix step 7).
        provider="custom:devin",
        api_key_env=None,  # browser login to a Devin Cloud account
        orchestration_eligible=False,
        direct_tools_assumed=True,
    ),
}

#: Per-agent known behavioral degradations surfaced by ``ziggy doctor`` and
#: ``ziggy agents list``. Intentionally empty until the deferred live probes
#: run — see docs/phase0/capability-matrix.md ("Deferred live checklist",
#: step 8 updates this mapping). Never guess entries from vendor docs: the
#: quirks named illustratively in spec REQ-002 (Devin terminal rendering, OpenCode
#: undo/redo over ACP) stay UNVERIFIED rows in the matrix until probed.
KNOWN_DEGRADATIONS: dict[str, list[str]] = {
    "claude": [],
    "codex": [],
    "opencode": [],
    "devin": [],
}

#: Exact install lines, surfaced as fix hints by ``ziggy doctor`` (REQ-002:
#: installs are deliberate; Ziggy never downloads during a run).
INSTALL_HINTS: dict[str, str] = {
    "claude": f"npm install -g {CLAUDE_ADAPTER_PIN}",
    "codex": f"npm install -g {CODEX_ADAPTER_PIN}",
    "opencode": f"npm install -g opencode-ai@{OPENCODE_REVIEWED_VERSION}",
    "devin": (
        "brew install --cask devin-cli (macOS) or curl -fsSL https://cli.devin.ai/install.sh | bash"
    ),
}
