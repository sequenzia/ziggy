"""Unit tests for ziggy.workflows.egress (REQ-011 cross-provider egress)."""

from __future__ import annotations

import pytest

from ziggy.agents import AgentRegistry
from ziggy.config import ResolvedConfig
from ziggy.config.schema import EgressConfig, ZiggyConfig
from ziggy.errors import EgressNotAcknowledgedError
from ziggy.models.agent import AgentConfig
from ziggy.models.workflow import StepDef, WorkflowDef
from ziggy.workflows.egress import (
    ACK_BY_CONFIG,
    ACK_BY_FLAG,
    build_egress_records,
    gate_egress,
    is_acknowledged,
    required_provider_set,
    step_provider,
)

# ---------------------------------------------------------------------------
# fixtures / builders


def make_registry(**providers: str | None) -> AgentRegistry:
    return AgentRegistry(
        {
            name: AgentConfig(name=name, command="/usr/bin/true", provider=provider)
            for name, provider in providers.items()
        }
    )


def make_workflow(steps: dict[str, StepDef]) -> WorkflowDef:
    return WorkflowDef(version=1, name="wf", steps=steps)


def step(
    agent: str,
    inputs: dict[str, str] | None = None,
    depends_on: list[str] | None = None,
) -> StepDef:
    return StepDef(
        agent=agent,
        prompt="go",
        inputs=inputs or {},
        depends_on=depends_on or [],
    )


def resolved_with_sets(acknowledged: list[list[str]]) -> ResolvedConfig:
    config = ZiggyConfig(
        schema_version=1,
        egress=EgressConfig(acknowledged_provider_sets=acknowledged),
    )
    return ResolvedConfig(config=config, provenance={}, fingerprint="test")


CROSSING = frozenset({"anthropic", "openai"})


def cross_provider_workflow() -> tuple[WorkflowDef, AgentRegistry]:
    """plan (claude/anthropic) -> fix (codex/openai) via inputs."""
    wf = make_workflow(
        {
            "plan": step("claude"),
            "fix": step("codex", inputs={"plan": "steps.plan.outputs.text"}),
        }
    )
    return wf, make_registry(claude="anthropic", codex="openai")


# ---------------------------------------------------------------------------
# step_provider


def test_step_provider_uses_declared_provider() -> None:
    cfg = AgentConfig(name="claude", command="/usr/bin/true", provider="anthropic")
    assert step_provider(cfg) == "anthropic"


def test_step_provider_custom_fallback_when_unset() -> None:
    cfg = AgentConfig(name="mytool", command="/usr/bin/true")
    assert step_provider(cfg) == "custom:mytool"


def test_step_provider_custom_fallback_when_empty_string() -> None:
    cfg = AgentConfig(name="mytool", command="/usr/bin/true", provider="")
    assert step_provider(cfg) == "custom:mytool"


# ---------------------------------------------------------------------------
# required_provider_set


def test_single_provider_workflow_has_no_crossing() -> None:
    wf = make_workflow(
        {
            "a": step("claude"),
            "b": step("claude2", inputs={"x": "steps.a.outputs.text"}),
        }
    )
    registry = make_registry(claude="anthropic", claude2="anthropic")
    assert required_provider_set(wf, registry) == frozenset()


def test_crossing_via_inputs_across_providers() -> None:
    wf, registry = cross_provider_workflow()
    assert required_provider_set(wf, registry) == CROSSING


def test_depends_on_only_never_crosses() -> None:
    wf = make_workflow(
        {
            "a": step("claude"),
            "b": step("codex", depends_on=["a"]),
        }
    )
    registry = make_registry(claude="anthropic", codex="openai")
    assert required_provider_set(wf, registry) == frozenset()


def test_vars_inputs_never_cross() -> None:
    wf = make_workflow(
        {
            "a": step("claude"),
            "b": step("codex", inputs={"issue": "vars.issue"}, depends_on=["a"]),
        }
    )
    registry = make_registry(claude="anthropic", codex="openai")
    assert required_provider_set(wf, registry) == frozenset()


def test_crossing_with_custom_fallback_provider_names() -> None:
    wf = make_workflow(
        {
            "a": step("mytool"),
            "b": step("claude", inputs={"x": "steps.a.outputs.text"}),
        }
    )
    registry = make_registry(mytool=None, claude="anthropic")
    assert required_provider_set(wf, registry) == frozenset({"custom:mytool", "anthropic"})


def test_two_unlabelled_agents_are_distinct_providers() -> None:
    wf = make_workflow(
        {
            "a": step("tool-a"),
            "b": step("tool-b", inputs={"x": "steps.a.outputs.text"}),
        }
    )
    registry = make_registry(**{"tool-a": None, "tool-b": None})
    assert required_provider_set(wf, registry) == frozenset({"custom:tool-a", "custom:tool-b"})


def test_provider_set_covers_only_steps_on_crossing_edges() -> None:
    """A third provider not on any cross-provider data edge stays out."""
    wf = make_workflow(
        {
            "a": step("claude"),
            "b": step("codex", inputs={"x": "steps.a.outputs.text"}),
            "c": step("other", depends_on=["b"]),
        }
    )
    registry = make_registry(claude="anthropic", codex="openai", other="google")
    assert required_provider_set(wf, registry) == CROSSING


# ---------------------------------------------------------------------------
# is_acknowledged


def test_acknowledged_via_config_exact_set() -> None:
    resolved = resolved_with_sets([["anthropic", "openai"]])
    assert is_acknowledged(CROSSING, resolved, None) == ACK_BY_CONFIG


def test_acknowledged_via_config_is_order_free() -> None:
    resolved = resolved_with_sets([["openai", "anthropic"]])
    assert is_acknowledged(CROSSING, resolved, None) == ACK_BY_CONFIG


def test_acknowledged_via_config_is_duplicate_free() -> None:
    resolved = resolved_with_sets([["openai", "anthropic", "openai"]])
    assert is_acknowledged(CROSSING, resolved, None) == ACK_BY_CONFIG


def test_config_subset_does_not_acknowledge() -> None:
    resolved = resolved_with_sets([["anthropic"]])
    assert is_acknowledged(CROSSING, resolved, None) is None


def test_config_superset_does_not_acknowledge() -> None:
    resolved = resolved_with_sets([["anthropic", "openai", "google"]])
    assert is_acknowledged(CROSSING, resolved, None) is None


def test_acknowledged_via_flag_exact_set() -> None:
    resolved = resolved_with_sets([])
    assert is_acknowledged(CROSSING, resolved, ["anthropic", "openai"]) == ACK_BY_FLAG


def test_flag_is_order_and_duplicate_free() -> None:
    resolved = resolved_with_sets([])
    assert is_acknowledged(CROSSING, resolved, ["openai", "anthropic", "openai"]) == ACK_BY_FLAG


def test_flag_subset_does_not_acknowledge() -> None:
    resolved = resolved_with_sets([])
    assert is_acknowledged(CROSSING, resolved, ["openai"]) is None


def test_flag_superset_does_not_acknowledge() -> None:
    resolved = resolved_with_sets([])
    assert is_acknowledged(CROSSING, resolved, ["anthropic", "openai", "google"]) is None


def test_flag_wins_when_both_config_and_flag_match() -> None:
    resolved = resolved_with_sets([["anthropic", "openai"]])
    assert is_acknowledged(CROSSING, resolved, ["openai", "anthropic"]) == ACK_BY_FLAG


def test_config_still_acknowledges_when_flag_is_wrong() -> None:
    resolved = resolved_with_sets([["anthropic", "openai"]])
    assert is_acknowledged(CROSSING, resolved, ["openai"]) == ACK_BY_CONFIG


def test_empty_provider_set_needs_no_acknowledgement() -> None:
    resolved = resolved_with_sets([["anthropic", "openai"]])
    assert is_acknowledged(frozenset(), resolved, ["anthropic", "openai"]) is None


# ---------------------------------------------------------------------------
# gate_egress


def test_gate_passes_when_no_crossing() -> None:
    gate_egress(frozenset(), None)  # must not raise


def test_gate_passes_when_acknowledged() -> None:
    gate_egress(CROSSING, ACK_BY_CONFIG)  # must not raise
    gate_egress(CROSSING, ACK_BY_FLAG)  # must not raise


def test_gate_raises_with_sorted_set_and_rerun_hint() -> None:
    with pytest.raises(EgressNotAcknowledgedError) as excinfo:
        gate_egress(frozenset({"openai", "anthropic"}), None)
    err = excinfo.value
    assert err.exit_code == 2
    assert "anthropic, openai" in err.message  # sorted order in the message
    assert "--acknowledge-egress anthropic,openai" in err.message  # rerun hint
    assert err.details["provider_set"] == ["anthropic", "openai"]
    assert "--acknowledge-egress" in err.details["hint"]


# ---------------------------------------------------------------------------
# build_egress_records


def test_records_only_for_steps_receiving_upstream_outputs() -> None:
    wf, registry = cross_provider_workflow()
    records = build_egress_records(wf, registry, ACK_BY_FLAG)
    assert [r.step_id for r in records] == ["fix"]
    record = records[0]
    assert record.provider == "openai"
    assert record.input_sources == ["steps.plan.outputs.text"]
    assert record.acknowledged_by == ACK_BY_FLAG


def test_single_provider_records_lineage_with_no_acknowledgement() -> None:
    wf = make_workflow(
        {
            "a": step("claude"),
            "b": step("claude2", inputs={"x": "steps.a.outputs.text"}),
        }
    )
    registry = make_registry(claude="anthropic", claude2="anthropic")
    records = build_egress_records(wf, registry, None)
    assert [r.step_id for r in records] == ["b"]
    assert records[0].provider == "anthropic"
    assert records[0].input_sources == ["steps.a.outputs.text"]
    assert records[0].acknowledged_by is None
    # and the crossing gate is not triggered for this workflow
    gate_egress(required_provider_set(wf, registry), None)


def test_multi_input_step_lineage_in_declaration_order() -> None:
    wf = make_workflow(
        {
            "a": step("claude"),
            "b": step("codex"),
            "merge": step(
                "claude",
                inputs={
                    "review": "steps.b.outputs.text",
                    "plan": "steps.a.outputs.text",
                    "issue": "vars.issue",
                },
            ),
        }
    )
    registry = make_registry(claude="anthropic", codex="openai")
    records = build_egress_records(wf, registry, ACK_BY_CONFIG)
    assert [r.step_id for r in records] == ["merge"]
    record = records[0]
    assert record.provider == "anthropic"
    # step-output sources only, in inputs declaration order; vars.* excluded
    assert record.input_sources == ["steps.b.outputs.text", "steps.a.outputs.text"]
    assert record.acknowledged_by == ACK_BY_CONFIG


def test_same_provider_receiver_not_stamped_in_crossing_workflow() -> None:
    wf = make_workflow(
        {
            "a": step("claude"),
            "b": step("claude2", inputs={"x": "steps.a.outputs.text"}),
            "c": step("codex", inputs={"y": "steps.b.outputs.text"}),
        }
    )
    registry = make_registry(claude="anthropic", claude2="anthropic", codex="openai")
    records = build_egress_records(wf, registry, ACK_BY_FLAG)
    by_id = {r.step_id: r for r in records}
    assert set(by_id) == {"b", "c"}
    assert by_id["b"].acknowledged_by is None  # anthropic -> anthropic: no crossing
    assert by_id["c"].acknowledged_by == ACK_BY_FLAG  # anthropic -> openai: crossing


def test_steps_with_only_vars_or_ordering_edges_have_no_record() -> None:
    wf = make_workflow(
        {
            "a": step("claude"),
            "b": step("codex", inputs={"issue": "vars.issue"}, depends_on=["a"]),
        }
    )
    registry = make_registry(claude="anthropic", codex="openai")
    assert build_egress_records(wf, registry, None) == []
