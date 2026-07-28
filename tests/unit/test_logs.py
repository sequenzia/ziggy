"""Unit tests for ziggy.store.logs (REQ-016 metadata-only structured logs)."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from ziggy.store.logs import (
    ALLOWED_DETAIL_FIELDS,
    MetadataLogger,
    NullLogger,
    open_metadata_logger,
)


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def today() -> date:
    return datetime.now(UTC).date()


def log_path(home: Path, day: date | None = None) -> Path:
    return home / "logs" / f"ziggy-{(day or today()).isoformat()}.jsonl"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def logger(home: Path) -> MetadataLogger:
    lg = MetadataLogger.open(home)
    yield lg
    lg.close()


def read_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class TestOpenAndModes:
    def test_open_creates_logs_dir_0700(self, home: Path) -> None:
        MetadataLogger.open(home).close()
        assert (home / "logs").is_dir()
        assert mode_of(home / "logs") == 0o700

    def test_log_file_created_0600_daily_name(self, logger: MetadataLogger, home: Path) -> None:
        logger.log("run_started", run_id="r1")
        path = log_path(home)
        assert path.is_file()
        assert mode_of(path) == 0o600

    def test_default_root_honors_ziggy_home(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ZIGGY_HOME", str(home))
        lg = MetadataLogger.open()
        lg.log("run_started")
        lg.close()
        assert log_path(home).is_file()

    def test_close_is_idempotent_and_log_reopens(self, logger: MetadataLogger, home: Path) -> None:
        logger.log("a")
        logger.close()
        logger.close()
        logger.log("b")
        assert [r["event"] for r in read_records(log_path(home))] == ["a", "b"]


class TestLogLines:
    def test_valid_jsonl_shape(self, logger: MetadataLogger, home: Path) -> None:
        logger.log(
            "step_finished",
            run_id="01JRUN",
            step_id="main",
            agent="claude",
            status="success",
            duration_ms=1234,
        )
        (record,) = read_records(log_path(home))
        assert set(record) == {"ts", "level", "event", "run_id", "step_id", "agent", "detail"}
        assert record["level"] == "info"
        assert record["event"] == "step_finished"
        assert record["run_id"] == "01JRUN"
        assert record["step_id"] == "main"
        assert record["agent"] == "claude"
        assert record["detail"] == {"status": "success", "duration_ms": 1234}
        # ts is ISO-8601 Z / UTC
        parsed = datetime.fromisoformat(record["ts"].replace("Z", "+00:00"))
        assert parsed.tzinfo is not None

    def test_optional_fields_omitted_when_absent(self, logger: MetadataLogger, home: Path) -> None:
        logger.log("run_started")
        (record,) = read_records(log_path(home))
        assert set(record) == {"ts", "level", "event"}

    def test_every_allowlisted_detail_field_accepted(
        self, logger: MetadataLogger, home: Path
    ) -> None:
        detail = {
            "status": "success",
            "duration_ms": 12,
            "exit_code": 0,
            "stop_reason": "end_turn",
            "rule_id": "read-in-workspace-allow",
            "decision": "allow",
            "kind": "fs_read",
            "target": "claude",
            "count": 3,
            "path_ref": "runs/01JRUN/result.json",
            "reason_code": "ok",
            "provider_set": ["anthropic", "openai"],
            "route": "direct",
        }
        assert set(detail) == set(ALLOWED_DETAIL_FIELDS)
        logger.log("permission_decided", run_id="01JRUN", **detail)
        (record,) = read_records(log_path(home))
        assert record["detail"] == detail

    def test_levels(self, logger: MetadataLogger, home: Path) -> None:
        logger.log("x", level="warning")
        logger.log("y", level="error")
        records = read_records(log_path(home))
        assert [r["level"] for r in records] == ["warning", "error"]


class TestFailsLoudly:
    def test_unknown_detail_key_raises_and_writes_nothing(self, home: Path) -> None:
        lg = MetadataLogger.open(home)
        with pytest.raises(ValueError, match="prompt"):
            lg.log("run_started", prompt="the user prompt text")
        lg.close()
        assert not log_path(home).exists()

    def test_unknown_key_error_lists_allowlist(self, logger: MetadataLogger) -> None:
        with pytest.raises(ValueError, match="not allowlisted") as exc_info:
            logger.log("x", workspace_path="/home/u/project")
        assert "status" in str(exc_info.value)

    def test_invalid_level_raises(self, logger: MetadataLogger) -> None:
        with pytest.raises(ValueError, match="invalid log level"):
            logger.log("x", level="verbose")

    def test_non_json_serializable_detail_value_raises(self, logger: MetadataLogger) -> None:
        with pytest.raises(ValueError, match="JSON-serializable"):
            logger.log("x", count=object())


class TestRetention:
    def test_prune_on_open_by_filename_date(self, home: Path) -> None:
        logs_dir = home / "logs"
        logs_dir.mkdir(parents=True)
        old = logs_dir / f"ziggy-{(today() - timedelta(days=40)).isoformat()}.jsonl"
        boundary = logs_dir / f"ziggy-{(today() - timedelta(days=30)).isoformat()}.jsonl"
        recent = logs_dir / f"ziggy-{(today() - timedelta(days=1)).isoformat()}.jsonl"
        unrelated = logs_dir / "notes.txt"
        malformed = logs_dir / "ziggy-9999-99-99.jsonl"
        for path in (old, boundary, recent, unrelated, malformed):
            path.write_text("", encoding="utf-8")

        MetadataLogger.open(home, retention_days=30).close()

        assert not old.exists()
        assert boundary.exists()  # exactly retention_days old is kept
        assert recent.exists()
        assert unrelated.exists()  # non-matching names never touched
        assert malformed.exists()  # impossible dates never touched

    def test_prune_respects_custom_retention(self, home: Path) -> None:
        logs_dir = home / "logs"
        logs_dir.mkdir(parents=True)
        old = logs_dir / f"ziggy-{(today() - timedelta(days=8)).isoformat()}.jsonl"
        old.write_text("", encoding="utf-8")
        MetadataLogger.open(home, retention_days=7).close()
        assert not old.exists()

    def test_daily_rotation_rolls_to_new_file(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lg = MetadataLogger.open(home)
        lg.log("a")
        tomorrow = today() + timedelta(days=1)
        monkeypatch.setattr("ziggy.store.logs._utc_today", lambda: tomorrow)
        lg.log("b")
        lg.close()
        assert [r["event"] for r in read_records(log_path(home))] == ["a"]
        assert [r["event"] for r in read_records(log_path(home, tomorrow))] == ["b"]


class TestNullLogger:
    def test_factory_returns_null_logger_for_no_save(self, home: Path) -> None:
        lg = open_metadata_logger(home, no_save=True)
        assert isinstance(lg, NullLogger)

    def test_factory_returns_real_logger_otherwise(self, home: Path) -> None:
        lg = open_metadata_logger(home)
        assert isinstance(lg, MetadataLogger)
        lg.close()

    def test_null_logger_writes_nothing(self, home: Path) -> None:
        lg = open_metadata_logger(home, no_save=True)
        lg.log("run_started", run_id="r1", agent="claude", status="running")
        lg.close()
        assert not home.exists()  # not even the store root is created

    def test_null_logger_still_rejects_unknown_detail_keys(self) -> None:
        lg = NullLogger()
        with pytest.raises(ValueError, match="not allowlisted"):
            lg.log("x", prompt="secret text")
        with pytest.raises(ValueError, match="invalid log level"):
            lg.log("x", level="verbose")
