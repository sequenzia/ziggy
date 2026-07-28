"""CLI surface tests (REQ-003/015): the typer app against mock agents
registered in a tmp ``ZIGGY_HOME`` user config.

The typer CliRunner keeps stdout and stderr separate, so every test can
assert the ``--json`` contract directly: stdout is the machine-readable
document only, progress/diagnostics land on stderr.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.mocks import RAW_AGENT_PATH, scenarios  # noqa: E402

from ziggy.cli.main import app, exit_code_for  # noqa: E402
from ziggy.errors import TypedError  # noqa: E402
from ziggy.ids import new_run_id, utc_now  # noqa: E402
from ziggy.models.common import CaptureProfile, RunKind, RunStatus, StepStatus  # noqa: E402
from ziggy.models.result import RunResult, StepResult  # noqa: E402

runner = CliRunner()


def agent_toml(name: str, scenario: str) -> str:
    return (
        f"[agents.{name}]\n"
        f"command = {json.dumps(sys.executable)}\n"
        f"args = [{json.dumps(str(RAW_AGENT_PATH))}, {json.dumps(scenario)}]\n"
    )


BASE_CONFIG = (
    "schema_version = 1\n\n"
    "[engine]\ndefault_step_timeout_seconds = 15\n\n"
    + agent_toml("mock-hello", scenarios.HELLO)
    + agent_toml("mock-crash", scenarios.CRASH_MID_TURN)
)


@pytest.fixture()
def cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Tmp ZIGGY_HOME with mock agents registered; cwd moved to a tmp workspace."""
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (home / "config.toml").write_text(BASE_CONFIG, encoding="utf-8")
    monkeypatch.setenv("ZIGGY_HOME", str(home))
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.chdir(workspace)
    return home, workspace


def seed_manifest(home: Path, *, days_old: int, status: str = "success") -> str:
    """Write a minimal durable manifest so prune/list have a completed run."""
    run_id = new_run_id()
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    ts = (utc_now() - timedelta(days=days_old)).isoformat().replace("+00:00", "Z")
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "kind": "agent",
        "target": "mock-hello",
        "status": status,
        "started_at": ts,
        "ended_at": ts,
        "duration_ms": 5,
        "workspace": "ws",
    }
    (run_dir / "result.json").write_text(json.dumps(manifest), encoding="utf-8")
    return run_id


# ------------------------------------------------------------------ ziggy run


@pytest.mark.slow
def test_run_json_contract(cli_home: tuple[Path, Path]) -> None:
    home, _workspace = cli_home
    result = runner.invoke(app, ["run", "mock-hello", "hi there", "--json"])
    assert result.exit_code == 0, result.stderr
    # stdout is ONLY the final RunResult JSON
    run_result = RunResult.model_validate_json(result.stdout)
    assert run_result.status is RunStatus.SUCCESS
    assert run_result.steps["main"].outputs["text"] == "".join(scenarios.HELLO_CHUNKS)
    # progress went to stderr, including the end-of-run summary
    assert "status: success" in result.stderr
    assert "[run]" in result.stderr
    # the run persisted into the tmp ZIGGY_HOME store
    assert run_result.result_path is not None
    assert (home / "runs" / run_result.run_id / "result.json").is_file()


@pytest.mark.slow
def test_run_human_output_plain_stream_and_summary(cli_home: tuple[Path, Path]) -> None:
    result = runner.invoke(app, ["run", "mock-hello", "hi"])
    assert result.exit_code == 0, result.stderr
    assert "".join(scenarios.HELLO_CHUNKS) in result.stdout
    assert "status: success" in result.stdout
    assert "result:" in result.stdout
    assert "\x1b[" not in result.stdout  # non-TTY defaults to plain: no ANSI


@pytest.mark.slow
def test_run_failing_scenario_exit_1(cli_home: tuple[Path, Path]) -> None:
    result = runner.invoke(app, ["run", "mock-crash", "go", "--json"])
    assert result.exit_code == 1, result.stderr
    run_result = RunResult.model_validate_json(result.stdout)
    assert run_result.status is RunStatus.FAILED
    assert run_result.steps["main"].errors


def test_run_unknown_agent_exit_2(cli_home: tuple[Path, Path]) -> None:
    result = runner.invoke(app, ["run", "no-such-agent", "hi", "--json"])
    assert result.exit_code == 2
    assert "unknown agent" in result.stderr
    assert result.stdout == ""


@pytest.mark.slow
def test_run_no_save_leaves_no_run_dir(cli_home: tuple[Path, Path]) -> None:
    home, _workspace = cli_home
    result = runner.invoke(app, ["run", "mock-hello", "hi", "--no-save", "--json"])
    assert result.exit_code == 0, result.stderr
    run_result = RunResult.model_validate_json(result.stdout)
    assert run_result.persisted is False
    assert run_result.result_path is None
    assert not (home / "runs").exists()


@pytest.mark.slow
def test_run_capture_flag_overrides_config(cli_home: tuple[Path, Path]) -> None:
    result = runner.invoke(app, ["run", "mock-hello", "hi", "--json", "--capture", "debug"])
    assert result.exit_code == 0, result.stderr
    run_result = RunResult.model_validate_json(result.stdout)
    assert run_result.capture_profile is CaptureProfile.DEBUG


def test_missing_arguments_usage_error_exit_2(cli_home: tuple[Path, Path]) -> None:
    assert runner.invoke(app, ["run", "mock-hello"]).exit_code == 2
    assert runner.invoke(app, ["no-such-command"]).exit_code == 2


# ------------------------------------------------------------------ runs ...


@pytest.mark.slow
def test_runs_list_show_reindex_roundtrip(cli_home: tuple[Path, Path]) -> None:
    home, _workspace = cli_home
    ran = runner.invoke(app, ["run", "mock-hello", "hi", "--json"])
    assert ran.exit_code == 0, ran.stderr
    run_id = RunResult.model_validate_json(ran.stdout).run_id

    listed = runner.invoke(app, ["runs", "list", "--json"])
    assert listed.exit_code == 0, listed.stderr
    rows = json.loads(listed.stdout)
    assert [row["run_id"] for row in rows] == [run_id]
    assert rows[0]["status"] == "success"
    assert rows[0]["kind"] == "agent"

    # filters
    failed_rows = json.loads(runner.invoke(app, ["runs", "list", "--failed", "--json"]).stdout)
    assert failed_rows == []
    agent_rows = json.loads(
        runner.invoke(app, ["runs", "list", "--agent", "mock-hello", "--json"]).stdout
    )
    assert [row["run_id"] for row in agent_rows] == [run_id]
    other_rows = json.loads(
        runner.invoke(app, ["runs", "list", "--agent", "other", "--json"]).stdout
    )
    assert other_rows == []
    since_rows = json.loads(runner.invoke(app, ["runs", "list", "--since", "1d", "--json"]).stdout)
    assert [row["run_id"] for row in since_rows] == [run_id]

    shown = runner.invoke(app, ["runs", "show", run_id])
    assert shown.exit_code == 0, shown.stderr
    assert run_id in shown.stdout
    assert "status: success" in shown.stdout
    assert "transcript" in shown.stdout  # capture completeness section
    assert "truncation: none" in shown.stdout

    shown_json = runner.invoke(app, ["runs", "show", run_id, "--json"])
    assert shown_json.exit_code == 0
    manifest = json.loads(shown_json.stdout)
    assert manifest["run_id"] == run_id
    assert manifest["config_fingerprint"]

    # destroy the derived index, rebuild it, and the run is listed again
    for suffix in ("", "-wal", "-shm"):
        (home / "runs" / f"index.db{suffix}").unlink(missing_ok=True)
    reindexed = runner.invoke(app, ["runs", "reindex"])
    assert reindexed.exit_code == 0, reindexed.stderr
    assert "indexed 1 run(s)" in reindexed.stdout
    rows_after = json.loads(runner.invoke(app, ["runs", "list", "--json"]).stdout)
    assert [row["run_id"] for row in rows_after] == [run_id]


def test_runs_list_bad_since_exit_2(cli_home: tuple[Path, Path]) -> None:
    result = runner.invoke(app, ["runs", "list", "--since", "yesterday-ish"])
    assert result.exit_code == 2


def test_runs_show_missing_run_exit_1(cli_home: tuple[Path, Path]) -> None:
    result = runner.invoke(app, ["runs", "show", new_run_id()])
    assert result.exit_code == 1
    assert "result manifest missing" in result.stderr


# ------------------------------------------------------------------ prune


def test_runs_prune_flow(cli_home: tuple[Path, Path], tmp_path: Path) -> None:
    home, _workspace = cli_home
    old_id = seed_manifest(home, days_old=40)
    recent_id = seed_manifest(home, days_old=1)
    # a symlinked "run dir" pointing outside the store must never be followed
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_text("keep me", encoding="utf-8")
    link_id = new_run_id()
    (home / "runs" / link_id).symlink_to(outside)

    # dry run: lists the expired id, deletes nothing
    dry = runner.invoke(app, ["runs", "prune", "--dry-run"])
    assert dry.exit_code == 0, dry.stderr
    assert old_id in dry.stdout
    assert recent_id not in dry.stdout
    assert link_id not in dry.stdout
    assert (home / "runs" / old_id).is_dir()

    # headless-safe: without --yes it lists the ids, hints, and exits 2
    refused = runner.invoke(app, ["runs", "prune"])
    assert refused.exit_code == 2
    assert old_id in refused.stdout
    assert "--yes" in refused.stderr
    assert (home / "runs" / old_id).is_dir()

    # --yes deletes only the expired run; the symlink target stays intact
    pruned = runner.invoke(app, ["runs", "prune", "--yes"])
    assert pruned.exit_code == 0, pruned.stderr
    assert old_id in pruned.stdout
    assert "deleted 1 run(s)" in pruned.stdout
    assert not (home / "runs" / old_id).exists()
    assert (home / "runs" / recent_id).is_dir()
    assert canary.read_text(encoding="utf-8") == "keep me"
    assert (home / "runs" / link_id).is_symlink()


def test_runs_prune_nothing_expired(cli_home: tuple[Path, Path]) -> None:
    home, _workspace = cli_home
    seed_manifest(home, days_old=1)
    result = runner.invoke(app, ["runs", "prune"])
    assert result.exit_code == 0
    assert "no completed runs older than 30 day(s)" in result.stdout


def test_runs_prune_older_than_override(cli_home: tuple[Path, Path]) -> None:
    home, _workspace = cli_home
    recent_id = seed_manifest(home, days_old=2)
    result = runner.invoke(app, ["runs", "prune", "--older-than", "1", "--yes"])
    assert result.exit_code == 0, result.stderr
    assert recent_id in result.stdout
    assert not (home / "runs" / recent_id).exists()


# ------------------------------------------------------------------ config


def test_config_show_provenance(cli_home: tuple[Path, Path]) -> None:
    _home, workspace = cli_home
    project_dir = workspace / ".ziggy"
    project_dir.mkdir()
    (project_dir / "config.toml").write_text(
        'schema_version = 1\n\n[results]\ncapture = "metadata"\n\n'
        '[workflows]\ndefault_name = "deploy"\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["config", "show", "--json"])
    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["fingerprint"]
    fields = {row["path"]: row for row in document["fields"]}
    assert fields["results.capture"]["value"] == "metadata"
    assert fields["results.capture"]["source"] == "project"
    assert fields["results.capture"]["project_action"] == "tightened"
    assert fields["workflows.default_name"]["value"] == "deploy"
    assert fields["workflows.default_name"]["source"] == "project"
    assert fields["workflows.default_name"]["project_action"] == "applied"
    assert fields["engine.default_step_timeout_seconds"]["source"] == "user"
    assert fields["engine.max_prompt_bytes"]["source"] == "default"

    human = runner.invoke(app, ["config", "show"])
    assert human.exit_code == 0
    assert "results.capture" in human.stdout
    assert "fingerprint:" in human.stdout


def test_config_validate_ok(cli_home: tuple[Path, Path]) -> None:
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 0
    assert "ok" in result.stdout


def test_config_validate_hostile_project_exit_2(cli_home: tuple[Path, Path]) -> None:
    _home, workspace = cli_home
    project_dir = workspace / ".ziggy"
    project_dir.mkdir()
    (project_dir / "config.toml").write_text(
        "schema_version = 1\n\n[server]\nmax_active_runs = 5\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 2
    assert "server.max_active_runs" in result.stderr
    assert result.stdout == ""


# ------------------------------------------------------------------ agents


@pytest.mark.slow
def test_agents_list_capability_snapshot(cli_home: tuple[Path, Path]) -> None:
    before = runner.invoke(app, ["agents", "list"])
    assert before.exit_code == 0, before.stderr
    for expected in ("claude", "codex", "mock-hello", "mock-crash"):
        assert expected in before.stdout
    assert "load_session" not in before.stdout  # no cached handshake snapshot yet

    ran = runner.invoke(app, ["run", "mock-hello", "hi", "--json"])
    assert ran.exit_code == 0, ran.stderr

    after = runner.invoke(app, ["agents", "list"])
    assert after.exit_code == 0, after.stderr
    hello_line = next(line for line in after.stdout.splitlines() if line.startswith("mock-hello"))
    assert "load_session" in hello_line  # snapshot from the most recent run
    assert os.environ.get("NO_COLOR") is None
    assert "\x1b[" not in after.stdout


# ------------------------------------------------------------- exit codes


def make_result(status: RunStatus, *, errors: list[TypedError] | None = None) -> RunResult:
    ts = utc_now().isoformat().replace("+00:00", "Z")
    return RunResult(
        run_id=new_run_id(),
        kind=RunKind.AGENT,
        target="mock-hello",
        status=status,
        started_at=ts,
        ended_at=ts,
        duration_ms=1,
        workspace="ws",
        steps={"main": StepResult(step_id="main", status=StepStatus.SUCCESS)},
        errors=errors or [],
    )


class TestExitCodeMapping:
    """REQ-003: 0 success, 1 execution/persistence failure, 2 config/trust,
    130 user cancellation — the terminal RunResult -> exit code mapping."""

    def test_success_is_0(self) -> None:
        assert exit_code_for(make_result(RunStatus.SUCCESS)) == 0

    @pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.PARTIAL, RunStatus.ABANDONED])
    def test_non_success_terminal_status_is_1(self, status: RunStatus) -> None:
        assert exit_code_for(make_result(status)) == 1

    def test_cancelled_is_130_even_with_errors(self) -> None:
        cancelled = make_result(
            RunStatus.CANCELLED,
            errors=[TypedError(code="CancelledError", message="user cancelled")],
        )
        assert exit_code_for(cancelled) == 130

    def test_persistence_failure_on_success_is_1(self) -> None:
        result = make_result(
            RunStatus.SUCCESS,
            errors=[TypedError(code="PersistenceError", message="store unavailable")],
        )
        assert exit_code_for(result) == 1

    def test_config_error_on_result_is_2(self) -> None:
        result = make_result(
            RunStatus.FAILED,
            errors=[TypedError(code="ConfigError", message="bad config")],
        )
        assert exit_code_for(result) == 2

    def test_unknown_error_code_falls_back_to_1(self) -> None:
        result = make_result(
            RunStatus.FAILED,
            errors=[TypedError(code="SomethingNovel", message="?")],
        )
        assert exit_code_for(result) == 1
