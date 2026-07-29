"""Unit tests for ziggy.store.runstore (REQ-005 run directories + atomic manifests)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from ziggy.errors import PersistenceError
from ziggy.ids import new_run_id
from ziggy.store import RunStore, ziggy_home
from ziggy.store.runstore import RESULT_FILENAME, WRITER_SENTINEL, process_start_marker


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def tmp_files(path: Path) -> list[Path]:
    return list(path.glob("*.tmp")) + list(path.glob(".*.tmp"))


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "home")


class TestZiggyHome:
    def test_default_is_home_dot_ziggy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ZIGGY_HOME", raising=False)
        assert ziggy_home() == Path.home() / ".ziggy"

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("ZIGGY_HOME", str(tmp_path / "custom"))
        assert ziggy_home() == tmp_path / "custom"

    def test_empty_env_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ZIGGY_HOME", "")
        assert ziggy_home() == Path.home() / ".ziggy"

    def test_run_store_defaults_to_ziggy_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("ZIGGY_HOME", str(tmp_path / "zh"))
        store = RunStore()
        assert store.runs_dir == tmp_path / "zh" / "runs"
        assert store.runs_dir.is_dir()


class TestLayoutModes:
    def test_directories_created_0700(self, store: RunStore) -> None:
        for path in (store.runs_dir, store.leases_dir, store.logs_dir):
            assert path.is_dir()
            assert mode_of(path) == 0o700
        assert mode_of(store.root) == 0o700

    def test_run_dir_and_files_modes(self, store: RunStore) -> None:
        run_id = new_run_id()
        writer = store.open_run(run_id)
        run_dir = store.run_dir(run_id)
        assert mode_of(run_dir) == 0o700
        assert mode_of(run_dir / WRITER_SENTINEL) == 0o600
        writer.append_event('{"seq": 1}')
        writer.flush()
        assert mode_of(writer.events_path) == 0o600
        writer.write_result('{"schema_version": 1}')
        assert mode_of(run_dir / RESULT_FILENAME) == 0o600
        writer.finalize()

    def test_changes_and_artifacts_lazy_create(self, store: RunStore) -> None:
        writer = store.open_run(new_run_id())
        assert not (writer.run_dir / "changes").exists()
        assert not (writer.run_dir / "artifacts").exists()
        changes = writer.changes_dir()
        artifacts = writer.artifacts_dir()
        assert changes.is_dir() and mode_of(changes) == 0o700
        assert artifacts.is_dir() and mode_of(artifacts) == 0o700
        writer.finalize()


class TestSingleWriter:
    def test_sentinel_contains_pid_and_start_marker(self, store: RunStore) -> None:
        run_id = new_run_id()
        writer = store.open_run(run_id)
        data = json.loads((writer.run_dir / WRITER_SENTINEL).read_text())
        assert data["pid"] == os.getpid()
        assert data["run_id"] == run_id
        assert data["started_at"].endswith("Z")
        # Process start marker distinguishes this incarnation from a reused pid,
        # so abandoned-run recovery is not blocked forever by pid reuse.
        assert data["process_start"] == process_start_marker(os.getpid())
        assert data["process_start"]
        writer.finalize()

    def test_second_writer_rejected(self, store: RunStore) -> None:
        run_id = new_run_id()
        writer = store.open_run(run_id)
        with pytest.raises(PersistenceError, match="active writer"):
            store.open_run(run_id)
        writer.finalize()

    def test_finalize_releases_sentinel_and_allows_reopen(self, store: RunStore) -> None:
        run_id = new_run_id()
        writer = store.open_run(run_id)
        writer.finalize()
        assert not (store.run_dir(run_id) / WRITER_SENTINEL).exists()
        writer2 = store.open_run(run_id)
        writer2.finalize()

    def test_finalize_idempotent(self, store: RunStore) -> None:
        writer = store.open_run(new_run_id())
        writer.finalize()
        writer.finalize()

    def test_append_after_finalize_raises(self, store: RunStore) -> None:
        writer = store.open_run(new_run_id())
        writer.finalize()
        with pytest.raises(PersistenceError, match="finalized"):
            writer.append_event("{}")
        with pytest.raises(PersistenceError, match="finalized"):
            writer.write_result("{}")

    def test_open_run_rejects_non_ulid(self, store: RunStore) -> None:
        with pytest.raises(PersistenceError, match="invalid run id"):
            store.open_run("not-a-ulid")


class TestEvents:
    def test_append_flush_fsync_roundtrip(self, store: RunStore) -> None:
        writer = store.open_run(new_run_id())
        writer.append_event('{"seq": 1}')
        writer.append_event('{"seq": 2}\n')
        writer.fsync_events()
        lines = writer.events_path.read_text().splitlines()
        assert lines == ['{"seq": 1}', '{"seq": 2}']
        writer.finalize()

    def test_fsync_without_events_is_noop(self, store: RunStore) -> None:
        writer = store.open_run(new_run_id())
        writer.flush()
        writer.fsync_events()
        assert not writer.events_path.exists()
        writer.finalize()

    def test_events_survive_finalize_and_reopen_appends(self, store: RunStore) -> None:
        run_id = new_run_id()
        writer = store.open_run(run_id)
        writer.append_event('{"seq": 1}')
        writer.finalize()
        writer2 = store.open_run(run_id)
        writer2.append_event('{"seq": 2}')
        writer2.finalize()
        lines = writer2.events_path.read_text().splitlines()
        assert lines == ['{"seq": 1}', '{"seq": 2}']


class TestAtomicResult:
    def test_write_result_content_and_no_temp_left(self, store: RunStore) -> None:
        writer = store.open_run(new_run_id())
        payload = json.dumps({"schema_version": 1, "run_id": writer.run_id})
        path = writer.write_result(payload)
        assert path == writer.run_dir / RESULT_FILENAME
        assert json.loads(path.read_text()) == json.loads(payload)
        assert tmp_files(writer.run_dir) == []
        writer.finalize()

    def test_write_result_replaces_atomically(self, store: RunStore) -> None:
        writer = store.open_run(new_run_id())
        writer.write_result('{"schema_version": 1, "v": "first"}')
        writer.write_result('{"schema_version": 1, "v": "second"}')
        data = json.loads((writer.run_dir / RESULT_FILENAME).read_text())
        assert data["v"] == "second"
        assert tmp_files(writer.run_dir) == []
        writer.finalize()

    def test_simulated_crash_between_temp_and_rename(
        self, store: RunStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        writer = store.open_run(new_run_id())

        def boom(src: object, dst: object) -> None:
            raise OSError("simulated crash before rename")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(PersistenceError, match="atomic write"):
            writer.write_result('{"schema_version": 1}')
        monkeypatch.undo()
        assert not (writer.run_dir / RESULT_FILENAME).exists()
        assert tmp_files(writer.run_dir) == []
        writer.write_result('{"schema_version": 1}')
        assert (writer.run_dir / RESULT_FILENAME).exists()
        writer.finalize()


class TestReadResult:
    def _persist(self, store: RunStore, manifest: dict[str, object]) -> str:
        run_id = new_run_id()
        writer = store.open_run(run_id)
        writer.write_result(json.dumps(manifest))
        writer.finalize()
        return run_id

    def test_roundtrip(self, store: RunStore) -> None:
        run_id = self._persist(store, {"schema_version": 1, "status": "success"})
        assert store.read_result(run_id)["status"] == "success"

    def test_missing_manifest(self, store: RunStore) -> None:
        run_id = new_run_id()
        store.open_run(run_id).finalize()
        with pytest.raises(PersistenceError, match="missing"):
            store.read_result(run_id)

    def test_missing_run_dir(self, store: RunStore) -> None:
        with pytest.raises(PersistenceError, match="missing"):
            store.read_result(new_run_id())

    def test_invalid_json(self, store: RunStore) -> None:
        run_id = new_run_id()
        writer = store.open_run(run_id)
        writer.write_result("{truncated")
        writer.finalize()
        with pytest.raises(PersistenceError, match="not valid JSON"):
            store.read_result(run_id)

    def test_non_object_json(self, store: RunStore) -> None:
        run_id = new_run_id()
        writer = store.open_run(run_id)
        writer.write_result("[1, 2]")
        writer.finalize()
        with pytest.raises(PersistenceError, match="not a JSON object"):
            store.read_result(run_id)

    def test_future_schema_rejected_whole(self, store: RunStore) -> None:
        run_id = self._persist(store, {"schema_version": 99, "status": "success", "run_id": "x"})
        with pytest.raises(PersistenceError, match="unsupported schema"):
            store.read_result(run_id)

    def test_non_int_schema_rejected(self, store: RunStore) -> None:
        run_id = self._persist(store, {"schema_version": "1"})
        with pytest.raises(PersistenceError, match="invalid schema_version"):
            store.read_result(run_id)

    def test_missing_schema_rejected(self, store: RunStore) -> None:
        run_id = self._persist(store, {"status": "success"})
        with pytest.raises(PersistenceError, match="invalid schema_version"):
            store.read_result(run_id)


class TestIterRunDirs:
    def test_yields_only_ulid_dirs(self, store: RunStore) -> None:
        run_ids = sorted(new_run_id() for _ in range(3))
        for run_id in run_ids:
            store.open_run(run_id).finalize()
        (store.runs_dir / "index.db").write_bytes(b"not a run")
        (store.runs_dir / "not-a-ulid").mkdir()
        found = [p.name for p in store.iter_run_dirs()]
        assert found == run_ids

    def test_empty_store(self, store: RunStore) -> None:
        assert list(store.iter_run_dirs()) == []
