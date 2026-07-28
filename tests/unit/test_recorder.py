"""Unit tests for ziggy.events.pipeline (REQ-004 canonical event pipeline)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ziggy.errors import PersistenceError
from ziggy.events import EventLimits, RunRecorder
from ziggy.ids import new_run_id
from ziggy.models.common import CaptureProfile, CaptureStatus
from ziggy.models.events import EventEnvelope
from ziggy.redact import Redactor
from ziggy.store import RunDirWriter, RunStore

SECRET = "sk-ant-" + "A1b2" * 8
MARKER = "[REDACTED:anthropic_api_key]"


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "home")


def make_recorder(
    *,
    writer: RunDirWriter | None = None,
    profile: CaptureProfile = CaptureProfile.STANDARD,
    limits: EventLimits | None = None,
    render_cb=None,
    redactor: Redactor | None = None,
    run_id: str | None = None,
) -> RunRecorder:
    return RunRecorder(
        run_id=run_id or (writer.run_id if writer is not None else new_run_id()),
        store_writer=writer,
        redactor=redactor or Redactor(),
        capture_profile=profile,
        limits=limits or EventLimits(),
        render_cb=render_cb,
    )


def read_events(writer: RunDirWriter) -> list[dict]:
    writer.flush()
    lines = writer.events_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def chunk(text: str, role: str = "agent") -> dict:
    return {"role": role, "text": text}


class TestSequencing:
    def test_seq_monotonic_from_zero(self) -> None:
        rec = make_recorder()
        envs = [
            rec.emit(event_type="run_started", payload={"kind": "agent"}),
            rec.emit(event_type="step_started", step_id="main"),
            rec.emit(event_type="message_chunk", step_id="main", payload=chunk("hi")),
        ]
        assert [e.seq for e in envs] == [0, 1, 2]
        assert rec.event_count == 3

    def test_ts_and_offset_monotonic(self) -> None:
        rec = make_recorder()
        envs = [rec.emit(event_type="message_chunk", payload=chunk(str(i))) for i in range(5)]
        ts_values = [e.ts for e in envs]
        offsets = [e.monotonic_offset_ms for e in envs]
        assert ts_values == sorted(ts_values)
        assert offsets == sorted(offsets)
        assert all(e.ts.endswith("Z") for e in envs)

    def test_envelope_passthrough_fields(self) -> None:
        rec = make_recorder()
        env = rec.emit(
            event_type="message_chunk",
            step_id="main",
            attempt_no=1,
            session_id="sess-1",
            payload=chunk("hi"),
        )
        assert env is not None
        assert (env.run_id, env.step_id, env.attempt_no, env.session_id) == (
            rec.run_id,
            "main",
            1,
            "sess-1",
        )

    def test_unknown_event_type_rejected(self) -> None:
        rec = make_recorder()
        with pytest.raises(ValueError, match="unknown event_type"):
            rec.emit(event_type="totally_new_thing", payload={})


class TestRedaction:
    def test_render_cb_sees_redacted_payload(self) -> None:
        seen: list[EventEnvelope] = []
        rec = make_recorder(render_cb=seen.append)
        env = rec.emit(
            event_type="message_chunk", step_id="main", payload=chunk(f"key is {SECRET} ok")
        )
        assert len(seen) == 1
        assert seen[0] is env
        assert SECRET not in seen[0].payload["text"]
        assert MARKER in seen[0].payload["text"]
        assert seen[0].redaction.applied
        assert seen[0].redaction.counts == {"anthropic_api_key": 1}

    def test_persisted_events_are_redacted(self, store: RunStore) -> None:
        writer = store.open_run(new_run_id())
        rec = make_recorder(writer=writer)
        rec.emit(event_type="message_chunk", step_id="main", payload=chunk(f"x {SECRET}"))
        writer.flush()
        raw = writer.events_path.read_text(encoding="utf-8")
        assert SECRET not in raw
        assert MARKER in raw

    def test_nested_payload_values_redacted(self) -> None:
        rec = make_recorder()
        env = rec.emit(
            event_type="tool_call",
            step_id="main",
            payload={"tool_call_id": "t1", "raw": {"raw_input": {"cmd": f"use {SECRET}"}}},
        )
        assert env is not None
        assert SECRET not in json.dumps(env.payload)

    def test_redaction_summary_passthrough(self) -> None:
        redactor = Redactor()
        rec = make_recorder(redactor=redactor)
        rec.emit(event_type="message_chunk", payload=chunk(SECRET))
        summary = rec.redaction_summary()
        assert summary.total_redactions == 1
        assert summary.by_kind == {"anthropic_api_key": 1}


class TestByteCeilings:
    LIMITS = EventLimits(max_event_bytes_per_step=2000)

    def test_step_ceiling_emits_one_truncation_then_metadata_only(self, store: RunStore) -> None:
        writer = store.open_run(new_run_id())
        seen: list[EventEnvelope] = []
        rec = make_recorder(writer=writer, limits=self.LIMITS, render_cb=seen.append)
        big = "x" * 1500

        first = rec.emit(event_type="message_chunk", step_id="main", payload=chunk(big))
        assert first is not None
        assert first.payload["text"] == big
        assert first.capture_status is CaptureStatus.COMPLETE

        second = rec.emit(event_type="message_chunk", step_id="main", payload=chunk(big))
        assert second is not None
        assert second.payload["truncated"] is True
        assert second.payload["original_bytes"] > 1500
        assert second.capture_status is CaptureStatus.PARTIAL
        assert second.event_type == "message_chunk"
        assert second.step_id == "main"

        third = rec.emit(event_type="message_chunk", step_id="main", payload=chunk(big))
        assert third is not None
        assert third.payload["truncated"] is True

        types = [e.event_type for e in seen]
        assert types == ["message_chunk", "truncation", "message_chunk", "message_chunk"]
        assert [e.seq for e in seen] == [0, 1, 2, 3]
        trunc = seen[1]
        assert trunc.payload["limit_bytes"] == self.LIMITS.max_event_bytes_per_step
        assert trunc.step_id == "main"

        persisted = read_events(writer)
        assert [e["event_type"] for e in persisted] == types
        assert sum(1 for e in persisted if e["event_type"] == "truncation") == 1

    def test_other_steps_unaffected_by_truncated_step(self) -> None:
        rec = make_recorder(limits=self.LIMITS)
        big = "x" * 1500
        rec.emit(event_type="message_chunk", step_id="a", payload=chunk(big))
        rec.emit(event_type="message_chunk", step_id="a", payload=chunk(big))
        other = rec.emit(event_type="message_chunk", step_id="b", payload=chunk("small"))
        assert other is not None
        assert other.payload["text"] == "small"
        assert other.capture_status is CaptureStatus.COMPLETE

    def test_oversized_single_payload_truncated(self) -> None:
        rec = make_recorder(limits=EventLimits(max_payload_bytes_per_event=200))
        env = rec.emit(event_type="message_chunk", step_id="main", payload=chunk("y" * 1000))
        assert env is not None
        assert env.payload["truncated"] is True
        assert env.payload["original_bytes"] > 1000
        assert env.capture_status is CaptureStatus.PARTIAL
        nxt = rec.emit(event_type="message_chunk", step_id="main", payload=chunk("tiny"))
        assert nxt is not None
        assert nxt.payload["text"] == "tiny"
        summary = rec.capture_summary()
        assert summary["transcript"].status is CaptureStatus.PARTIAL
        assert summary["transcript"].truncated is True


class TestCaptureProfiles:
    def test_standard_reduces_thought_chunks_only(self) -> None:
        rec = make_recorder(profile=CaptureProfile.STANDARD)
        thought = rec.emit(
            event_type="thought_chunk", step_id="main", payload={"text": "secret reasoning"}
        )
        assert thought is not None
        assert thought.payload["text"] == {"bytes": len("secret reasoning"), "type": "str"}
        assert thought.capture_status is CaptureStatus.PARTIAL
        msg = rec.emit(event_type="message_chunk", step_id="main", payload=chunk("visible"))
        assert msg is not None
        assert msg.payload["text"] == "visible"
        assert msg.capture_status is CaptureStatus.COMPLETE

    def test_metadata_profile_reduces_content_keeps_metadata(self) -> None:
        rec = make_recorder(profile=CaptureProfile.METADATA)
        msg = rec.emit(event_type="message_chunk", step_id="main", payload=chunk("hello"))
        assert msg is not None
        assert msg.payload["text"] == {"bytes": 5, "type": "str"}
        tc = rec.emit(
            event_type="tool_call",
            step_id="main",
            payload={"tool_call_id": "t1", "title": "Edit", "raw": {"raw_input": {"a": 1}}},
        )
        assert tc is not None
        assert tc.payload["tool_call_id"] == "t1"
        assert tc.payload["title"] == "Edit"
        assert set(tc.payload["raw"]) == {"bytes", "type"}
        fw = rec.emit(
            event_type="fs_write",
            step_id="main",
            payload={"path": "notes.md", "content": "body text"},
        )
        assert fw is not None
        assert fw.payload["path"] == "notes.md"
        assert set(fw.payload["content"]) == {"bytes", "type"}
        assert rec.text_output("main") == ""
        assert [c.path for c in rec.file_changes("main")] == ["notes.md"]
        assert [t.tool_call_id for t in rec.tool_calls("main")] == ["t1"]

    def test_debug_keeps_thoughts_and_raw_frames(self, store: RunStore) -> None:
        writer = store.open_run(new_run_id())
        rec = make_recorder(writer=writer, profile=CaptureProfile.DEBUG)
        thought = rec.emit(event_type="thought_chunk", step_id="main", payload={"text": "t"})
        assert thought is not None
        assert thought.payload["text"] == "t"
        frame = rec.emit(
            event_type="raw_frame",
            payload={"direction": "in", "frame": {"jsonrpc": "2.0"}},
            protocol_payload_ref="artifacts/frame-0.json",
        )
        assert frame is not None
        assert frame.protocol_payload_ref == "artifacts/frame-0.json"
        assert [e["event_type"] for e in read_events(writer)] == ["thought_chunk", "raw_frame"]

    def test_standard_drops_raw_frames_entirely(self, store: RunStore) -> None:
        writer = store.open_run(new_run_id())
        seen: list[EventEnvelope] = []
        rec = make_recorder(writer=writer, render_cb=seen.append)
        assert rec.emit(event_type="raw_frame", payload={"frame": {}}) is None
        assert rec.event_count == 0
        assert seen == []
        env = rec.emit(
            event_type="message_chunk",
            payload=chunk("hi"),
            protocol_payload_ref="artifacts/x.json",
        )
        assert env is not None
        assert env.protocol_payload_ref is None
        assert env.seq == 0


class TestAggregations:
    def test_tool_call_merge_by_id(self) -> None:
        rec = make_recorder()
        rec.emit(
            event_type="tool_call",
            step_id="main",
            payload={
                "tool_call_id": "t1",
                "title": "Edit file",
                "kind": "edit",
                "status": "pending",
                "locations": ["src/a.py"],
            },
        )
        rec.emit(
            event_type="tool_call_update",
            step_id="main",
            payload={"tool_call_id": "t1", "status": "completed"},
        )
        rec.emit(
            event_type="tool_call",
            step_id="main",
            payload={"tool_call_id": "t2", "title": "Read", "status": "in_progress"},
        )
        records = rec.tool_calls("main")
        assert [r.tool_call_id for r in records] == ["t1", "t2"]
        first = records[0]
        assert first.title == "Edit file"
        assert first.kind == "edit"
        assert first.status == "completed"
        assert first.locations == ["src/a.py"]

    def test_file_changes_from_diff_content_and_fs_write(self) -> None:
        rec = make_recorder()
        rec.emit(
            event_type="tool_call",
            step_id="main",
            payload={
                "tool_call_id": "t1",
                "raw": {
                    "content": [
                        {"type": "diff", "path": "src/a.py", "old_text": "x", "new_text": "y"},
                        {"type": "diff", "path": "src/new.py", "new_text": "created"},
                        {"type": "content", "content": {"type": "text", "text": "noise"}},
                    ]
                },
            },
        )
        rec.emit(
            event_type="tool_call_update",
            step_id="main",
            payload={
                "tool_call_id": "t1",
                "raw": {
                    "content": [
                        {"type": "diff", "path": "src/a.py", "old_text": "y", "new_text": "z"}
                    ]
                },
            },
        )
        rec.emit(
            event_type="fs_write",
            step_id="main",
            payload={"path": "notes.md", "content": "hello"},
        )
        changes = rec.file_changes("main")
        by_path = {c.path: c for c in changes}
        assert set(by_path) == {"src/a.py", "src/new.py", "notes.md"}
        assert by_path["src/a.py"].capture_method == "acp_tool_call"
        assert by_path["src/a.py"].change_type == "modified"
        assert by_path["src/a.py"].capture_status is CaptureStatus.DERIVED
        assert by_path["src/new.py"].change_type == "created"
        assert by_path["notes.md"].capture_method == "acp_fs_write"
        assert by_path["notes.md"].capture_status is CaptureStatus.COMPLETE

    def test_permission_decisions_aggregated(self) -> None:
        rec = make_recorder()
        env = rec.emit(
            event_type="permission_decided",
            step_id="main",
            payload={
                "request_summary": "write src/a.py",
                "options_offered": ["allow_once", "reject_once"],
                "decision": "approved",
                "rule_id": "rule-1",
                "policy_name": "default",
                "policy_source": "user",
            },
        )
        assert env is not None
        decisions = rec.permission_decisions("main")
        assert len(decisions) == 1
        assert decisions[0].decision == "approved"
        assert decisions[0].rule_id == "rule-1"
        assert decisions[0].ts == env.ts

    def test_text_output_concatenates_agent_chunks_only(self) -> None:
        rec = make_recorder()
        rec.emit(event_type="message_chunk", step_id="main", payload=chunk("Hello, "))
        rec.emit(event_type="message_chunk", step_id="main", payload=chunk("nope", role="user"))
        rec.emit(event_type="thought_chunk", step_id="main", payload={"text": "thinking"})
        rec.emit(event_type="message_chunk", step_id="main", payload=chunk("world"))
        rec.emit(event_type="message_chunk", step_id="other", payload=chunk("elsewhere"))
        assert rec.text_output("main") == "Hello, world"
        assert rec.text_output("other") == "elsewhere"
        assert rec.text_output("missing") == ""

    def test_usage_aggregation(self) -> None:
        rec = make_recorder()
        assert rec.usage_summary() is None
        rec.emit(
            event_type="usage",
            step_id="main",
            payload={"provider": "anthropic", "units": "tokens", "input_tokens": 10},
        )
        rec.emit(
            event_type="usage",
            step_id="main",
            payload={"input_tokens": 25, "output_tokens": 40, "cost": 0.5, "currency": "USD"},
        )
        usage = rec.usage_summary()
        assert usage is not None
        assert usage.provider == "anthropic"
        assert usage.input_tokens == 25
        assert usage.output_tokens == 40
        assert usage.cost == 0.5
        assert usage.currency == "USD"
        assert usage.capture_status == "complete"
        assert usage.ceiling_enforceable is False


class TestPersistence:
    async def test_events_jsonl_one_valid_line_per_event(self, store: RunStore) -> None:
        writer = store.open_run(new_run_id())
        rec = make_recorder(writer=writer)
        rec.emit(event_type="run_started", payload={"kind": "agent"})
        rec.emit(event_type="message_chunk", step_id="main", payload=chunk("hi"))
        rec.emit(event_type="run_finished", payload={"status": "success"})
        await rec.finalize()
        lines = writer.events_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        envelopes = [EventEnvelope.model_validate(json.loads(line)) for line in lines]
        assert [e.seq for e in envelopes] == [0, 1, 2]
        assert all(e.run_id == rec.run_id for e in envelopes)
        await rec.finalize()

    async def test_no_save_mode(self, tmp_path: Path) -> None:
        rec = make_recorder(writer=None)
        rec.emit(event_type="message_chunk", step_id="main", payload=chunk("hi"))
        rec.emit(
            event_type="tool_call", step_id="main", payload={"tool_call_id": "t1", "kind": "read"}
        )
        await rec.finalize()
        assert list(tmp_path.iterdir()) == []
        assert rec.events_path is None
        assert rec.text_output("main") == "hi"
        assert [t.tool_call_id for t in rec.tool_calls("main")] == ["t1"]
        summary = rec.capture_summary()
        assert summary["transcript"].status is CaptureStatus.COMPLETE
        assert summary["transcript"].path is None
        assert rec.persistence_errors == []

    def test_write_failure_collected_not_raised(
        self, store: RunStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        writer = store.open_run(new_run_id())
        rec = make_recorder(writer=writer)
        rec.emit(event_type="message_chunk", step_id="main", payload=chunk("ok"))

        def boom(line: str) -> None:
            raise PersistenceError("disk full", details={"path": str(writer.events_path)})

        monkeypatch.setattr(writer, "append_event", boom)
        env = rec.emit(event_type="message_chunk", step_id="main", payload=chunk("lost"))
        assert env is not None
        assert rec.write_failures == 1
        assert len(rec.persistence_errors) == 1
        assert isinstance(rec.persistence_errors[0], PersistenceError)
        assert rec.text_output("main") == "oklost"
        assert rec.capture_summary()["transcript"].status is CaptureStatus.PARTIAL

    def test_write_failure_from_start_is_unavailable(
        self, store: RunStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        writer = store.open_run(new_run_id())
        rec = make_recorder(writer=writer)

        def boom(line: str) -> None:
            raise PersistenceError("readonly fs")

        monkeypatch.setattr(writer, "append_event", boom)
        rec.emit(event_type="message_chunk", step_id="main", payload=chunk("x"))
        summary = rec.capture_summary()
        assert summary["transcript"].status is CaptureStatus.UNAVAILABLE
        assert summary["tool_calls"].status is CaptureStatus.UNAVAILABLE


class TestRenderCallback:
    def test_exceptions_swallowed_and_counted(self) -> None:
        calls: list[str] = []

        def bad_cb(env: EventEnvelope) -> None:
            calls.append(env.event_type)
            raise RuntimeError("render exploded")

        rec = make_recorder(render_cb=bad_cb)
        env = rec.emit(event_type="message_chunk", step_id="main", payload=chunk("a"))
        assert env is not None
        assert rec.render_failures == 1
        rec.emit(event_type="message_chunk", step_id="main", payload=chunk("b"))
        assert rec.render_failures == 2
        assert calls == ["message_chunk", "message_chunk"]
        assert rec.text_output("main") == "ab"


class TestCaptureSummaryHonesty:
    def test_clean_standard_run_is_complete(self, store: RunStore) -> None:
        writer = store.open_run(new_run_id())
        rec = make_recorder(writer=writer)
        rec.emit(event_type="message_chunk", step_id="main", payload=chunk("hi"))
        rec.emit(event_type="tool_call", step_id="main", payload={"tool_call_id": "t1"})
        summary = rec.capture_summary()
        assert summary["transcript"].status is CaptureStatus.COMPLETE
        assert summary["tool_calls"].status is CaptureStatus.COMPLETE
        assert summary["permissions"].status is CaptureStatus.COMPLETE
        assert summary["file_changes"].status is CaptureStatus.DERIVED
        assert summary["transcript"].event_count == 1
        assert summary["transcript"].byte_count > 0
        assert summary["transcript"].path == str(writer.events_path)

    def test_truncation_degrades_class_to_partial(self) -> None:
        rec = make_recorder(limits=EventLimits(max_event_bytes_per_step=2000))
        big = "x" * 1500
        rec.emit(event_type="message_chunk", step_id="main", payload=chunk(big))
        rec.emit(event_type="message_chunk", step_id="main", payload=chunk(big))
        summary = rec.capture_summary()
        assert summary["transcript"].status is CaptureStatus.PARTIAL
        assert summary["transcript"].truncated is True
        assert summary["tool_calls"].status is CaptureStatus.COMPLETE
        assert summary["tool_calls"].truncated is False

    def test_truncated_tool_call_records_marked_partial(self) -> None:
        rec = make_recorder(
            limits=EventLimits(max_event_bytes_per_step=2000, max_payload_bytes_per_event=500)
        )
        rec.emit(event_type="tool_call", step_id="main", payload={"tool_call_id": "t1"})
        rec.emit(
            event_type="tool_call_update",
            step_id="main",
            payload={"tool_call_id": "t1", "raw": {"raw_output": "z" * 1000}},
        )
        records = rec.tool_calls("main")
        assert records[0].capture_status is CaptureStatus.PARTIAL
        assert rec.capture_summary()["tool_calls"].truncated is True

    def test_metadata_profile_reported_honestly(self) -> None:
        rec = make_recorder(profile=CaptureProfile.METADATA)
        rec.emit(event_type="message_chunk", step_id="main", payload=chunk("hi"))
        summary = rec.capture_summary()
        assert summary["transcript"].status is CaptureStatus.PARTIAL
        assert summary["transcript"].source == "metadata_profile"
        assert summary["tool_calls"].status is CaptureStatus.PARTIAL
        assert summary["tool_calls"].source == "metadata_profile"
        assert summary["permissions"].status is CaptureStatus.COMPLETE
        assert summary["file_changes"].status is CaptureStatus.DERIVED
