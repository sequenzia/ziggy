"""Unit tests for ziggy.engine.lease (spec §6.4 workspace lease, REQ-010)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from ziggy.engine.lease import (
    AcquiredLease,
    LeaseManager,
    _process_start_marker,
    canonical_workspace_hash,
    lease_path,
)
from ziggy.errors import WorkspaceBusyError
from ziggy.models import WorkspaceLease
from ziggy.store import RunStore


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "home")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def manager() -> LeaseManager:
    return LeaseManager()


def make_lease(
    workspace: Path,
    *,
    run_id: str,
    pid: int,
    marker: str,
    pgid: int,
    acquired_at: str = "2026-07-28T00:00:00Z",
) -> WorkspaceLease:
    return WorkspaceLease(
        canonical_workspace_hash=canonical_workspace_hash(workspace),
        workspace=Path(os.path.realpath(workspace)).as_posix(),
        run_id=run_id,
        owner_pid=pid,
        owner_process_start=marker,
        owner_process_group=pgid,
        acquired_at=acquired_at,
    )


def write_lease(store: RunStore, workspace: Path, lease: WorkspaceLease) -> Path:
    path = lease_path(store, workspace)
    path.write_text(lease.model_dump_json(), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


@pytest.fixture
def dead_child() -> tuple[int, str, int]:
    """(pid, start-marker, pgid) of a child that has fully exited.

    The marker is captured while the child is alive; ``start_new_session``
    gives the child its own process group (pgid == pid), so after exit both
    the pid and the recorded pgid probe report ESRCH.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        marker = _process_start_marker(proc.pid)
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=30)
    assert marker is not None, "could not capture start marker for live child"
    return proc.pid, marker, proc.pid


class TestAcquireRelease:
    def test_round_trip_and_file_mode(
        self, manager: LeaseManager, store: RunStore, workspace: Path
    ) -> None:
        acquired = manager.acquire(store, workspace, "run-alpha")
        expected = store.leases_dir / f"{canonical_workspace_hash(workspace)}.json"
        assert acquired.path == expected
        assert acquired.path.is_file()
        assert mode_of(acquired.path) == 0o600

        persisted = WorkspaceLease.model_validate_json(acquired.path.read_bytes())
        assert persisted.run_id == "run-alpha"
        assert persisted.owner_pid == os.getpid()
        assert persisted.owner_process_group == os.getpgrp()
        canonical = Path(os.path.realpath(workspace)).as_posix()
        assert persisted.workspace == canonical
        assert (
            persisted.canonical_workspace_hash
            == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        )

        acquired.release()
        assert not acquired.path.exists()
        # workspace is free again
        again = manager.acquire(store, workspace, "run-beta")
        assert again.path == expected
        again.release()

    def test_context_manager_releases(
        self, manager: LeaseManager, store: RunStore, workspace: Path
    ) -> None:
        with manager.acquire(store, workspace, "run-ctx") as acquired:
            assert isinstance(acquired, AcquiredLease)
            assert acquired.path.is_file()
        assert not acquired.path.exists()

    def test_release_idempotent(
        self, manager: LeaseManager, store: RunStore, workspace: Path
    ) -> None:
        acquired = manager.acquire(store, workspace, "run-idem")
        acquired.release()
        acquired.release()  # second call is a no-op, no error
        assert not acquired.path.exists()

    def test_release_tolerates_already_gone(
        self, manager: LeaseManager, store: RunStore, workspace: Path
    ) -> None:
        acquired = manager.acquire(store, workspace, "run-gone")
        acquired.path.unlink()
        acquired.release()  # no error

    def test_release_does_not_remove_someone_elses_lease(
        self, manager: LeaseManager, store: RunStore, workspace: Path
    ) -> None:
        acquired = manager.acquire(store, workspace, "run-ours")
        other = make_lease(
            workspace,
            run_id="run-theirs",
            pid=os.getpid(),
            marker=_process_start_marker(os.getpid()) or "",
            pgid=os.getpgrp(),
        )
        write_lease(store, workspace, other)
        acquired.release()
        assert acquired.path.is_file()
        assert json.loads(acquired.path.read_text())["run_id"] == "run-theirs"


class TestContention:
    def test_second_acquire_same_workspace_busy_with_holder(
        self, manager: LeaseManager, store: RunStore, workspace: Path
    ) -> None:
        first = manager.acquire(store, workspace, "run-holder")
        with pytest.raises(WorkspaceBusyError) as excinfo:
            manager.acquire(store, workspace, "run-second")
        details = excinfo.value.details
        assert details["run_id"] == "run-holder"
        assert details["reason"] == "held"
        assert details["workspace"] == Path(os.path.realpath(workspace)).as_posix()
        assert details["acquired_at"] == first.lease.acquired_at
        # holder is intact and can still release
        first.release()
        assert not first.path.exists()

    def test_different_workspace_ok(
        self, manager: LeaseManager, store: RunStore, tmp_path: Path
    ) -> None:
        ws_a = tmp_path / "a"
        ws_b = tmp_path / "b"
        ws_a.mkdir()
        ws_b.mkdir()
        lease_a = manager.acquire(store, ws_a, "run-a")
        lease_b = manager.acquire(store, ws_b, "run-b")
        assert lease_a.path != lease_b.path
        lease_a.release()
        lease_b.release()

    def test_symlinked_workspace_hashes_to_realpath_target(
        self, manager: LeaseManager, store: RunStore, tmp_path: Path
    ) -> None:
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"
        link.symlink_to(target)
        assert lease_path(store, link) == lease_path(store, target)
        assert canonical_workspace_hash(link) == canonical_workspace_hash(target)

        held = manager.acquire(store, link, "run-via-link")
        with pytest.raises(WorkspaceBusyError) as excinfo:
            manager.acquire(store, target, "run-via-target")
        assert excinfo.value.details["run_id"] == "run-via-link"
        held.release()


class TestStaleRecovery:
    def test_dead_pid_lease_recovered(
        self,
        manager: LeaseManager,
        store: RunStore,
        workspace: Path,
        dead_child: tuple[int, str, int],
    ) -> None:
        pid, marker, pgid = dead_child
        write_lease(
            store,
            workspace,
            make_lease(workspace, run_id="run-stale", pid=pid, marker=marker, pgid=pgid),
        )
        acquired = manager.acquire(store, workspace, "run-recovered")
        assert acquired.path.is_file()
        assert mode_of(acquired.path) == 0o600
        persisted = WorkspaceLease.model_validate_json(acquired.path.read_bytes())
        assert persisted.run_id == "run-recovered"
        assert persisted.owner_pid == os.getpid()
        acquired.release()

    def test_dead_pid_with_pgid_le_one_skips_group_probe(
        self,
        manager: LeaseManager,
        store: RunStore,
        workspace: Path,
        dead_child: tuple[int, str, int],
    ) -> None:
        pid, marker, _ = dead_child
        write_lease(
            store,
            workspace,
            make_lease(workspace, run_id="run-stale", pid=pid, marker=marker, pgid=0),
        )
        acquired = manager.acquire(store, workspace, "run-recovered")
        assert json.loads(acquired.path.read_text())["run_id"] == "run-recovered"
        acquired.release()

    def test_dead_pid_but_live_pgid_stays_busy(
        self,
        manager: LeaseManager,
        store: RunStore,
        workspace: Path,
        dead_child: tuple[int, str, int],
    ) -> None:
        pid, marker, _ = dead_child
        # recorded process group is OUR live group: not provably dead
        write_lease(
            store,
            workspace,
            make_lease(workspace, run_id="run-stale", pid=pid, marker=marker, pgid=os.getpgrp()),
        )
        with pytest.raises(WorkspaceBusyError) as excinfo:
            manager.acquire(store, workspace, "run-new")
        assert excinfo.value.details["reason"] == "ambiguous"
        assert excinfo.value.details["run_id"] == "run-stale"

    def test_concurrent_recovery_exactly_one_wins(
        self,
        store: RunStore,
        workspace: Path,
        dead_child: tuple[int, str, int],
    ) -> None:
        pid, marker, pgid = dead_child
        write_lease(
            store,
            workspace,
            make_lease(workspace, run_id="run-stale", pid=pid, marker=marker, pgid=pgid),
        )
        barrier = threading.Barrier(2)
        results: dict[str, AcquiredLease | WorkspaceBusyError] = {}

        def contender(run_id: str) -> None:
            barrier.wait(timeout=30)
            try:
                results[run_id] = LeaseManager().acquire(store, workspace, run_id)
            except WorkspaceBusyError as exc:
                results[run_id] = exc

        threads = [threading.Thread(target=contender, args=(rid,)) for rid in ("run-a", "run-b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        wins = [r for r in results.values() if isinstance(r, AcquiredLease)]
        busy = [r for r in results.values() if isinstance(r, WorkspaceBusyError)]
        assert len(wins) == 1
        assert len(busy) == 1
        persisted = json.loads(wins[0].path.read_text())
        assert persisted["run_id"] == wins[0].lease.run_id
        assert busy[0].details["run_id"] == wins[0].lease.run_id
        wins[0].release()

    def test_replace_readback_detects_cross_process_winner(
        self,
        manager: LeaseManager,
        store: RunStore,
        workspace: Path,
        dead_child: tuple[int, str, int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Simulate another process winning between our os.replace and read-back."""
        pid, marker, pgid = dead_child
        target = write_lease(
            store,
            workspace,
            make_lease(workspace, run_id="run-stale", pid=pid, marker=marker, pgid=pgid),
        )
        competitor = make_lease(
            workspace,
            run_id="run-competitor",
            pid=os.getpid(),
            marker=_process_start_marker(os.getpid()) or "",
            pgid=os.getpgrp(),
        )
        real_replace = os.replace
        hijacked = False

        def racing_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            nonlocal hijacked
            real_replace(src, dst)
            if Path(dst) == target and not hijacked:
                hijacked = True
                target.write_text(competitor.model_dump_json(), encoding="utf-8")

        monkeypatch.setattr(os, "replace", racing_replace)
        with pytest.raises(WorkspaceBusyError) as excinfo:
            manager.acquire(store, workspace, "run-loser")
        assert hijacked
        assert excinfo.value.details["run_id"] == "run-competitor"
        assert excinfo.value.details["reason"] == "held"
        assert json.loads(target.read_text())["run_id"] == "run-competitor"


class TestAmbiguousStaysBusy:
    def test_eperm_liveness_probe_is_ambiguous(
        self,
        manager: LeaseManager,
        store: RunStore,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_lease(
            store,
            workspace,
            make_lease(
                workspace,
                run_id="run-other-user",
                pid=os.getpid(),
                marker="ps:x",
                pgid=os.getpgrp(),
            ),
        )

        def eperm_kill(pid: int, sig: int) -> None:
            raise PermissionError("Operation not permitted")

        monkeypatch.setattr(os, "kill", eperm_kill)
        with pytest.raises(WorkspaceBusyError) as excinfo:
            manager.acquire(store, workspace, "run-new")
        assert excinfo.value.details["reason"] == "ambiguous"
        assert excinfo.value.details["run_id"] == "run-other-user"
        # lease file untouched
        assert json.loads(lease_path(store, workspace).read_text())["run_id"] == "run-other-user"

    def test_marker_mismatch_with_live_pid_is_ambiguous(
        self, manager: LeaseManager, store: RunStore, workspace: Path
    ) -> None:
        write_lease(
            store,
            workspace,
            make_lease(
                workspace,
                run_id="run-reused-pid",
                pid=os.getpid(),
                marker="ps:definitely-not-the-real-marker",
                pgid=os.getpgrp(),
            ),
        )
        with pytest.raises(WorkspaceBusyError) as excinfo:
            manager.acquire(store, workspace, "run-new")
        assert excinfo.value.details["reason"] == "ambiguous"
        assert excinfo.value.details["run_id"] == "run-reused-pid"

    def test_corrupt_lease_file_is_ambiguous(
        self, manager: LeaseManager, store: RunStore, workspace: Path
    ) -> None:
        path = lease_path(store, workspace)
        path.write_bytes(b"{ this is not json")
        with pytest.raises(WorkspaceBusyError) as excinfo:
            manager.acquire(store, workspace, "run-new")
        assert excinfo.value.details["reason"] == "ambiguous"
        assert excinfo.value.details["run_id"] is None
        assert path.read_bytes() == b"{ this is not json"

    def test_wrong_shape_json_is_ambiguous(
        self, manager: LeaseManager, store: RunStore, workspace: Path
    ) -> None:
        path = lease_path(store, workspace)
        path.write_text(json.dumps({"run_id": "x"}), encoding="utf-8")
        with pytest.raises(WorkspaceBusyError) as excinfo:
            manager.acquire(store, workspace, "run-new")
        assert excinfo.value.details["reason"] == "ambiguous"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
    def test_unreadable_lease_file_is_ambiguous(
        self, manager: LeaseManager, store: RunStore, workspace: Path
    ) -> None:
        path = write_lease(
            store,
            workspace,
            make_lease(
                workspace,
                run_id="run-hidden",
                pid=os.getpid(),
                marker="ps:x",
                pgid=os.getpgrp(),
            ),
        )
        os.chmod(path, 0o000)
        try:
            with pytest.raises(WorkspaceBusyError) as excinfo:
                manager.acquire(store, workspace, "run-new")
            assert excinfo.value.details["reason"] == "ambiguous"
            assert excinfo.value.details["run_id"] is None
        finally:
            os.chmod(path, 0o600)


class TestProcessStartMarker:
    def test_current_pid_marker_stable(self) -> None:
        first = _process_start_marker(os.getpid())
        second = _process_start_marker(os.getpid())
        assert first is not None
        assert first == second

    def test_dead_pid_marker_unavailable(self, dead_child: tuple[int, str, int]) -> None:
        pid, _, _ = dead_child
        assert _process_start_marker(pid) is None

    def test_nonpositive_pid_marker_unavailable(self) -> None:
        assert _process_start_marker(0) is None
        assert _process_start_marker(-5) is None
