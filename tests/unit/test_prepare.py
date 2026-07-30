"""Unit tests for ziggy.engine.prepare (config + overrides -> RunSpec).

Also carries the FIX #21 teardown-ordering unit test for
``ziggy.engine.runner.execute_step`` (a runner internal exercised without a
real subprocess).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ziggy.acp import HandshakeInfo, StopInfo
from ziggy.config import ResolvedConfig, load_config
from ziggy.engine import runner as runner_module
from ziggy.engine.prepare import (
    ACK_BY_CONFIG,
    ACK_BY_FLAG,
    PreparedRun,
    RunOverrides,
    prepare_run,
)
from ziggy.engine.runner import StepExecutionContext, execute_step
from ziggy.errors import ConfigError, ResourceLimitError
from ziggy.events import RunRecorder
from ziggy.models.common import CaptureProfile, StepStatus
from ziggy.policy import GUARDED_POLICY_NAME
from ziggy.redact import Redactor
from ziggy.store.logs import MetadataLogger, NullLogger

BASE_ENV = {"HOME": "/h", "PATH": "/p"}

AGENT_TOML = """
[agents.mock]
command = "/usr/bin/true"
"""


def resolve(
    tmp_path: Path,
    user_toml: str = "",
    *,
    project_toml: str | None = None,
    workspace: Path | None = None,
) -> ResolvedConfig:
    user_file = tmp_path / "user-config.toml"
    user_file.write_text("schema_version = 1\n" + user_toml, encoding="utf-8")
    if project_toml is not None:
        assert workspace is not None
        project_dir = workspace / ".ziggy"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "config.toml").write_text(
            "schema_version = 1\n" + project_toml, encoding="utf-8"
        )
    return load_config(workspace, user_path=user_file, env={})


def make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return workspace


def prepare(
    tmp_path: Path,
    user_toml: str = AGENT_TOML,
    *,
    agent_name: str = "mock",
    prompt: str = "go",
    overrides: RunOverrides | None = None,
    base_env: dict[str, str] | None = None,
    project_toml: str | None = None,
) -> PreparedRun:
    workspace = make_workspace(tmp_path)
    resolved = resolve(
        tmp_path,
        user_toml,
        project_toml=project_toml,
        workspace=workspace if project_toml is not None else None,
    )
    return prepare_run(
        resolved,
        agent_name=agent_name,
        prompt=prompt,
        workspace=workspace,
        overrides=overrides or RunOverrides(),
        base_env=base_env or dict(BASE_ENV),
    )


class TestHappyPath:
    def test_spec_assembled_from_config(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        toml = AGENT_TOML + (
            f'[results]\nstore_path = "{home}"\n\n[engine]\n'
            "default_step_timeout_seconds = 100\ncancel_grace_seconds = 2.5\n"
            "max_event_bytes_per_step = 1024\nmax_artifact_bytes_per_run = 2048\n"
        )
        prepared = prepare(tmp_path, toml, overrides=RunOverrides(no_save=True))
        spec = prepared.spec

        assert spec.agent_name == "mock"
        assert spec.command == "/usr/bin/true"
        assert spec.args == []
        assert spec.cwd == str(tmp_path / "ws")
        assert spec.prompt == "go"
        assert spec.capture_profile is CaptureProfile.STANDARD
        assert spec.step_timeout_seconds == 100.0
        assert spec.cancel_grace_seconds == 2.5
        assert spec.limits.max_event_bytes_per_step == 1024
        assert spec.limits.max_artifact_bytes_per_run == 2048
        assert spec.store_root == home
        assert spec.env == BASE_ENV  # baseline only; nothing inherited implicitly
        assert spec.secret_values == []
        assert spec.redaction_patterns is None

        assert spec.policy is prepared.policy
        assert spec.policy_provenance is prepared.policy_provenance
        assert spec.logger is prepared.logger
        assert spec.config_fingerprint == prepared.config_fingerprint
        assert prepared.agent_config.name == "mock"

    def test_policy_is_guarded_over_workspace(self, tmp_path: Path) -> None:
        import os

        prepared = prepare(tmp_path)
        workspace_real = Path(os.path.realpath(tmp_path / "ws"))
        assert prepared.policy.policy_name == GUARDED_POLICY_NAME
        assert prepared.policy.workspace == workspace_real
        # direct runs: the step working directory IS the workspace
        assert prepared.policy.step_dir == workspace_real
        assert prepared.policy_provenance.policy_name == GUARDED_POLICY_NAME
        assert prepared.policy_provenance.enforcement == "advisory"

    def test_fingerprint_matches_resolved_config(self, tmp_path: Path) -> None:
        workspace = make_workspace(tmp_path)
        resolved = resolve(tmp_path, AGENT_TOML)
        prepared = prepare_run(
            resolved,
            agent_name="mock",
            prompt="go",
            workspace=workspace,
            overrides=RunOverrides(no_save=True),
            base_env=dict(BASE_ENV),
        )
        assert prepared.config_fingerprint == resolved.fingerprint
        assert prepared.spec.config_fingerprint == resolved.fingerprint


class TestValidation:
    def test_unknown_agent_is_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as exc_info:
            prepare(tmp_path, agent_name="nope")
        assert "nope" in str(exc_info.value)

    def test_prompt_over_ceiling_is_resource_limit_error(self, tmp_path: Path) -> None:
        toml = AGENT_TOML + "[engine]\nmax_prompt_bytes = 8\n"
        with pytest.raises(ResourceLimitError) as exc_info:
            prepare(tmp_path, toml, prompt="123456789")
        assert exc_info.value.details["max_prompt_bytes"] == 8

    def test_prompt_ceiling_counts_utf8_bytes(self, tmp_path: Path) -> None:
        toml = AGENT_TOML + "[engine]\nmax_prompt_bytes = 8\n"
        with pytest.raises(ResourceLimitError):
            prepare(tmp_path, toml, prompt="ééééé")  # 5 chars, 10 bytes

    def test_missing_api_key_env_is_config_error_before_launch(self, tmp_path: Path) -> None:
        toml = '[agents.mock]\ncommand = "/usr/bin/true"\napi_key_env = "MOCK_KEY"\n'
        with pytest.raises(ConfigError) as exc_info:
            prepare(tmp_path, toml)
        assert "MOCK_KEY" in str(exc_info.value)


class TestOverrides:
    def test_capture_defaults_to_config_value(self, tmp_path: Path) -> None:
        toml = AGENT_TOML + '[results]\ncapture = "metadata"\n'
        prepared = prepare(tmp_path, toml, overrides=RunOverrides(no_save=True))
        assert prepared.spec.capture_profile is CaptureProfile.METADATA

    def test_capture_cli_flag_is_user_intent_and_may_exceed_config(self, tmp_path: Path) -> None:
        toml = AGENT_TOML + '[results]\ncapture = "metadata"\n'
        prepared = prepare(
            tmp_path, toml, overrides=RunOverrides(no_save=True, capture=CaptureProfile.DEBUG)
        )
        assert prepared.spec.capture_profile is CaptureProfile.DEBUG

    def test_timeout_override_may_only_lower_the_ceiling(self, tmp_path: Path) -> None:
        toml = AGENT_TOML + "[engine]\ndefault_step_timeout_seconds = 100\n"
        lower = prepare(tmp_path, toml, overrides=RunOverrides(no_save=True, timeout_seconds=50.0))
        assert lower.spec.step_timeout_seconds == 50.0
        higher = prepare(
            tmp_path, toml, overrides=RunOverrides(no_save=True, timeout_seconds=500.0)
        )
        assert higher.spec.step_timeout_seconds == 100.0
        default = prepare(tmp_path, toml, overrides=RunOverrides(no_save=True))
        assert default.spec.step_timeout_seconds == 100.0


class TestRedactionSeeding:
    def test_api_key_and_extra_env_values_become_secret_values(self, tmp_path: Path) -> None:
        toml = (
            '[agents.mock]\ncommand = "/usr/bin/true"\napi_key_env = "MOCK_KEY"\n\n'
            '[redaction]\nextra_value_env_vars = ["EXTRA_SECRET", "UNSET_NAME"]\n'
        )
        base_env = {
            **BASE_ENV,
            "MOCK_KEY": "mock-key-value",
            "EXTRA_SECRET": "extra-secret-value",
        }
        prepared = prepare(tmp_path, toml, overrides=RunOverrides(no_save=True), base_env=base_env)
        assert prepared.spec.secret_values == [
            ("env:MOCK_KEY", "mock-key-value"),
            ("env:EXTRA_SECRET", "extra-secret-value"),
        ]

    def test_custom_patterns_pass_through(self, tmp_path: Path) -> None:
        toml = AGENT_TOML + (
            '[[redaction.patterns]]\nkind = "corp-token"\nregex = "XSEC-[0-9]+"\nmax_width = 32\n'
        )
        prepared = prepare(tmp_path, toml, overrides=RunOverrides(no_save=True))
        assert prepared.spec.redaction_patterns is not None
        [pattern] = prepared.spec.redaction_patterns
        assert pattern.kind == "corp-token"
        assert pattern.regex == "XSEC-[0-9]+"
        assert pattern.max_width == 32


class TestLoggerSelection:
    def test_no_save_gets_null_logger(self, tmp_path: Path) -> None:
        prepared = prepare(tmp_path, overrides=RunOverrides(no_save=True))
        assert isinstance(prepared.logger, NullLogger)
        assert prepared.spec.no_save is True

    def test_persist_false_in_config_disables_saving(self, tmp_path: Path) -> None:
        toml = AGENT_TOML + "[results]\npersist = false\n"
        prepared = prepare(tmp_path, toml)
        assert isinstance(prepared.logger, NullLogger)
        assert prepared.spec.no_save is True

    def test_saved_run_gets_real_logger_under_store_path(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        toml = AGENT_TOML + f'[results]\nstore_path = "{home}"\n'
        prepared = prepare(tmp_path, toml)
        assert isinstance(prepared.logger, MetadataLogger)
        assert prepared.logger.logs_dir == home / "logs"
        assert prepared.spec.no_save is False
        prepared.logger.close()

    def test_logger_retention_from_config(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        toml = AGENT_TOML + f'[results]\nstore_path = "{home}"\n\n[logs]\nretention_days = 7\n'
        prepared = prepare(tmp_path, toml)
        assert isinstance(prepared.logger, MetadataLogger)
        assert prepared.logger.retention_days == 7
        prepared.logger.close()


class TestPolicyComposition:
    def test_project_denials_flow_into_policy(self, tmp_path: Path) -> None:
        project = '[[permissions.project_denials]]\nkind = "path"\npattern = "secrets/**"\n'
        prepared = prepare(tmp_path, overrides=RunOverrides(no_save=True), project_toml=project)
        [denial] = prepared.policy.project_denials
        assert denial.kind == "path"
        assert denial.pattern == "secrets/**"
        assert prepared.policy_provenance.tightened_by == ["project-denials:1"]

        decision = prepared.policy.decide_fs_read(str(tmp_path / "ws" / "secrets" / "x"))
        assert decision.allowed is False
        assert decision.rule_id == "project-denial:0"

    def test_user_profile_selected_by_default_policy(self, tmp_path: Path) -> None:
        from ziggy.acp import TerminalRequestN

        toml = AGENT_TOML + (
            '[permissions]\ndefault_policy = "mine"\n\n'
            '[permissions.profiles.mine]\ndeny_paths = ["private/**"]\n\n'
            "[[permissions.profiles.mine.terminal_allowlist]]\n"
            'command = "git"\nargs_prefix = ["status"]\n'
        )
        prepared = prepare(tmp_path, toml, overrides=RunOverrides(no_save=True))
        assert prepared.policy.policy_name == "mine"
        assert prepared.policy_provenance.policy_name == "mine"

        allowed = prepared.policy.decide_terminal(
            TerminalRequestN(op="create", payload={"command": "git", "args": ["status"]})
        )
        assert allowed.allowed is True
        denied = prepared.policy.decide_fs_read(str(tmp_path / "ws" / "private" / "f"))
        assert denied.allowed is False


class TestEgressAcknowledgement:
    PROVIDER_TOML = '[agents.mock]\ncommand = "/usr/bin/true"\nprovider = "anthropic"\n'

    def test_flag_acknowledgement_wins(self, tmp_path: Path) -> None:
        toml = self.PROVIDER_TOML + '[egress]\nacknowledged_provider_sets = [["anthropic"]]\n'
        prepared = prepare(
            tmp_path,
            toml,
            overrides=RunOverrides(no_save=True, acknowledge_egress=["anthropic"]),
        )
        assert prepared.spec.egress_acknowledged_by == ACK_BY_FLAG
        assert prepared.spec.provider == "anthropic"

    def test_config_acknowledgement(self, tmp_path: Path) -> None:
        toml = (
            self.PROVIDER_TOML
            + '[egress]\nacknowledged_provider_sets = [["anthropic", "openai"]]\n'
        )
        prepared = prepare(tmp_path, toml, overrides=RunOverrides(no_save=True))
        assert prepared.spec.egress_acknowledged_by == ACK_BY_CONFIG

    def test_no_acknowledgement(self, tmp_path: Path) -> None:
        prepared = prepare(tmp_path, self.PROVIDER_TOML, overrides=RunOverrides(no_save=True))
        assert prepared.spec.egress_acknowledged_by is None

    def test_unlabelled_agent_gets_the_custom_fallback_identity(self, tmp_path: Path) -> None:
        """An agent with no declared provider still has an egress identity on the
        direct-run path — the same ``custom:<name>`` a workflow step would use.
        Without this, the run's own RunResult recorded no EgressRecord at all."""
        toml = '[agents.mock]\ncommand = "/usr/bin/true"\n'
        prepared = prepare(tmp_path, toml, overrides=RunOverrides(no_save=True))
        assert prepared.spec.provider == "custom:mock"
        assert prepared.spec.egress_acknowledged_by is None

    def test_unlabelled_agent_identity_is_acknowledgeable(self, tmp_path: Path) -> None:
        """The fallback identity is the string the user acknowledges, by flag or
        config — it is a real identity, not a placeholder."""
        toml = (
            '[agents.mock]\ncommand = "/usr/bin/true"\n\n'
            '[egress]\nacknowledged_provider_sets = [["custom:mock"]]\n'
        )
        by_config = prepare(tmp_path, toml, overrides=RunOverrides(no_save=True))
        assert by_config.spec.egress_acknowledged_by == ACK_BY_CONFIG

        by_flag = prepare(
            tmp_path,
            '[agents.mock]\ncommand = "/usr/bin/true"\n',
            overrides=RunOverrides(no_save=True, acknowledge_egress=["custom:mock"]),
        )
        assert by_flag.spec.egress_acknowledged_by == ACK_BY_FLAG


# ---------------------------------------------------------------------- FIX #21
# execute_step must never disarm its finally-teardown before the cancel ladder
# runs: if the ladder raises/cancels at a pre-signal await, client.shutdown(0.0)
# (idempotent) must still fire so the agent process group is never leaked.


class _FakeClient:
    """Minimal AgentProcessClient stand-in whose cancel() misbehaves."""

    def __init__(self, cancel_exc: BaseException) -> None:
        self._cancel_exc = cancel_exc
        self.shutdown_calls = 0
        self.pid = 4242
        self.pgid = 4242

    async def initialize(self) -> HandshakeInfo:
        return HandshakeInfo(
            protocol_version=1,
            agent_name="fake",
            agent_version="0",
            agent_title=None,
            capabilities={},
            auth_methods=[],
        )

    async def new_session(self, cwd: str) -> str:
        return "sess-1"

    async def prompt(self, session_id: str, text: str) -> StopInfo:
        await asyncio.Event().wait()  # never completes: forces the cancel path
        return StopInfo(stop_reason="end_turn")  # pragma: no cover

    async def cancel(self, session_id: str) -> None:
        raise self._cancel_exc

    async def shutdown(self, grace_seconds: float) -> int | None:
        self.shutdown_calls += 1
        return 0


def _step_ctx(cancel_event: asyncio.Event) -> StepExecutionContext:
    recorder = RunRecorder(
        run_id="01J000000000000000000000AA",
        store_writer=None,
        redactor=Redactor(),
        capture_profile=CaptureProfile.STANDARD,
    )
    return StepExecutionContext(
        step_id="main",
        command="/bin/true",
        args=[],
        env={},
        cwd="/tmp",
        prompt="go",
        timeout_seconds=30.0,
        grace_seconds=0.05,
        recorder=recorder,
        session_label="fake",
        cancel_event=cancel_event,
    )


class TestCancelLadderTeardown:
    async def test_non_connection_exception_in_cancel_still_shuts_down(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeClient(RuntimeError("boom"))

        async def fake_launch(**kwargs: Any) -> _FakeClient:
            return fake

        monkeypatch.setattr(runner_module.AgentProcessClient, "launch", fake_launch)
        cancel_event = asyncio.Event()
        cancel_event.set()

        outcome = await execute_step(_step_ctx(cancel_event))

        # The bounded rung-1 notify swallows the exception and the ladder still
        # reaches teardown; the run ends cancelled and shutdown ran.
        assert outcome.status is StepStatus.CANCELLED
        assert fake.shutdown_calls >= 1

    async def test_cancelled_error_in_cancel_still_shuts_down(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeClient(asyncio.CancelledError())

        async def fake_launch(**kwargs: Any) -> _FakeClient:
            return fake

        monkeypatch.setattr(runner_module.AgentProcessClient, "launch", fake_launch)
        cancel_event = asyncio.Event()
        cancel_event.set()

        # A CancelledError is BaseException, not suppressed by the rung-1 guard,
        # so it propagates out of the ladder before its internal shutdown — the
        # finally guard (shutdown_done still False) must fire client.shutdown.
        with pytest.raises(asyncio.CancelledError):
            await execute_step(_step_ctx(cancel_event))
        assert fake.shutdown_calls >= 1, "finally-teardown was disarmed before the ladder"
