"""Run identifiers and timing helpers."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from ulid import ULID


def new_run_id() -> str:
    """Sortable unique run identifier (ULID, 26 chars, Crockford base32)."""
    return str(ULID())


def is_run_id(value: str) -> bool:
    try:
        ULID.from_str(value)
    except (ValueError, TypeError):
        return False
    return True


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_iso() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


class MonotonicClock:
    """Monotonic duration source anchored at creation.

    Persisted timestamps use UTC wall-clock; durations always come from here.
    """

    def __init__(self) -> None:
        self._start = time.monotonic()

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._start) * 1000)
