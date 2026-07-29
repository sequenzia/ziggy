"""Unit tests for ziggy.store.index (derived SQLite index, reindex, recovery)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from ziggy.errors import PersistenceError
from ziggy.ids import new_run_id
from ziggy.store import IndexRow, RunIndex, RunStore
from ziggy.store.runstore import RESULT_FILENAME, WRITER_SENTINEL


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "home")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "home" / "runs" / "index.db"


def make_row(run_id: str | None = None, **overrides: Any) -> IndexRow:
    base: dict[str, Any] = {
        "run_id": run_id or new_run_id(),
        "kind": "agent",
        "target": "claude",
        "status": "success",
        "started_at": "2026-07-28T10:00:00Z",
        "ended_at": "2026-07-28T10:01:00Z",
        "duration_ms": 60000,
        "workspace": "/tmp/ws",
        "result_path": "/tmp/ws/result.json",
    }
    base.update(overrides)
    return IndexRow(**base)


def persist_run(
    store: RunStore,
    *,
    status: str = "success",
    started_at: str = "2026-07-28T10:00:00Z",
    kind: str = "agent",
    target: str = "claude",
    schema_version: int = 1,
) -> str:
    run_id = new_run_id()
    writer = store.open_run(run_id)
    manifest = {
        "schema_version": schema_version,
        "run_id": run_id,
        "kind": kind,
        "target": target,
        "status": status,
        "started_at": started_at,
        "ended_at": "2026-07-28T10:05:00Z",
        "duration_ms": 300000,
        "workspace": "/tmp/ws",
    }
    writer.write_result(json.dumps(manifest))
    writer.finalize()
    return run_id


def age_dir(path: Path, seconds: float = 60.0) -> None:
    """Back-date a run dir so no-sentinel recovery does not treat it as ambiguous."""
    stamp = path.stat().st_mtime - seconds
    os.utime(path, (stamp, stamp))


def dead_pid() -> int:
    proc = subprocess.Popen(["/bin/sleep", "0"])
    proc.wait()
    return proc.pid


class TestInit:
    def test_creates_schema_and_meta(self, db_path: Path) -> None:
        with RunIndex(db_path):
            pass
        conn = sqlite3.connect(db_path)
        try:
            version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            assert version == ("1",)
            columns = [row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()]
            assert columns == [
                "run_id",
                "kind",
                "target",
                "status",
                "started_at",
                "ended_at",
                "duration_ms",
                "workspace",
                "result_path",
            ]
            mode = conn.execute("PRAGMA journal_mode").fetchone()
            assert mode is not None and mode[0].lower() == "wal"
        finally:
            conn.close()

    def test_idempotent_reinit_keeps_rows(self, db_path: Path) -> None:
        row = make_row()
        with RunIndex(db_path) as index:
            index.insert_or_replace(row)
        with RunIndex(db_path) as index:
            assert index.get(row.run_id) == row

    def test_db_file_mode_0600(self, db_path: Path) -> None:
        with RunIndex(db_path):
            pass
        assert (db_path.stat().st_mode & 0o777) == 0o600

    def test_future_schema_rejected(self, db_path: Path) -> None:
        with RunIndex(db_path):
            pass
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
        conn.commit()
        conn.close()
        with pytest.raises(PersistenceError, match="unsupported index schema"):
            RunIndex(db_path)

    def test_garbage_schema_rejected(self, db_path: Path) -> None:
        with RunIndex(db_path):
            pass
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE meta SET value='bananas' WHERE key='schema_version'")
        conn.commit()
        conn.close()
        with pytest.raises(PersistenceError, match="unsupported index schema"):
            RunIndex(db_path)

    def test_corrupt_db_file_raises_persistence_error(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_bytes(b"this is not a sqlite database at all")
        with pytest.raises(PersistenceError):
            RunIndex(db_path)

    def test_concurrent_init_two_threads(self, db_path: Path) -> None:
        barrier = threading.Barrier(2)
        errors: list[Exception] = []
        rows = [make_row(), make_row()]

        def worker(row: IndexRow) -> None:
            try:
                barrier.wait(timeout=10)
                with RunIndex(db_path) as index:
                    index.insert_or_replace(row)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(row,)) for row in rows]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert errors == []
        with RunIndex(db_path) as index:
            assert {r.run_id for r in index.list_runs()} == {r.run_id for r in rows}

    def test_concurrent_init_two_processes(self, db_path: Path) -> None:
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "from ziggy.store import IndexRow, RunIndex\n"
            "with RunIndex(Path(sys.argv[1])) as ix:\n"
            "    ix.insert_or_replace(IndexRow(\n"
            "        run_id=sys.argv[2], kind='agent', target='t', status='success',\n"
            "        started_at='2026-07-28T10:00:00Z', ended_at=None, duration_ms=None,\n"
            "        workspace='/w', result_path='/r'))\n"
        )
        run_ids = [new_run_id(), new_run_id()]
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(db_path), run_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for run_id in run_ids
        ]
        for proc in procs:
            _, stderr = proc.communicate(timeout=60)
            assert proc.returncode == 0, stderr.decode()
        with RunIndex(db_path) as index:
            assert {r.run_id for r in index.list_runs()} == set(run_ids)


class TestRowOperations:
    def test_insert_get_delete(self, db_path: Path) -> None:
        row = make_row()
        with RunIndex(db_path) as index:
            index.insert_or_replace(row)
            assert index.get(row.run_id) == row
            assert index.delete(row.run_id) is True
            assert index.get(row.run_id) is None
            assert index.delete(row.run_id) is False

    def test_insert_or_replace_updates(self, db_path: Path) -> None:
        row = make_row(status="failed", ended_at=None, duration_ms=None)
        with RunIndex(db_path) as index:
            index.insert_or_replace(row)
            updated = make_row(row.run_id, status="success")
            index.insert_or_replace(updated)
            assert index.get(row.run_id) == updated
            assert len(index.list_runs()) == 1


class TestListRuns:
    def _seed(self, index: RunIndex) -> dict[str, IndexRow]:
        rows = {
            "old_success": make_row(started_at="2026-07-01T00:00:00Z"),
            "failed": make_row(started_at="2026-07-02T00:00:00Z", status="failed"),
            "partial": make_row(started_at="2026-07-03T00:00:00Z", status="partial"),
            "cancelled": make_row(started_at="2026-07-04T00:00:00Z", status="cancelled"),
            "abandoned": make_row(started_at="2026-07-05T00:00:00Z", status="abandoned"),
            "workflow": make_row(
                started_at="2026-07-06T00:00:00Z", kind="workflow", target="release"
            ),
            "new_success": make_row(started_at="2026-07-07T00:00:00Z"),
        }
        for row in rows.values():
            index.insert_or_replace(row)
        return rows

    def test_newest_first_and_limit(self, db_path: Path) -> None:
        with RunIndex(db_path) as index:
            rows = self._seed(index)
            listed = index.list_runs()
            assert [r.started_at for r in listed] == sorted(
                (r.started_at for r in rows.values()), reverse=True
            )
            assert [r.run_id for r in index.list_runs(limit=2)] == [
                rows["new_success"].run_id,
                rows["workflow"].run_id,
            ]

    def test_failed_only(self, db_path: Path) -> None:
        with RunIndex(db_path) as index:
            rows = self._seed(index)
            failed = index.list_runs(failed_only=True)
            assert {r.run_id for r in failed} == {
                rows["failed"].run_id,
                rows["partial"].run_id,
                rows["abandoned"].run_id,
            }

    def test_kind_filter(self, db_path: Path) -> None:
        with RunIndex(db_path) as index:
            rows = self._seed(index)
            listed = index.list_runs(kind="workflow")
            assert [r.run_id for r in listed] == [rows["workflow"].run_id]

    def test_agent_target_filter(self, db_path: Path) -> None:
        with RunIndex(db_path) as index:
            rows = self._seed(index)
            listed = index.list_runs(agent_target="release")
            assert [r.run_id for r in listed] == [rows["workflow"].run_id]

    def test_since_iso_filter(self, db_path: Path) -> None:
        with RunIndex(db_path) as index:
            rows = self._seed(index)
            listed = index.list_runs(since_iso="2026-07-05T00:00:00Z")
            assert [r.run_id for r in listed] == [
                rows["new_success"].run_id,
                rows["workflow"].run_id,
                rows["abandoned"].run_id,
            ]

    def test_combined_filters(self, db_path: Path) -> None:
        with RunIndex(db_path) as index:
            rows = self._seed(index)
            listed = index.list_runs(
                failed_only=True, kind="agent", since_iso="2026-07-03T00:00:00Z"
            )
            assert {r.run_id for r in listed} == {
                rows["partial"].run_id,
                rows["abandoned"].run_id,
            }


class TestReindex:
    def test_rebuilds_from_manifests_after_db_loss(self, store: RunStore) -> None:
        run_a = persist_run(store, started_at="2026-07-28T09:00:00Z")
        run_b = persist_run(store, status="failed", started_at="2026-07-28T10:00:00Z")
        db = store.index_db_path
        with RunIndex(db) as index:
            index.reindex(store)
            assert index.get(run_a) is not None
        for suffix in ("", "-wal", "-shm"):
            Path(str(db) + suffix).unlink(missing_ok=True)
        with RunIndex(db) as index:
            count = index.reindex(store)
            assert count == 2
            row_b = index.get(run_b)
            assert row_b is not None
            assert row_b.status == "failed"
            assert row_b.result_path == str(store.run_dir(run_b) / RESULT_FILENAME)
            assert index.get(run_a) is not None

    def test_removes_stale_rows_atomically(self, store: RunStore) -> None:
        run_id = persist_run(store)
        with RunIndex(store.index_db_path) as index:
            index.insert_or_replace(make_row())  # stale: no manifest on disk
            count = index.reindex(store)
            assert count == 1
            listed = index.list_runs()
            assert [r.run_id for r in listed] == [run_id]

    def test_keeps_run_persisted_concurrently_during_scan(
        self, store: RunStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A run committed by another process *during* the manifest scan must
        survive reindex — it must not be wiped by a blind DELETE-all."""
        existing = persist_run(store)
        concurrent_id = new_run_id()
        real_iter = store.iter_run_dirs

        def racing_iter() -> Any:
            yield from real_iter()
            # After the last dir is yielded (scan complete), a concurrent process
            # persists a new run: manifest on disk AND its index row committed
            # through a separate connection, before our reindex transaction runs.
            writer = store.open_run(concurrent_id)
            manifest = {
                "schema_version": 1,
                "run_id": concurrent_id,
                "kind": "agent",
                "target": "claude",
                "status": "success",
                "started_at": "2026-07-28T11:00:00Z",
                "ended_at": "2026-07-28T11:01:00Z",
                "duration_ms": 60000,
                "workspace": "/tmp/ws",
            }
            writer.write_result(json.dumps(manifest))
            writer.finalize()
            with RunIndex(store.index_db_path) as other:
                other.insert_or_replace(
                    make_row(
                        concurrent_id,
                        started_at="2026-07-28T11:00:00Z",
                        result_path=str(store.run_dir(concurrent_id) / RESULT_FILENAME),
                    )
                )

        monkeypatch.setattr(store, "iter_run_dirs", racing_iter)
        with RunIndex(store.index_db_path) as index:
            index.reindex(store)
            assert index.get(concurrent_id) is not None, "concurrent run was wiped by reindex"
            assert index.get(existing) is not None

    def test_skips_unsupported_and_incomplete_manifests(self, store: RunStore) -> None:
        good = persist_run(store)
        persist_run(store, schema_version=99)  # future schema: excluded entirely
        run_no_manifest = new_run_id()
        store.open_run(run_no_manifest).finalize()
        with RunIndex(store.index_db_path) as index:
            count = index.reindex(store)
            assert count == 1
            assert [r.run_id for r in index.list_runs()] == [good]


class TestRecoverAbandoned:
    def test_no_sentinel_dir_recovered(self, store: RunStore) -> None:
        run_id = new_run_id()
        run_dir = store.runs_dir / run_id
        run_dir.mkdir()
        age_dir(run_dir)
        with RunIndex(store.index_db_path) as index:
            recovered = index.recover_abandoned(store)
            assert recovered == [run_id]
            manifest = store.read_result(run_id)
            assert manifest["status"] == "abandoned"
            assert manifest["schema_version"] == 1
            assert manifest["run_id"] == run_id
            assert manifest["errors"][0]["code"] == "AbandonedError"
            row = index.get(run_id)
            assert row is not None
            assert row.status == "abandoned"
            assert row.kind == "unknown"

    def test_dead_pid_sentinel_recovered(self, store: RunStore) -> None:
        run_id = new_run_id()
        writer = store.open_run(run_id)
        writer.append_event('{"seq": 1}')
        writer.fsync_events()
        sentinel = writer.run_dir / WRITER_SENTINEL
        sentinel.write_text(
            json.dumps({"pid": dead_pid(), "run_id": run_id, "started_at": "2026-07-28T08:00:00Z"})
        )
        with RunIndex(store.index_db_path) as index:
            recovered = index.recover_abandoned(store)
            assert recovered == [run_id]
            assert not sentinel.exists()
            manifest = store.read_result(run_id)
            assert manifest["status"] == "abandoned"
            assert manifest["started_at"] == "2026-07-28T08:00:00Z"
            row = index.get(run_id)
            assert row is not None
            assert row.status == "abandoned"

    def test_live_pid_sentinel_left_untouched(self, store: RunStore) -> None:
        run_id = new_run_id()
        writer = store.open_run(run_id)  # sentinel holds our live pid
        with RunIndex(store.index_db_path) as index:
            assert index.recover_abandoned(store) == []
            assert not (writer.run_dir / RESULT_FILENAME).exists()
            assert (writer.run_dir / WRITER_SENTINEL).exists()
            assert index.get(run_id) is None
        writer.finalize()

    def test_live_pid_matching_marker_left_untouched(self, store: RunStore) -> None:
        run_id = new_run_id()
        writer = store.open_run(run_id)  # real sentinel: our live pid + real marker
        data = json.loads((writer.run_dir / WRITER_SENTINEL).read_text())
        assert data["process_start"], "writer sentinel must record a start marker"
        with RunIndex(store.index_db_path) as index:
            assert index.recover_abandoned(store) == []
            assert not (writer.run_dir / RESULT_FILENAME).exists()
            assert (writer.run_dir / WRITER_SENTINEL).exists()
        writer.finalize()

    def test_reused_pid_mismatched_marker_recovered(self, store: RunStore) -> None:
        """A live pid whose recorded start marker no longer matches is a reused
        pid: the original writer is provably gone, so the run is finalized."""
        run_id = new_run_id()
        writer = store.open_run(run_id)  # sentinel holds OUR live pid
        writer.append_event('{"seq": 1}')
        writer.fsync_events()
        sentinel = writer.run_dir / WRITER_SENTINEL
        data = json.loads(sentinel.read_text())
        assert data["pid"] == os.getpid()
        # Same (live) pid, but a start marker that cannot match this incarnation.
        data["process_start"] = "ps:not-the-original-incarnation"
        sentinel.write_text(json.dumps(data))
        with RunIndex(store.index_db_path) as index:
            recovered = index.recover_abandoned(store)
            assert recovered == [run_id]
            assert not sentinel.exists()
            manifest = store.read_result(run_id)
            assert manifest["status"] == "abandoned"
            row = index.get(run_id)
            assert row is not None
            assert row.status == "abandoned"

    def test_live_pid_missing_marker_is_ambiguous(self, store: RunStore) -> None:
        """A sentinel without a start marker cannot disprove a live pid: left be."""
        run_id = new_run_id()
        writer = store.open_run(run_id)
        sentinel = writer.run_dir / WRITER_SENTINEL
        # Legacy-shaped sentinel: our live pid, no process_start field.
        sentinel.write_text(
            json.dumps({"pid": os.getpid(), "run_id": run_id, "started_at": "2026-07-28T08:00:00Z"})
        )
        with RunIndex(store.index_db_path) as index:
            assert index.recover_abandoned(store) == []
            assert not (writer.run_dir / RESULT_FILENAME).exists()
            assert sentinel.exists()
        writer.finalize()

    def test_corrupt_sentinel_is_ambiguous(self, store: RunStore) -> None:
        run_id = new_run_id()
        run_dir = store.runs_dir / run_id
        run_dir.mkdir()
        (run_dir / WRITER_SENTINEL).write_text("not json {")
        age_dir(run_dir)
        with RunIndex(store.index_db_path) as index:
            assert index.recover_abandoned(store) == []
            assert not (run_dir / RESULT_FILENAME).exists()

    def test_recent_no_sentinel_dir_is_ambiguous(self, store: RunStore) -> None:
        run_id = new_run_id()
        (store.runs_dir / run_id).mkdir()  # fresh: writer may be between mkdir and sentinel
        with RunIndex(store.index_db_path) as index:
            assert index.recover_abandoned(store) == []
            assert not (store.runs_dir / run_id / RESULT_FILENAME).exists()

    def test_durable_manifest_never_touched(self, store: RunStore) -> None:
        run_id = persist_run(store)
        run_dir = store.run_dir(run_id)
        # simulate crash after result write but before sentinel cleanup
        (run_dir / WRITER_SENTINEL).write_text(json.dumps({"pid": dead_pid()}))
        before = (run_dir / RESULT_FILENAME).read_bytes()
        with RunIndex(store.index_db_path) as index:
            assert index.recover_abandoned(store) == []
        assert (run_dir / RESULT_FILENAME).read_bytes() == before

    def test_facts_derived_from_events(self, store: RunStore) -> None:
        run_id = new_run_id()
        run_dir = store.runs_dir / run_id
        run_dir.mkdir()
        envelope = {
            "seq": 1,
            "ts": "2026-07-28T07:00:00Z",
            "event_type": "run_started",
            "payload": {"kind": "workflow", "target": "release", "workspace": "/tmp/proj"},
        }
        (run_dir / "events.jsonl").write_text(json.dumps(envelope) + "\n")
        age_dir(run_dir)
        with RunIndex(store.index_db_path) as index:
            assert index.recover_abandoned(store) == [run_id]
            row = index.get(run_id)
            assert row is not None
            assert row.kind == "workflow"
            assert row.target == "release"
            assert row.workspace == "/tmp/proj"
            assert row.started_at == "2026-07-28T07:00:00Z"

    def test_recovered_run_visible_in_failed_listing(self, store: RunStore) -> None:
        run_id = new_run_id()
        run_dir = store.runs_dir / run_id
        run_dir.mkdir()
        age_dir(run_dir)
        with RunIndex(store.index_db_path) as index:
            index.recover_abandoned(store)
            assert [r.run_id for r in index.list_runs(failed_only=True)] == [run_id]
