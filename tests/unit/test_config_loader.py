"""Unit tests for ziggy.config.loader (REQ-007 scopes, merge, provenance)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ziggy.config import MergeRule, load_config, merge_rule_for
from ziggy.errors import ConfigError
from ziggy.models.common import CaptureProfile, ConfigSource, ProjectAction

#: Fake Anthropic-style token that matches the builtin secret pattern.
FAKE_TOKEN = "sk-ant-" + "a" * 24


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def user_file(tmp_path: Path) -> Path:
    return tmp_path / "home" / "config.toml"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def project_file(workspace: Path) -> Path:
    return workspace / ".ziggy" / "config.toml"


class TestMergeRuleTable:
    def test_rule_classes(self) -> None:
        assert merge_rule_for("engine.max_prompt_bytes") is MergeRule.TIGHTEN_MIN
        # results.retention_days is a deletion window, NOT a security ceiling:
        # an untrusted project may not lower it (that destroys audit evidence
        # sooner), so it stays USER_ONLY like every other unlisted field.
        assert merge_rule_for("results.retention_days") is MergeRule.USER_ONLY
        assert merge_rule_for("results.capture") is MergeRule.TIGHTEN_CAPTURE
        assert merge_rule_for("permissions.project_denials") is MergeRule.PROJECT_DENIALS
        assert merge_rule_for("workflows.default_name") is MergeRule.PROJECT_OK
        # anything unlisted fails closed to USER_ONLY
        assert merge_rule_for("server.max_active_runs") is MergeRule.USER_ONLY
        assert merge_rule_for("engine.cancel_grace_seconds") is MergeRule.USER_ONLY
        assert merge_rule_for("results.persist") is MergeRule.USER_ONLY
        assert merge_rule_for("agents.claude.command") is MergeRule.USER_ONLY


class TestDefaultsAndScopes:
    def test_no_files_gives_defaults(self, user_file: Path) -> None:
        rc = load_config(None, user_path=user_file, env={})
        assert rc.config.schema_version == 1
        assert rc.config.engine.max_workflow_steps == 16
        assert rc.warnings == []
        assert len(rc.fingerprint) == 64
        entry = rc.provenance["engine.max_workflow_steps"]
        assert entry.source is ConfigSource.DEFAULT
        assert entry.project_action is ProjectAction.NONE

    def test_user_values_apply_with_user_source(self, user_file: Path) -> None:
        write(user_file, "schema_version = 1\n[engine]\nmax_prompt_bytes = 2048\n")
        rc = load_config(None, user_path=user_file, env={})
        assert rc.config.engine.max_prompt_bytes == 2048
        assert rc.provenance["engine.max_prompt_bytes"].source is ConfigSource.USER
        assert rc.provenance["engine.max_workflow_steps"].source is ConfigSource.DEFAULT

    def test_project_discovered_from_workspace(self, user_file: Path, workspace: Path) -> None:
        write(project_file(workspace), 'schema_version = 1\n[workflows]\ndefault_name = "ci"\n')
        rc = load_config(workspace, user_path=user_file, env={})
        assert rc.config.workflows.default_name == "ci"
        entry = rc.provenance["workflows.default_name"]
        assert entry.source is ConfigSource.PROJECT
        assert entry.project_action is ProjectAction.APPLIED

    def test_missing_schema_version_user(self, user_file: Path) -> None:
        write(user_file, "[engine]\nmax_prompt_bytes = 2048\n")
        with pytest.raises(ConfigError, match="schema_version"):
            load_config(None, user_path=user_file, env={})

    def test_missing_schema_version_project(self, user_file: Path, workspace: Path) -> None:
        write(project_file(workspace), '[workflows]\ndefault_name = "ci"\n')
        with pytest.raises(ConfigError, match="schema_version"):
            load_config(workspace, user_path=user_file, env={})

    def test_wrong_schema_version(self, user_file: Path) -> None:
        write(user_file, "schema_version = 2\n")
        with pytest.raises(ConfigError, match="schema_version"):
            load_config(None, user_path=user_file, env={})


class TestZiggyHome:
    def test_user_config_found_under_ziggy_home(self, tmp_path: Path) -> None:
        home = tmp_path / "zh"
        write(home / "config.toml", "schema_version = 1\n[engine]\nmax_workflow_steps = 4\n")
        rc = load_config(None, env={"ZIGGY_HOME": str(home)})
        assert rc.config.engine.max_workflow_steps == 4
        assert rc.provenance["engine.max_workflow_steps"].source is ConfigSource.USER

    def test_ziggy_home_is_not_an_override_var(self, tmp_path: Path) -> None:
        # ZIGGY_HOME has no '__' and must never be parsed as ZIGGY_<SECTION>__<KEY>.
        rc = load_config(None, env={"ZIGGY_HOME": str(tmp_path / "nowhere")})
        assert rc.config.schema_version == 1


class TestParseErrors:
    def test_user_toml_parse_error(self, user_file: Path) -> None:
        write(user_file, "schema_version = [broken\n")
        with pytest.raises(ConfigError, match=str(user_file)):
            load_config(None, user_path=user_file, env={})

    def test_project_toml_parse_error(self, user_file: Path, workspace: Path) -> None:
        pf = write(project_file(workspace), "= nope")
        with pytest.raises(ConfigError, match=str(pf)):
            load_config(workspace, user_path=user_file, env={})


class TestUnknownKeys:
    def test_user_unknown_key_has_path_and_file(self, user_file: Path) -> None:
        write(user_file, "schema_version = 1\n[engine]\nbogus = 1\n")
        with pytest.raises(ConfigError) as exc:
            load_config(None, user_path=user_file, env={})
        assert "engine.bogus" in str(exc.value)
        assert str(user_file) in str(exc.value)

    def test_project_unknown_key_has_path_and_file(self, user_file: Path, workspace: Path) -> None:
        pf = write(project_file(workspace), 'schema_version = 1\n[workflows]\ndefalt_name = "x"\n')
        with pytest.raises(ConfigError) as exc:
            load_config(workspace, user_path=user_file, env={})
        assert "workflows.defalt_name" in str(exc.value)
        assert "unknown" in str(exc.value)
        assert str(pf) in str(exc.value)


class TestEnvOverrides:
    def test_coercion_and_precedence_over_user_file(self, user_file: Path) -> None:
        write(user_file, "schema_version = 1\n[engine]\nmax_prompt_bytes = 2048\n")
        env = {
            "ZIGGY_ENGINE__MAX_PROMPT_BYTES": "1024",
            "ZIGGY_ENGINE__CANCEL_GRACE_SECONDS": "2.5",
            "ZIGGY_RESULTS__AUTO_PRUNE": "yes",
            "ZIGGY_RESULTS__CAPTURE": "debug",
            "ZIGGY_RESULTS__STORE_PATH": "/tmp/elsewhere",
            "ZIGGY_WORKFLOWS__DEFAULT_NAME": "nightly",
        }
        rc = load_config(None, user_path=user_file, env=env)
        assert rc.config.engine.max_prompt_bytes == 1024
        assert rc.config.engine.cancel_grace_seconds == 2.5
        assert rc.config.results.auto_prune is True
        assert rc.config.results.capture is CaptureProfile.DEBUG
        assert rc.config.results.store_path == "/tmp/elsewhere"
        assert rc.config.workflows.default_name == "nightly"
        assert rc.provenance["engine.max_prompt_bytes"].source is ConfigSource.ENV
        assert rc.provenance["workflows.default_name"].source is ConfigSource.ENV

    def test_bool_false_forms(self, user_file: Path) -> None:
        rc = load_config(None, user_path=user_file, env={"ZIGGY_RESULTS__PERSIST": "0"})
        assert rc.config.results.persist is False

    @pytest.mark.parametrize(
        ("var", "value", "fragment"),
        [
            ("ZIGGY_REDACTION__EXTRA_VALUE_ENV_VARS", "A,B", "list/table"),
            ("ZIGGY_PERMISSIONS__PROJECT_DENIALS", "[]", "list/table"),
            ("ZIGGY_PERMISSIONS__PROFILES", "{}", "list/table"),
            ("ZIGGY_AGENTS__CLAUDE", "x", "environment overrides"),
            ("ZIGGY_BOGUS__KEY", "1", "unknown config section"),
            ("ZIGGY_ENGINE__NOPE", "1", "unknown config key"),
            ("ZIGGY_ENGINE__MAX_PROMPT_BYTES", "abc", "integer"),
            ("ZIGGY_RESULTS__AUTO_PRUNE", "maybe", "boolean"),
            ("ZIGGY_RESULTS__CAPTURE", "verbose", "invalid value"),
        ],
    )
    def test_rejections(self, user_file: Path, var: str, value: str, fragment: str) -> None:
        with pytest.raises(ConfigError) as exc:
            load_config(None, user_path=user_file, env={var: value})
        assert var in str(exc.value)
        assert fragment in str(exc.value)


class TestUserOnlyRejection:
    def test_all_violations_collected_into_one_error(
        self, user_file: Path, workspace: Path
    ) -> None:
        pf = write(
            project_file(workspace),
            "schema_version = 1\n"
            '[agents.evil]\ncommand = "/bin/sh"\n'
            "[server]\nmax_active_runs = 5\n"
            "[engine]\ndefault_step_timeout_seconds = 60\n",  # legal tighten, still rejected run
        )
        with pytest.raises(ConfigError) as exc:
            load_config(workspace, user_path=user_file, env={})
        message = str(exc.value)
        assert "agents.evil.command" in message
        assert "server.max_active_runs" in message
        assert "forbidden in project scope" in message
        assert str(pf) in message

    @pytest.mark.parametrize(
        "body",
        [
            '[permissions]\ndefault_policy = "guarded"\n',
            "[permissions.profiles.loose]\ndeny_paths = []\n",
            "[results]\npersist = false\n",
            '[results]\nstore_path = "/tmp/x"\n',
            "[results]\nauto_prune = true\n",
            # Deletion window: a project may NOT lower it (would shrink the
            # audit-retention window and delete history sooner). USER_ONLY.
            "[results]\nretention_days = 1\n",
            "[engine]\ncancel_grace_seconds = 1.0\n",
            "[logs]\nretention_days = 1\n",
            '[redaction]\nextra_value_env_vars = ["X"]\n',
            '[egress]\nacknowledged_provider_sets = [["a", "b"]]\n',
            '[orchestrator]\nagent = "claude"\n',
            "[server]\nmax_active_runs = 2\n",
        ],
    )
    def test_user_only_sections_rejected(self, user_file: Path, workspace: Path, body: str) -> None:
        write(project_file(workspace), "schema_version = 1\n" + body)
        with pytest.raises(ConfigError, match="forbidden in project scope"):
            load_config(workspace, user_path=user_file, env={})

    def test_project_retention_days_rejected_as_user_only(
        self, user_file: Path, workspace: Path
    ) -> None:
        """FIX #12: even LOWERING retention_days from project scope is rejected
        (it is not a security ceiling; a smaller window destroys audit evidence
        sooner). The user value is untouched."""
        write(user_file, "schema_version = 1\n[results]\nretention_days = 30\n")
        write(project_file(workspace), "schema_version = 1\n[results]\nretention_days = 1\n")
        with pytest.raises(ConfigError) as exc:
            load_config(workspace, user_path=user_file, env={})
        message = str(exc.value)
        assert "results.retention_days" in message
        assert "forbidden in project scope" in message
        # A raise attempt is likewise rejected as USER_ONLY (never a ceiling).
        write(project_file(workspace), "schema_version = 1\n[results]\nretention_days = 3650\n")
        with pytest.raises(ConfigError, match=r"results\.retention_days"):
            load_config(workspace, user_path=user_file, env={})

    def test_retention_days_must_be_at_least_one(self, user_file: Path) -> None:
        """FIX #12: ``ge=1`` forbids a zero/negative retention window."""
        write(user_file, "schema_version = 1\n[results]\nretention_days = 0\n")
        with pytest.raises(ConfigError, match=r"results\.retention_days"):
            load_config(None, user_path=user_file, env={})

    def test_unknown_and_forbidden_collected_together(
        self, user_file: Path, workspace: Path
    ) -> None:
        write(
            project_file(workspace),
            "schema_version = 1\n[server]\nmax_active_runs = 2\n[workflows]\ntypo_key = 1\n",
        )
        with pytest.raises(ConfigError) as exc:
            load_config(workspace, user_path=user_file, env={})
        message = str(exc.value)
        assert "server.max_active_runs" in message
        assert "workflows.typo_key" in message


class TestTightenMin:
    def test_lower_applies_as_tightened(self, user_file: Path, workspace: Path) -> None:
        write(project_file(workspace), "schema_version = 1\n[engine]\nmax_prompt_bytes = 512\n")
        rc = load_config(workspace, user_path=user_file, env={})
        assert rc.config.engine.max_prompt_bytes == 512
        entry = rc.provenance["engine.max_prompt_bytes"]
        assert entry.source is ConfigSource.PROJECT
        assert entry.project_action is ProjectAction.TIGHTENED

    def test_equal_is_ignored(self, user_file: Path, workspace: Path) -> None:
        write(user_file, "schema_version = 1\n[engine]\nmax_prompt_bytes = 2048\n")
        write(project_file(workspace), "schema_version = 1\n[engine]\nmax_prompt_bytes = 2048\n")
        rc = load_config(workspace, user_path=user_file, env={})
        assert rc.config.engine.max_prompt_bytes == 2048
        entry = rc.provenance["engine.max_prompt_bytes"]
        assert entry.source is ConfigSource.USER
        assert entry.project_action is ProjectAction.IGNORED

    def test_raise_attempt_fails_closed(self, user_file: Path, workspace: Path) -> None:
        write(user_file, "schema_version = 1\n[engine]\nmax_prompt_bytes = 2048\n")
        write(project_file(workspace), "schema_version = 1\n[engine]\nmax_prompt_bytes = 999999\n")
        with pytest.raises(ConfigError) as exc:
            load_config(workspace, user_path=user_file, env={})
        assert "engine.max_prompt_bytes" in str(exc.value)
        assert "ceiling" in str(exc.value)

    def test_multiple_raise_attempts_collected(self, user_file: Path, workspace: Path) -> None:
        # Two TIGHTEN_MIN ceilings the project tries to RAISE: both violations
        # are collected into one ConfigError (retention_days is no longer a
        # ceiling — it is USER_ONLY — so it cannot appear here).
        write(
            project_file(workspace),
            "schema_version = 1\n[engine]\nmax_workflow_steps = 999\nmax_prompt_bytes = 999999\n",
        )
        with pytest.raises(ConfigError) as exc:
            load_config(workspace, user_path=user_file, env={})
        message = str(exc.value)
        assert "engine.max_workflow_steps" in message
        assert "engine.max_prompt_bytes" in message

    def test_tighten_applies_against_env_ceiling(self, user_file: Path, workspace: Path) -> None:
        write(project_file(workspace), "schema_version = 1\n[engine]\nmax_prompt_bytes = 4096\n")
        env = {"ZIGGY_ENGINE__MAX_PROMPT_BYTES": "1024"}
        with pytest.raises(ConfigError, match="ceiling"):
            load_config(workspace, user_path=user_file, env=env)


class TestTightenCapture:
    def test_toward_less_capture_tightens(self, user_file: Path, workspace: Path) -> None:
        write(project_file(workspace), 'schema_version = 1\n[results]\ncapture = "metadata"\n')
        rc = load_config(workspace, user_path=user_file, env={})
        assert rc.config.results.capture is CaptureProfile.METADATA
        entry = rc.provenance["results.capture"]
        assert entry.source is ConfigSource.PROJECT
        assert entry.project_action is ProjectAction.TIGHTENED

    def test_debug_request_rejected_not_fatal(self, user_file: Path, workspace: Path) -> None:
        write(project_file(workspace), 'schema_version = 1\n[results]\ncapture = "debug"\n')
        rc = load_config(workspace, user_path=user_file, env={})
        assert rc.config.results.capture is CaptureProfile.STANDARD
        entry = rc.provenance["results.capture"]
        assert entry.project_action is ProjectAction.REJECTED
        assert entry.source is ConfigSource.DEFAULT
        assert any("results.capture" in w for w in rc.warnings)

    def test_metadata_to_standard_rejected(self, user_file: Path, workspace: Path) -> None:
        write(user_file, 'schema_version = 1\n[results]\ncapture = "metadata"\n')
        write(project_file(workspace), 'schema_version = 1\n[results]\ncapture = "standard"\n')
        rc = load_config(workspace, user_path=user_file, env={})
        assert rc.config.results.capture is CaptureProfile.METADATA
        assert rc.provenance["results.capture"].project_action is ProjectAction.REJECTED

    def test_equal_capture_ignored(self, user_file: Path, workspace: Path) -> None:
        write(project_file(workspace), 'schema_version = 1\n[results]\ncapture = "standard"\n')
        rc = load_config(workspace, user_path=user_file, env={})
        assert rc.provenance["results.capture"].project_action is ProjectAction.IGNORED


class TestProjectDenials:
    def test_project_denials_accepted(self, user_file: Path, workspace: Path) -> None:
        write(
            project_file(workspace),
            "schema_version = 1\n"
            "[[permissions.project_denials]]\n"
            'kind = "path"\npattern = "**/prod/**"\n'
            "[[permissions.project_denials]]\n"
            'kind = "terminal"\npattern = "rm"\n',
        )
        rc = load_config(workspace, user_path=user_file, env={})
        denials = rc.config.permissions.project_denials
        assert [(d.kind, d.pattern) for d in denials] == [
            ("path", "**/prod/**"),
            ("terminal", "rm"),
        ]
        entry = rc.provenance["permissions.project_denials"]
        assert entry.source is ConfigSource.PROJECT
        assert entry.project_action is ProjectAction.APPLIED

    def test_project_denials_forbidden_in_user_scope(self, user_file: Path) -> None:
        write(
            user_file,
            "schema_version = 1\n[[permissions.project_denials]]\n"
            'kind = "path"\npattern = "**/x/**"\n',
        )
        with pytest.raises(ConfigError, match="project_denials"):
            load_config(None, user_path=user_file, env={})

    def test_other_permissions_keys_forbidden_in_project(
        self, user_file: Path, workspace: Path
    ) -> None:
        write(
            project_file(workspace),
            "schema_version = 1\n[permissions]\n"
            'default_policy = "guarded"\n'
            "[[permissions.project_denials]]\n"
            'kind = "path"\npattern = "**/x/**"\n',
        )
        with pytest.raises(ConfigError, match=r"permissions\.default_policy"):
            load_config(workspace, user_path=user_file, env={})


class TestSecretLiterals:
    def test_user_scope_secret_rejected_without_echo(self, user_file: Path) -> None:
        write(
            user_file,
            f'schema_version = 1\n[agents.claude]\nenv = {{ TOKEN = "{FAKE_TOKEN}" }}\n',
        )
        with pytest.raises(ConfigError) as exc:
            load_config(None, user_path=user_file, env={})
        message = str(exc.value)
        assert "agents.claude.env.TOKEN" in message
        assert FAKE_TOKEN not in message

    def test_project_scope_secret_rejected(self, user_file: Path, workspace: Path) -> None:
        pf = write(
            project_file(workspace),
            f'schema_version = 1\n[workflows]\ndefault_name = "{FAKE_TOKEN}"\n',
        )
        with pytest.raises(ConfigError) as exc:
            load_config(workspace, user_path=user_file, env={})
        message = str(exc.value)
        assert "workflows.default_name" in message
        assert str(pf) in message
        assert FAKE_TOKEN not in message

    def test_env_override_secret_rejected(self, user_file: Path) -> None:
        env = {"ZIGGY_WORKFLOWS__DEFAULT_NAME": FAKE_TOKEN}
        with pytest.raises(ConfigError) as exc:
            load_config(None, user_path=user_file, env=env)
        message = str(exc.value)
        assert "ZIGGY_WORKFLOWS__DEFAULT_NAME" in message
        assert FAKE_TOKEN not in message


class TestAgentValidation:
    def test_builtins_may_omit_command(self, user_file: Path) -> None:
        write(
            user_file,
            "schema_version = 1\n"
            '[agents.claude]\napi_key_env = "ANTHROPIC_API_KEY"\n'
            "[agents.codex]\norchestration_eligible = true\n",
        )
        rc = load_config(None, user_path=user_file, env={})
        assert rc.config.agents["claude"].command is None
        assert rc.config.agents["codex"].command is None

    def test_custom_agent_requires_command(self, user_file: Path) -> None:
        write(user_file, 'schema_version = 1\n[agents.helper]\napi_key_env = "HELPER_KEY"\n')
        with pytest.raises(ConfigError, match=r"agents\.helper"):
            load_config(None, user_path=user_file, env={})

    def test_custom_agent_with_command_ok(self, user_file: Path) -> None:
        write(user_file, 'schema_version = 1\n[agents.helper]\ncommand = "/opt/helper"\n')
        rc = load_config(None, user_path=user_file, env={})
        assert rc.config.agents["helper"].command == "/opt/helper"

    def test_api_key_env_must_be_env_var_name(self, user_file: Path) -> None:
        write(user_file, 'schema_version = 1\n[agents.claude]\napi_key_env = "not-a-name"\n')
        with pytest.raises(ConfigError) as exc:
            load_config(None, user_path=user_file, env={})
        message = str(exc.value)
        assert "agents.claude.api_key_env" in message
        assert "not-a-name" not in message  # value never echoed

    def test_unknown_orchestrator_agent_rejected(self, user_file: Path) -> None:
        write(user_file, 'schema_version = 1\n[orchestrator]\nagent = "ghost"\n')
        with pytest.raises(ConfigError, match=r"orchestrator\.agent"):
            load_config(None, user_path=user_file, env={})


class TestCustomRedactionPatterns:
    def test_invalid_regex_wrapped_as_config_error(self, user_file: Path) -> None:
        write(
            user_file,
            'schema_version = 1\n[[redaction.patterns]]\nkind = "x"\nregex = "("\n',
        )
        with pytest.raises(ConfigError, match=r"redaction\.patterns"):
            load_config(None, user_path=user_file, env={})

    def test_bad_max_width_wrapped_as_config_error(self, user_file: Path) -> None:
        write(
            user_file,
            'schema_version = 1\n[[redaction.patterns]]\nkind = "x"\nregex = "ab"\nmax_width = 0\n',
        )
        with pytest.raises(ConfigError, match=r"redaction\.patterns"):
            load_config(None, user_path=user_file, env={})

    def test_valid_pattern_accepted(self, user_file: Path) -> None:
        write(
            user_file,
            'schema_version = 1\n[[redaction.patterns]]\nkind = "x"\nregex = "tok-[a-z]{4}"\n'
            "max_width = 12\n",
        )
        rc = load_config(None, user_path=user_file, env={})
        assert rc.config.redaction.patterns[0].kind == "x"


class TestProvenanceAndFingerprint:
    def test_source_chain_default_user_env_project(self, user_file: Path, workspace: Path) -> None:
        write(
            user_file,
            "schema_version = 1\n[engine]\nmax_prompt_bytes = 4096\n"
            "default_step_timeout_seconds = 900\n",
        )
        write(
            project_file(workspace),
            "schema_version = 1\n[engine]\ndefault_step_timeout_seconds = 300\n",
        )
        env = {"ZIGGY_ENGINE__MAX_PROMPT_BYTES": "2048"}
        rc = load_config(workspace, user_path=user_file, env=env)
        assert rc.provenance["engine.max_workflow_steps"].source is ConfigSource.DEFAULT
        assert rc.provenance["schema_version"].source is ConfigSource.USER
        assert rc.provenance["engine.max_prompt_bytes"].source is ConfigSource.ENV
        assert rc.provenance["engine.default_step_timeout_seconds"].source is ConfigSource.PROJECT
        assert rc.config.engine.max_prompt_bytes == 2048
        assert rc.config.engine.default_step_timeout_seconds == 300

    def test_fingerprint_stable_for_same_input(self, user_file: Path, workspace: Path) -> None:
        write(user_file, "schema_version = 1\n[engine]\nmax_prompt_bytes = 4096\n")
        write(project_file(workspace), 'schema_version = 1\n[workflows]\ndefault_name = "a"\n')
        first = load_config(workspace, user_path=user_file, env={})
        second = load_config(workspace, user_path=user_file, env={})
        assert first.fingerprint == second.fingerprint

    def test_fingerprint_changes_with_project_change(
        self, user_file: Path, workspace: Path
    ) -> None:
        write(user_file, "schema_version = 1\n")
        write(project_file(workspace), 'schema_version = 1\n[workflows]\ndefault_name = "a"\n')
        first = load_config(workspace, user_path=user_file, env={})
        write(project_file(workspace), 'schema_version = 1\n[workflows]\ndefault_name = "b"\n')
        second = load_config(workspace, user_path=user_file, env={})
        assert first.fingerprint != second.fingerprint

    def test_ignored_project_value_still_changes_fingerprint(
        self, user_file: Path, workspace: Path
    ) -> None:
        write(user_file, "schema_version = 1\n[engine]\nmax_prompt_bytes = 2048\n")
        without_project = load_config(None, user_path=user_file, env={})
        write(project_file(workspace), "schema_version = 1\n[engine]\nmax_prompt_bytes = 2048\n")
        with_project = load_config(workspace, user_path=user_file, env={})
        assert without_project.fingerprint != with_project.fingerprint

    def test_describe_rows(self, user_file: Path, workspace: Path) -> None:
        write(
            project_file(workspace),
            "schema_version = 1\n[engine]\nmax_prompt_bytes = 512\n",
        )
        rc = load_config(workspace, user_path=user_file, env={})
        rows = rc.describe()
        assert all(set(row) == {"path", "value", "source", "project_action"} for row in rows)
        row = next(r for r in rows if r["path"] == "engine.max_prompt_bytes")
        assert row == {
            "path": "engine.max_prompt_bytes",
            "value": 512,
            "source": "project",
            "project_action": "tightened",
        }
