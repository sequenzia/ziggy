"""Unit tests for ziggy.agents (REQ-002 registration: builtins + registry merge)."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from ziggy.agents import (
    BUILTIN_AGENTS,
    DEFAULT_PROBED_AGENTS,
    INSTALL_HINTS,
    KNOWN_DEGRADATIONS,
    VENDOR_CLI_AGENTS,
    AgentRegistry,
)
from ziggy.agents.builtins import (
    CLAUDE_ADAPTER_PIN,
    CODEX_ADAPTER_PIN,
    OPENCODE_REVIEWED_VERSION,
)
from ziggy.config.loader import BUILTIN_AGENT_NAMES
from ziggy.errors import ConfigError
from ziggy.workflows.egress import step_provider


class FakeAgentEntry(BaseModel):
    """Mirror of the phase2-contracts AgentEntry shape (explicit-field aware)."""

    model_config = ConfigDict(extra="forbid")

    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}
    inherit_env: list[str] = []
    working_dir: str | None = None
    api_key_env: str | None = None
    provider: str | None = None
    orchestration_eligible: bool = False


@dataclass
class FakeZiggyConfig:
    agents: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeResolved:
    config: FakeZiggyConfig = field(default_factory=FakeZiggyConfig)


def resolved(agents: dict[str, Any] | None = None) -> FakeResolved:
    return FakeResolved(config=FakeZiggyConfig(agents=agents or {}))


class TestBuiltins:
    def test_claude_pin(self) -> None:
        claude = BUILTIN_AGENTS["claude"]
        assert claude.name == "claude"
        assert claude.builtin is True
        assert claude.command == "npx"
        assert claude.args == ["--no-install", "@agentclientprotocol/claude-agent-acp@0.64.0"]
        assert claude.provider == "anthropic"
        assert claude.api_key_env is None
        assert claude.orchestration_eligible is False
        assert claude.direct_tools_assumed is True

    def test_codex_pin(self) -> None:
        codex = BUILTIN_AGENTS["codex"]
        assert codex.name == "codex"
        assert codex.builtin is True
        assert codex.command == "npx"
        assert codex.args == ["--no-install", "@agentclientprotocol/codex-acp@1.1.7"]
        assert codex.provider == "openai"
        assert codex.api_key_env is None
        assert codex.orchestration_eligible is False
        assert codex.direct_tools_assumed is True

    def test_opencode_launches_vendor_cli_acp_subcommand(self) -> None:
        opencode = BUILTIN_AGENTS["opencode"]
        assert opencode.name == "opencode"
        assert opencode.builtin is True
        # the vendor CLI speaks ACP itself: PATH-resolved, no npm adapter to pin
        assert opencode.command == "opencode"
        assert opencode.args == ["acp"]
        # provider-agnostic, so never labelled with a vendor (see builtins.py)
        assert opencode.provider == "custom:opencode"
        assert opencode.api_key_env is None
        assert opencode.orchestration_eligible is False
        assert opencode.direct_tools_assumed is True

    def test_devin_launches_vendor_cli_acp_subcommand(self) -> None:
        devin = BUILTIN_AGENTS["devin"]
        assert devin.name == "devin"
        assert devin.builtin is True
        assert devin.command == "devin"
        assert devin.args == ["acp"]
        assert devin.provider == "custom:devin"
        assert devin.api_key_env is None
        assert devin.orchestration_eligible is False
        assert devin.direct_tools_assumed is True

    def test_vendor_cli_builtins_never_launch_through_npx(self) -> None:
        """No ``npx``, so ``--no-install`` cannot apply; nothing is downloadable."""
        for name in VENDOR_CLI_AGENTS:
            agent = BUILTIN_AGENTS[name]
            assert agent.command != "npx"
            assert "--no-install" not in agent.args
            assert not any("@" in arg for arg in agent.args)  # no version pin in args

    def test_vendor_cli_egress_identity_is_recorded_and_consistent(self) -> None:
        """A declared built-in identity must not diverge from the fallback a
        workflow step would synthesize for the same agent — one audit identity
        across the direct-run, workflow, and planner paths."""
        for name in VENDOR_CLI_AGENTS:
            agent = BUILTIN_AGENTS[name]
            assert agent.provider == f"custom:{name}"
            assert step_provider(agent) == agent.provider
            assert agent.provider not in {"anthropic", "openai"}  # claims no vendor

    def test_vendor_cli_agents_are_builtins(self) -> None:
        assert sorted(VENDOR_CLI_AGENTS) == ["devin", "opencode"]
        assert VENDOR_CLI_AGENTS.issubset(BUILTIN_AGENTS)

    def test_default_probed_agents_excludes_optional_vendor_clis(self) -> None:
        """A bare ``ziggy doctor`` must not go red over an optional vendor CLI."""
        assert sorted(DEFAULT_PROBED_AGENTS) == ["claude", "codex"]
        assert DEFAULT_PROBED_AGENTS.issubset(BUILTIN_AGENTS)
        assert DEFAULT_PROBED_AGENTS.isdisjoint(VENDOR_CLI_AGENTS)

    def test_builtins_are_conservative_until_live_probes(self) -> None:
        for agent in BUILTIN_AGENTS.values():
            assert agent.builtin is True
            assert agent.direct_tools_assumed is True
            assert agent.orchestration_eligible is False

    def test_known_degradations_empty_until_live_probes(self) -> None:
        assert set(KNOWN_DEGRADATIONS) == set(BUILTIN_AGENTS)
        assert all(entries == [] for entries in KNOWN_DEGRADATIONS.values())

    def test_install_hints_carry_exact_pins(self) -> None:
        assert set(INSTALL_HINTS) == set(BUILTIN_AGENTS)
        assert INSTALL_HINTS["claude"] == f"npm install -g {CLAUDE_ADAPTER_PIN}"
        assert INSTALL_HINTS["codex"] == f"npm install -g {CODEX_ADAPTER_PIN}"
        assert (
            INSTALL_HINTS["opencode"] == f"npm install -g opencode-ai@{OPENCODE_REVIEWED_VERSION}"
        )
        assert "cli.devin.ai/install.sh" in INSTALL_HINTS["devin"]

    def test_config_loader_builtin_names_do_not_drift(self) -> None:
        """Only builtins may omit ``command`` in config — one source of truth."""
        assert sorted(BUILTIN_AGENT_NAMES) == sorted(BUILTIN_AGENTS)


class TestRegistryFromConfig:
    def test_empty_config_yields_builtins(self) -> None:
        registry = AgentRegistry.from_config(resolved())
        assert registry.names() == ["claude", "codex", "opencode", "devin"]
        assert registry.get("claude").command == "npx"
        assert all(registry.is_builtin(name) for name in registry.names())

    def test_builtin_partial_override_keeps_unset_fields(self) -> None:
        registry = AgentRegistry.from_config(
            resolved(
                {
                    "claude": FakeAgentEntry(
                        api_key_env="ANTHROPIC_API_KEY", inherit_env=["SSH_AUTH_SOCK"]
                    )
                }
            )
        )
        claude = registry.get("claude")
        assert claude.api_key_env == "ANTHROPIC_API_KEY"
        assert claude.inherit_env == ["SSH_AUTH_SOCK"]
        # everything not explicitly set keeps the pinned builtin defaults
        assert claude.command == "npx"
        assert claude.args == ["--no-install", CLAUDE_ADAPTER_PIN]
        assert claude.provider == "anthropic"
        assert claude.builtin is True
        assert claude.direct_tools_assumed is True

    def test_trusted_user_may_override_builtin_command(self) -> None:
        registry = AgentRegistry.from_config(
            resolved({"claude": FakeAgentEntry(command="/opt/agents/claude-acp", args=["acp"])})
        )
        claude = registry.get("claude")
        assert claude.command == "/opt/agents/claude-acp"
        assert claude.args == ["acp"]
        assert claude.builtin is True

    def test_explicit_empty_args_override_builtin_args(self) -> None:
        registry = AgentRegistry.from_config(resolved({"codex": FakeAgentEntry(args=[])}))
        assert registry.get("codex").args == []

    def test_orchestration_eligible_override(self) -> None:
        registry = AgentRegistry.from_config(
            resolved({"codex": FakeAgentEntry(orchestration_eligible=True)})
        )
        assert registry.get("codex").orchestration_eligible is True
        assert registry.get("claude").orchestration_eligible is False

    def test_override_does_not_mutate_module_builtins(self) -> None:
        AgentRegistry.from_config(
            resolved({"claude": FakeAgentEntry(command="/x", api_key_env="K", args=["a"])})
        )
        assert BUILTIN_AGENTS["claude"].command == "npx"
        assert BUILTIN_AGENTS["claude"].api_key_env is None
        assert BUILTIN_AGENTS["claude"].args == ["--no-install", CLAUDE_ADAPTER_PIN]

    def test_duck_typed_entry_without_fields_set(self) -> None:
        entry = SimpleNamespace(
            command=None,
            args=[],
            env={},
            inherit_env=[],
            working_dir=None,
            api_key_env="ANTHROPIC_API_KEY",
            provider=None,
            orchestration_eligible=False,
        )
        registry = AgentRegistry.from_config(resolved({"claude": entry}))
        claude = registry.get("claude")
        assert claude.api_key_env == "ANTHROPIC_API_KEY"
        assert claude.command == "npx"  # command=None never clobbers the pin
        assert claude.args == ["--no-install", CLAUDE_ADAPTER_PIN]

    def test_custom_agent_registration(self) -> None:
        registry = AgentRegistry.from_config(
            resolved(
                {
                    "internal-helper": FakeAgentEntry(
                        command="/opt/agents/helper",
                        args=["acp"],
                        env={"HELPER_MODE": "ci"},
                        inherit_env=["SSH_AUTH_SOCK"],
                        working_dir="/srv/helper",
                        api_key_env="HELPER_API_KEY",
                        provider="custom",
                    )
                }
            )
        )
        helper = registry.get("internal-helper")
        assert helper.name == "internal-helper"
        assert helper.builtin is False
        assert helper.command == "/opt/agents/helper"
        assert helper.args == ["acp"]
        assert helper.env == {"HELPER_MODE": "ci"}
        assert helper.inherit_env == ["SSH_AUTH_SOCK"]
        assert helper.working_dir == "/srv/helper"
        assert helper.api_key_env == "HELPER_API_KEY"
        assert helper.provider == "custom"
        assert helper.orchestration_eligible is False
        assert helper.direct_tools_assumed is True
        assert not registry.is_builtin("internal-helper")

    def test_custom_agent_without_command_rejected(self) -> None:
        with pytest.raises(ConfigError, match="requires a command"):
            AgentRegistry.from_config(
                resolved({"helper": FakeAgentEntry(api_key_env="HELPER_API_KEY")})
            )

    def test_custom_agent_invalid_name_rejected(self) -> None:
        with pytest.raises(ConfigError, match="invalid agent config"):
            AgentRegistry.from_config(resolved({"bad name!": FakeAgentEntry(command="/bin/x")}))


class TestRegistryLookup:
    def test_unknown_agent_lists_registered_names(self) -> None:
        registry = AgentRegistry.from_config(
            resolved({"helper": FakeAgentEntry(command="/bin/helper")})
        )
        with pytest.raises(ConfigError, match="unknown agent 'nope'") as exc_info:
            registry.get("nope")
        message = str(exc_info.value)
        assert "claude" in message
        assert "codex" in message
        assert "helper" in message
        assert exc_info.value.details["registered"] == [
            "claude",
            "codex",
            "opencode",
            "devin",
            "helper",
        ]
        assert exc_info.value.exit_code == 2

    def test_list_ordered_builtins_then_customs_alphabetical(self) -> None:
        registry = AgentRegistry.from_config(
            resolved(
                {
                    "zeta": FakeAgentEntry(command="/bin/zeta"),
                    "alpha": FakeAgentEntry(command="/bin/alpha"),
                }
            )
        )
        assert [cfg.name for cfg in registry.list()] == [
            "claude",
            "codex",
            "opencode",
            "devin",
            "alpha",
            "zeta",
        ]
