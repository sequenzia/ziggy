"""Unit tests for ziggy.orchestrator.catalog (REQ-013 catalog + meta-prompt).

Trusted-workflow drop semantics under test: a pinned entry whose path fails
fail-closed containment (``resolve_contained``), whose file is unreadable,
whose content hash no longer matches the pin, or whose YAML fails validation
DROPS OUT of the catalog with a ``Catalog.warnings`` entry — it is never an
error (spec REQ-013: a changed hash drops out until re-approved). Eligible-
agent incoherence, by contrast, is a trusted-user config problem and raises
``ConfigError``.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from ziggy.agents.registry import AgentRegistry
from ziggy.config import ResolvedConfig, ZiggyConfig
from ziggy.errors import ConfigError
from ziggy.models.agent import AgentConfig
from ziggy.orchestrator.catalog import (
    DESCRIPTION_CLOSE,
    DESCRIPTION_OPEN,
    GOAL_CLOSE,
    GOAL_OPEN,
    MAX_DESCRIPTION_CHARS,
    TRUNCATION_MARKER,
    Catalog,
    CatalogAgent,
    build_catalog,
    render_meta_prompt,
)

LIMITS = {"max_inline_steps": 8, "max_prompt_bytes": 262144}


def make_resolved(
    orchestrator: dict[str, Any] | None = None, agents: dict[str, Any] | None = None
) -> ResolvedConfig:
    config = ZiggyConfig.model_validate(
        {
            "schema_version": 1,
            "agents": agents or {},
            "orchestrator": orchestrator or {},
        }
    )
    return ResolvedConfig(config=config, provenance={}, fingerprint="test")


def workflow_text(name: str, description: str = "Reviews the target") -> str:
    return (
        "version: 1\n"
        f"name: {name}\n"
        f'description: "{description}"\n'
        "variables:\n"
        "  target:\n"
        "    type: string\n"
        "    required: true\n"
        "  depth:\n"
        "    type: integer\n"
        "    default: 2\n"
        "steps:\n"
        "  main:\n"
        "    agent: claude\n"
        '    prompt: "Review {{ vars.target }}"\n'
    )


def write_workflow(directory: Path, name: str, text: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.yaml"
    path.write_text(text if text is not None else workflow_text(name), encoding="utf-8")
    return path


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pin(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": sha256_of(path)}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def ziggy_home_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "ziggy-home"
    home.mkdir()
    monkeypatch.setenv("ZIGGY_HOME", str(home))
    return home


class TestEligibleAgents:
    def test_happy_catalog(self, workspace: Path, ziggy_home_dir: Path) -> None:
        resolved = make_resolved(
            orchestrator={"eligible_agents": ["helper", "claude"]},
            agents={
                "claude": {"orchestration_eligible": True},
                "helper": {
                    "command": "/opt/helper",
                    "provider": "custom",
                    "orchestration_eligible": True,
                },
            },
        )
        registry = AgentRegistry.from_config(resolved)
        catalog = build_catalog(resolved, registry, workspace)
        assert [a.name for a in catalog.eligible_agents] == ["claude", "helper"]
        claude, helper = catalog.eligible_agents
        assert claude.provider == "anthropic"
        assert helper.provider == "custom"
        assert "provider=custom" in helper.capability_line
        # both are direct_tools_assumed in v0.1 -> honest advisory caveat
        assert "direct local tools assumed (advisory mediation)" in claude.capability_line
        assert "direct local tools assumed (advisory mediation)" in helper.capability_line
        assert catalog.warnings == []
        assert catalog.workflows == []

    def test_unregistered_agent_is_config_error(
        self, workspace: Path, ziggy_home_dir: Path
    ) -> None:
        resolved = make_resolved(orchestrator={"eligible_agents": ["ghost"]})
        registry = AgentRegistry.from_config(resolved)
        with pytest.raises(ConfigError) as exc:
            build_catalog(resolved, registry, workspace)
        message = str(exc.value)
        assert "ghost" in message
        assert "not registered" in message

    def test_registered_but_not_marked_eligible_is_config_error(
        self, workspace: Path, ziggy_home_dir: Path
    ) -> None:
        # codex exists as a builtin but the user never set orchestration_eligible
        resolved = make_resolved(orchestrator={"eligible_agents": ["codex"]})
        registry = AgentRegistry.from_config(resolved)
        with pytest.raises(ConfigError) as exc:
            build_catalog(resolved, registry, workspace)
        message = str(exc.value)
        assert "codex" in message
        assert "orchestration_eligible" in message

    def test_agents_sorted_and_deduplicated(self, workspace: Path, ziggy_home_dir: Path) -> None:
        resolved = make_resolved(
            orchestrator={"eligible_agents": ["helper", "claude", "helper"]},
            agents={
                "claude": {"orchestration_eligible": True},
                "helper": {"command": "/opt/helper", "orchestration_eligible": True},
            },
        )
        registry = AgentRegistry.from_config(resolved)
        catalog = build_catalog(resolved, registry, workspace)
        assert [a.name for a in catalog.eligible_agents] == ["claude", "helper"]

    def test_mediated_agent_capability_line_has_no_direct_tools_caveat(
        self, workspace: Path, ziggy_home_dir: Path
    ) -> None:
        tame = AgentConfig(
            name="tame",
            command="/opt/tame",
            provider="custom",
            orchestration_eligible=True,
            direct_tools_assumed=False,
        )
        registry = AgentRegistry({"tame": tame})
        resolved = make_resolved(orchestrator={"eligible_agents": ["tame"]})
        catalog = build_catalog(resolved, registry, workspace)
        (agent,) = catalog.eligible_agents
        assert agent.capability_line == "provider=custom"

    def test_missing_provider_reported_as_unknown(
        self, workspace: Path, ziggy_home_dir: Path
    ) -> None:
        resolved = make_resolved(
            orchestrator={"eligible_agents": ["helper"]},
            agents={"helper": {"command": "/opt/helper", "orchestration_eligible": True}},
        )
        registry = AgentRegistry.from_config(resolved)
        catalog = build_catalog(resolved, registry, workspace)
        (agent,) = catalog.eligible_agents
        assert agent.provider == "unknown"
        assert "provider=unknown" in agent.capability_line


class TestTrustedWorkflows:
    def test_happy_workflow_entry(self, workspace: Path, ziggy_home_dir: Path) -> None:
        wf_path = write_workflow(workspace / ".ziggy" / "workflows", "review")
        resolved = make_resolved(orchestrator={"trusted_workflows": [pin(wf_path)]})
        registry = AgentRegistry.from_config(resolved)
        catalog = build_catalog(resolved, registry, workspace)
        assert catalog.warnings == []
        (workflow,) = catalog.workflows
        assert workflow.name == "review"
        assert workflow.description == "Reviews the target"
        assert workflow.path == Path(os.path.realpath(wf_path))
        assert workflow.variables == {
            "depth": {"type": "integer", "required": False, "secret": False, "max_bytes": 65536},
            "target": {"type": "string", "required": True, "secret": False, "max_bytes": 65536},
        }

    def test_user_workflows_dir_is_a_valid_base(
        self, workspace: Path, ziggy_home_dir: Path
    ) -> None:
        wf_path = write_workflow(ziggy_home_dir / "workflows", "userwf")
        resolved = make_resolved(orchestrator={"trusted_workflows": [pin(wf_path)]})
        registry = AgentRegistry.from_config(resolved)
        catalog = build_catalog(resolved, registry, workspace)
        assert catalog.warnings == []
        assert [wf.name for wf in catalog.workflows] == ["userwf"]

    def test_hash_mismatch_drops_with_warning(self, workspace: Path, ziggy_home_dir: Path) -> None:
        wf_path = write_workflow(workspace / ".ziggy" / "workflows", "review")
        pinned = pin(wf_path)  # pin the pristine content...
        wf_path.write_text(
            workflow_text("review", description="Tampered after approval"), encoding="utf-8"
        )  # ...then mutate the file
        resolved = make_resolved(orchestrator={"trusted_workflows": [pinned]})
        registry = AgentRegistry.from_config(resolved)
        catalog = build_catalog(resolved, registry, workspace)  # no exception
        assert catalog.workflows == []
        (warning,) = catalog.warnings
        assert str(wf_path) in warning
        assert "hash" in warning
        assert "dropped" in warning

    def test_path_outside_bases_drops_with_warning(
        self, tmp_path: Path, workspace: Path, ziggy_home_dir: Path
    ) -> None:
        # Containment failure (resolve_contained) is a DROP, not an error.
        wf_path = write_workflow(tmp_path / "elsewhere", "review")
        resolved = make_resolved(orchestrator={"trusted_workflows": [pin(wf_path)]})
        registry = AgentRegistry.from_config(resolved)
        catalog = build_catalog(resolved, registry, workspace)
        assert catalog.workflows == []
        (warning,) = catalog.warnings
        assert str(wf_path) in warning
        assert "dropped" in warning
        assert str(workspace) in warning

    def test_missing_file_drops_with_warning(self, workspace: Path, ziggy_home_dir: Path) -> None:
        missing = workspace / "nope.yaml"
        resolved = make_resolved(
            orchestrator={"trusted_workflows": [{"path": str(missing), "sha256": "0" * 64}]}
        )
        registry = AgentRegistry.from_config(resolved)
        catalog = build_catalog(resolved, registry, workspace)
        assert catalog.workflows == []
        (warning,) = catalog.warnings
        assert "dropped" in warning
        assert "unreadable" in warning

    def test_invalid_workflow_drops_with_warning(
        self, workspace: Path, ziggy_home_dir: Path
    ) -> None:
        # Correct hash pin, but the YAML fails validation (no steps).
        bad = workspace / "bad.yaml"
        bad.write_text("version: 1\nname: bad\nsteps: {}\n", encoding="utf-8")
        resolved = make_resolved(orchestrator={"trusted_workflows": [pin(bad)]})
        registry = AgentRegistry.from_config(resolved)
        catalog = build_catalog(resolved, registry, workspace)
        assert catalog.workflows == []
        (warning,) = catalog.warnings
        assert "dropped" in warning
        assert "validation failed" in warning

    def test_hash_comparison_is_case_insensitive(
        self, workspace: Path, ziggy_home_dir: Path
    ) -> None:
        wf_path = write_workflow(workspace / ".ziggy" / "workflows", "review")
        entry = {"path": str(wf_path), "sha256": sha256_of(wf_path).upper()}
        resolved = make_resolved(orchestrator={"trusted_workflows": [entry]})
        registry = AgentRegistry.from_config(resolved)
        catalog = build_catalog(resolved, registry, workspace)
        assert catalog.warnings == []
        assert [wf.name for wf in catalog.workflows] == ["review"]

    def test_duplicate_workflow_name_second_entry_drops(
        self, workspace: Path, ziggy_home_dir: Path
    ) -> None:
        first = write_workflow(workspace / "a", "review")
        second = write_workflow(workspace / "b", "review")
        resolved = make_resolved(orchestrator={"trusted_workflows": [pin(first), pin(second)]})
        registry = AgentRegistry.from_config(resolved)
        catalog = build_catalog(resolved, registry, workspace)
        (workflow,) = catalog.workflows
        assert workflow.path == Path(os.path.realpath(first))
        (warning,) = catalog.warnings
        assert "duplicate" in warning
        assert str(second) in warning

    def test_workflows_sorted_by_name(self, workspace: Path, ziggy_home_dir: Path) -> None:
        zebra = write_workflow(workspace / ".ziggy" / "workflows", "zebra")
        alpha = write_workflow(workspace / ".ziggy" / "workflows", "alpha")
        resolved = make_resolved(orchestrator={"trusted_workflows": [pin(zebra), pin(alpha)]})
        registry = AgentRegistry.from_config(resolved)
        catalog = build_catalog(resolved, registry, workspace)
        assert [wf.name for wf in catalog.workflows] == ["alpha", "zebra"]


@pytest.fixture
def happy_catalog(workspace: Path, ziggy_home_dir: Path) -> Catalog:
    wf_path = write_workflow(workspace / ".ziggy" / "workflows", "review")
    resolved = make_resolved(
        orchestrator={
            "eligible_agents": ["claude", "helper"],
            "trusted_workflows": [pin(wf_path)],
        },
        agents={
            "claude": {"orchestration_eligible": True},
            "helper": {
                "command": "/opt/helper",
                "provider": "custom",
                "orchestration_eligible": True,
            },
        },
    )
    registry = AgentRegistry.from_config(resolved)
    return build_catalog(resolved, registry, workspace)


class TestRenderMetaPrompt:
    def test_goal_wrapped_in_delimiters(self, happy_catalog: Catalog) -> None:
        prompt = render_meta_prompt(happy_catalog, "summarize the repo", LIMITS)
        assert f"{GOAL_OPEN}\nsummarize the repo\n{GOAL_CLOSE}" in prompt

    def test_descriptions_wrapped_in_untrusted_delimiters(self, happy_catalog: Catalog) -> None:
        prompt = render_meta_prompt(happy_catalog, "goal", LIMITS)
        # two agents + one workflow, each description delimited
        assert prompt.count(DESCRIPTION_OPEN) == 3
        assert prompt.count(DESCRIPTION_CLOSE) == 3
        assert f"{DESCRIPTION_OPEN}Reviews the target{DESCRIPTION_CLOSE}" in prompt

    def test_catalog_listing_content(self, happy_catalog: Catalog) -> None:
        prompt = render_meta_prompt(happy_catalog, "goal", LIMITS)
        assert "- name: claude" in prompt
        assert "- name: helper" in prompt
        assert "provider: anthropic" in prompt
        assert "- name: review" in prompt
        assert "- target: type=string, required=true, secret=false, max_bytes=65536" in prompt
        assert "- depth: type=integer, required=false, secret=false, max_bytes=65536" in prompt

    def test_hard_limits_section(self, happy_catalog: Catalog) -> None:
        prompt = render_meta_prompt(happy_catalog, "goal", LIMITS)
        assert "at most 8 steps" in prompt
        assert "262144 bytes" in prompt
        assert "serially" in prompt
        assert "nested" in prompt
        assert "scripts" in prompt

    def test_output_contract_lists_all_three_shapes(self, happy_catalog: Catalog) -> None:
        prompt = render_meta_prompt(happy_catalog, "goal", LIMITS)
        assert '"plan_type": "single_agent"' in prompt
        assert '"plan_type": "named_workflow"' in prompt
        assert '"plan_type": "inline_agent_workflow"' in prompt
        for fragment in (
            '"rationale"',
            '"agent"',
            '"prompt"',
            '"workflow_name"',
            '"variables"',
            '"steps"',
            '"id"',
            '"inputs"',
            '"depends_on"',
        ):
            assert fragment in prompt
        assert "RAW JSON" in prompt
        assert "No prose, no markdown" in prompt
        assert "at most 2000 characters" in prompt
        assert '"steps.<id>.outputs.text"' in prompt
        assert '"goal"' in prompt

    def test_long_description_truncated(self, workspace: Path, ziggy_home_dir: Path) -> None:
        long_description = "x" * (MAX_DESCRIPTION_CHARS + 100)
        wf_path = write_workflow(
            workspace / ".ziggy" / "workflows",
            "verbose",
            workflow_text("verbose", description=long_description),
        )
        resolved = make_resolved(orchestrator={"trusted_workflows": [pin(wf_path)]})
        registry = AgentRegistry.from_config(resolved)
        catalog = build_catalog(resolved, registry, workspace)
        # the catalog itself keeps the full description; render bounds it
        assert catalog.workflows[0].description == long_description
        prompt = render_meta_prompt(catalog, "goal", LIMITS)
        assert "x" * MAX_DESCRIPTION_CHARS + TRUNCATION_MARKER in prompt
        assert "x" * (MAX_DESCRIPTION_CHARS + 1) not in prompt

    def test_deterministic_across_calls(self, happy_catalog: Catalog) -> None:
        first = render_meta_prompt(happy_catalog, "the goal", LIMITS)
        second = render_meta_prompt(happy_catalog, "the goal", LIMITS)
        assert first == second

    def test_render_sorts_by_name_regardless_of_catalog_order(self) -> None:
        shuffled = Catalog(
            eligible_agents=[
                CatalogAgent(name="zeta", provider="custom", capability_line="provider=custom"),
                CatalogAgent(name="alpha", provider="custom", capability_line="provider=custom"),
            ]
        )
        prompt = render_meta_prompt(shuffled, "goal", LIMITS)
        assert prompt.index("- name: alpha") < prompt.index("- name: zeta")

    def test_empty_catalog_renders_placeholders(self) -> None:
        prompt = render_meta_prompt(Catalog(), "goal", LIMITS)
        assert prompt.count("(none)") == 2
        assert GOAL_OPEN in prompt
