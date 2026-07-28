"""Unit tests for ziggy.workflows.vars (REQ-009/011 typed vars + secret gate),
plus config loader coverage for ``workflows.secret_variable_allowances``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ziggy.config import MergeRule, load_config, merge_rule_for
from ziggy.errors import ConfigError, ValidationError
from ziggy.models.common import ConfigSource
from ziggy.models.workflow import WorkflowDef
from ziggy.redact import Redactor
from ziggy.workflows import (
    check_secret_allowances,
    parse_var_args,
    render_prompt,
    secret_redaction_values,
    validate_variables,
)


def make_wf(
    variables: dict[str, dict[str, Any]] | None = None,
    steps: dict[str, dict[str, Any]] | None = None,
) -> WorkflowDef:
    return WorkflowDef.model_validate(
        {
            "version": 1,
            "name": "wf",
            "variables": variables or {},
            "steps": steps or {"plan": {"agent": "claude", "prompt": "hi"}},
        }
    )


class TestParseVarArgs:
    def test_basic_and_value_with_equals(self) -> None:
        assert parse_var_args(["a=1", "b=x=y", "c="]) == {"a": "1", "b": "x=y", "c": ""}

    def test_missing_equals_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            parse_var_args(["novalue"])
        assert "<name>=<value>" in str(exc.value)

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            parse_var_args(["=v"])

    def test_duplicate_name_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            parse_var_args(["a=1", "a=2"])
        assert "more than once" in str(exc.value)


class TestTypedParsing:
    def test_string_verbatim(self) -> None:
        wf = make_wf({"s": {"type": "string"}})
        assert validate_variables(wf, {"s": "  spaced {{ text }} "}) == {
            "s": "  spaced {{ text }} "
        }

    @pytest.mark.parametrize(("raw", "expected"), [("42", 42), ("+7", 7), ("-3", -3), ("0", 0)])
    def test_integer_strict_ok(self, raw: str, expected: int) -> None:
        wf = make_wf({"n": {"type": "integer"}})
        result = validate_variables(wf, {"n": raw})
        assert result == {"n": expected}
        assert isinstance(result["n"], int)

    @pytest.mark.parametrize("raw", ["4.5", "1_0", "0x10", "five", "", " 5"])
    def test_integer_strict_rejects(self, raw: str) -> None:
        wf = make_wf({"n": {"type": "integer"}})
        with pytest.raises(ValidationError) as exc:
            validate_variables(wf, {"n": raw})
        assert "variables.n" in str(exc.value)

    @pytest.mark.parametrize(("raw", "expected"), [("true", True), ("false", False)])
    def test_boolean_strict_ok(self, raw: str, expected: bool) -> None:
        wf = make_wf({"b": {"type": "boolean"}})
        assert validate_variables(wf, {"b": raw}) == {"b": expected}

    @pytest.mark.parametrize("raw", ["True", "FALSE", "1", "0", "yes", "no", ""])
    def test_boolean_strict_rejects(self, raw: str) -> None:
        wf = make_wf({"b": {"type": "boolean"}})
        with pytest.raises(ValidationError) as exc:
            validate_variables(wf, {"b": raw})
        assert "'true' or 'false'" in str(exc.value)

    def test_json_round_trip(self) -> None:
        wf = make_wf({"cfg": {"type": "json"}})
        raw = '{"a": [1, 2, {"b": "c"}], "d": null}'
        values = validate_variables(wf, {"cfg": raw})
        assert values == {"cfg": json.loads(raw)}
        # And a rendered prompt round-trips back to the same structure.
        step = make_wf(
            {"cfg": {"type": "json"}},
            {"plan": {"agent": "claude", "prompt": "{{ vars.cfg }}"}},
        ).steps["plan"]
        assert json.loads(render_prompt(step, values, {})) == json.loads(raw)

    def test_json_invalid_rejected(self) -> None:
        wf = make_wf({"cfg": {"type": "json"}})
        with pytest.raises(ValidationError) as exc:
            validate_variables(wf, {"cfg": "{not json"})
        assert "invalid JSON" in str(exc.value)


class TestPresenceRules:
    def test_unknown_var_rejected(self) -> None:
        wf = make_wf({"a": {"type": "string"}})
        with pytest.raises(ValidationError) as exc:
            validate_variables(wf, {"nope": "x"})
        message = str(exc.value)
        assert "nope" in message
        assert "unknown variable" in message

    def test_missing_required_rejected(self) -> None:
        wf = make_wf({"issue": {"type": "string", "required": True}})
        with pytest.raises(ValidationError) as exc:
            validate_variables(wf, {})
        assert "variables.issue" in str(exc.value)
        assert "required" in str(exc.value)

    def test_defaults_applied_typed(self) -> None:
        wf = make_wf(
            {
                "s": {"type": "string", "default": "d"},
                "n": {"type": "integer", "default": 5},
                "b": {"type": "boolean", "default": True},
                "j": {"type": "json", "default": {"k": 1}},
            }
        )
        assert validate_variables(wf, {}) == {"s": "d", "n": 5, "b": True, "j": {"k": 1}}

    def test_cli_value_overrides_default(self) -> None:
        wf = make_wf({"s": {"type": "string", "default": "d"}})
        assert validate_variables(wf, {"s": "given"}) == {"s": "given"}

    def test_optional_without_default_absent(self) -> None:
        wf = make_wf({"s": {"type": "string"}})
        assert validate_variables(wf, {}) == {}

    def test_default_type_mismatch_rejected(self) -> None:
        wf = make_wf({"n": {"type": "integer", "default": "5"}})
        with pytest.raises(ValidationError) as exc:
            validate_variables(wf, {})
        assert "does not match declared type" in str(exc.value)

    def test_boolean_default_not_accepted_for_integer(self) -> None:
        wf = make_wf({"n": {"type": "integer", "default": True}})
        with pytest.raises(ValidationError):
            validate_variables(wf, {})

    def test_problems_collected_together(self) -> None:
        wf = make_wf(
            {
                "req": {"type": "string", "required": True},
                "n": {"type": "integer"},
            }
        )
        with pytest.raises(ValidationError) as exc:
            validate_variables(wf, {"n": "x", "ghost": "1"})
        message = str(exc.value)
        assert "variables.req" in message
        assert "variables.n" in message
        assert "ghost" in message


class TestMaxBytes:
    def test_encoded_length_over_limit_rejected(self) -> None:
        wf = make_wf({"s": {"type": "string", "max_bytes": 8}})
        with pytest.raises(ValidationError) as exc:
            validate_variables(wf, {"s": "123456789"})
        message = str(exc.value)
        assert "9 bytes" in message
        assert "max_bytes is 8" in message

    def test_exact_limit_accepted(self) -> None:
        wf = make_wf({"s": {"type": "string", "max_bytes": 8}})
        assert validate_variables(wf, {"s": "12345678"}) == {"s": "12345678"}

    def test_multibyte_utf8_counted_as_bytes_not_chars(self) -> None:
        wf = make_wf({"s": {"type": "string", "max_bytes": 8}})
        # five chars, ten UTF-8 bytes
        with pytest.raises(ValidationError) as exc:
            validate_variables(wf, {"s": "ééééé"})
        assert "10 bytes" in str(exc.value)

    def test_default_over_limit_rejected(self) -> None:
        wf = make_wf({"s": {"type": "string", "max_bytes": 4, "default": "12345"}})
        with pytest.raises(ValidationError) as exc:
            validate_variables(wf, {})
        assert "max_bytes is 4" in str(exc.value)

    def test_json_raw_bytes_enforced(self) -> None:
        wf = make_wf({"j": {"type": "json", "max_bytes": 10}})
        with pytest.raises(ValidationError):
            validate_variables(wf, {"j": '{"a": "0123456789"}'})


SECRET_WF_STEPS = {
    "plan": {"agent": "claude", "prompt": "Use {{ vars.token }}"},
    "verify": {"agent": "claude", "prompt": "no secrets here"},
}


def secret_wf(steps: dict[str, dict[str, Any]] | None = None) -> WorkflowDef:
    return make_wf(
        {
            "token": {"type": "string", "secret": True, "required": True},
            "plain": {"type": "string", "default": "ok"},
        },
        steps or SECRET_WF_STEPS,
    )


class TestSecretAllowanceGate:
    def test_secret_without_allowance_rejected(self) -> None:
        wf = secret_wf()
        with pytest.raises(ValidationError) as exc:
            check_secret_allowances(
                wf,
                step_providers={"plan": "anthropic", "verify": "anthropic"},
                allowances={},
            )
        message = str(exc.value)
        assert "steps.plan" in message
        assert "'token'" in message
        assert "'anthropic'" in message
        assert "secret_variable_allowances" in message

    def test_secret_with_wrong_provider_allowance_rejected(self) -> None:
        wf = secret_wf()
        with pytest.raises(ValidationError):
            check_secret_allowances(
                wf,
                step_providers={"plan": "anthropic", "verify": "anthropic"},
                allowances={"token": ["openai"]},
            )

    def test_secret_with_matching_allowance_passes(self) -> None:
        wf = secret_wf()
        check_secret_allowances(
            wf,
            step_providers={"plan": "anthropic", "verify": "anthropic"},
            allowances={"token": ["anthropic"]},
        )

    def test_secret_via_var_sourced_input_gated(self) -> None:
        wf = secret_wf(
            {
                "plan": {
                    "agent": "claude",
                    "prompt": "Use {{ inputs.tok }}",
                    "inputs": {"tok": "vars.token"},
                }
            }
        )
        with pytest.raises(ValidationError) as exc:
            check_secret_allowances(wf, step_providers={"plan": "custom:claude"}, allowances={})
        assert "'token'" in str(exc.value)

    def test_unreferenced_secret_needs_no_allowance(self) -> None:
        wf = secret_wf({"verify": {"agent": "claude", "prompt": "nothing secret"}})
        check_secret_allowances(wf, step_providers={"verify": "anthropic"}, allowances={})

    def test_non_secret_vars_not_gated(self) -> None:
        wf = secret_wf({"plan": {"agent": "claude", "prompt": "{{ vars.plain }}"}})
        check_secret_allowances(wf, step_providers={"plan": "anthropic"}, allowances={})

    def test_missing_provider_identity_fails_closed(self) -> None:
        wf = secret_wf()
        with pytest.raises(ValidationError) as exc:
            check_secret_allowances(wf, step_providers={}, allowances={"token": ["anthropic"]})
        assert "no resolved provider" in str(exc.value)

    def test_allowed_secret_renders_and_registers_for_redaction(self) -> None:
        wf = secret_wf()
        secret_value = "hunter2-hunter2-hunter2"
        values = validate_variables(wf, {"token": secret_value})
        check_secret_allowances(
            wf,
            step_providers={"plan": "anthropic", "verify": "anthropic"},
            allowances={"token": ["anthropic"]},
        )
        prompt = render_prompt(wf.steps["plan"], values, {})
        assert prompt == f"Use {secret_value}"

        pairs = secret_redaction_values(wf, values)
        assert pairs == [("var:token", secret_value)]
        redacted, counts = Redactor(secret_values=pairs).redact_text(prompt)
        assert secret_value not in redacted
        assert counts.get("var:token") == 1

    def test_redaction_values_exclude_non_secret_and_unset(self) -> None:
        wf = secret_wf()
        assert secret_redaction_values(wf, {"plain": "ok"}) == []


class TestSecretAllowancesConfigField:
    """Loader coverage for the new USER_ONLY workflows.secret_variable_allowances."""

    def test_merge_rule_is_user_only(self) -> None:
        assert merge_rule_for("workflows.secret_variable_allowances.token") is MergeRule.USER_ONLY

    def test_user_scope_loads_with_provenance(self, tmp_path: Path) -> None:
        user_file = tmp_path / "home" / "config.toml"
        user_file.parent.mkdir(parents=True)
        user_file.write_text(
            "schema_version = 1\n"
            "[workflows.secret_variable_allowances]\n"
            'token = ["anthropic", "openai"]\n',
            encoding="utf-8",
        )
        rc = load_config(None, user_path=user_file, env={})
        assert rc.config.workflows.secret_variable_allowances == {"token": ["anthropic", "openai"]}
        entry = rc.provenance["workflows.secret_variable_allowances.token"]
        assert entry.source is ConfigSource.USER

    def test_defaults_to_empty(self, tmp_path: Path) -> None:
        rc = load_config(None, user_path=tmp_path / "missing.toml", env={})
        assert rc.config.workflows.secret_variable_allowances == {}

    def test_project_scope_rejected(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        project_file = workspace / ".ziggy" / "config.toml"
        project_file.parent.mkdir(parents=True)
        project_file.write_text(
            'schema_version = 1\n[workflows.secret_variable_allowances]\ntoken = ["anthropic"]\n',
            encoding="utf-8",
        )
        with pytest.raises(ConfigError) as exc:
            load_config(workspace, user_path=tmp_path / "missing.toml", env={})
        message = str(exc.value)
        assert "workflows.secret_variable_allowances.token" in message
        assert "forbidden in project scope" in message
