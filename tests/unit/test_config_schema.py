"""Unit tests for ziggy.config.schema (REQ-007 pydantic config tree)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from ziggy.config import (
    AgentEntry,
    EgressConfig,
    EngineConfig,
    PermissionsConfig,
    PolicyProfile,
    ProjectDenial,
    TerminalRule,
    TrustedWorkflow,
    ZiggyConfig,
)
from ziggy.models.common import CaptureProfile


class TestDefaults:
    def test_root_defaults(self) -> None:
        cfg = ZiggyConfig(schema_version=1)
        assert cfg.engine.max_workflow_steps == 16
        assert cfg.engine.max_prompt_bytes == 262144
        assert cfg.engine.default_step_timeout_seconds == 1800
        assert cfg.engine.default_workflow_timeout_seconds == 3600
        assert cfg.engine.cancel_grace_seconds == 5.0
        assert cfg.engine.max_event_bytes_per_step == 10 * 2**20
        assert cfg.engine.max_artifact_bytes_per_run == 50 * 2**20
        assert cfg.agents == {}
        assert cfg.permissions.default_policy == "guarded"
        assert cfg.permissions.profiles == {}
        assert cfg.permissions.project_denials == []
        assert cfg.results.persist is True
        assert cfg.results.capture is CaptureProfile.STANDARD
        assert cfg.results.retention_days == 30
        assert cfg.results.auto_prune is False
        assert cfg.results.store_path is None
        assert cfg.server.max_active_runs == 1
        assert cfg.orchestrator.max_inline_steps == 8
        assert cfg.orchestrator.auto_execute is True
        assert cfg.orchestrator.allow_uncontained_planner is False
        assert cfg.orchestrator.eligible_agents == []
        assert cfg.orchestrator.trusted_workflows == []
        assert cfg.redaction.extra_value_env_vars == []
        assert cfg.redaction.patterns == []
        assert cfg.logs.retention_days == 30
        assert cfg.workflows.default_name is None
        assert cfg.egress.acknowledged_provider_sets == []

    def test_section_defaults_are_independent_instances(self) -> None:
        a = ZiggyConfig(schema_version=1)
        b = ZiggyConfig(schema_version=1)
        a.agents["x"] = AgentEntry(command="/bin/x")
        assert b.agents == {}


class TestSchemaVersion:
    def test_required(self) -> None:
        with pytest.raises(PydanticValidationError):
            ZiggyConfig()  # type: ignore[call-arg]

    def test_only_version_1(self) -> None:
        with pytest.raises(PydanticValidationError):
            ZiggyConfig(schema_version=2)  # type: ignore[arg-type]


class TestExtraForbidden:
    def test_unknown_top_level_key(self) -> None:
        with pytest.raises(PydanticValidationError):
            ZiggyConfig.model_validate({"schema_version": 1, "bogus": True})

    def test_unknown_nested_key(self) -> None:
        with pytest.raises(PydanticValidationError):
            ZiggyConfig.model_validate({"schema_version": 1, "engine": {"nope": 1}})

    def test_unknown_agent_key(self) -> None:
        with pytest.raises(PydanticValidationError):
            AgentEntry.model_validate({"command": "/bin/x", "sudo": True})


class TestContractDecisions:
    def test_policy_profile_has_no_allow_read_outside_workspace(self) -> None:
        assert "allow_read_outside_workspace" not in PolicyProfile.model_fields
        with pytest.raises(PydanticValidationError):
            PolicyProfile.model_validate({"allow_read_outside_workspace": True})

    def test_agent_entry_has_no_acknowledged_egress(self) -> None:
        assert "acknowledged_egress" not in AgentEntry.model_fields
        with pytest.raises(PydanticValidationError):
            AgentEntry.model_validate({"command": "/bin/x", "acknowledged_egress": [["a"]]})

    def test_egress_holds_acknowledged_provider_sets(self) -> None:
        egress = EgressConfig.model_validate(
            {"acknowledged_provider_sets": [["anthropic", "openai"]]}
        )
        assert egress.acknowledged_provider_sets == [["anthropic", "openai"]]


class TestSubModels:
    def test_terminal_rule_requires_command(self) -> None:
        rule = TerminalRule.model_validate({"command": "git", "args_prefix": ["status"]})
        assert rule.command == "git"
        with pytest.raises(PydanticValidationError):
            TerminalRule.model_validate({"args_prefix": ["x"]})

    def test_policy_profile_fields(self) -> None:
        profile = PolicyProfile.model_validate(
            {"terminal_allowlist": [{"command": "git"}], "deny_paths": ["**/secrets/**"]}
        )
        assert profile.terminal_allowlist[0].args_prefix == []
        assert profile.deny_paths == ["**/secrets/**"]

    def test_project_denial_kind_literal(self) -> None:
        assert ProjectDenial(kind="path", pattern="**/prod/**").kind == "path"
        assert ProjectDenial(kind="terminal", pattern="rm").kind == "terminal"
        with pytest.raises(PydanticValidationError):
            ProjectDenial.model_validate({"kind": "exec", "pattern": "x"})

    def test_trusted_workflow_requires_path_and_hash(self) -> None:
        wf = TrustedWorkflow.model_validate({"path": "wf.yaml", "sha256": "ab" * 32})
        assert wf.sha256 == "ab" * 32
        with pytest.raises(PydanticValidationError):
            TrustedWorkflow.model_validate({"path": "wf.yaml"})

    def test_capture_accepts_string(self) -> None:
        cfg = ZiggyConfig.model_validate({"schema_version": 1, "results": {"capture": "metadata"}})
        assert cfg.results.capture is CaptureProfile.METADATA

    def test_permissions_profiles_mapping(self) -> None:
        cfg = PermissionsConfig.model_validate(
            {"default_policy": "guarded", "profiles": {"tight": {"deny_paths": ["**/.git/**"]}}}
        )
        assert cfg.profiles["tight"].deny_paths == ["**/.git/**"]

    def test_engine_types(self) -> None:
        engine = EngineConfig.model_validate({"cancel_grace_seconds": 2})
        assert engine.cancel_grace_seconds == 2.0
        with pytest.raises(PydanticValidationError):
            EngineConfig.model_validate({"max_prompt_bytes": "lots"})
