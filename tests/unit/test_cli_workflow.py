"""CLI surface tests for ``ziggy workflow run|list`` (REQ-009/010/011).

Mock agents from ``tests/mocks/raw_agent.py`` are registered in a tmp
``ZIGGY_HOME`` user config; workflows are written under the tmp workspace's
``.ziggy/workflows/``. The single end-to-end run here proves the CLI plumbing
(prepare -> execute_workflow -> render -> exit code -> persistence); deep
state-machine scenarios live in the integration suite.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.mocks import RAW_AGENT_PATH, scenarios  # noqa: E402

from ziggy.cli.main import app  # noqa: E402
from ziggy.models.common import RunKind, RunStatus, StepStatus  # noqa: E402
from ziggy.models.result import RunResult  # noqa: E402

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
    + '\n[agents.mock-a]\ncommand = "/usr/bin/true"\nprovider = "anthropic"\n\n'
    '[agents.mock-b]\ncommand = "/usr/bin/true"\nprovider = "openai"\n'
)

TWO_STEP_WF = """\
version: 1
name: two-step
description: Two mock steps threading text output.
steps:
  first:
    agent: mock-hello
    prompt: say hello
  second:
    agent: mock-hello
    inputs:
      prior: steps.first.outputs.text
    prompt: |
      continue from:
      {{ inputs.prior }}
"""

VARS_WF = """\
version: 1
name: vars-demo
variables:
  n:
    type: integer
    required: true
  data:
    type: json
    required: true
steps:
  only:
    agent: mock-hello
    prompt: |
      n={{ vars.n }} data={{ vars.data }}
"""

CROSSING_WF = """\
version: 1
name: cross-egress
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
"""


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


def write_project_workflow(workspace: Path, name: str, text: str) -> Path:
    return _write(workspace / ".ziggy" / "workflows", name, text)


def write_user_workflow(home: Path, name: str, text: str) -> Path:
    return _write(home / "workflows", name, text)


def _write(wf_dir: Path, name: str, text: str) -> Path:
    wf_dir.mkdir(parents=True, exist_ok=True)
    path = wf_dir / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------- workflow run


@pytest.mark.slow
def test_workflow_run_end_to_end_two_steps(cli_home: tuple[Path, Path]) -> None:
    home, workspace = cli_home
    write_project_workflow(workspace, "two-step", TWO_STEP_WF)
    result = runner.invoke(app, ["workflow", "run", "two-step", "--json"])
    assert result.exit_code == 0, result.stderr

    run_result = RunResult.model_validate_json(result.stdout)
    assert run_result.kind is RunKind.WORKFLOW
    assert run_result.target == "two-step"
    assert run_result.status is RunStatus.SUCCESS
    assert set(run_result.steps) == {"first", "second"}
    hello = "".join(scenarios.HELLO_CHUNKS)
    for step_id in ("first", "second"):
        assert run_result.steps[step_id].status is StepStatus.SUCCESS
        assert run_result.steps[step_id].outputs["text"] == hello
    # upstream text threaded into the downstream step, recorded as lineage
    assert run_result.steps["second"].input_sources == {"prior": "steps.first.outputs.text"}
    assert run_result.steps["second"].inputs_resolved["prior"] == hello

    # persisted like any run: durable manifest in the tmp store
    assert run_result.persisted is True
    assert (home / "runs" / run_result.run_id / "result.json").is_file()
    # progress went to stderr under --json
    assert "status: success" in result.stderr
    assert result.stdout.startswith("{")


@pytest.mark.slow
def test_workflow_run_var_parsing_integer_and_json(cli_home: tuple[Path, Path]) -> None:
    _home, workspace = cli_home
    write_project_workflow(workspace, "vars-demo", VARS_WF)
    result = runner.invoke(
        app,
        [
            "workflow",
            "run",
            "vars-demo",
            "--json",
            "--var",
            "n=5",
            "--var",
            'data={"k": [1, 2]}',
        ],
    )
    assert result.exit_code == 0, result.stderr
    run_result = RunResult.model_validate_json(result.stdout)
    prompt = run_result.steps["only"].inputs_resolved["prompt"]
    assert "n=5" in prompt
    assert '"k": [1, 2]' in prompt  # json round-trips through json.loads/dumps


def test_workflow_run_bad_integer_var_exit_2(cli_home: tuple[Path, Path]) -> None:
    _home, workspace = cli_home
    write_project_workflow(workspace, "vars-demo", VARS_WF)
    result = runner.invoke(
        app, ["workflow", "run", "vars-demo", "--var", "n=abc", "--var", "data=1"]
    )
    assert result.exit_code == 2
    assert "integer" in result.stderr


def test_workflow_run_malformed_var_exit_2(cli_home: tuple[Path, Path]) -> None:
    _home, workspace = cli_home
    write_project_workflow(workspace, "vars-demo", VARS_WF)
    result = runner.invoke(app, ["workflow", "run", "vars-demo", "--var", "oops"])
    assert result.exit_code == 2
    assert "expected <name>=<value>" in result.stderr


def test_workflow_run_unknown_name_exit_2(cli_home: tuple[Path, Path]) -> None:
    result = runner.invoke(app, ["workflow", "run", "no-such-workflow", "--json"])
    assert result.exit_code == 2
    assert "no-such-workflow" in result.stderr
    assert result.stdout == ""


def test_workflow_run_unacknowledged_egress_exit_2_with_hint(
    cli_home: tuple[Path, Path],
) -> None:
    _home, workspace = cli_home
    write_project_workflow(workspace, "cross-egress", CROSSING_WF)
    result = runner.invoke(app, ["workflow", "run", "cross-egress", "--var", "issue=x"])
    assert result.exit_code == 2
    assert "--acknowledge-egress anthropic,openai" in result.stderr  # rerun hint


# ------------------------------------------------------------ workflow list


def test_workflow_list_table_and_json(cli_home: tuple[Path, Path]) -> None:
    home, workspace = cli_home
    project_path = write_project_workflow(workspace, "vars-demo", VARS_WF)
    user_path = write_user_workflow(home, "two-step", TWO_STEP_WF)

    human = runner.invoke(app, ["workflow", "list"])
    assert human.exit_code == 0, human.stderr
    assert "vars-demo" in human.stdout
    assert "two-step" in human.stdout
    assert "project" in human.stdout
    assert "user" in human.stdout
    assert "Two mock steps threading text output." in human.stdout
    assert "n:integer*" in human.stdout  # required marker in the variables summary

    listed = runner.invoke(app, ["workflow", "list", "--json"])
    assert listed.exit_code == 0, listed.stderr
    rows = {row["name"]: row for row in json.loads(listed.stdout)}
    assert set(rows) == {"vars-demo", "two-step"}
    assert rows["vars-demo"]["scope"] == "project"
    assert rows["vars-demo"]["path"] == str(project_path)
    assert rows["vars-demo"]["variables"]["n"]["type"] == "integer"
    assert rows["vars-demo"]["variables"]["n"]["required"] is True
    assert rows["two-step"]["scope"] == "user"
    assert rows["two-step"]["path"] == str(user_path)
    assert rows["two-step"]["description"] == "Two mock steps threading text output."


def test_workflow_list_empty(cli_home: tuple[Path, Path]) -> None:
    result = runner.invoke(app, ["workflow", "list"])
    assert result.exit_code == 0
    assert "no workflows found" in result.stdout


def test_workflow_duplicate_name_exit_2_names_both_paths(
    cli_home: tuple[Path, Path],
) -> None:
    home, workspace = cli_home
    project_path = write_project_workflow(workspace, "two-step", TWO_STEP_WF)
    user_path = write_user_workflow(home, "two-step", TWO_STEP_WF)

    listed = runner.invoke(app, ["workflow", "list"])
    assert listed.exit_code == 2
    assert str(project_path) in listed.stderr
    assert str(user_path) in listed.stderr

    ran = runner.invoke(app, ["workflow", "run", "two-step"])
    assert ran.exit_code == 2
    assert str(project_path) in ran.stderr
    assert str(user_path) in ran.stderr
