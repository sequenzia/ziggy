"""Unit tests for ziggy.workflows.scheduler (REQ-010 serial DAG scheduling)
plus the pure parts of the workflow runner (status aggregation).

End-to-end workflow execution belongs to the integration stage; everything
here is pure logic — no subprocesses, no filesystem, no event pipeline.
"""

from __future__ import annotations

import pytest

from ziggy.errors import ValidationError
from ziggy.models.common import RunStatus, StepStatus
from ziggy.models.workflow import WorkflowDef
from ziggy.workflows.runner import aggregate_status
from ziggy.workflows.scheduler import (
    WorkflowDeadline,
    input_source_step,
    propagate_failure,
    step_edges,
    topo_order,
)


def make_workflow(steps: dict[str, dict], variables: dict | None = None) -> WorkflowDef:
    """Workflow with defaulted agent/prompt; declaration order = dict order."""
    data: dict = {
        "version": 1,
        "name": "wf",
        "steps": {
            step_id: {"agent": "claude", "prompt": "p", **body} for step_id, body in steps.items()
        },
    }
    if variables is not None:
        data["variables"] = variables
    return WorkflowDef.model_validate(data)


def output_of(step_id: str) -> str:
    return f"steps.{step_id}.outputs.text"


class TestInputSourceStep:
    def test_step_output_source(self) -> None:
        assert input_source_step("steps.plan.outputs.text") == "plan"

    def test_vars_source_is_none(self) -> None:
        assert input_source_step("vars.issue") is None

    def test_malformed_source_is_none(self) -> None:
        assert input_source_step("steps.plan.text") is None
        assert input_source_step("steps.plan.outputs.text.extra") is None


class TestStepEdges:
    def test_depends_on_edges(self) -> None:
        wf = make_workflow({"a": {}, "b": {"depends_on": ["a"]}})
        assert step_edges(wf) == {"a": (), "b": ("a",)}

    def test_inputs_create_edges(self) -> None:
        wf = make_workflow({"a": {}, "b": {"inputs": {"x": output_of("a")}}})
        assert step_edges(wf) == {"a": (), "b": ("a",)}

    def test_union_of_depends_on_and_inputs_dedupes(self) -> None:
        wf = make_workflow(
            {
                "a": {},
                "c": {},
                "b": {"depends_on": ["a", "c"], "inputs": {"x": output_of("a")}},
            }
        )
        assert step_edges(wf)["b"] == ("a", "c")

    def test_vars_inputs_are_not_edges(self) -> None:
        wf = make_workflow(
            {"a": {}, "b": {"inputs": {"x": "vars.issue"}}},
            variables={"issue": {"type": "string"}},
        )
        assert step_edges(wf) == {"a": (), "b": ()}

    def test_unknown_depends_on_ref(self) -> None:
        wf = make_workflow({"a": {}, "b": {"depends_on": ["ghost"]}})
        with pytest.raises(ValidationError) as excinfo:
            step_edges(wf)
        assert "steps.b.depends_on" in str(excinfo.value)
        assert "unknown step 'ghost'" in str(excinfo.value)

    def test_unknown_inputs_ref(self) -> None:
        wf = make_workflow({"a": {}, "b": {"inputs": {"x": output_of("ghost")}}})
        with pytest.raises(ValidationError) as excinfo:
            step_edges(wf)
        assert "steps.b.inputs.x" in str(excinfo.value)
        assert "unknown step 'ghost'" in str(excinfo.value)

    def test_self_dependency_via_depends_on(self) -> None:
        wf = make_workflow({"a": {"depends_on": ["a"]}})
        with pytest.raises(ValidationError) as excinfo:
            step_edges(wf)
        assert "steps.a.depends_on" in str(excinfo.value)
        assert "itself" in str(excinfo.value)

    def test_self_dependency_via_inputs(self) -> None:
        wf = make_workflow({"a": {"inputs": {"x": output_of("a")}}})
        with pytest.raises(ValidationError) as excinfo:
            step_edges(wf)
        assert "steps.a.inputs.x" in str(excinfo.value)

    def test_all_problems_collected_in_one_error(self) -> None:
        wf = make_workflow(
            {
                "a": {"depends_on": ["ghost"]},
                "b": {"inputs": {"x": output_of("phantom")}, "depends_on": ["b"]},
            }
        )
        with pytest.raises(ValidationError) as excinfo:
            step_edges(wf)
        message = str(excinfo.value)
        assert "ghost" in message
        assert "phantom" in message
        assert "steps.b.depends_on" in message


class TestTopoOrder:
    def test_no_deps_is_declaration_order(self) -> None:
        wf = make_workflow({"c": {}, "a": {}, "b": {}})
        assert topo_order(wf) == ["c", "a", "b"]

    def test_diamond(self) -> None:
        wf = make_workflow(
            {
                "a": {},
                "b": {"depends_on": ["a"]},
                "c": {"depends_on": ["a"]},
                "d": {"depends_on": ["b", "c"]},
            }
        )
        assert topo_order(wf) == ["a", "b", "c", "d"]

    def test_declaration_order_breaks_ties(self) -> None:
        # c and b are both ready after a; c is declared first and must win.
        wf = make_workflow(
            {
                "a": {},
                "c": {"depends_on": ["a"]},
                "b": {"depends_on": ["a"]},
                "d": {"depends_on": ["b", "c"]},
            }
        )
        assert topo_order(wf) == ["a", "c", "b", "d"]

    def test_later_declared_root_still_runs_first_when_depended_on(self) -> None:
        # b is declared first but depends on z, declared last.
        wf = make_workflow({"b": {"depends_on": ["z"]}, "z": {}})
        assert topo_order(wf) == ["z", "b"]

    def test_inputs_only_edges_order(self) -> None:
        wf = make_workflow(
            {
                "fix": {"inputs": {"plan": output_of("plan")}},
                "plan": {},
                "verify": {"depends_on": ["fix"]},
            }
        )
        assert topo_order(wf) == ["plan", "fix", "verify"]

    def test_stable_across_repeated_calls(self) -> None:
        wf = make_workflow(
            {
                "a": {},
                "e": {"depends_on": ["a"]},
                "b": {"inputs": {"x": output_of("a")}},
                "d": {"depends_on": ["e", "b"]},
                "c": {},
            }
        )
        first = topo_order(wf)
        for _ in range(10):
            assert topo_order(wf) == first

    def test_order_is_topologically_valid_on_wider_dag(self) -> None:
        wf = make_workflow(
            {
                "s1": {},
                "s2": {"depends_on": ["s1"]},
                "s3": {"inputs": {"x": output_of("s1")}},
                "s4": {"depends_on": ["s2"], "inputs": {"y": output_of("s3")}},
                "s5": {},
                "s6": {"depends_on": ["s5", "s4"]},
                "s7": {"inputs": {"a": output_of("s6"), "b": output_of("s2")}},
                "s8": {"depends_on": ["s7"]},
            }
        )
        order = topo_order(wf)
        assert sorted(order) == sorted(wf.steps)
        edges = step_edges(wf)
        for step_id, deps in edges.items():
            for dep in deps:
                assert order.index(dep) < order.index(step_id)

    def test_unknown_agent_is_not_the_schedulers_concern(self) -> None:
        # Agent existence is validated by the schema/registry layer; the
        # scheduler orders by step ids only.
        wf = make_workflow({"a": {"agent": "not-a-registered-agent"}, "b": {"depends_on": ["a"]}})
        assert topo_order(wf) == ["a", "b"]

    def test_two_cycle_detected(self) -> None:
        wf = make_workflow({"a": {"depends_on": ["b"]}, "b": {"depends_on": ["a"]}})
        with pytest.raises(ValidationError) as excinfo:
            topo_order(wf)
        assert "cycle" in str(excinfo.value)

    def test_self_loop_rejected(self) -> None:
        wf = make_workflow({"a": {"depends_on": ["a"]}})
        with pytest.raises(ValidationError):
            topo_order(wf)

    def test_mixed_edge_cycle_detected(self) -> None:
        # a -> b via inputs, b -> a via depends_on.
        wf = make_workflow(
            {
                "a": {"inputs": {"x": output_of("b")}},
                "b": {"depends_on": ["a"]},
            }
        )
        with pytest.raises(ValidationError) as excinfo:
            topo_order(wf)
        assert "cycle" in str(excinfo.value)

    def test_three_cycle_detected(self) -> None:
        wf = make_workflow(
            {
                "a": {"depends_on": ["c"]},
                "b": {"inputs": {"x": output_of("a")}},
                "c": {"depends_on": ["b"]},
            }
        )
        with pytest.raises(ValidationError):
            topo_order(wf)

    def test_cycle_error_names_only_cycle_members(self) -> None:
        wf = make_workflow(
            {
                "alpha": {},
                "beta": {"depends_on": ["gamma"]},
                "gamma": {"depends_on": ["beta"]},
                "delta": {"depends_on": ["gamma"]},
            }
        )
        with pytest.raises(ValidationError) as excinfo:
            topo_order(wf)
        message = str(excinfo.value)
        assert "beta" in message
        assert "gamma" in message
        assert "delta" not in message
        assert "alpha" not in message
        assert excinfo.value.details["cycle"] == ["beta", "gamma"]


class TestPropagateFailure:
    def four_step(self) -> WorkflowDef:
        # a -> b -> d, with c independent (spec §10.2 critical path shape).
        return make_workflow(
            {
                "a": {},
                "b": {"depends_on": ["a"]},
                "c": {},
                "d": {"inputs": {"x": output_of("b")}},
            }
        )

    def test_dependents_blocked_others_skipped(self) -> None:
        wf = self.four_step()
        order = topo_order(wf)
        assert order == ["a", "b", "c", "d"]
        statuses = propagate_failure(order, step_edges(wf), "b")
        assert statuses == {"c": StepStatus.SKIPPED, "d": StepStatus.BLOCKED}

    def test_completed_steps_are_not_in_the_result(self) -> None:
        wf = self.four_step()
        statuses = propagate_failure(topo_order(wf), step_edges(wf), "b")
        assert "a" not in statuses
        assert "b" not in statuses

    def test_transitive_dependents_blocked(self) -> None:
        wf = make_workflow(
            {
                "a": {},
                "b": {"depends_on": ["a"]},
                "c": {"inputs": {"x": output_of("b")}},
                "d": {"depends_on": ["c"]},
                "e": {},
            }
        )
        statuses = propagate_failure(topo_order(wf), step_edges(wf), "b")
        assert statuses == {
            "c": StepStatus.BLOCKED,
            "d": StepStatus.BLOCKED,
            "e": StepStatus.SKIPPED,
        }

    def test_diamond_join_is_blocked_even_with_a_healthy_side(self) -> None:
        wf = make_workflow(
            {
                "a": {},
                "b": {"depends_on": ["a"]},
                "c": {"depends_on": ["a"]},
                "d": {"depends_on": ["b", "c"]},
            }
        )
        statuses = propagate_failure(topo_order(wf), step_edges(wf), "b")
        assert statuses == {"c": StepStatus.SKIPPED, "d": StepStatus.BLOCKED}

    def test_failure_of_last_step_propagates_nothing(self) -> None:
        wf = self.four_step()
        assert propagate_failure(topo_order(wf), step_edges(wf), "d") == {}

    def test_failure_of_first_step_blocks_whole_dependent_chain(self) -> None:
        wf = self.four_step()
        statuses = propagate_failure(topo_order(wf), step_edges(wf), "a")
        assert statuses == {
            "b": StepStatus.BLOCKED,
            "c": StepStatus.SKIPPED,
            "d": StepStatus.BLOCKED,
        }

    def test_unknown_failed_step_is_a_caller_bug(self) -> None:
        wf = self.four_step()
        with pytest.raises(ValueError):
            propagate_failure(topo_order(wf), step_edges(wf), "ghost")


class TestWorkflowDeadline:
    def test_bookkeeping_with_injected_clock(self) -> None:
        now = [100.0]
        deadline = WorkflowDeadline(timeout_seconds=60.0, clock=lambda: now[0])
        assert deadline.elapsed_seconds() == 0.0
        assert deadline.remaining_seconds() == 60.0
        assert not deadline.exceeded()

        now[0] = 130.0
        assert deadline.elapsed_seconds() == 30.0
        assert deadline.remaining_seconds() == 30.0
        assert not deadline.exceeded()

        now[0] = 160.0
        assert deadline.remaining_seconds() == 0.0
        assert deadline.exceeded()

        now[0] = 200.0  # past the deadline: remaining never goes negative
        assert deadline.remaining_seconds() == 0.0
        assert deadline.exceeded()

    def test_clamp_bounds_step_timeout_by_remaining(self) -> None:
        now = [0.0]
        deadline = WorkflowDeadline(timeout_seconds=100.0, clock=lambda: now[0])
        assert deadline.clamp(30.0) == 30.0  # plenty of budget: step value wins
        now[0] = 90.0
        assert deadline.clamp(30.0) == 10.0  # deadline closer than step timeout
        now[0] = 150.0
        assert deadline.clamp(30.0) == 0.0  # never negative

    def test_default_clock_is_monotonic(self) -> None:
        deadline = WorkflowDeadline(timeout_seconds=3600.0)
        assert deadline.elapsed_seconds() >= 0.0
        assert not deadline.exceeded()


class TestAggregateStatus:
    def test_all_success(self) -> None:
        assert aggregate_status([StepStatus.SUCCESS, StepStatus.SUCCESS]) is RunStatus.SUCCESS

    def test_any_success_with_failure_is_partial(self) -> None:
        assert aggregate_status([StepStatus.SUCCESS, StepStatus.FAILED]) is RunStatus.PARTIAL

    def test_success_with_blocked_and_skipped_is_partial(self) -> None:
        statuses = [StepStatus.SUCCESS, StepStatus.FAILED, StepStatus.BLOCKED, StepStatus.SKIPPED]
        assert aggregate_status(statuses) is RunStatus.PARTIAL

    def test_no_success_is_failed(self) -> None:
        statuses = [StepStatus.FAILED, StepStatus.BLOCKED, StepStatus.SKIPPED]
        assert aggregate_status(statuses) is RunStatus.FAILED

    def test_all_skipped_is_failed(self) -> None:
        # e.g. the lease could not be acquired: nothing ran, nothing succeeded.
        assert aggregate_status([StepStatus.SKIPPED, StepStatus.SKIPPED]) is RunStatus.FAILED

    def test_cancelled_flag_overrides_success(self) -> None:
        statuses = [StepStatus.SUCCESS, StepStatus.SKIPPED]
        assert aggregate_status(statuses, cancelled=True) is RunStatus.CANCELLED

    def test_cancelled_step_overrides_without_flag(self) -> None:
        statuses = [StepStatus.SUCCESS, StepStatus.CANCELLED, StepStatus.SKIPPED]
        assert aggregate_status(statuses) is RunStatus.CANCELLED
