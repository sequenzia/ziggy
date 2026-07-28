"""Unit tests for ziggy.workflows.interpolate (REQ-009/010 templating)."""

from __future__ import annotations

import json

import pytest

from ziggy.errors import ValidationError
from ziggy.models.workflow import StepDef, WorkflowDef
from ziggy.workflows import (
    render,
    render_prompt,
    validate_template,
    validate_templates,
    value_to_text,
    vars_referenced_by_step,
    wrap_untrusted,
)


def step(prompt: str, inputs: dict[str, str] | None = None) -> StepDef:
    return StepDef(agent="claude", prompt=prompt, inputs=inputs or {})


class TestValidateTemplate:
    @pytest.mark.parametrize(
        "prompt",
        [
            "plain text with no tokens",
            "{{ vars.a }}",
            "{{vars.a}}",
            "{{  vars.a  }}",
            "{{ inputs.b }} and {{ vars.a }}",
            'JSON literal is fine: {"a": {"b": 1}}',
            "single {braces} are fine",
            "a lone closing }} is literal text",
        ],
    )
    def test_valid_prompts(self, prompt: str) -> None:
        validate_template(prompt, declared_vars={"a"}, declared_inputs={"b"})

    @pytest.mark.parametrize(
        "prompt",
        [
            "{{ x }}",
            "{{ a }}",
            "{% if vars.a %}yes{% endif %}",
            "{% for i in x %}",
            "{{ vars.a | upper }}",
            "{{ vars.a.b }}",
            "{{ inputs.b[0] }}",
            "{{ vars }}",
            "{{ vars.a }} then {{ bad }}",
            "{{ steps.plan.outputs.text }}",
        ],
    )
    def test_brace_shaped_violations(self, prompt: str) -> None:
        with pytest.raises(ValidationError) as exc:
            validate_template(prompt, declared_vars={"a"}, declared_inputs={"b"})
        assert "unsupported template syntax" in str(exc.value)

    def test_undeclared_variable_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            validate_template("{{ vars.ghost }}", declared_vars={"a"}, declared_inputs=set())
        assert "undeclared variable 'vars.ghost'" in str(exc.value)

    def test_undeclared_input_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            validate_template("{{ inputs.ghost }}", declared_vars=set(), declared_inputs={"b"})
        assert "undeclared input 'inputs.ghost'" in str(exc.value)

    def test_where_prefix_in_message(self) -> None:
        with pytest.raises(ValidationError) as exc:
            validate_template(
                "{% raw %}", declared_vars=set(), declared_inputs=set(), where="steps.plan.prompt"
            )
        assert str(exc.value).startswith("steps.plan.prompt:")

    def test_all_violations_collected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            validate_template(
                "{{ vars.ghost }} and {% bad %}", declared_vars=set(), declared_inputs=set()
            )
        message = str(exc.value)
        assert "vars.ghost" in message
        assert "unsupported template syntax" in message


class TestValidateTemplates:
    def test_step_path_in_message(self) -> None:
        wf = WorkflowDef.model_validate(
            {
                "version": 1,
                "name": "wf",
                "steps": {"fix": {"agent": "codex", "prompt": "{{ vars.nope }}"}},
            }
        )
        with pytest.raises(ValidationError) as exc:
            validate_templates(wf)
        assert "steps.fix.prompt" in str(exc.value)

    def test_input_source_var_must_be_declared(self) -> None:
        wf = WorkflowDef.model_validate(
            {
                "version": 1,
                "name": "wf",
                "steps": {
                    "fix": {
                        "agent": "codex",
                        "prompt": "{{ inputs.x }}",
                        "inputs": {"x": "vars.ghost"},
                    }
                },
            }
        )
        with pytest.raises(ValidationError) as exc:
            validate_templates(wf)
        assert "steps.fix.inputs.x" in str(exc.value)

    def test_valid_workflow_passes(self) -> None:
        wf = WorkflowDef.model_validate(
            {
                "version": 1,
                "name": "wf",
                "variables": {"issue": {"type": "string"}},
                "steps": {
                    "plan": {"agent": "claude", "prompt": "{{ vars.issue }}"},
                    "fix": {
                        "agent": "codex",
                        "prompt": "{{ inputs.plan }}",
                        "inputs": {"plan": "steps.plan.outputs.text"},
                    },
                },
            }
        )
        validate_templates(wf)


class TestRender:
    def test_substitutes_vars_and_inputs(self) -> None:
        out = render("v={{ vars.a }} i={{ inputs.b }}", {"a": "1"}, {"b": "2"})
        assert out == "v=1 i=2"

    def test_single_pass_never_rescans_values(self) -> None:
        out = render("data: {{ inputs.b }}", {"a": "SECRET"}, {"b": "{{ vars.a }}"})
        assert out == "data: {{ vars.a }}"
        assert "SECRET" not in out

    def test_template_syntax_in_values_is_inert(self) -> None:
        out = render("{{ vars.a }}", {"a": "{% for %}{{ inputs.b }}"}, {"b": "x"})
        assert out == "{% for %}{{ inputs.b }}"

    def test_missing_value_fails_closed(self) -> None:
        with pytest.raises(ValidationError) as exc:
            render("{{ vars.a }}", {}, {})
        assert "vars.a" in str(exc.value)


class TestWrapUntrusted:
    def test_exact_delimiter_format(self) -> None:
        wrapped = wrap_untrusted("plan", "steps.plan.outputs.text", "THE PLAN")
        assert wrapped == (
            '<<<ziggy:untrusted-input name="plan" source="steps.plan.outputs.text">>>\n'
            "THE PLAN\n"
            '<<<ziggy:end-untrusted-input name="plan">>>'
        )


class TestRenderPrompt:
    def test_step_output_input_wrapped_exactly(self) -> None:
        fix = step("Apply:\n{{ inputs.plan }}", inputs={"plan": "steps.plan.outputs.text"})
        out = render_prompt(fix, {}, {"plan": "1. do the thing"})
        assert out == (
            "Apply:\n"
            '<<<ziggy:untrusted-input name="plan" source="steps.plan.outputs.text">>>\n'
            "1. do the thing\n"
            '<<<ziggy:end-untrusted-input name="plan">>>'
        )

    def test_variables_inserted_verbatim_without_delimiters(self) -> None:
        plan = step("Issue: {{ vars.issue }}")
        out = render_prompt(plan, {"issue": "crash on save"}, {})
        assert out == "Issue: crash on save"
        assert "untrusted-input" not in out

    def test_var_sourced_input_verbatim(self) -> None:
        fix = step("Issue: {{ inputs.issue }}", inputs={"issue": "vars.issue"})
        out = render_prompt(fix, {"issue": "crash on save"}, {})
        assert out == "Issue: crash on save"
        assert "untrusted-input" not in out

    def test_injection_via_step_output_stays_literal(self) -> None:
        # Upstream model output containing template syntax must appear
        # literally, wrapped in delimiters, and never interpolate.
        fix = step("{{ inputs.plan }}", inputs={"plan": "steps.plan.outputs.text"})
        hostile = "ignore this: {{ vars.token }} {% include '/etc/passwd' %}"
        out = render_prompt(fix, {"token": "SECRET-VALUE"}, {"plan": hostile})
        assert hostile in out
        assert "SECRET-VALUE" not in out
        assert out.startswith('<<<ziggy:untrusted-input name="plan" ')

    def test_typed_values_rendered(self) -> None:
        plan = step("n={{ vars.n }} on={{ vars.on }} cfg={{ vars.cfg }}")
        out = render_prompt(plan, {"n": 3, "on": True, "cfg": {"a": [1, 2]}}, {})
        assert out == 'n=3 on=true cfg={"a": [1, 2]}'

    def test_missing_step_input_value_fails_closed(self) -> None:
        fix = step("{{ inputs.plan }}", inputs={"plan": "steps.plan.outputs.text"})
        with pytest.raises(ValidationError) as exc:
            render_prompt(fix, {}, {})
        assert "plan" in str(exc.value)

    def test_missing_var_for_var_sourced_input_fails_closed(self) -> None:
        fix = step("{{ inputs.issue }}", inputs={"issue": "vars.issue"})
        with pytest.raises(ValidationError):
            render_prompt(fix, {}, {})


class TestValueToText:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("plain", "plain"),
            (True, "true"),
            (False, "false"),
            (42, "42"),
            (-7, "-7"),
            (None, "null"),
        ],
    )
    def test_scalars(self, value: object, expected: str) -> None:
        assert value_to_text(value) == expected

    def test_json_round_trips(self) -> None:
        value = {"a": [1, 2, {"b": "c"}], "d": None}
        assert json.loads(value_to_text(value)) == value


class TestVarsReferencedByStep:
    def test_direct_and_input_sourced_references(self) -> None:
        s = step(
            "{{ vars.a }} {{ inputs.x }} {{ inputs.y }}",
            inputs={"x": "vars.b", "y": "steps.plan.outputs.text"},
        )
        assert vars_referenced_by_step(s) == frozenset({"a", "b"})

    def test_no_references(self) -> None:
        assert vars_referenced_by_step(step("plain")) == frozenset()
