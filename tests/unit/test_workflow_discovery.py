"""Unit tests for ziggy.workflows.discovery (REQ-009 discovery + direct paths)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ziggy.errors import ValidationError
from ziggy.workflows import (
    SCOPE_PROJECT,
    SCOPE_USER,
    discover,
    project_workflows_dir,
    resolve,
    user_workflows_dir,
)


def workflow_text(name: str) -> str:
    return f"version: 1\nname: {name}\nsteps:\n  a:\n    agent: claude\n    prompt: hi\n"


def write_workflow(directory: Path, name: str, suffix: str = ".yaml") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}{suffix}"
    path.write_text(workflow_text(name), encoding="utf-8")
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def ziggy_home_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "ziggy-home"
    home.mkdir()
    monkeypatch.setenv("ZIGGY_HOME", str(home))
    return home


class TestDiscover:
    def test_no_directories_gives_empty(self, workspace: Path, ziggy_home_dir: Path) -> None:
        assert discover(workspace) == {}

    def test_project_and_user_scopes(self, workspace: Path, ziggy_home_dir: Path) -> None:
        project_path = write_workflow(project_workflows_dir(workspace), "proj-wf")
        user_path = write_workflow(ziggy_home_dir / "workflows", "user-wf")
        found = discover(workspace)
        assert set(found) == {"proj-wf", "user-wf"}
        assert found["proj-wf"].source_scope == SCOPE_PROJECT
        assert found["proj-wf"].path == project_path
        assert found["user-wf"].source_scope == SCOPE_USER
        assert found["user-wf"].path == user_path
        assert found["proj-wf"].definition.name == "proj-wf"

    def test_yml_extension_discovered(self, workspace: Path, ziggy_home_dir: Path) -> None:
        write_workflow(project_workflows_dir(workspace), "short", suffix=".yml")
        assert set(discover(workspace)) == {"short"}

    def test_non_workflow_files_ignored(self, workspace: Path, ziggy_home_dir: Path) -> None:
        wf_dir = project_workflows_dir(workspace)
        write_workflow(wf_dir, "real")
        (wf_dir / "README.md").write_text("not a workflow", encoding="utf-8")
        assert set(discover(workspace)) == {"real"}

    def test_ziggy_home_is_honored(
        self, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        other_home = tmp_path / "other-home"
        monkeypatch.setenv("ZIGGY_HOME", str(other_home))
        assert user_workflows_dir() == other_home / "workflows"
        write_workflow(other_home / "workflows", "from-home")
        assert set(discover(workspace)) == {"from-home"}

    def test_duplicate_across_scopes_names_both_paths(
        self, workspace: Path, ziggy_home_dir: Path
    ) -> None:
        project_path = write_workflow(project_workflows_dir(workspace), "dup")
        user_path = write_workflow(ziggy_home_dir / "workflows", "dup")
        with pytest.raises(ValidationError) as exc:
            discover(workspace)
        message = str(exc.value)
        assert "duplicate workflow name 'dup'" in message
        assert str(project_path) in message
        assert str(user_path) in message

    def test_duplicate_within_scope_names_both_paths(
        self, workspace: Path, ziggy_home_dir: Path
    ) -> None:
        first = write_workflow(project_workflows_dir(workspace), "dup", suffix=".yaml")
        second = write_workflow(project_workflows_dir(workspace), "dup", suffix=".yml")
        with pytest.raises(ValidationError) as exc:
            discover(workspace)
        message = str(exc.value)
        assert str(first) in message
        assert str(second) in message

    def test_broken_workflow_on_search_path_fails_path_precise(
        self, workspace: Path, ziggy_home_dir: Path
    ) -> None:
        wf_dir = project_workflows_dir(workspace)
        wf_dir.mkdir(parents=True)
        bad = wf_dir / "bad.yaml"
        bad.write_text("version: 2\nname: bad\nsteps: {}\n", encoding="utf-8")
        with pytest.raises(ValidationError) as exc:
            discover(workspace)
        assert str(bad) in str(exc.value)


class TestResolveByName:
    def test_resolve_project_name(self, workspace: Path, ziggy_home_dir: Path) -> None:
        write_workflow(project_workflows_dir(workspace), "mine")
        wf = resolve("mine", workspace)
        assert wf.name == "mine"
        assert wf.source_scope == SCOPE_PROJECT

    def test_resolve_unknown_name_lists_search_dirs(
        self, workspace: Path, ziggy_home_dir: Path
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            resolve("ghost", workspace)
        message = str(exc.value)
        assert "'ghost'" in message
        assert str(project_workflows_dir(workspace)) in message
        assert str(user_workflows_dir()) in message

    def test_resolve_by_name_hits_duplicate_error(
        self, workspace: Path, ziggy_home_dir: Path
    ) -> None:
        write_workflow(project_workflows_dir(workspace), "dup")
        write_workflow(ziggy_home_dir / "workflows", "dup")
        with pytest.raises(ValidationError) as exc:
            resolve("dup", workspace)
        assert "duplicate workflow name" in str(exc.value)


class TestResolveDirectPath:
    def test_absolute_path_inside_workspace(self, workspace: Path, ziggy_home_dir: Path) -> None:
        path = write_workflow(project_workflows_dir(workspace), "direct")
        wf = resolve(str(path), workspace)
        assert wf.name == "direct"
        assert wf.source_scope == SCOPE_PROJECT
        assert wf.path == Path(os.path.realpath(path))

    def test_relative_path_resolves_against_workspace(
        self, workspace: Path, ziggy_home_dir: Path
    ) -> None:
        write_workflow(project_workflows_dir(workspace), "rel")
        wf = resolve(".ziggy/workflows/rel.yaml", workspace)
        assert wf.name == "rel"
        assert wf.source_scope == SCOPE_PROJECT

    def test_path_inside_user_workflows_dir(self, workspace: Path, ziggy_home_dir: Path) -> None:
        path = write_workflow(ziggy_home_dir / "workflows", "homey")
        wf = resolve(str(path), workspace)
        assert wf.name == "homey"
        assert wf.source_scope == SCOPE_USER

    def test_direct_path_bypasses_duplicate_names(
        self, workspace: Path, ziggy_home_dir: Path
    ) -> None:
        project_path = write_workflow(project_workflows_dir(workspace), "dup")
        write_workflow(ziggy_home_dir / "workflows", "dup")
        wf = resolve(str(project_path), workspace)
        assert wf.name == "dup"
        assert wf.source_scope == SCOPE_PROJECT

    def test_path_outside_workspace_and_user_dir_rejected(
        self, workspace: Path, ziggy_home_dir: Path, tmp_path: Path
    ) -> None:
        outside = write_workflow(tmp_path / "elsewhere", "sneaky")
        with pytest.raises(ValidationError) as exc:
            resolve(str(outside), workspace)
        message = str(exc.value)
        assert "must resolve canonically inside" in message
        assert str(workspace) in message

    def test_traversal_escape_rejected(
        self, workspace: Path, ziggy_home_dir: Path, tmp_path: Path
    ) -> None:
        write_workflow(tmp_path / "elsewhere", "sneaky")
        with pytest.raises(ValidationError):
            resolve("../elsewhere/sneaky.yaml", workspace)

    def test_symlink_escape_rejected(
        self, workspace: Path, ziggy_home_dir: Path, tmp_path: Path
    ) -> None:
        outside = write_workflow(tmp_path / "elsewhere", "sneaky")
        link = workspace / "sneaky.yaml"
        link.symlink_to(outside)
        with pytest.raises(ValidationError) as exc:
            resolve(str(link), workspace)
        assert "must resolve canonically inside" in str(exc.value)

    def test_contained_but_missing_file_rejected(
        self, workspace: Path, ziggy_home_dir: Path
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            resolve(str(workspace / "nope.yaml"), workspace)
        assert "not found" in str(exc.value)

    def test_direct_path_still_enforces_name_stem_rule(
        self, workspace: Path, ziggy_home_dir: Path
    ) -> None:
        path = workspace / "renamed.yaml"
        path.write_text(workflow_text("original"), encoding="utf-8")
        with pytest.raises(ValidationError) as exc:
            resolve(str(path), workspace)
        assert "stem" in str(exc.value)

    def test_direct_path_still_validates_schema(
        self, workspace: Path, ziggy_home_dir: Path
    ) -> None:
        path = workspace / "bad.yaml"
        path.write_text("version: 1\nname: bad\nsteps:\n  a:\n    type: shell\n", encoding="utf-8")
        with pytest.raises(ValidationError) as exc:
            resolve(str(path), workspace)
        assert "step type 'shell' is not supported" in str(exc.value)
