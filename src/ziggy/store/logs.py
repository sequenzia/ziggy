"""Metadata-only structured logs (REQ-016, spec §5.10).

Daily-rotated JSONL at ``$ZIGGY_HOME/logs/ziggy-YYYY-MM-DD.jsonl`` (0600 files
in a 0700 directory). Each line is ``{ts, level, event, run_id?, step_id?,
agent?, detail?}`` and is metadata ONLY: never prompts, responses, tool
payloads, diffs, plans, permission bodies, secret names, or filesystem paths
beyond run-directory references. That guarantee is enforced structurally —
``detail`` keys are restricted to :data:`ALLOWED_DETAIL_FIELDS` and anything
else raises ``ValueError`` so misuse fails loudly at the call site instead of
leaking. Retention prunes whole files by filename date on logger open;
``--no-save`` runs use :class:`NullLogger` (same interface, writes nothing).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

from ziggy.errors import PersistenceError
from ziggy.ids import utc_now_iso
from ziggy.store.runstore import FILE_MODE, RunStore

DEFAULT_RETENTION_DAYS = 30

#: The only ``detail`` keys ``.log()`` accepts (metadata-only enforcement).
#: ``path_ref`` values must be run-directory references, never workspace paths.
ALLOWED_DETAIL_FIELDS = frozenset(
    {
        "status",
        "duration_ms",
        "exit_code",
        "stop_reason",
        "rule_id",
        "decision",
        "kind",
        "target",
        "count",
        "path_ref",
        "reason_code",
        "provider_set",
        "route",
    }
)

ALLOWED_LEVELS = frozenset({"debug", "info", "warning", "error"})

_FILENAME_RE = re.compile(r"^ziggy-(\d{4})-(\d{2})-(\d{2})\.jsonl$")


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _log_filename(day: date) -> str:
    return f"ziggy-{day.isoformat()}.jsonl"


def _validate(level: str, detail: Mapping[str, Any]) -> None:
    if level not in ALLOWED_LEVELS:
        raise ValueError(f"invalid log level {level!r}; allowed: {sorted(ALLOWED_LEVELS)}")
    unknown = sorted(set(detail) - ALLOWED_DETAIL_FIELDS)
    if unknown:
        raise ValueError(
            f"metadata log detail keys not allowlisted: {', '.join(unknown)} "
            f"(allowed: {', '.join(sorted(ALLOWED_DETAIL_FIELDS))}); metadata "
            "logs never carry prompts, payloads, or workspace paths"
        )


class MetadataLogger:
    """Append-only daily JSONL metadata log for one Ziggy store root."""

    def __init__(self, logs_dir: Path, *, retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
        self.logs_dir = logs_dir
        self.retention_days = retention_days
        self._fh: TextIO | None = None
        self._fh_day: date | None = None

    @classmethod
    def open(
        cls,
        store_root: Path | None = None,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> MetadataLogger:
        """Open the logger for ``store_root`` (default ``$ZIGGY_HOME``/~/.ziggy),
        creating ``logs/`` at 0700 and pruning files past ``retention_days``."""
        logger = cls(RunStore(store_root).logs_dir, retention_days=retention_days)
        logger._prune()
        return logger

    def log(
        self,
        event: str,
        *,
        run_id: str | None = None,
        step_id: str | None = None,
        agent: str | None = None,
        level: str = "info",
        **detail: Any,
    ) -> None:
        """Append one metadata-only line; unknown ``detail`` keys raise ValueError."""
        _validate(level, detail)
        record: dict[str, Any] = {"ts": utc_now_iso(), "level": level, "event": event}
        if run_id is not None:
            record["run_id"] = run_id
        if step_id is not None:
            record["step_id"] = step_id
        if agent is not None:
            record["agent"] = agent
        if detail:
            record["detail"] = detail
        try:
            line = json.dumps(record, separators=(",", ":"))
        except TypeError as exc:
            raise ValueError(
                f"metadata log detail values must be JSON-serializable: {exc}"
            ) from exc
        fh = self._ensure_file()
        try:
            fh.write(line + "\n")
            fh.flush()
        except OSError as exc:
            raise PersistenceError(
                f"metadata log write failed: {exc}",
                details={"path": str(self.logs_dir / _log_filename(_utc_today()))},
            ) from exc

    def close(self) -> None:
        """Idempotent; a later ``log()`` reopens the current daily file."""
        if self._fh is not None:
            with contextlib.suppress(OSError, ValueError):
                self._fh.flush()
                self._fh.close()
            self._fh = None
            self._fh_day = None

    def _ensure_file(self) -> TextIO:
        """Open (or roll to) today's file: daily filename = rotation."""
        today = _utc_today()
        if self._fh is not None and self._fh_day == today:
            return self._fh
        self.close()
        path = self.logs_dir / _log_filename(today)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
            os.chmod(path, FILE_MODE)  # enforce 0600 even on pre-existing files
        except OSError as exc:
            raise PersistenceError(
                f"cannot open metadata log: {exc}", details={"path": str(path)}
            ) from exc
        self._fh = os.fdopen(fd, "a", encoding="utf-8")
        self._fh_day = today
        return self._fh

    def _prune(self) -> None:
        """Best-effort deletion of log files older than ``retention_days``,
        judged by filename date only. Non-matching or undated files are never
        touched; symlinks are unlinked, never followed."""
        cutoff = _utc_today() - timedelta(days=self.retention_days)
        for entry in self.logs_dir.iterdir():
            match = _FILENAME_RE.match(entry.name)
            if match is None:
                continue
            try:
                file_day = date(int(match[1]), int(match[2]), int(match[3]))
            except ValueError:
                continue  # impossible date in name: leave it alone
            if file_day < cutoff:
                with contextlib.suppress(OSError):
                    entry.unlink()


class NullLogger:
    """Disabled logger for ``--no-save`` runs: same interface, writes nothing.

    Detail-key and level validation still apply so misuse fails loudly even
    when logging is disabled.
    """

    def log(
        self,
        event: str,
        *,
        run_id: str | None = None,
        step_id: str | None = None,
        agent: str | None = None,
        level: str = "info",
        **detail: Any,
    ) -> None:
        _validate(level, detail)

    def close(self) -> None:
        return None


def open_metadata_logger(
    store_root: Path | None = None,
    *,
    no_save: bool = False,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> MetadataLogger | NullLogger:
    """Factory: a real logger, or a :class:`NullLogger` (touching nothing on
    disk) when ``no_save`` is set."""
    if no_save:
        return NullLogger()
    return MetadataLogger.open(store_root, retention_days=retention_days)
