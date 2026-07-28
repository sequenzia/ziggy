"""Unit tests for ziggy.workflows.schema (REQ-009 workflow definition)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ziggy.errors import ValidationError
from ziggy.workflows import load_workflow

VALID = """\
version: 1
name: demo
description: Minimal valid workflow.

variables:
  issue:
    type: string
    required: true
    max_bytes: 16384

steps:
  plan:
    agent: claude
    prompt: |
      Analyze this issue:
      {{ vars.issue }}
  fix:
    agent: codex
    inputs:
      plan: steps.plan.outputs.text
    prompt: |
      Apply the plan:
      {{ inputs.plan }}
  verify:
    agent: claude
    prompt: Review the workspace changes.
    depends_on: [fix]
"""


def write(tmp_path: Path, filename: str, text: str) -> Path:
    path = tmp_path / filename
    path.write_text(text, encoding="utf-8")
    return path


class TestValidLoad:
    def test_full_workflow_loads(self, tmp_path: Path) -> None:
        wf = load_workflow(write(tmp_path, "demo.yaml", VALID))
        assert wf.version == 1
        assert wf.name == "demo"
        assert list(wf.steps) == ["plan", "fix", "verify"]
        assert wf.variables["issue"].required is True
        assert wf.variables["issue"].max_bytes == 16384
        assert wf.steps["fix"].inputs == {"plan": "steps.plan.outputs.text"}
        assert wf.steps["verify"].depends_on == ["fix"]

    def test_accepts_str_path(self, tmp_path: Path) -> None:
        path = write(tmp_path, "demo.yaml", VALID)
        assert load_workflow(str(path)).name == "demo"

    def test_yml_extension_stem_matches(self, tmp_path: Path) -> None:
        text = "version: 1\nname: tiny\nsteps:\n  a:\n    agent: claude\n    prompt: hi\n"
        assert load_workflow(write(tmp_path, "tiny.yml", text)).name == "tiny"


class TestVersionLiteral:
    def test_version_2_rejected(self, tmp_path: Path) -> None:
        text = VALID.replace("version: 1", "version: 2")
        with pytest.raises(ValidationError) as exc:
            load_workflow(write(tmp_path, "demo.yaml", text))
        message = str(exc.value)
        assert "version" in message
        assert "literal 1" in message

    def test_version_missing_rejected(self, tmp_path: Path) -> None:
        text = VALID.replace("version: 1\n", "")
        with pytest.raises(ValidationError) as exc:
            load_workflow(write(tmp_path, "demo.yaml", text))
        assert "version" in str(exc.value)

    def test_version_string_rejected(self, tmp_path: Path) -> None:
        text = VALID.replace("version: 1", 'version: "1"')
        with pytest.raises(ValidationError):
            load_workflow(write(tmp_path, "demo.yaml", text))


class TestUnknownKeys:
    def test_top_level_unknown_key_path_precise(self, tmp_path: Path) -> None:
        path = write(tmp_path, "demo.yaml", VALID + "unexpected_key: 1\n")
        with pytest.raises(ValidationError) as exc:
            load_workflow(path)
        message = str(exc.value)
        assert str(path) in message
        assert "unexpected_key" in message
        assert "unknown key" in message

    def test_step_level_unknown_key_path_precise(self, tmp_path: Path) -> None:
        text = VALID.replace("    agent: codex\n", "    agent: codex\n    retries: 3\n")
        with pytest.raises(ValidationError) as exc:
            load_workflow(write(tmp_path, "demo.yaml", text))
        message = str(exc.value)
        assert "steps.fix.retries" in message
        assert "unknown key" in message

    def test_variable_level_unknown_key_path_precise(self, tmp_path: Path) -> None:
        text = VALID.replace("    required: true\n", "    required: true\n    scope: all\n")
        with pytest.raises(ValidationError) as exc:
            load_workflow(write(tmp_path, "demo.yaml", text))
        assert "variables.issue.scope" in str(exc.value)


class TestStepTypeRejection:
    @pytest.mark.parametrize("step_type", ["script", "shell", "python"])
    def test_non_agent_type_specific_message(self, tmp_path: Path, step_type: str) -> None:
        text = (
            "version: 1\nname: demo\nsteps:\n  build:\n"
            f"    type: {step_type}\n    agent: claude\n    prompt: hi\n"
        )
        with pytest.raises(ValidationError) as exc:
            load_workflow(write(tmp_path, "demo.yaml", text))
        message = str(exc.value)
        assert (
            f"step type '{step_type}' is not supported in schema version 1 "
            "(deferred post-MVP)" in message
        )
        assert "steps.build.type" in message

    def test_agent_type_explicitly_allowed(self, tmp_path: Path) -> None:
        text = (
            "version: 1\nname: demo\nsteps:\n  a:\n"
            "    type: agent\n    agent: claude\n    prompt: hi\n"
        )
        assert load_workflow(write(tmp_path, "demo.yaml", text)).steps["a"].type == "agent"


class TestNameFilenameRule:
    def test_name_must_match_filename_stem(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc:
            load_workflow(write(tmp_path, "other.yaml", VALID))
        message = str(exc.value)
        assert "'demo'" in message
        assert "'other'" in message
        assert "stem" in message


class TestYamlSafety:
    def test_non_mapping_list_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc:
            load_workflow(write(tmp_path, "demo.yaml", "- a\n- b\n"))
        assert "must be a mapping" in str(exc.value)

    def test_non_mapping_scalar_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc:
            load_workflow(write(tmp_path, "demo.yaml", "just a string\n"))
        assert "must be a mapping" in str(exc.value)

    def test_empty_file_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc:
            load_workflow(write(tmp_path, "demo.yaml", ""))
        assert "must be a mapping" in str(exc.value)

    def test_missing_file_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc:
            load_workflow(tmp_path / "demo.yaml")
        assert "unreadable" in str(exc.value)

    def test_anchors_and_aliases_resolve_safely(self, tmp_path: Path) -> None:
        text = (
            "version: 1\nname: demo\nsteps:\n"
            "  a: &tmpl\n    agent: claude\n    prompt: shared prompt\n"
            "  b: *tmpl\n"
        )
        wf = load_workflow(write(tmp_path, "demo.yaml", text))
        assert wf.steps["a"].prompt == "shared prompt"
        assert wf.steps["b"].prompt == "shared prompt"

    def test_python_object_tag_rejected_not_constructed(self, tmp_path: Path) -> None:
        # safe_load must refuse arbitrary-object construction as a parse error.
        text = 'version: 1\nname: demo\nsteps: !!python/object/apply:os.system ["echo pwned"]\n'
        with pytest.raises(ValidationError) as exc:
            load_workflow(write(tmp_path, "demo.yaml", text))
        assert "YAML parse error" in str(exc.value)

    def test_yaml_syntax_error_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc:
            load_workflow(write(tmp_path, "demo.yaml", "version: [1\n"))
        assert "YAML parse error" in str(exc.value)


class TestStructureRules:
    def test_empty_steps_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc:
            load_workflow(write(tmp_path, "demo.yaml", "version: 1\nname: demo\nsteps: {}\n"))
        assert "steps" in str(exc.value)

    def test_empty_prompt_rejected(self, tmp_path: Path) -> None:
        text = "version: 1\nname: demo\nsteps:\n  a:\n    agent: claude\n    prompt: ''\n"
        with pytest.raises(ValidationError) as exc:
            load_workflow(write(tmp_path, "demo.yaml", text))
        assert "steps.a.prompt" in str(exc.value)

    def test_bad_input_source_shape_rejected(self, tmp_path: Path) -> None:
        text = (
            "version: 1\nname: demo\nsteps:\n  a:\n    agent: claude\n"
            "    prompt: hi\n    inputs:\n      x: nonsense\n"
        )
        with pytest.raises(ValidationError) as exc:
            load_workflow(write(tmp_path, "demo.yaml", text))
        message = str(exc.value)
        assert "steps.a" in message
        assert "vars.<name>" in message

    def test_required_variable_with_default_rejected(self, tmp_path: Path) -> None:
        text = (
            "version: 1\nname: demo\nvariables:\n  v:\n    required: true\n"
            "    default: x\nsteps:\n  a:\n    agent: claude\n    prompt: hi\n"
        )
        with pytest.raises(ValidationError) as exc:
            load_workflow(write(tmp_path, "demo.yaml", text))
        assert "variables.v" in str(exc.value)


class TestTemplateValidationAtLoad:
    def test_bare_token_rejected_at_load(self, tmp_path: Path) -> None:
        text = "version: 1\nname: demo\nsteps:\n  a:\n    agent: claude\n    prompt: '{{ x }}'\n"
        path = write(tmp_path, "demo.yaml", text)
        with pytest.raises(ValidationError) as exc:
            load_workflow(path)
        message = str(exc.value)
        assert str(path) in message
        assert "steps.a.prompt" in message

    def test_undeclared_variable_reference_rejected(self, tmp_path: Path) -> None:
        text = (
            "version: 1\nname: demo\nsteps:\n  a:\n    agent: claude\n"
            "    prompt: 'see {{ vars.nope }}'\n"
        )
        with pytest.raises(ValidationError) as exc:
            load_workflow(write(tmp_path, "demo.yaml", text))
        assert "vars.nope" in str(exc.value)

    def test_undeclared_input_reference_rejected(self, tmp_path: Path) -> None:
        text = (
            "version: 1\nname: demo\nsteps:\n  a:\n    agent: claude\n"
            "    prompt: 'see {{ inputs.nope }}'\n"
        )
        with pytest.raises(ValidationError) as exc:
            load_workflow(write(tmp_path, "demo.yaml", text))
        assert "inputs.nope" in str(exc.value)

    def test_input_sourced_from_undeclared_variable_rejected(self, tmp_path: Path) -> None:
        text = (
            "version: 1\nname: demo\nsteps:\n  a:\n    agent: claude\n"
            "    prompt: '{{ inputs.x }}'\n    inputs:\n      x: vars.ghost\n"
        )
        with pytest.raises(ValidationError) as exc:
            load_workflow(write(tmp_path, "demo.yaml", text))
        message = str(exc.value)
        assert "steps.a.inputs.x" in message
        assert "ghost" in message
