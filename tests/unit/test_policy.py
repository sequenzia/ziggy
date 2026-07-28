"""Branch-exhaustive tests for the guarded mediation policy (REQ-008, spec 10.1).

Every decide_* branch is pinned: allow paths, each deny rule id, project-denial
precedence, permission-request kind mapping, and hostile inputs that must
produce deny Decisions instead of exceptions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ziggy.acp.types import PermissionRequestN, TerminalRequestN
from ziggy.models.common import EnforcementScope
from ziggy.policy import (
    GUARDED_POLICY_NAME,
    RULE_OUTSIDE_STEPDIR_DENY,
    RULE_OUTSIDE_WORKSPACE_DENY,
    RULE_PATH_AMBIGUITY_DENY,
    RULE_READ_IN_WORKSPACE_ALLOW,
    RULE_SENSITIVE_PATH_DENY,
    RULE_TERMINAL_DEFAULT_DENY,
    RULE_TERMINAL_USER_ALLOWLIST,
    RULE_UNMATCHED_DEFAULT_DENY,
    RULE_WRITE_IN_STEPDIR_ALLOW,
    SOURCE_DEFAULT,
    SOURCE_PROJECT_DENIAL,
    SOURCE_USER,
    Decision,
    MediationPolicy,
    PathAmbiguity,
    ProjectDenial,
    build_policy_provenance,
    project_denial_rule_id,
)


@dataclass
class FakeTerminalRule:
    """Structural stand-in for config.TerminalRule (config/ lands separately)."""

    command: str
    args_prefix: list[str] = field(default_factory=list)


@dataclass
class FakeProfile:
    """Structural stand-in for config.PolicyProfile."""

    terminal_allowlist: list[FakeTerminalRule] = field(default_factory=list)
    deny_paths: list[str] = field(default_factory=list)


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "step").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "docs" / "a.md").write_text("doc")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("code")
    (root / ".env").write_text("SECRET=1")
    return Path(os.path.realpath(root))


@pytest.fixture
def step(ws: Path) -> Path:
    return ws / "step"


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    root = tmp_path / "outside"
    root.mkdir()
    (root / "target.txt").write_text("secret")
    return Path(os.path.realpath(root))


@pytest.fixture
def policy(ws: Path, step: Path) -> MediationPolicy:
    return MediationPolicy.guarded(workspace=ws, step_dir=step)


def make_permission(tool_call: Any) -> PermissionRequestN:
    return PermissionRequestN(session_id="sess-1", tool_call=tool_call, options=[])


def create_terminal(command: Any = None, args: Any = None, **extra: Any) -> TerminalRequestN:
    payload: dict[str, Any] = dict(extra)
    if command is not None:
        payload["command"] = command
    if args is not None:
        payload["args"] = args
    return TerminalRequestN(op="create", payload=payload)


def assert_decision(
    decision: Decision,
    *,
    allowed: bool,
    rule_id: str,
    source: str,
    policy_name: str = GUARDED_POLICY_NAME,
) -> None:
    assert decision.allowed is allowed
    assert decision.rule_id == rule_id
    assert decision.policy_source == source
    assert decision.policy_name == policy_name
    assert decision.enforcement_scope is EnforcementScope.ACP_MEDIATED
    assert decision.reason  # every decision carries a human reason


class TestGuardedConstruction:
    def test_step_dir_must_be_contained_in_workspace(self, ws: Path, outside: Path) -> None:
        with pytest.raises(PathAmbiguity):
            MediationPolicy.guarded(workspace=ws, step_dir=outside)

    def test_workspace_and_step_dir_are_canonicalized(
        self, tmp_path: Path, ws: Path, step: Path
    ) -> None:
        link = tmp_path / "ws-link"
        link.symlink_to(ws)
        policy = MediationPolicy.guarded(workspace=link, step_dir=link / "step")
        assert policy.workspace == ws
        assert policy.step_dir == step

    def test_profile_name_defaults_to_guarded(self, ws: Path, step: Path) -> None:
        policy = MediationPolicy.guarded(workspace=ws, step_dir=step, profile=FakeProfile())
        assert policy.policy_name == GUARDED_POLICY_NAME

    def test_profile_name_is_used_when_given(self, ws: Path, step: Path) -> None:
        policy = MediationPolicy.guarded(
            workspace=ws, step_dir=step, profile=FakeProfile(), profile_name="team-profile"
        )
        assert policy.policy_name == "team-profile"
        decision = policy.decide_fs_read(str(ws / "src" / "main.py"))
        assert decision.policy_name == "team-profile"


class TestFsRead:
    def test_inside_workspace_allows(self, policy: MediationPolicy, ws: Path) -> None:
        decision = policy.decide_fs_read(str(ws / "src" / "main.py"))
        assert_decision(
            decision, allowed=True, rule_id=RULE_READ_IN_WORKSPACE_ALLOW, source=SOURCE_DEFAULT
        )

    def test_relative_path_resolves_against_workspace(self, policy: MediationPolicy) -> None:
        decision = policy.decide_fs_read("src/main.py")
        assert decision.allowed
        assert decision.rule_id == RULE_READ_IN_WORKSPACE_ALLOW

    def test_nonexistent_leaf_inside_allows(self, policy: MediationPolicy, ws: Path) -> None:
        # Agent creating a new file then reading it back before it exists.
        decision = policy.decide_fs_read(str(ws / "generated" / "new.txt"))
        assert_decision(
            decision, allowed=True, rule_id=RULE_READ_IN_WORKSPACE_ALLOW, source=SOURCE_DEFAULT
        )

    def test_outside_workspace_denies(self, policy: MediationPolicy, outside: Path) -> None:
        decision = policy.decide_fs_read(str(outside / "target.txt"))
        assert_decision(
            decision, allowed=False, rule_id=RULE_OUTSIDE_WORKSPACE_DENY, source=SOURCE_DEFAULT
        )

    def test_etc_hosts_denies_outside(self, policy: MediationPolicy) -> None:
        decision = policy.decide_fs_read("/etc/hosts")
        assert decision.rule_id == RULE_OUTSIDE_WORKSPACE_DENY

    def test_sensitive_default_glob_denies_even_inside(
        self, policy: MediationPolicy, ws: Path
    ) -> None:
        decision = policy.decide_fs_read(str(ws / ".env"))
        assert_decision(
            decision, allowed=False, rule_id=RULE_SENSITIVE_PATH_DENY, source=SOURCE_DEFAULT
        )

    def test_nested_sensitive_glob_denies(self, policy: MediationPolicy, ws: Path) -> None:
        decision = policy.decide_fs_read(str(ws / "src" / ".env"))
        assert decision.rule_id == RULE_SENSITIVE_PATH_DENY

    def test_profile_deny_paths_deny_with_user_source(self, ws: Path, step: Path) -> None:
        policy = MediationPolicy.guarded(
            workspace=ws, step_dir=step, profile=FakeProfile(deny_paths=["docs/**"])
        )
        decision = policy.decide_fs_read(str(ws / "docs" / "a.md"))
        assert_decision(
            decision, allowed=False, rule_id=RULE_SENSITIVE_PATH_DENY, source=SOURCE_USER
        )

    def test_extra_sensitive_globs_deny_with_user_source(self, ws: Path, step: Path) -> None:
        policy = MediationPolicy.guarded(
            workspace=ws, step_dir=step, sensitive_globs=["**/*.sqlite"]
        )
        decision = policy.decide_fs_read(str(ws / "src" / "index.sqlite"))
        assert_decision(
            decision, allowed=False, rule_id=RULE_SENSITIVE_PATH_DENY, source=SOURCE_USER
        )

    def test_non_matching_user_glob_falls_through_to_allow(self, ws: Path, step: Path) -> None:
        """User deny globs that do not match the path are iterated past and
        the read is allowed (closes the user-glob loop's no-match branch)."""
        policy = MediationPolicy.guarded(
            workspace=ws, step_dir=step, profile=FakeProfile(deny_paths=["docs/**"])
        )
        decision = policy.decide_fs_read(str(ws / "src" / "main.py"))
        assert_decision(
            decision, allowed=True, rule_id=RULE_READ_IN_WORKSPACE_ALLOW, source=SOURCE_DEFAULT
        )

    def test_symlink_escape_denies_as_ambiguity(
        self, policy: MediationPolicy, ws: Path, outside: Path
    ) -> None:
        (ws / "escape").symlink_to(outside / "target.txt")
        decision = policy.decide_fs_read(str(ws / "escape"))
        assert_decision(
            decision, allowed=False, rule_id=RULE_PATH_AMBIGUITY_DENY, source=SOURCE_DEFAULT
        )

    def test_traversal_escape_denies_as_ambiguity(self, policy: MediationPolicy) -> None:
        decision = policy.decide_fs_read("../outside/target.txt")
        assert_decision(
            decision, allowed=False, rule_id=RULE_PATH_AMBIGUITY_DENY, source=SOURCE_DEFAULT
        )

    def test_dotdot_staying_inside_allows(self, policy: MediationPolicy) -> None:
        decision = policy.decide_fs_read("docs/../src/main.py")
        assert decision.allowed
        assert decision.rule_id == RULE_READ_IN_WORKSPACE_ALLOW

    def test_garbage_path_denies_without_raising(self, policy: MediationPolicy) -> None:
        decision = policy.decide_fs_read("bad\x00path")
        assert_decision(
            decision, allowed=False, rule_id=RULE_PATH_AMBIGUITY_DENY, source=SOURCE_DEFAULT
        )

    def test_non_string_path_denies_without_raising(self, policy: MediationPolicy) -> None:
        decision = policy.decide_fs_read(1234)  # type: ignore[arg-type]
        assert not decision.allowed
        assert decision.rule_id == RULE_PATH_AMBIGUITY_DENY

    def test_workspace_symlink_read_allows(self, tmp_path: Path, ws: Path) -> None:
        link = tmp_path / "linked-ws"
        link.symlink_to(ws)
        policy = MediationPolicy.guarded(workspace=link, step_dir=link)
        decision = policy.decide_fs_read(str(link / "src" / "main.py"))
        assert decision.allowed
        assert decision.rule_id == RULE_READ_IN_WORKSPACE_ALLOW


class TestFsWrite:
    def test_inside_step_dir_allows(self, policy: MediationPolicy, step: Path) -> None:
        decision = policy.decide_fs_write(str(step / "out.txt"))
        assert_decision(
            decision, allowed=True, rule_id=RULE_WRITE_IN_STEPDIR_ALLOW, source=SOURCE_DEFAULT
        )

    def test_nonexistent_nested_inside_step_dir_allows(
        self, policy: MediationPolicy, step: Path
    ) -> None:
        decision = policy.decide_fs_write(str(step / "new" / "deep" / "out.txt"))
        assert decision.allowed

    def test_in_workspace_outside_step_dir_denies(self, policy: MediationPolicy, ws: Path) -> None:
        decision = policy.decide_fs_write(str(ws / "src" / "main.py"))
        assert_decision(
            decision, allowed=False, rule_id=RULE_OUTSIDE_STEPDIR_DENY, source=SOURCE_DEFAULT
        )

    def test_outside_workspace_denies(self, policy: MediationPolicy, outside: Path) -> None:
        decision = policy.decide_fs_write(str(outside / "target.txt"))
        assert_decision(
            decision, allowed=False, rule_id=RULE_OUTSIDE_WORKSPACE_DENY, source=SOURCE_DEFAULT
        )

    def test_sensitive_path_denies_write_even_in_step_dir(
        self, policy: MediationPolicy, step: Path
    ) -> None:
        decision = policy.decide_fs_write(str(step / ".env"))
        assert_decision(
            decision, allowed=False, rule_id=RULE_SENSITIVE_PATH_DENY, source=SOURCE_DEFAULT
        )

    def test_symlink_escape_denies_write(
        self, policy: MediationPolicy, step: Path, outside: Path
    ) -> None:
        (step / "escape").symlink_to(outside / "target.txt")
        decision = policy.decide_fs_write(str(step / "escape"))
        assert decision.rule_id == RULE_PATH_AMBIGUITY_DENY

    def test_traversal_escape_denies_write(self, policy: MediationPolicy) -> None:
        decision = policy.decide_fs_write("step/../../outside/target.txt")
        assert decision.rule_id == RULE_PATH_AMBIGUITY_DENY

    def test_step_dir_equal_to_workspace_allows_anywhere_inside(self, ws: Path) -> None:
        policy = MediationPolicy.guarded(workspace=ws, step_dir=ws)
        decision = policy.decide_fs_write(str(ws / "src" / "main.py"))
        assert decision.allowed
        assert decision.rule_id == RULE_WRITE_IN_STEPDIR_ALLOW


class TestTerminal:
    def test_no_allowlist_denies(self, policy: MediationPolicy) -> None:
        decision = policy.decide_terminal(create_terminal("git", ["status"]))
        assert_decision(
            decision, allowed=False, rule_id=RULE_TERMINAL_DEFAULT_DENY, source=SOURCE_DEFAULT
        )

    def test_matching_allowlist_allows(self, ws: Path, step: Path) -> None:
        profile = FakeProfile(terminal_allowlist=[FakeTerminalRule("git", ["status"])])
        policy = MediationPolicy.guarded(workspace=ws, step_dir=step, profile=profile)
        decision = policy.decide_terminal(create_terminal("git", ["status", "-s"]))
        assert_decision(
            decision, allowed=True, rule_id=RULE_TERMINAL_USER_ALLOWLIST, source=SOURCE_USER
        )

    def test_argv0_match_wrong_prefix_denies(self, ws: Path, step: Path) -> None:
        profile = FakeProfile(terminal_allowlist=[FakeTerminalRule("git", ["status"])])
        policy = MediationPolicy.guarded(workspace=ws, step_dir=step, profile=profile)
        decision = policy.decide_terminal(create_terminal("git", ["push", "--force"]))
        assert_decision(
            decision, allowed=False, rule_id=RULE_TERMINAL_DEFAULT_DENY, source=SOURCE_DEFAULT
        )

    def test_argv0_mismatch_denies(self, ws: Path, step: Path) -> None:
        profile = FakeProfile(terminal_allowlist=[FakeTerminalRule("git", [])])
        policy = MediationPolicy.guarded(workspace=ws, step_dir=step, profile=profile)
        decision = policy.decide_terminal(create_terminal("ls", []))
        assert decision.rule_id == RULE_TERMINAL_DEFAULT_DENY

    def test_argv0_match_is_exact_not_path_normalized(self, ws: Path, step: Path) -> None:
        profile = FakeProfile(terminal_allowlist=[FakeTerminalRule("git", [])])
        policy = MediationPolicy.guarded(workspace=ws, step_dir=step, profile=profile)
        decision = policy.decide_terminal(create_terminal("/usr/bin/git", ["status"]))
        assert not decision.allowed

    def test_empty_prefix_matches_any_args(self, ws: Path, step: Path) -> None:
        profile = FakeProfile(terminal_allowlist=[FakeTerminalRule("git", [])])
        policy = MediationPolicy.guarded(workspace=ws, step_dir=step, profile=profile)
        assert policy.decide_terminal(create_terminal("git", ["push", "--force"])).allowed
        assert policy.decide_terminal(create_terminal("git")).allowed

    def test_prefix_longer_than_args_denies(self, ws: Path, step: Path) -> None:
        profile = FakeProfile(terminal_allowlist=[FakeTerminalRule("git", ["status"])])
        policy = MediationPolicy.guarded(workspace=ws, step_dir=step, profile=profile)
        decision = policy.decide_terminal(create_terminal("git", []))
        assert not decision.allowed

    @pytest.mark.parametrize("op", ["output", "wait_for_exit", "kill", "release"])
    def test_non_create_ops_default_deny(self, policy: MediationPolicy, op: str) -> None:
        decision = policy.decide_terminal(TerminalRequestN(op=op, payload={"terminal_id": "t1"}))
        assert_decision(
            decision, allowed=False, rule_id=RULE_TERMINAL_DEFAULT_DENY, source=SOURCE_DEFAULT
        )

    def test_create_without_command_denies(self, policy: MediationPolicy) -> None:
        decision = policy.decide_terminal(TerminalRequestN(op="create", payload={}))
        assert decision.rule_id == RULE_TERMINAL_DEFAULT_DENY

    def test_create_with_empty_command_denies(self, policy: MediationPolicy) -> None:
        decision = policy.decide_terminal(create_terminal(""))
        assert decision.rule_id == RULE_TERMINAL_DEFAULT_DENY

    def test_create_with_non_string_command_denies(self, policy: MediationPolicy) -> None:
        decision = policy.decide_terminal(create_terminal(["git"]))
        assert decision.rule_id == RULE_TERMINAL_DEFAULT_DENY

    def test_malformed_args_deny(self, ws: Path, step: Path) -> None:
        profile = FakeProfile(terminal_allowlist=[FakeTerminalRule("git", [])])
        policy = MediationPolicy.guarded(workspace=ws, step_dir=step, profile=profile)
        assert not policy.decide_terminal(create_terminal("git", "status")).allowed
        assert not policy.decide_terminal(create_terminal("git", [1, 2])).allowed

    def test_hostile_request_object_denies_without_raising(self, policy: MediationPolicy) -> None:
        decision = policy.decide_terminal(
            SimpleNamespace(op="create", payload="not-a-dict")  # type: ignore[arg-type]
        )
        assert decision.rule_id == RULE_TERMINAL_DEFAULT_DENY


class TestProjectDenials:
    def test_path_denial_overrides_would_be_read_allow(self, ws: Path, step: Path) -> None:
        policy = MediationPolicy.guarded(
            workspace=ws,
            step_dir=step,
            project_denials=[ProjectDenial(kind="path", pattern="docs/**")],
        )
        decision = policy.decide_fs_read(str(ws / "docs" / "a.md"))
        assert_decision(
            decision,
            allowed=False,
            rule_id=project_denial_rule_id(0),
            source=SOURCE_PROJECT_DENIAL,
        )

    def test_path_denial_overrides_would_be_write_allow(self, ws: Path, step: Path) -> None:
        policy = MediationPolicy.guarded(
            workspace=ws,
            step_dir=step,
            project_denials=[ProjectDenial(kind="path", pattern="**/generated.txt")],
        )
        decision = policy.decide_fs_write(str(step / "generated.txt"))
        assert decision.rule_id == project_denial_rule_id(0)
        assert decision.policy_source == SOURCE_PROJECT_DENIAL

    def test_non_matching_path_denial_leaves_allow(self, ws: Path, step: Path) -> None:
        policy = MediationPolicy.guarded(
            workspace=ws,
            step_dir=step,
            project_denials=[ProjectDenial(kind="path", pattern="docs/**")],
        )
        assert policy.decide_fs_read(str(ws / "src" / "main.py")).allowed

    def test_terminal_denial_beats_user_allowlist(self, ws: Path, step: Path) -> None:
        profile = FakeProfile(terminal_allowlist=[FakeTerminalRule("git", [])])
        policy = MediationPolicy.guarded(
            workspace=ws,
            step_dir=step,
            profile=profile,
            project_denials=[ProjectDenial(kind="terminal", pattern="git", args_prefix=("push",))],
        )
        denied = policy.decide_terminal(create_terminal("git", ["push", "--force"]))
        assert_decision(
            denied, allowed=False, rule_id=project_denial_rule_id(0), source=SOURCE_PROJECT_DENIAL
        )
        # the denial only tightens: non-matching invocations keep the allow
        allowed = policy.decide_terminal(create_terminal("git", ["status"]))
        assert allowed.allowed
        assert allowed.rule_id == RULE_TERMINAL_USER_ALLOWLIST

    def test_terminal_denial_with_empty_prefix_denies_all_invocations(
        self, ws: Path, step: Path
    ) -> None:
        profile = FakeProfile(terminal_allowlist=[FakeTerminalRule("git", [])])
        policy = MediationPolicy.guarded(
            workspace=ws,
            step_dir=step,
            profile=profile,
            project_denials=[ProjectDenial(kind="terminal", pattern="git")],
        )
        assert not policy.decide_terminal(create_terminal("git", ["status"])).allowed

    def test_denial_rule_id_carries_list_index(self, ws: Path, step: Path) -> None:
        policy = MediationPolicy.guarded(
            workspace=ws,
            step_dir=step,
            project_denials=[
                ProjectDenial(kind="path", pattern="docs/**"),
                ProjectDenial(kind="path", pattern="src/**"),
            ],
        )
        decision = policy.decide_fs_read(str(ws / "src" / "main.py"))
        assert decision.rule_id == project_denial_rule_id(1)

    def test_terminal_denial_does_not_affect_paths_and_vice_versa(
        self, ws: Path, step: Path
    ) -> None:
        policy = MediationPolicy.guarded(
            workspace=ws,
            step_dir=step,
            project_denials=[ProjectDenial(kind="terminal", pattern="git")],
        )
        assert policy.decide_fs_read(str(ws / "docs" / "a.md")).allowed

    def test_config_shaped_denials_without_args_prefix_are_normalized(
        self, ws: Path, step: Path
    ) -> None:
        # config.ProjectDenial carries only {kind, pattern}; a terminal denial
        # without args_prefix denies every invocation of that argv0.
        from ziggy.config.schema import ProjectDenial as ConfigProjectDenial

        profile = FakeProfile(terminal_allowlist=[FakeTerminalRule("git", [])])
        policy = MediationPolicy.guarded(
            workspace=ws,
            step_dir=step,
            profile=profile,
            project_denials=[
                ConfigProjectDenial(kind="path", pattern="docs/**"),
                ConfigProjectDenial(kind="terminal", pattern="git"),
            ],
        )
        assert policy.project_denials == (
            ProjectDenial(kind="path", pattern="docs/**"),
            ProjectDenial(kind="terminal", pattern="git"),
        )
        assert policy.decide_fs_read(str(ws / "docs" / "a.md")).rule_id == (
            project_denial_rule_id(0)
        )
        assert policy.decide_terminal(create_terminal("git", ["status"])).rule_id == (
            project_denial_rule_id(1)
        )


class TestPermissionMapping:
    def test_read_kind_with_inside_location_allows(self, policy: MediationPolicy, ws: Path) -> None:
        req = make_permission(
            {"kind": "read", "locations": [{"path": str(ws / "src" / "main.py"), "line": 3}]}
        )
        decision = policy.decide_permission(req)
        assert_decision(
            decision, allowed=True, rule_id=RULE_READ_IN_WORKSPACE_ALLOW, source=SOURCE_DEFAULT
        )

    def test_read_kind_with_outside_location_denies(
        self, policy: MediationPolicy, outside: Path
    ) -> None:
        req = make_permission(
            {"kind": "read", "locations": [{"path": str(outside / "target.txt")}]}
        )
        decision = policy.decide_permission(req)
        assert decision.rule_id == RULE_OUTSIDE_WORKSPACE_DENY

    def test_edit_kind_maps_to_write_decision(self, policy: MediationPolicy, step: Path) -> None:
        req = make_permission({"kind": "edit", "locations": [{"path": str(step / "out.txt")}]})
        decision = policy.decide_permission(req)
        assert_decision(
            decision, allowed=True, rule_id=RULE_WRITE_IN_STEPDIR_ALLOW, source=SOURCE_DEFAULT
        )

    def test_delete_kind_maps_to_write_decision(self, policy: MediationPolicy, ws: Path) -> None:
        req = make_permission(
            {"kind": "delete", "locations": [{"path": str(ws / "src" / "main.py")}]}
        )
        decision = policy.decide_permission(req)
        assert decision.rule_id == RULE_OUTSIDE_STEPDIR_DENY

    def test_move_kind_partial_deny_records_denying_rule(
        self, policy: MediationPolicy, step: Path, ws: Path
    ) -> None:
        req = make_permission(
            {
                "kind": "move",
                "locations": [
                    {"path": str(step / "a.txt")},  # would be allowed
                    {"path": str(ws / "docs" / "a.md")},  # outside step dir
                ],
            }
        )
        decision = policy.decide_permission(req)
        assert_decision(
            decision, allowed=False, rule_id=RULE_OUTSIDE_STEPDIR_DENY, source=SOURCE_DEFAULT
        )

    def test_multi_location_read_partial_deny(
        self, policy: MediationPolicy, ws: Path, outside: Path
    ) -> None:
        req = make_permission(
            {
                "kind": "read",
                "locations": [
                    {"path": str(ws / "src" / "main.py")},
                    {"path": str(outside / "target.txt")},
                ],
            }
        )
        decision = policy.decide_permission(req)
        assert not decision.allowed
        assert decision.rule_id == RULE_OUTSIDE_WORKSPACE_DENY

    def test_multi_location_all_allowed_allows(self, policy: MediationPolicy, ws: Path) -> None:
        req = make_permission(
            {
                "kind": "read",
                "locations": [
                    {"path": str(ws / "src" / "main.py")},
                    {"path": str(ws / "docs" / "a.md")},
                ],
            }
        )
        decision = policy.decide_permission(req)
        assert decision.allowed
        assert decision.rule_id == RULE_READ_IN_WORKSPACE_ALLOW

    def test_read_of_sensitive_location_denies(self, policy: MediationPolicy, ws: Path) -> None:
        req = make_permission({"kind": "read", "locations": [{"path": str(ws / ".env")}]})
        assert policy.decide_permission(req).rule_id == RULE_SENSITIVE_PATH_DENY

    def test_string_locations_are_accepted(self, policy: MediationPolicy) -> None:
        req = make_permission({"kind": "read", "locations": ["src/main.py"]})
        decision = policy.decide_permission(req)
        assert decision.allowed
        assert decision.rule_id == RULE_READ_IN_WORKSPACE_ALLOW

    def test_execute_kind_uses_terminal_decision(self, ws: Path, step: Path) -> None:
        profile = FakeProfile(terminal_allowlist=[FakeTerminalRule("git", ["status"])])
        policy = MediationPolicy.guarded(workspace=ws, step_dir=step, profile=profile)
        req = make_permission(
            {"kind": "execute", "raw_input": {"command": "git", "args": ["status"]}}
        )
        decision = policy.decide_permission(req)
        assert_decision(
            decision, allowed=True, rule_id=RULE_TERMINAL_USER_ALLOWLIST, source=SOURCE_USER
        )

    def test_execute_kind_unlisted_command_denies(self, policy: MediationPolicy) -> None:
        req = make_permission({"kind": "execute", "raw_input": {"command": "rm", "args": ["-rf"]}})
        decision = policy.decide_permission(req)
        assert decision.rule_id == RULE_TERMINAL_DEFAULT_DENY

    def test_execute_without_command_default_denies(self, policy: MediationPolicy) -> None:
        req = make_permission({"kind": "execute", "raw_input": {"description": "run tests"}})
        decision = policy.decide_permission(req)
        assert_decision(
            decision, allowed=False, rule_id=RULE_TERMINAL_DEFAULT_DENY, source=SOURCE_DEFAULT
        )

    def test_execute_top_level_command_fallback(self, ws: Path, step: Path) -> None:
        profile = FakeProfile(terminal_allowlist=[FakeTerminalRule("git", [])])
        policy = MediationPolicy.guarded(workspace=ws, step_dir=step, profile=profile)
        req = make_permission({"kind": "execute", "command": "git", "args": ["status"]})
        assert policy.decide_permission(req).allowed

    def test_execute_malformed_args_denies(self, ws: Path, step: Path) -> None:
        profile = FakeProfile(terminal_allowlist=[FakeTerminalRule("git", [])])
        policy = MediationPolicy.guarded(workspace=ws, step_dir=step, profile=profile)
        req = make_permission({"kind": "execute", "raw_input": {"command": "git", "args": 7}})
        decision = policy.decide_permission(req)
        assert decision.rule_id == RULE_TERMINAL_DEFAULT_DENY

    def test_execute_respects_project_terminal_denial(self, ws: Path, step: Path) -> None:
        profile = FakeProfile(terminal_allowlist=[FakeTerminalRule("git", [])])
        policy = MediationPolicy.guarded(
            workspace=ws,
            step_dir=step,
            profile=profile,
            project_denials=[ProjectDenial(kind="terminal", pattern="git", args_prefix=("push",))],
        )
        req = make_permission(
            {"kind": "execute", "raw_input": {"command": "git", "args": ["push"]}}
        )
        assert policy.decide_permission(req).rule_id == project_denial_rule_id(0)

    @pytest.mark.parametrize("kind", ["search", "fetch", "think", "switch_mode", "other", None, 7])
    def test_unmapped_kinds_default_deny(self, policy: MediationPolicy, kind: Any) -> None:
        req = make_permission({"kind": kind, "locations": [{"path": "src/main.py"}]})
        decision = policy.decide_permission(req)
        assert_decision(
            decision, allowed=False, rule_id=RULE_UNMATCHED_DEFAULT_DENY, source=SOURCE_DEFAULT
        )

    def test_read_with_missing_locations_default_denies(self, policy: MediationPolicy) -> None:
        decision = policy.decide_permission(make_permission({"kind": "read"}))
        assert decision.rule_id == RULE_UNMATCHED_DEFAULT_DENY

    def test_read_with_empty_locations_default_denies(self, policy: MediationPolicy) -> None:
        decision = policy.decide_permission(make_permission({"kind": "read", "locations": []}))
        assert decision.rule_id == RULE_UNMATCHED_DEFAULT_DENY

    def test_edit_with_empty_locations_default_denies(self, policy: MediationPolicy) -> None:
        decision = policy.decide_permission(make_permission({"kind": "edit", "locations": []}))
        assert decision.rule_id == RULE_UNMATCHED_DEFAULT_DENY

    @pytest.mark.parametrize(
        "locations",
        ["not-a-list", [42], [{"line": 3}], [{"path": 42}], [None]],
    )
    def test_malformed_locations_default_deny(
        self, policy: MediationPolicy, locations: Any
    ) -> None:
        req = make_permission({"kind": "read", "locations": locations})
        decision = policy.decide_permission(req)
        assert decision.rule_id == RULE_UNMATCHED_DEFAULT_DENY

    def test_tool_call_not_a_dict_default_denies(self, policy: MediationPolicy) -> None:
        decision = policy.decide_permission(make_permission("hostile"))
        assert_decision(
            decision, allowed=False, rule_id=RULE_UNMATCHED_DEFAULT_DENY, source=SOURCE_DEFAULT
        )

    def test_hostile_request_object_denies_without_raising(self, policy: MediationPolicy) -> None:
        decision = policy.decide_permission(SimpleNamespace())  # type: ignore[arg-type]
        assert decision.rule_id == RULE_UNMATCHED_DEFAULT_DENY


class TestPolicyProvenance:
    def test_default_policy_provenance(self, policy: MediationPolicy) -> None:
        prov = build_policy_provenance(policy)
        assert prov.policy_name == GUARDED_POLICY_NAME
        assert prov.ceiling_source == SOURCE_DEFAULT
        assert prov.enforcement == "advisory"
        assert prov.enforcement_scope_default is EnforcementScope.ACP_MEDIATED
        assert prov.tightened_by == []
        rule_ids = [rule["rule_id"] for rule in prov.rules]
        for expected in (
            RULE_READ_IN_WORKSPACE_ALLOW,
            RULE_WRITE_IN_STEPDIR_ALLOW,
            RULE_SENSITIVE_PATH_DENY,
            RULE_OUTSIDE_WORKSPACE_DENY,
            RULE_OUTSIDE_STEPDIR_DENY,
            RULE_TERMINAL_DEFAULT_DENY,
            RULE_PATH_AMBIGUITY_DENY,
            RULE_UNMATCHED_DEFAULT_DENY,
        ):
            assert expected in rule_ids
        sensitive = next(r for r in prov.rules if r["rule_id"] == RULE_SENSITIVE_PATH_DENY)
        assert sensitive["patterns"] == list(policy.default_sensitive_globs)

    def test_user_profile_provenance(self, ws: Path, step: Path) -> None:
        profile = FakeProfile(
            terminal_allowlist=[FakeTerminalRule("git", ["status"])],
            deny_paths=["private/**"],
        )
        policy = MediationPolicy.guarded(
            workspace=ws, step_dir=step, profile=profile, profile_name="team"
        )
        prov = build_policy_provenance(policy)
        assert prov.policy_name == "team"
        assert prov.ceiling_source == SOURCE_USER  # inferred from user rules
        allow_rules = [r for r in prov.rules if r["rule_id"] == RULE_TERMINAL_USER_ALLOWLIST]
        assert allow_rules == [
            {
                "rule_id": RULE_TERMINAL_USER_ALLOWLIST,
                "effect": "allow",
                "source": SOURCE_USER,
                "command": "git",
                "args_prefix": ["status"],
            }
        ]
        user_globs = [
            r
            for r in prov.rules
            if r["rule_id"] == RULE_SENSITIVE_PATH_DENY and r["source"] == SOURCE_USER
        ]
        assert user_globs == [
            {
                "rule_id": RULE_SENSITIVE_PATH_DENY,
                "effect": "deny",
                "source": SOURCE_USER,
                "patterns": ["private/**"],
            }
        ]

    def test_project_denials_recorded_and_counted(self, ws: Path, step: Path) -> None:
        policy = MediationPolicy.guarded(
            workspace=ws,
            step_dir=step,
            project_denials=[
                ProjectDenial(kind="path", pattern="docs/**"),
                ProjectDenial(kind="terminal", pattern="git", args_prefix=("push",)),
            ],
        )
        prov = build_policy_provenance(policy)
        assert prov.tightened_by == ["project-denials:2"]
        denial_rules = [r for r in prov.rules if r["rule_id"].startswith("project-denial:")]
        assert denial_rules == [
            {
                "rule_id": project_denial_rule_id(0),
                "effect": "deny",
                "source": "project",
                "kind": "path",
                "pattern": "docs/**",
            },
            {
                "rule_id": project_denial_rule_id(1),
                "effect": "deny",
                "source": "project",
                "kind": "terminal",
                "pattern": "git",
                "args_prefix": ["push"],
            },
        ]

    def test_ceiling_source_can_be_overridden(self, policy: MediationPolicy) -> None:
        prov = build_policy_provenance(policy, ceiling_source="env")
        assert prov.ceiling_source == "env"

    def test_provenance_model_round_trips(self, policy: MediationPolicy) -> None:
        prov = build_policy_provenance(policy)
        dumped = prov.model_dump()
        assert dumped["enforcement"] == "advisory"
        assert type(prov).model_validate(dumped) == prov
