"""Terminal rendering for the CLI (REQ-003 §5.2, REQ-015).

:class:`RunRenderer` consumes :class:`EventEnvelope` objects straight from the
RunRecorder's ``render_cb`` — the same canonical pipeline the persisted record
uses (one event stream, two consumers). Two modes:

- **rich** — a transient Live status line plus streamed agent text and
  tool/permission/policy lines, for interactive terminals.
- **plain** — line-oriented output with no ANSI escapes; selected by
  ``--plain``, a set ``NO_COLOR``, or a non-TTY stream.

Under ``--json`` the caller points the renderer at stderr so stdout stays
machine-readable. The module also owns the plain-text table and run-detail
formatting used by ``agents list``, ``runs list/show``, and ``config show``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any, TextIO

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from ziggy.models.common import PermissionDecisionKind
from ziggy.models.events import EventEnvelope
from ziggy.models.result import RunResult


def use_plain(stream: TextIO, *, plain_flag: bool, env: Mapping[str, str] | None = None) -> bool:
    """Plain output when requested, when ``NO_COLOR`` is set, or on a non-TTY."""
    env_map: Mapping[str, str] = os.environ if env is None else env
    if plain_flag or env_map.get("NO_COLOR"):
        return True
    isatty = getattr(stream, "isatty", None)
    return not (callable(isatty) and isatty())


_STATUS_BY_EVENT: dict[str, str] = {
    "run_started": "starting",
    "agent_launching": "launching agent",
    "agent_launched": "agent launched",
    "handshake": "handshake complete",
    "session_created": "session created",
    "prompt_started": "streaming",
    "cancel_requested": "cancelling",
    "terminated": "agent terminated",
    "step_finished": "finalizing",
    "run_finished": "finished",
}


def _event_line(envelope: EventEnvelope) -> str | None:
    """One human-readable progress line for an event, or None to stay quiet."""
    p = envelope.payload
    et = envelope.event_type
    if et == "run_started":
        return (
            f"[run] {envelope.run_id} started: agent {p.get('target', '?')} "
            f"(capture={p.get('capture_profile', '?')})"
        )
    if et == "policy_resolved":
        return f"[policy] {p.get('policy_name', '?')} (enforcement: {p.get('enforcement', '?')})"
    if et == "agent_launching":
        return f"[agent] launching: {p.get('command', '?')}"
    if et == "agent_launched":
        return f"[agent] launched (pid {p.get('pid', '?')})"
    if et == "handshake":
        version = p.get("agent_version")
        identity = str(p.get("agent_name", "?")) + (f" {version}" if version else "")
        return f"[agent] {identity} (protocol v{p.get('protocol_version', '?')})"
    if et == "session_created":
        return f"[agent] session {p.get('session_id', '?')}"
    if et == "prompt_started":
        return "[agent] prompt sent"
    if et in ("tool_call", "tool_call_update"):
        title = p.get("title") or p.get("tool_call_id") or "tool"
        kind = f" ({p['kind']})" if p.get("kind") else ""
        status = p.get("status") or ("started" if et == "tool_call" else "update")
        return f"[tool] {title}{kind}: {status}"
    if et == "permission_requested":
        return f"[permission] requested: {p.get('request_summary', '?')}"
    if et == "permission_decided":
        return (
            f"[permission] {p.get('request_summary', '?')}: {p.get('decision', '?')} "
            f"(rule {p.get('rule_id', '?')})"
        )
    if et == "fs_read":
        return (
            f"[fs] read {p.get('path', '?')}: {p.get('decision', '?')} "
            f"(rule {p.get('rule_id', '?')})"
        )
    if et == "fs_write":
        path = p.get("path") or p.get("requested_path") or "?"
        return f"[fs] write {path}: {p.get('decision', '?')} (rule {p.get('rule_id', '?')})"
    if et == "terminal_op":
        return f"[terminal] {p.get('op', '?')}: {p.get('decision', '?')}"
    if et == "file_change":
        return f"[file] {p.get('change_type', 'change')} {p.get('path', '?')}"
    if et == "cancel_requested":
        return f"[cancel] requested ({p.get('reason', '?')}); grace {p.get('grace_seconds', '?')}s"
    if et == "terminated":
        return f"[agent] terminated (exit {p.get('exit_code')}, {p.get('reason', '?')})"
    if et == "truncation":
        return "[truncation] per-step event byte ceiling reached; capture degraded to metadata"
    if et == "error":
        return f"[error] {p.get('code', '?')}: {p.get('message', '')}"
    if et == "egress_notice":
        return f"[egress] provider {p.get('provider', '?')}"
    if et == "step_finished":
        return (
            f"[step] {envelope.step_id or '?'}: {p.get('status', '?')} "
            f"({p.get('duration_ms', '?')} ms)"
        )
    if et == "run_finished":
        return f"[run] finished: {p.get('status', '?')}"
    return None


def _files_changed(result: RunResult) -> int:
    return sum(len(step.file_changes) for step in result.steps.values())


def _permissions_denied(result: RunResult) -> int:
    return sum(
        1
        for step in result.steps.values()
        for decision in step.permission_decisions
        if decision.decision is PermissionDecisionKind.DENIED
    )


class RunRenderer:
    """Renders one direct run's event stream to a terminal stream.

    ``on_event`` is shaped as a RunRecorder ``render_cb``. All output goes to
    ``stream`` (stderr under ``--json``). ``finish`` renders the end-of-run
    summary table; ``close`` (idempotent) flushes streaming state and stops
    any live display, and is safe to call before ``finish``.
    """

    def __init__(self, stream: TextIO, *, plain: bool) -> None:
        self._stream = stream
        self._plain = plain
        self._mid_stream = False  # plain mode: last write had no trailing newline
        self._pending = ""  # rich mode: buffered partial line of agent text
        self._console: Console | None = None
        self._live: Live | None = None
        if not plain:
            self._console = Console(file=stream, highlight=False)
            self._live = Live(
                Text("starting", style="dim"),
                console=self._console,
                refresh_per_second=10,
                transient=True,
            )
            self._live.start()

    # ------------------------------------------------------------ pipeline

    def on_event(self, envelope: EventEnvelope) -> None:
        """RunRecorder render_cb: stream text chunks, one line per other event."""
        if envelope.event_type == "message_chunk":
            self._stream_text(str(envelope.payload.get("text", "")))
            return
        line = _event_line(envelope)
        if line is not None:
            self._line(line)
        status = _STATUS_BY_EVENT.get(envelope.event_type)
        if status is not None and self._live is not None:
            self._live.update(Text(status, style="dim"))

    def finish(self, result: RunResult) -> None:
        """Flush streaming state and render the end-of-run summary table."""
        self.close()
        rows: list[tuple[str, str]] = [
            ("status", result.status.value),
            (
                "duration",
                f"{result.duration_ms} ms" if result.duration_ms is not None else "-",
            ),
            ("files changed", str(_files_changed(result))),
            ("permissions denied", str(_permissions_denied(result))),
            ("result", result.result_path or "(not saved)"),
        ]
        if self._plain or self._console is None:
            self._write("--- run summary ---\n")
            for label, value in rows:
                self._write(f"{label}: {value}\n")
            self._flush()
            return
        table = Table(title="run summary", show_header=False)
        table.add_column("field", style="bold")
        table.add_column("value")
        for label, value in rows:
            table.add_row(label, value)
        self._console.print(table)

    def close(self) -> None:
        """Stop live rendering and flush any partial streamed line (idempotent)."""
        if self._pending and self._console is not None:
            self._console.print(Text(self._pending))
        self._pending = ""
        if self._mid_stream:
            self._write("\n")
            self._mid_stream = False
        if self._live is not None:
            self._live.stop()
            self._live = None
        self._flush()

    # ------------------------------------------------------------- helpers

    def _write(self, text: str) -> None:
        self._stream.write(text)

    def _flush(self) -> None:
        flush = getattr(self._stream, "flush", None)
        if callable(flush):
            flush()

    def _stream_text(self, text: str) -> None:
        if not text:
            return
        if self._plain or self._console is None:
            self._write(text)
            self._mid_stream = not text.endswith("\n")
            self._flush()
            return
        self._pending += text
        while "\n" in self._pending:
            line, _, self._pending = self._pending.partition("\n")
            self._console.print(Text(line))

    def _line(self, line: str) -> None:
        if self._plain or self._console is None:
            if self._mid_stream:
                self._write("\n")
                self._mid_stream = False
            self._write(line + "\n")
            self._flush()
            return
        if self._pending:
            self._console.print(Text(self._pending))
            self._pending = ""
        self._console.print(Text(line, style="cyan"))


# ---------------------------------------------------------------- tables


def format_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    """Plain aligned text table (no ANSI): header, rule, then one row per line."""
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))

    def fmt(cells: Sequence[str]) -> str:
        return "  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(cells)).rstrip()

    lines = [fmt(list(headers)), fmt(["-" * width for width in widths])]
    lines.extend(fmt(row) for row in rows)
    return lines


# ------------------------------------------------------------- runs show


def _step_lines(step_id: str, step: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    attempts = step.get("attempts") or []
    stop_reason = None
    if attempts and isinstance(attempts[-1], Mapping):
        stop_reason = attempts[-1].get("stop_reason")
    suffix = f", stop {stop_reason}" if stop_reason else ""
    lines.append(f"  {step_id}: {step.get('status', '-')} (agent {step.get('agent', '-')}{suffix})")
    changes = [c for c in (step.get("file_changes") or []) if isinstance(c, Mapping)]
    lines.append(f"    files changed: {len(changes)}")
    for change in changes:
        lines.append(
            f"      {change.get('change_type', '?')} {change.get('path', '?')} "
            f"[{change.get('capture_method', '?')}]"
        )
    decisions = [d for d in (step.get("permission_decisions") or []) if isinstance(d, Mapping)]
    if decisions:
        lines.append(f"    policy decisions: {len(decisions)}")
        for decision in decisions:
            lines.append(
                f"      {decision.get('decision', '?')} "
                f"'{decision.get('request_summary', '?')}' "
                f"rule={decision.get('rule_id', '?')} "
                f"scope={decision.get('enforcement_scope', '?')}"
            )
    for error in step.get("errors") or []:
        if isinstance(error, Mapping):
            lines.append(f"    error {error.get('code', '?')}: {error.get('message', '')}")
    return lines


def run_show_lines(data: Mapping[str, Any]) -> list[str]:
    """REQ-015 detail view of one manifest dict (tolerant of partial shapes,
    e.g. store-recovered ``abandoned`` manifests without steps)."""

    def text(value: Any, default: str = "-") -> str:
        return str(value) if value not in (None, "") else default

    duration = data.get("duration_ms")
    lines = [
        f"run: {text(data.get('run_id'))}",
        (
            f"kind: {text(data.get('kind'))}  target: {text(data.get('target'))}  "
            f"status: {text(data.get('status'))}"
        ),
        (
            f"started: {text(data.get('started_at'))}  ended: {text(data.get('ended_at'))}  "
            f"duration: {f'{duration} ms' if isinstance(duration, int) else '-'}"
        ),
        f"workspace: {text(data.get('workspace'))}",
        (
            f"persisted: {text(data.get('persisted'))}  "
            f"result: {text(data.get('result_path'), '(not saved)')}"
        ),
    ]
    fingerprint = data.get("config_fingerprint")
    if fingerprint:
        lines.append(f"config fingerprint: {fingerprint}")
    policy = data.get("policy")
    if isinstance(policy, Mapping):
        lines.append(
            f"policy: {text(policy.get('policy_name'))} "
            f"(ceiling: {text(policy.get('ceiling_source'))}; "
            f"enforcement: {text(policy.get('enforcement'))}; "
            f"default scope: {text(policy.get('enforcement_scope_default'))})"
        )
    capture = data.get("capture")
    truncated: list[str] = []
    if isinstance(capture, Mapping) and capture:
        lines.append("capture:")
        for name in sorted(capture):
            entry = capture[name]
            if not isinstance(entry, Mapping):
                continue
            if entry.get("truncated"):
                truncated.append(name)
            mark = " (truncated)" if entry.get("truncated") else ""
            lines.append(
                f"  {name}: {entry.get('status', '-')} ({entry.get('event_count', 0)} events, "
                f"{entry.get('byte_count', 0)} bytes){mark}"
            )
        lines.append("truncation: " + (", ".join(truncated) if truncated else "none"))
    else:
        lines.append("capture: (not recorded)")
        lines.append("truncation: unknown")
    steps = data.get("steps")
    if isinstance(steps, Mapping) and steps:
        lines.append("steps:")
        for step_id, step in steps.items():
            if isinstance(step, Mapping):
                lines.extend(_step_lines(str(step_id), step))
    egress = data.get("egress")
    if isinstance(egress, list) and egress:
        lines.append("egress:")
        for record in egress:
            if isinstance(record, Mapping):
                acknowledged = record.get("acknowledged_by") or "not acknowledged"
                lines.append(
                    f"  {record.get('step_id', '?')}: provider {record.get('provider', '?')} "
                    f"(acknowledged by: {acknowledged})"
                )
    errors = data.get("errors")
    if isinstance(errors, list) and errors:
        lines.append("errors:")
        for error in errors:
            if isinstance(error, Mapping):
                lines.append(f"  {error.get('code', '?')}: {error.get('message', '')}")
    return lines
