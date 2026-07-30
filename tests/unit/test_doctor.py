"""Doctor tests (REQ-014): check list, live mock handshake, exit codes.

A mock agent is registered via the trusted user config in a tmp
``ZIGGY_HOME``; the acp-handshake check launches it for a real
``initialize`` round-trip and clean shutdown.
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

from ziggy.agents import BUILTIN_AGENTS, AgentRegistry  # noqa: E402
from ziggy.cli.doctor import _select_agents  # noqa: E402
from ziggy.cli.main import app  # noqa: E402
from ziggy.models.agent import AgentConfig  # noqa: E402

runner = CliRunner()

API_KEY_NAME = "ZIGGY_TEST_API_KEY"

CONFIG = (
    "schema_version = 1\n\n"
    "[agents.mock-agent]\n"
    f"command = {json.dumps(sys.executable)}\n"
    f"args = [{json.dumps(str(RAW_AGENT_PATH))}, {json.dumps(scenarios.HELLO)}]\n\n"
    "[agents.keyed]\n"
    f"command = {json.dumps(sys.executable)}\n"
    f"args = [{json.dumps(str(RAW_AGENT_PATH))}, {json.dumps(scenarios.HELLO)}]\n"
    f'api_key_env = "{API_KEY_NAME}"\n'
)


@pytest.fixture()
def cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (home / "config.toml").write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("ZIGGY_HOME", str(home))
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv(API_KEY_NAME, raising=False)
    monkeypatch.chdir(workspace)
    return home, workspace


def checks_by_name(payload: dict) -> dict[str, dict]:
    return {check["name"]: check for check in payload["checks"]}


@pytest.mark.slow
def test_doctor_mock_agent_all_pass_json(cli_home: tuple[Path, Path]) -> None:
    result = runner.invoke(app, ["doctor", "--agent", "mock-agent", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    by_name = checks_by_name(payload)
    assert not [c for c in payload["checks"] if c["status"] == "fail"]

    assert by_name["config-load"]["status"] == "pass"
    assert by_name["config-forbidden-project-keys"]["status"] == "pass"
    assert by_name["store-writable"]["status"] == "pass"
    assert by_name["index-integrity"]["status"] == "skip"  # no persisted runs yet
    assert by_name["agent-command-resolvable:mock-agent"]["status"] == "pass"
    assert by_name["api-key-env-set:mock-agent"]["status"] == "skip"

    handshake = by_name["acp-handshake:mock-agent"]
    assert handshake["status"] == "pass"
    assert f"protocol v{scenarios.PROTOCOL_VERSION}" in handshake["detail"]
    assert scenarios.RAW_AGENT_NAME in handshake["detail"]

    capability = by_name["capability-summary:mock-agent"]
    assert capability["status"] == "pass"
    assert "load_session" in capability["detail"]

    advisory = by_name["direct-tools-advisory:mock-agent"]
    assert advisory["status"] == "warn"
    assert "advisory" in advisory["detail"]

    assert by_name["orchestrator-planning-eligibility"]["status"] == "skip"
    assert by_name["trusted-workflow-hashes"]["status"] == "skip"
    assert by_name["server-readiness"]["status"] == "pass"
    assert "max_active_runs=1" in by_name["server-readiness"]["detail"]


def test_doctor_missing_api_key_fails_nonzero(cli_home: tuple[Path, Path]) -> None:
    result = runner.invoke(app, ["doctor", "--agent", "keyed", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    by_name = checks_by_name(payload)
    check = by_name["api-key-env-set:keyed"]
    assert check["status"] == "fail"
    assert API_KEY_NAME in check["detail"]
    assert API_KEY_NAME in (check["hint"] or "")
    # compose-child-env refuses before launch, so the handshake fails too
    assert by_name["acp-handshake:keyed"]["status"] == "fail"


@pytest.mark.slow
def test_doctor_api_key_present_never_prints_value(
    cli_home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_value = "doctor-test-value-not-a-real-credential"
    monkeypatch.setenv(API_KEY_NAME, secret_value)
    result = runner.invoke(app, ["doctor", "--agent", "keyed", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    by_name = checks_by_name(payload)
    assert by_name["api-key-env-set:keyed"]["status"] == "pass"
    assert secret_value not in result.stdout
    assert secret_value not in result.stderr


class TestAgentScope:
    """Scope selection is unit-tested directly: it decides which agents get
    launched, so exercising it must not launch any."""

    @staticmethod
    def _registry() -> AgentRegistry:
        custom = AgentConfig(name="helper", command="/bin/helper")
        return AgentRegistry({**BUILTIN_AGENTS, "helper": custom})

    def test_default_scope_skips_optional_vendor_clis(self) -> None:
        """Not everyone installs the OpenCode/Devin CLIs; a bare doctor run must
        not fail (exit 1) just because an optional vendor CLI is absent."""
        selected = _select_agents(self._registry(), agent=None, all_agents=False)
        assert selected == ["claude", "codex"]

    def test_all_widens_to_vendor_clis_and_customs(self) -> None:
        selected = _select_agents(self._registry(), agent=None, all_agents=True)
        assert selected == ["claude", "codex", "opencode", "devin", "helper"]

    @pytest.mark.parametrize("name", ["opencode", "devin"])
    def test_vendor_cli_builtins_are_probeable_by_name(self, name: str) -> None:
        assert _select_agents(self._registry(), agent=name, all_agents=False) == [name]


def test_doctor_unknown_agent_exit_2(cli_home: tuple[Path, Path]) -> None:
    result = runner.invoke(app, ["doctor", "--agent", "ghost"])
    assert result.exit_code == 2
    assert "unknown agent" in result.stderr


def test_doctor_broken_config_fails_and_skips_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (home / "config.toml").write_text("schema_version = 1\nnonsense = true\n", encoding="utf-8")
    monkeypatch.setenv("ZIGGY_HOME", str(home))
    monkeypatch.chdir(workspace)
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    by_name = checks_by_name(payload)
    assert by_name["config-load"]["status"] == "fail"
    assert "nonsense" in by_name["config-load"]["detail"]
    for name, check in by_name.items():
        if name != "config-load":
            assert check["status"] == "skip", name


def test_doctor_human_output_shows_failures_and_hints(
    cli_home: tuple[Path, Path],
) -> None:
    result = runner.invoke(app, ["doctor", "--agent", "keyed"])
    assert result.exit_code == 1
    assert "api-key-env-set:keyed" in result.stdout
    assert "hint:" in result.stdout
    assert "doctor: problems found" in result.stdout
