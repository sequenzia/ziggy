"""SQLite run index: a derived view over durable ``result.json`` manifests.

WAL mode + busy timeout + ``BEGIN IMMEDIATE`` transactions make the index safe
for multiple concurrent Ziggy processes (§10.3). The index is never the source
of truth: it can be rebuilt at any time from manifests (``reindex``), and run
directories that crashed before persisting a manifest are conservatively
finalized as ``abandoned`` (``recover_abandoned``, §6.4).
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ziggy import INDEX_SCHEMA_VERSION, RESULT_SCHEMA_VERSION
from ziggy.errors import AbandonedError, PersistenceError
from ziggy.ids import utc_now_iso
from ziggy.models.common import RunStatus
from ziggy.store.runstore import (
    FILE_MODE,
    RESULT_FILENAME,
    WRITER_SENTINEL,
    RunStore,
    atomic_write_bytes,
    process_start_marker,
)

_BUSY_TIMEOUT_MS = 5000

#: A run dir with no ``.writer`` sentinel and no manifest may be a writer that
#: was created microseconds ago (mkdir happens before the O_EXCL sentinel).
#: Directories younger than this are treated as ambiguous and left untouched.
_NO_SENTINEL_GRACE_SECONDS = 5.0

_META_DDL = "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
_RUNS_DDL = """CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL,
  status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
  duration_ms INTEGER, workspace TEXT NOT NULL, result_path TEXT NOT NULL);"""
_IDX_DDL = "CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);"

_COLUMNS = "run_id, kind, target, status, started_at, ended_at, duration_ms, workspace, result_path"

#: Statuses returned by ``list_runs(failed_only=True)``: everything that ended
#: without completing all requested work (user-initiated cancellation excluded).
FAILED_STATUSES = (
    RunStatus.FAILED.value,
    RunStatus.PARTIAL.value,
    RunStatus.ABANDONED.value,
)


@dataclass(slots=True)
class IndexRow:
    run_id: str
    kind: str
    target: str
    status: str
    started_at: str
    ended_at: str | None
    duration_ms: int | None
    workspace: str
    result_path: str


def _pid_provably_dead(pid: int) -> bool:
    """True only when no process with ``pid`` exists right now.

    A live pid may be a reused pid, and ``PermissionError`` means some process
    exists but is not ours to signal — both are ambiguous, so both return False.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _writer_provably_dead(owner: dict[str, Any]) -> bool:
    """True when the sentinel's writer is provably gone.

    Provably dead in two ways: the recorded pid no longer exists (``ESRCH``),
    or the pid *is* alive but belongs to a different process incarnation — the
    recorded process start marker no longer matches the marker of whatever runs
    at that pid now (a reused pid). Everything else — a live matching marker, or
    a marker that is unavailable on either side — is ambiguous and stays False,
    so a still-running writer or an unverifiable one is never clobbered.
    """
    pid = owner["pid"]
    if _pid_provably_dead(pid):
        return True
    recorded = owner.get("process_start")
    if not isinstance(recorded, str) or not recorded:
        return False  # no marker recorded: cannot disprove liveness
    current = process_start_marker(pid)
    if current is None:
        return False  # marker unavailable now: ambiguous, leave untouched
    return current != recorded


def _read_sentinel(path: Path) -> dict[str, Any] | None:
    """Parse a ``.writer`` sentinel; None when corrupt (treated as ambiguous)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8")[:4096])
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    return data


def _derive_run_facts(run_dir: Path) -> dict[str, str]:
    """Best-effort kind/target/workspace/started_at from the first events.jsonl line."""
    facts: dict[str, str] = {}
    try:
        with open(run_dir / "events.jsonl", encoding="utf-8", errors="replace") as fh:
            envelope = json.loads(fh.readline(65536))
    except (OSError, ValueError):
        return facts
    if not isinstance(envelope, dict):
        return facts
    ts = envelope.get("ts")
    if isinstance(ts, str) and ts:
        facts["started_at"] = ts
    payload = envelope.get("payload")
    if isinstance(payload, dict):
        for key in ("kind", "target", "workspace"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                facts[key] = value
    return facts


def _mtime_iso(path: Path) -> str:
    try:
        ts = path.stat().st_mtime
    except OSError:
        return utc_now_iso()
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def _row_from_manifest(data: dict[str, Any], result_path: str) -> IndexRow | None:
    def required(key: str) -> str | None:
        value = data.get(key)
        return value if isinstance(value, str) and value else None

    run_id = required("run_id")
    kind = required("kind")
    target = required("target")
    status = required("status")
    started_at = required("started_at")
    workspace = required("workspace")
    if None in (run_id, kind, target, status, started_at, workspace):
        return None
    ended_at = data.get("ended_at")
    duration_ms = data.get("duration_ms")
    return IndexRow(
        run_id=run_id,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        target=target,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        started_at=started_at,  # type: ignore[arg-type]
        ended_at=ended_at if isinstance(ended_at, str) else None,
        duration_ms=(
            duration_ms
            if isinstance(duration_ms, int) and not isinstance(duration_ms, bool)
            else None
        ),
        workspace=workspace,  # type: ignore[arg-type]
        result_path=result_path,
    )


class RunIndex:
    """One connection to the derived run index (open one per process/thread)."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        parent = db_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        os.chmod(parent, 0o700)
        try:
            self._conn = sqlite3.connect(
                str(db_path), timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None
            )
        except sqlite3.Error as exc:
            raise PersistenceError(
                f"cannot open run index: {exc}", details={"db_path": str(db_path)}
            ) from exc
        try:
            self._setup_with_retry()
            with contextlib.suppress(OSError):
                os.chmod(db_path, FILE_MODE)
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                self._conn.close()
            raise

    def _setup_with_retry(self) -> None:
        """Configure WAL and initialize the schema, retrying transient busy errors.

        ``PRAGMA journal_mode=WAL`` can return SQLITE_BUSY *without* invoking
        the busy handler when another connection is mid-initialization, so
        concurrent first-time init needs an explicit bounded retry loop.
        """
        deadline = time.monotonic() + _BUSY_TIMEOUT_MS / 1000
        while True:
            try:
                self._setup()
                return
            except PersistenceError as exc:
                cause = exc.__cause__
                transient = isinstance(cause, sqlite3.OperationalError) and (
                    "locked" in str(cause) or "busy" in str(cause)
                )
                if not transient or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def _setup(self) -> None:
        self._run(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        mode_row = self._run("PRAGMA journal_mode=WAL").fetchone()
        if mode_row is None or str(mode_row[0]).lower() != "wal":
            raise PersistenceError(
                "run index requires WAL journal mode; filesystem refused it",
                details={"db_path": str(self.db_path)},
            )
        self._run("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> RunIndex:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _run(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        try:
            return self._conn.execute(sql, params)
        except sqlite3.Error as exc:
            raise PersistenceError(
                f"run index query failed: {exc}", details={"db_path": str(self.db_path)}
            ) from exc

    @contextlib.contextmanager
    def _immediate(self) -> Iterator[None]:
        """One write transaction; IMMEDIATE takes the write lock up front."""
        self._run("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                self._conn.execute("ROLLBACK")
            raise
        else:
            self._run("COMMIT")

    def _init_schema(self) -> None:
        with self._immediate():
            self._run(_META_DDL)
            row = self._run("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row is not None:
                self._check_schema_version(str(row[0]))
            self._run(_RUNS_DDL)
            self._run(_IDX_DDL)
            self._run(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(INDEX_SCHEMA_VERSION),),
            )

    def _check_schema_version(self, value: str) -> None:
        """Tolerate current + immediately previous schema; anything else is explicit."""
        try:
            version = int(value)
        except ValueError:
            version = None
        supported = {INDEX_SCHEMA_VERSION, INDEX_SCHEMA_VERSION - 1}
        if version is None or version < 1 or version not in supported:
            raise PersistenceError(
                f"unsupported index schema version {value!r}; this ziggy supports "
                f"{sorted(v for v in supported if v >= 1)}",
                details={"db_path": str(self.db_path), "schema_version": value},
            )

    def insert_or_replace(self, row: IndexRow) -> None:
        self._run(
            f"INSERT OR REPLACE INTO runs({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                row.run_id,
                row.kind,
                row.target,
                row.status,
                row.started_at,
                row.ended_at,
                row.duration_ms,
                row.workspace,
                row.result_path,
            ),
        )

    def list_runs(
        self,
        *,
        failed_only: bool = False,
        kind: str | None = None,
        agent_target: str | None = None,
        since_iso: str | None = None,
        limit: int = 200,
    ) -> list[IndexRow]:
        """Newest-first run listing. ``failed_only`` matches ``FAILED_STATUSES``."""
        clauses: list[str] = []
        params: list[Any] = []
        if failed_only:
            marks = ",".join("?" for _ in FAILED_STATUSES)
            clauses.append(f"status IN ({marks})")
            params.extend(FAILED_STATUSES)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if agent_target is not None:
            clauses.append("target = ?")
            params.append(agent_target)
        if since_iso is not None:
            clauses.append("started_at >= ?")
            params.append(since_iso)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT {_COLUMNS} FROM runs{where} ORDER BY started_at DESC, run_id DESC LIMIT ?"
        params.append(limit)
        return [IndexRow(*row) for row in self._run(sql, tuple(params)).fetchall()]

    def get(self, run_id: str) -> IndexRow | None:
        row = self._run(f"SELECT {_COLUMNS} FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return IndexRow(*row) if row is not None else None

    def delete(self, run_id: str) -> bool:
        cursor = self._run("DELETE FROM runs WHERE run_id = ?", (run_id,))
        return cursor.rowcount > 0

    def reindex(self, store: RunStore) -> int:
        """Rebuild the runs table from durable manifests without dropping concurrent runs.

        Manifests that are missing, unreadable, or of an unsupported schema are
        excluded entirely (never partially interpreted). Returns rows indexed.

        Safe for concurrent processes: a run whose manifest another process
        persists *during* the scan window must not be wiped. So this never does
        a blind ``DELETE FROM runs``. Instead it snapshots the run_ids present
        before the scan, upserts every scanned manifest, and — under the write
        lock — deletes only rows that (a) predate the scan (were in the initial
        snapshot) and (b) were not found on disk. A row inserted concurrently is
        absent from the pre-scan snapshot, so it is never a deletion candidate.
        Idempotent: re-running finds the same snapshot/scan and changes nothing.
        """
        pre_scan_ids = {str(row[0]) for row in self._run("SELECT run_id FROM runs").fetchall()}
        scanned: dict[str, IndexRow] = {}
        for run_dir in store.iter_run_dirs():
            try:
                data = store.read_result(run_dir.name)
            except PersistenceError:
                continue
            row = _row_from_manifest(data, result_path=str(run_dir / RESULT_FILENAME))
            if row is not None:
                scanned[row.run_id] = row
        stale = pre_scan_ids - scanned.keys()
        with self._immediate():
            for row in scanned.values():
                self.insert_or_replace(row)
            for run_id in stale:
                self._run("DELETE FROM runs WHERE run_id = ?", (run_id,))
        return len(scanned)

    def recover_abandoned(self, store: RunStore) -> list[str]:
        """Finalize provably-dead interrupted runs as ``abandoned`` (§6.4).

        A run dir *without* a durable ``result.json`` is recovered only when its
        ``.writer`` sentinel is absent (and the dir is old enough to rule out a
        writer between mkdir and sentinel creation) or names a provably dead
        writer — a pid that no longer exists, or a live pid whose recorded start
        marker no longer matches (a reused pid). Ambiguous liveness — a live pid
        with a matching or unverifiable marker, corrupt sentinel, too-recent dir
        — leaves the directory untouched. Dirs with durable
        manifests are never touched. The synthesized manifest goes through the
        same atomic write path as normal results, and the index row is inserted
        only after the manifest is durable.
        """
        recovered: list[str] = []
        now = time.time()
        for run_dir in store.iter_run_dirs():
            if (run_dir / RESULT_FILENAME).exists():
                continue
            run_id = run_dir.name
            sentinel = run_dir / WRITER_SENTINEL
            owner: dict[str, Any] | None = None
            if sentinel.exists():
                owner = _read_sentinel(sentinel)
                if owner is None:
                    continue
                if not _writer_provably_dead(owner):
                    continue
                try:
                    sentinel.unlink()
                except OSError:
                    continue
            else:
                try:
                    age = now - run_dir.stat().st_mtime
                except OSError:
                    continue
                if age < _NO_SENTINEL_GRACE_SECONDS:
                    continue
            facts = _derive_run_facts(run_dir)
            started_at: str | None = None
            if owner is not None and isinstance(owner.get("started_at"), str):
                started_at = owner["started_at"]
            started_at = started_at or facts.get("started_at") or _mtime_iso(run_dir)
            ended_at = utc_now_iso()
            manifest: dict[str, Any] = {
                "schema_version": RESULT_SCHEMA_VERSION,
                "run_id": run_id,
                "kind": facts.get("kind", "unknown"),
                "target": facts.get("target", "unknown"),
                "status": RunStatus.ABANDONED.value,
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_ms": None,
                "workspace": facts.get("workspace", "unknown"),
                "finalized_by": "store_recovery",
                "errors": [
                    {
                        "code": AbandonedError.code,
                        "message": (
                            "run interrupted before a terminal result was persisted; "
                            "finalized as abandoned by store inspection"
                        ),
                        "details": {},
                    }
                ],
            }
            result_path = atomic_write_bytes(
                run_dir, RESULT_FILENAME, json.dumps(manifest, indent=2).encode("utf-8")
            )
            self.insert_or_replace(
                IndexRow(
                    run_id=run_id,
                    kind=manifest["kind"],
                    target=manifest["target"],
                    status=RunStatus.ABANDONED.value,
                    started_at=started_at,
                    ended_at=ended_at,
                    duration_ms=None,
                    workspace=manifest["workspace"],
                    result_path=str(result_path),
                )
            )
            recovered.append(run_id)
        return recovered
