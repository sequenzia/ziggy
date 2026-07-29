"""Unit tests for ziggy.engine.env (REQ-007 minimal child environment)."""

from __future__ import annotations

from typing import Any

import pytest

from ziggy.engine.env import BASELINE_ENV_VARS, compose_child_env
from ziggy.errors import ConfigError
from ziggy.models.agent import AgentConfig


def agent(**overrides: Any) -> AgentConfig:
    data: dict[str, Any] = {"name": "claude", "command": "npx"}
    data.update(overrides)
    return AgentConfig.model_validate(data)


FULL_BASE = {
    "HOME": "/home/u",
    "PATH": "/usr/bin",
    "TERM": "xterm-256color",
    "LANG": "C.UTF-8",
    "FOO": "foo-val",
    "MY_KEY": "sekret-value",
    "SHELL": "/bin/zsh",  # never forwarded implicitly
    "AWS_SECRET_ACCESS_KEY": "leak-me-not",  # never forwarded implicitly
}


class TestComposition:
    def test_exact_keys_all_sources(self) -> None:
        ag = agent(inherit_env=["FOO"], env={"BAR": "1"}, api_key_env="MY_KEY")
        env, secrets = compose_child_env(ag, FULL_BASE)
        assert env == {
            "HOME": "/home/u",
            "PATH": "/usr/bin",
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
            "FOO": "foo-val",
            "BAR": "1",
            "MY_KEY": "sekret-value",
        }
        assert secrets == [("env:MY_KEY", "sekret-value")]

    def test_baseline_only_when_present(self) -> None:
        env, secrets = compose_child_env(agent(), {"HOME": "/h", "PATH": "/p", "IGN": "x"})
        assert env == {"HOME": "/h", "PATH": "/p"}
        assert secrets == []

    def test_baseline_names_are_documented_constant(self) -> None:
        assert BASELINE_ENV_VARS == ("HOME", "PATH", "TERM", "LANG")

    def test_missing_inherit_name_skipped_silently(self) -> None:
        ag = agent(inherit_env=["NOT_SET_ANYWHERE"])
        env, _ = compose_child_env(ag, {"HOME": "/h", "PATH": "/p"})
        assert env == {"HOME": "/h", "PATH": "/p"}

    def test_env_literals_win_over_baseline_and_inherited(self) -> None:
        ag = agent(inherit_env=["FOO"], env={"PATH": "/custom/bin", "FOO": "literal"})
        env, _ = compose_child_env(ag, {"HOME": "/h", "PATH": "/p", "FOO": "inherited"})
        assert env == {"HOME": "/h", "PATH": "/custom/bin", "FOO": "literal"}

    def test_no_api_key_env_means_no_secret_values(self) -> None:
        env, secrets = compose_child_env(agent(), FULL_BASE)
        assert secrets == []
        assert "MY_KEY" not in env

    def test_default_base_env_is_process_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ZIGGY_TEST_CRED", "from-parent")
        env, secrets = compose_child_env(agent(api_key_env="ZIGGY_TEST_CRED"))
        assert env["ZIGGY_TEST_CRED"] == "from-parent"
        assert secrets == [("env:ZIGGY_TEST_CRED", "from-parent")]


class TestMissingApiKey:
    def test_missing_api_key_env_exact_message(self) -> None:
        ag = agent(api_key_env="ANTHROPIC_API_KEY")
        with pytest.raises(ConfigError) as exc_info:
            compose_child_env(ag, {"HOME": "/h", "PATH": "/p"})
        assert str(exc_info.value) == "Agent 'claude' requires env var ANTHROPIC_API_KEY (not set)."
        assert exc_info.value.exit_code == 2
        assert exc_info.value.details == {"agent": "claude", "env_var": "ANTHROPIC_API_KEY"}

    def test_empty_api_key_value_is_not_set(self) -> None:
        ag = agent(name="codex", api_key_env="OPENAI_API_KEY")
        with pytest.raises(ConfigError) as exc_info:
            compose_child_env(ag, {"HOME": "/h", "PATH": "/p", "OPENAI_API_KEY": ""})
        assert str(exc_info.value) == "Agent 'codex' requires env var OPENAI_API_KEY (not set)."

    def test_error_never_contains_other_env_values(self) -> None:
        ag = agent(api_key_env="MISSING_KEY")
        with pytest.raises(ConfigError) as exc_info:
            compose_child_env(ag, FULL_BASE)
        assert "sekret-value" not in str(exc_info.value)
        assert "leak-me-not" not in str(exc_info.value)
