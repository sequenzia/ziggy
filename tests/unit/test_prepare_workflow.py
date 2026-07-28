"""Unit tests for ziggy.engine.prepare.prepare_workflow (REQ-009/010/011).

Every test runs against a tmp ``ZIGGY_HOME`` (discovery consults the user
workflows directory) and a tmp workspace with workflows written under
``.ziggy/workflows/``. Nothing here launches an agent — prepare is the
fail-before-launch seam.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ziggy.config import ResolvedConfig, load_config
from ziggy.engine.prepare import RunOverrides, prepare_workflow
from ziggy.errors import (
    ConfigError,
    EgressNotAcknowledgedError,
    ResourceLimitError,
    ValidationError,
)
from ziggy.models.common import CaptureProfile
from ziggy.store.logs import NullLogger
from ziggy.workflows.egress import ACK_BY_CONFIG, ACK_BY_FLAG
from ziggy.workflows.schema import load_workflow

REPO_ROOT = Path(__file__).resolve().parents[2]

BASE_ENV = {"HOME": "/h", "PATH": "/p"}

AGENTS_TOML = """
[agents.mock-a]
command = "/usr/bin/true"
provider = "anthropic"

[agents.mock-b]
command = "/usr/bin/true"
provider = "openai"
"""

CROSSING_WF = """\
version: 1
name: crossing
variables:
  issue:
    type: string
    required: true
steps:
  plan:
    agent: mock-a
    prompt: |
      plan: {{ vars.issue }}
  fix:
    agent: mock-b
    inputs:
      plan: steps.plan.outputs.text
    prompt: |
      apply: {{ inputs.plan }}
    working_dir: sub
    timeout_seconds: 5
  verify:
    agent: mock-a
    prompt: verify things
    depends_on: [fix]
    timeout_seconds: 5000
"""

SINGLE_WF = """\
version: 1
name: single
steps:
  only:
    agent: mock-a
    prompt: go
"""

ACK_FLAG = ["anthropic", "openai"]


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Tmp workspace + tmp ZIGGY_HOME so discovery never touches ~/.ziggy."""
    home = tmp_path / "ziggy-home"
    home.mkdir()
    monkeypatch.setenv("ZIGGY_HOME", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def write_workflow(workspace: Path, name: str, text: str) -> Path:
    wf_dir = workspace / ".ziggy" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    path = wf_dir / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def resolve_config(tmp_path: Path, user_toml: str = AGENTS_TOML) -> ResolvedConfig:
    user_file = tmp_path / "user-config.toml"
    user_file.write_text("schema_version = 1\n" + user_toml, encoding="utf-8")
    return load_config(None, user_path=user_file, env={})


def prepare(
    tmp_path: Path,
    workspace: Path,
    name_or_path: str,
    *,
    user_toml: str = AGENTS_TOML,
    cli_vars: dict[str, str] | None = None,
    overrides: RunOverrides | None = None,
    base_env: dict[str, str] | None = None,
):
    resolved = resolve_config(tmp_path, user_toml)
    return prepare_workflow(
        resolved,
        name_or_path=name_or_path,
        cli_vars=cli_vars or {},
        workspace=workspace,
        overrides=overrides or RunOverrides(no_save=True),
        base_env=base_env or dict(BASE_ENV),
    )


class TestHappyPath:
    TOML = AGENTS_TOML + (
        "[engine]\ndefault_step_timeout_seconds = 100\n"
        "default_workflow_timeout_seconds = 200\n"
        "max_event_bytes_per_step = 1024\nmax_artifact_bytes_per_run = 2048\n"
    )

    def build(self, tmp_path: Path, workspace: Path):
        write_workflow(workspace, "crossing", CROSSING_WF)
        return prepare(
            tmp_path,
            workspace,
            "crossing",
            user_toml=self.TOML,
            cli_vars={"issue": "the issue"},
            overrides=RunOverrides(no_save=True, acknowledge_egress=list(ACK_FLAG)),
        )

    def test_step_order_and_coverage(self, tmp_path: Path, workspace: Path) -> None:
        prepared = self.build(tmp_path, workspace)
        assert prepared.order == ["plan", "fix", "verify"]
        assert set(prepared.steps) == set(prepared.workflow.steps)
        assert prepared.workflow.name == "crossing"
        assert prepared.variables == {"issue": "the issue"}
        assert prepared.workspace == workspace

    def test_per_step_policies_use_step_working_dir(self, tmp_path: Path, workspace: Path) -> None:
        prepared = self.build(tmp_path, workspace)
        ws_real = Path(os.path.realpath(workspace))
        assert prepared.steps["plan"].policy.step_dir == ws_real
        assert prepared.steps["fix"].policy.step_dir == ws_real / "sub"
        assert prepared.steps["fix"].policy.workspace == ws_real
        assert prepared.policy_provenance is not None
        assert prepared.policy_provenance.policy_name == "guarded"

    def test_per_step_timeouts_clamped_to_user_ceiling(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        prepared = self.build(tmp_path, workspace)
        assert prepared.steps["plan"].timeout_seconds == 100.0  # config default
        assert prepared.steps["fix"].timeout_seconds == 5.0  # step value under ceiling
        assert prepared.steps["verify"].timeout_seconds == 100.0  # clamped down
        assert prepared.workflow_timeout_seconds == 200.0

    def test_egress_gate_passes_with_flag(self, tmp_path: Path, workspace: Path) -> None:
        prepared = self.build(tmp_path, workspace)
        assert prepared.egress.provider_set == frozenset({"anthropic", "openai"})
        assert prepared.egress.acknowledged_by == ACK_BY_FLAG
        [record] = prepared.egress.records
        assert record.step_id == "fix"
        assert record.provider == "openai"
        assert record.input_sources == ["steps.plan.outputs.text"]
        assert record.acknowledged_by == ACK_BY_FLAG

    def test_providers_env_and_bookkeeping(self, tmp_path: Path, workspace: Path) -> None:
        prepared = self.build(tmp_path, workspace)
        assert prepared.steps["plan"].provider == "anthropic"
        assert prepared.steps["fix"].provider == "openai"
        assert prepared.steps["plan"].env == BASE_ENV  # baseline only
        assert prepared.capture_profile is CaptureProfile.STANDARD
        assert prepared.no_save is True
        assert isinstance(prepared.logger, NullLogger)
        assert prepared.store_root is None
        assert prepared.config_fingerprint
        assert prepared.limits.max_event_bytes_per_step == 1024
        assert prepared.limits.max_artifact_bytes_per_run == 2048


class TestRedactionSeeds:
    SECRET_WF = """\
version: 1
name: secretive
variables:
  token:
    type: string
    required: true
    secret: true
steps:
  only:
    agent: mock-a
    prompt: |
      use {{ vars.token }}
"""
    TOML = (
        '[agents.mock-a]\ncommand = "/usr/bin/true"\nprovider = "anthropic"\n'
        'api_key_env = "MOCK_KEY"\n\n'
        '[redaction]\nextra_value_env_vars = ["EXTRA_SECRET"]\n\n'
        '[workflows.secret_variable_allowances]\ntoken = ["anthropic"]\n'
    )

    def test_seeds_include_api_key_extra_env_and_secret_vars(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        write_workflow(workspace, "secretive", self.SECRET_WF)
        prepared = prepare(
            tmp_path,
            workspace,
            "secretive",
            user_toml=self.TOML,
            cli_vars={"token": "sekrit-value"},
            base_env={**BASE_ENV, "MOCK_KEY": "key-value", "EXTRA_SECRET": "extra-value"},
        )
        assert ("env:MOCK_KEY", "key-value") in prepared.secret_values
        assert ("env:EXTRA_SECRET", "extra-value") in prepared.secret_values
        assert ("var:token", "sekrit-value") in prepared.secret_values

    def test_secret_var_without_allowance_fails_before_execution(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        write_workflow(workspace, "secretive", self.SECRET_WF)
        toml = '[agents.mock-a]\ncommand = "/usr/bin/true"\nprovider = "anthropic"\n'
        with pytest.raises(ValidationError) as exc_info:
            prepare(
                tmp_path,
                workspace,
                "secretive",
                user_toml=toml,
                cli_vars={"token": "sekrit-value"},
            )
        assert "anthropic" in exc_info.value.message
        assert "token" in exc_info.value.message


class TestEgressGate:
    def test_unacknowledged_crossing_raises_exit_2_with_hint(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        write_workflow(workspace, "crossing", CROSSING_WF)
        with pytest.raises(EgressNotAcknowledgedError) as exc_info:
            prepare(tmp_path, workspace, "crossing", cli_vars={"issue": "x"})
        assert exc_info.value.exit_code == 2
        assert "--acknowledge-egress anthropic,openai" in exc_info.value.message

    def test_config_acknowledgement_passes(self, tmp_path: Path, workspace: Path) -> None:
        write_workflow(workspace, "crossing", CROSSING_WF)
        toml = AGENTS_TOML + ('[egress]\nacknowledged_provider_sets = [["openai", "anthropic"]]\n')
        prepared = prepare(tmp_path, workspace, "crossing", user_toml=toml, cli_vars={"issue": "x"})
        assert prepared.egress.acknowledged_by == ACK_BY_CONFIG

    def test_single_provider_workflow_needs_no_acknowledgement(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        write_workflow(workspace, "single", SINGLE_WF)
        prepared = prepare(tmp_path, workspace, "single")
        assert prepared.egress.provider_set == frozenset()
        assert prepared.egress.acknowledged_by is None


class TestPreflightRejections:
    def test_step_count_over_ceiling_is_resource_limit_error(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        write_workflow(workspace, "crossing", CROSSING_WF)
        toml = AGENTS_TOML + "[engine]\nmax_workflow_steps = 2\n"
        with pytest.raises(ResourceLimitError) as exc_info:
            prepare(tmp_path, workspace, "crossing", user_toml=toml, cli_vars={"issue": "x"})
        assert exc_info.value.details["max_workflow_steps"] == 2
        assert exc_info.value.details["step_count"] == 3

    def test_unknown_workflow_name_is_validation_error_exit_2(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        with pytest.raises(ValidationError) as exc_info:
            prepare(tmp_path, workspace, "missing")
        assert exc_info.value.exit_code == 2
        assert "missing" in exc_info.value.message

    def test_unknown_agent_is_config_error(self, tmp_path: Path, workspace: Path) -> None:
        wf = "version: 1\nname: ghostly\nsteps:\n  a:\n    agent: ghost\n    prompt: hi\n"
        write_workflow(workspace, "ghostly", wf)
        with pytest.raises(ConfigError) as exc_info:
            prepare(tmp_path, workspace, "ghostly")
        assert "ghost" in exc_info.value.message

    def test_working_dir_escape_is_validation_error(self, tmp_path: Path, workspace: Path) -> None:
        wf = (
            "version: 1\nname: escapee\nsteps:\n  a:\n    agent: mock-a\n"
            "    prompt: hi\n    working_dir: ../outside\n"
        )
        write_workflow(workspace, "escapee", wf)
        with pytest.raises(ValidationError) as exc_info:
            prepare(tmp_path, workspace, "escapee")
        assert "working_dir" in exc_info.value.message

    def test_unknown_policy_profile_is_validation_error(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        wf = (
            "version: 1\nname: profiled\nsteps:\n  a:\n    agent: mock-a\n"
            "    prompt: hi\n    policy_profile: nope\n"
        )
        write_workflow(workspace, "profiled", wf)
        with pytest.raises(ValidationError) as exc_info:
            prepare(tmp_path, workspace, "profiled")
        assert "policy_profile" in exc_info.value.message

    def test_typed_var_violation_surfaces(self, tmp_path: Path, workspace: Path) -> None:
        wf = (
            "version: 1\nname: typed\nvariables:\n  n:\n    type: integer\n"
            "    required: true\nsteps:\n  a:\n    agent: mock-a\n"
            "    prompt: 'n={{ vars.n }}'\n"
        )
        write_workflow(workspace, "typed", wf)
        with pytest.raises(ValidationError) as exc_info:
            prepare(tmp_path, workspace, "typed", cli_vars={"n": "abc"})
        assert "integer" in exc_info.value.message


class TestStepPolicyProfiles:
    def test_step_policy_profile_selects_user_profile(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        wf = (
            "version: 1\nname: profiled\nsteps:\n  a:\n    agent: mock-a\n"
            "    prompt: hi\n    policy_profile: strict\n"
        )
        write_workflow(workspace, "profiled", wf)
        toml = AGENTS_TOML + ('[permissions.profiles.strict]\ndeny_paths = ["private/**"]\n')
        prepared = prepare(tmp_path, workspace, "profiled", user_toml=toml)
        policy = prepared.steps["a"].policy
        assert policy.policy_name == "strict"
        denied = policy.decide_fs_read(str(workspace / "private" / "f"))
        assert denied.allowed is False


def test_example_review_and_fix_loads_and_validates() -> None:
    workflow = load_workflow(REPO_ROOT / "examples" / "workflows" / "review-and-fix.yaml")
    assert workflow.version == 1
    assert workflow.name == "review-and-fix"
    assert list(workflow.steps) == ["plan", "fix", "verify"]
    assert workflow.steps["plan"].agent == "claude"
    assert workflow.steps["fix"].agent == "codex"
    assert workflow.steps["fix"].inputs == {"plan": "steps.plan.outputs.text"}
    assert workflow.steps["verify"].depends_on == ["fix"]
    issue = workflow.variables["issue"]
    assert issue.type == "string"
    assert issue.required is True
    assert issue.max_bytes == 16384
