"""Run-directory store: restrictive modes, single-writer dirs, atomic manifests.

Implements REQ-005 persistence. Each run lives in ``<root>/runs/<run-id>/``
holding an append-only ``events.jsonl`` and a ``result.json`` manifest written
via tmp file + fsync + atomic rename + directory fsync. The presence of
``result.json`` is the durability marker separating completed runs from
recovery candidates: directories with a durable manifest are never mutated
again (see ``ziggy.store.index`` for abandoned-run recovery).
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO

from ziggy import RESULT_SCHEMA_VERSION
from ziggy.errors import PersistenceError
from ziggy.ids import is_run_id, utc_now_iso

DIR_MODE = 0o700
FILE_MODE = 0o600
WRITER_SENTINEL = ".writer"
RESULT_FILENAME = "result.json"
EVENTS_FILENAME = "events.jsonl"

#: Manifest schema versions this reader fully understands: the current version
#: plus, once one exists, the immediately previous version (REQ-005). Anything
#: else fails explicitly and is never partially interpreted.
SUPPORTED_RESULT_SCHEMAS = frozenset({RESULT_SCHEMA_VERSION})


def ziggy_home() -> Path:
    """Store root: ``$ZIGGY_HOME`` when set and non-empty, else ``~/.ziggy``."""
    env = os.environ.get("ZIGGY_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".ziggy"


def _ensure_dir(path: Path) -> Path:
    """Create ``path`` if needed and enforce 0700 even if it already existed."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, DIR_MODE)
    return path


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(dir_path: Path, name: str, data: bytes) -> Path:
    """Durably write ``dir_path/name``: unique tmp in the same directory (0600),
    fsync file, atomic ``os.replace``, fsync directory.

    On any failure the tmp file is removed and ``PersistenceError`` is raised;
    the target is either the previous complete content or the new complete
    content, never a partial file.
    """
    target = dir_path / name
    tmp = dir_path / f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        _fsync_dir(dir_path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise PersistenceError(
            f"atomic write of {name} failed: {exc}",
            details={"path": str(target)},
        ) from exc
    return target


class RunDirWriter:
    """Single-writer handle for one run directory.

    Construction acquires an ``O_EXCL`` ``.writer`` sentinel recording the
    owning pid and a start marker; a second concurrent writer for the same run
    directory fails with ``PersistenceError``. ``finalize()`` releases it.
    """

    def __init__(self, *, run_id: str, run_dir: Path) -> None:
        self.run_id = run_id
        self.run_dir = run_dir
        self.events_path = run_dir / EVENTS_FILENAME
        self._events_fh: TextIO | None = None
        self._finalized = False
        self._sentinel = run_dir / WRITER_SENTINEL
        self._acquire_sentinel()

    def _acquire_sentinel(self) -> None:
        payload = json.dumps(
            {"pid": os.getpid(), "run_id": self.run_id, "started_at": utc_now_iso()}
        )
        try:
            fd = os.open(self._sentinel, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
        except FileExistsError as exc:
            raise PersistenceError(
                "run directory already has an active writer",
                details={"run_id": self.run_id, "sentinel": str(self._sentinel)},
            ) from exc
        except OSError as exc:
            raise PersistenceError(
                f"cannot create writer sentinel: {exc}",
                details={"run_id": self.run_id},
            ) from exc
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())

    def _check_open(self) -> None:
        if self._finalized:
            raise PersistenceError("run writer already finalized", details={"run_id": self.run_id})

    def append_event(self, line: str) -> None:
        """Buffered append of one JSONL line (a trailing newline is ensured)."""
        self._check_open()
        if self._events_fh is None:
            try:
                fd = os.open(self.events_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
            except OSError as exc:
                raise PersistenceError(
                    f"cannot open events file: {exc}",
                    details={"path": str(self.events_path)},
                ) from exc
            self._events_fh = os.fdopen(fd, "a", encoding="utf-8")
        self._events_fh.write(line if line.endswith("\n") else line + "\n")

    def flush(self) -> None:
        if self._events_fh is not None:
            self._events_fh.flush()

    def fsync_events(self) -> None:
        if self._events_fh is not None:
            self._events_fh.flush()
            os.fsync(self._events_fh.fileno())

    def changes_dir(self) -> Path:
        return _ensure_dir(self.run_dir / "changes")

    def artifacts_dir(self) -> Path:
        return _ensure_dir(self.run_dir / "artifacts")

    def write_result(self, result_json: str) -> Path:
        """Atomically persist the ``result.json`` manifest (the durability marker)."""
        self._check_open()
        return atomic_write_bytes(self.run_dir, RESULT_FILENAME, result_json.encode("utf-8"))

    def finalize(self) -> None:
        """Flush and close the events handle, then release the writer sentinel.

        Idempotent. Close-time flush errors are suppressed: callers needing a
        durability guarantee for events must call ``fsync_events()`` first.
        """
        if self._finalized:
            return
        self._finalized = True
        if self._events_fh is not None:
            with contextlib.suppress(OSError, ValueError):
                self._events_fh.flush()
                os.fsync(self._events_fh.fileno())
            with contextlib.suppress(OSError):
                self._events_fh.close()
            self._events_fh = None
        with contextlib.suppress(OSError):
            self._sentinel.unlink(missing_ok=True)


class RunStore:
    """Filesystem layout owner for one Ziggy store root (0700 dirs, 0600 files)."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or ziggy_home()).expanduser()

    def _subdir(self, name: str) -> Path:
        _ensure_dir(self.root)
        return _ensure_dir(self.root / name)

    @property
    def runs_dir(self) -> Path:
        return self._subdir("runs")

    @property
    def leases_dir(self) -> Path:
        return self._subdir("leases")

    @property
    def logs_dir(self) -> Path:
        return self._subdir("logs")

    @property
    def index_db_path(self) -> Path:
        return self.runs_dir / "index.db"

    def run_dir(self, run_id: str) -> Path:
        """Path of a run directory (not created)."""
        return self.runs_dir / run_id

    def open_run(self, run_id: str) -> RunDirWriter:
        """Create ``runs/<run-id>/`` (0700) and acquire its single-writer handle."""
        if not is_run_id(run_id):
            raise PersistenceError(
                f"invalid run id {run_id!r} (run ids are ULIDs)", details={"run_id": run_id}
            )
        run_dir = _ensure_dir(self.runs_dir / run_id)
        return RunDirWriter(run_id=run_id, run_dir=run_dir)

    def iter_run_dirs(self) -> Iterator[Path]:
        """Yield run directories (ULID-named dirs under runs/), sorted by run id."""
        for entry in sorted(self.runs_dir.iterdir()):
            if entry.is_dir() and is_run_id(entry.name):
                yield entry

    def read_result(self, run_id: str) -> dict[str, Any]:
        """Load a run's ``result.json`` as a dict.

        Raises ``PersistenceError`` when the manifest is missing, unreadable,
        not a JSON object, or declares a schema version outside
        ``SUPPORTED_RESULT_SCHEMAS`` — an unsupported (e.g. future) schema is
        rejected whole, never partially interpreted.
        """
        path = self.runs_dir / run_id / RESULT_FILENAME
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise PersistenceError(
                f"result manifest missing for run {run_id}", details={"path": str(path)}
            ) from exc
        except OSError as exc:
            raise PersistenceError(
                f"result manifest unreadable for run {run_id}: {exc}",
                details={"path": str(path)},
            ) from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise PersistenceError(
                f"result manifest for run {run_id} is not valid JSON",
                details={"path": str(path)},
            ) from exc
        if not isinstance(data, dict):
            raise PersistenceError(
                f"result manifest for run {run_id} is not a JSON object",
                details={"path": str(path)},
            )
        version = data.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise PersistenceError(
                f"result manifest for run {run_id} has an invalid schema_version",
                details={"path": str(path), "schema_version": repr(version)},
            )
        if version not in SUPPORTED_RESULT_SCHEMAS:
            raise PersistenceError(
                f"unsupported schema version {version} in result manifest for run "
                f"{run_id}; this ziggy supports {sorted(SUPPORTED_RESULT_SCHEMAS)}",
                details={"path": str(path), "schema_version": version},
            )
        return data
