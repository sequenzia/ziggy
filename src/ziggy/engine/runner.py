"""Phase-1 direct-run engine (REQ-004/005, §5.1, §6.4).

``execute_run`` drives one agent through one implicit step named ``main`` and
always returns a full :class:`RunResult`; run-level failures live inside the
result as typed errors, never as raised exceptions. The caller (CLI in Phase
2) maps result contents to exit codes.

Stop-reason mapping (documented decision): ``end_turn`` is the only stop
reason that makes the step ``success``. ``cancelled`` maps to a ``cancelled``
step with a ``CancelledError`` — whether Ziggy requested the cancel or the
agent reported it unprompted. Every other stop reason (``refusal``,
``max_tokens``, ``max_turn_requests``) means the agent ended the turn without
completing the requested work, so the step is ``failed`` with a
``ProtocolError`` carrying ``details={"stop_reason": ...}``; these are valid
agent outcomes, but Phase 1 has no retry/partial semantics to express them
more precisely, and failing honestly beats claiming success.

Persistence degradation (spec §5.3 edge cases): if the store cannot even be
bootstrapped, the run still executes with in-memory recording only and the
result carries a ``PersistenceError``; if ``result.json`` cannot be written at
finalize, the in-memory result is still returned with ``persisted=False`` and
the error attached. The SQLite index row is inserted only after the manifest
is durable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path

from ziggy.acp import AgentProcessClient, HandshakeInfo, StopInfo
from ziggy.engine.hooks import DEFAULT_DENY_POLICY_NAME, DEFAULT_DENY_RULE_ID, RecordingHooks
from ziggy.errors import (
    AgentLaunchError,
    CancelledError,
    PersistenceError,
    ProtocolError,
    StepTimeoutError,
    ZiggyError,
)
from ziggy.events import DEFAULT_LIMITS, EventLimits, RunRecorder
from ziggy.ids import MonotonicClock, new_run_id, utc_now_iso
from ziggy.models.common import (
    CaptureProfile,
    CaptureStatus,
    EnforcementScope,
    RunKind,
    RunStatus,
    StepStatus,
)
from ziggy.models.events import EventEnvelope
from ziggy.models.result import (
    AgentInfo,
    Attempt,
    EgressRecord,
    PolicyProvenance,
    RunResult,
    StepResult,
)
from ziggy.redact import Redactor
from ziggy.store import IndexRow, RunDirWriter, RunIndex, RunStore

MAIN_STEP_ID = "main"

RenderCallback = Callable[[EventEnvelope], None]

#: Env var NAME fragments treated as credential-bearing for exact-match
#: redaction. Phase-1 stand-in for config-declared secrets (``api_key_env``
#: plus the configured secret list, Phase 2).
_SECRET_ENV_MARKERS = ("API_KEY", "APIKEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")

_RUN_STATUS_OF_STEP: dict[StepStatus, RunStatus] = {
    StepStatus.SUCCESS: RunStatus.SUCCESS,
    StepStatus.FAILED: RunStatus.FAILED,
    StepStatus.CANCELLED: RunStatus.CANCELLED,
}

_CAPTURE_SEVERITY: dict[CaptureStatus, int] = {
    CaptureStatus.COMPLETE: 0,
    CaptureStatus.DERIVED: 1,
    CaptureStatus.PARTIAL: 2,
    CaptureStatus.UNAVAILABLE: 3,
}


@dataclass(slots=True)
class RunSpec:
    """Fully-resolved inputs for one direct agent run.

    This is the Phase-1 stand-in for the config layer: Phase 2 constructs it
    from merged trusted config + CLI flags. ``env`` is the complete explicit
    subprocess environment (nothing is inherited implicitly).
    ``store_root=None`` uses the RunStore default (``$ZIGGY_HOME`` or
    ``~/.ziggy``).
    """

    agent_name: str
    command: str
    args: list[str]
    env: dict[str, str]
    cwd: str
    prompt: str
    capture_profile: CaptureProfile = CaptureProfile.STANDARD
    no_save: bool = False
    step_timeout_seconds: float = 1800.0
    cancel_grace_seconds: float = 5.0
    provider: str | None = None
    limits: EventLimits = DEFAULT_LIMITS
    store_root: Path | None = None


@dataclass(slots=True)
class _StepOutcome:
    status: StepStatus
    error: ZiggyError | None = None
    stop_reason: str | None = None
    exit_code: int | None = None
    handshake: HandshakeInfo | None = None
    session_id: str | None = None
    #: True when the turn was interrupted (crash/timeout/cancel), so no
    #: artifact class may claim better than ``partial`` capture.
    capture_degraded: bool = False


def _secret_env_values(env: Mapping[str, str]) -> list[tuple[str, str]]:
    return [
        (f"env:{name}", value)
        for name, value in env.items()
        if value and any(marker in name.upper() for marker in _SECRET_ENV_MARKERS)
    ]


def _emit_error(recorder: RunRecorder, error: ZiggyError, *, session_id: str | None = None) -> None:
    recorder.emit(
        event_type="error",
        step_id=MAIN_STEP_ID,
        attempt_no=1,
        session_id=session_id,
        payload={"code": error.code, "message": error.message, "details": error.details},
    )


async def _reap_prompt(prompt_task: asyncio.Task[StopInfo]) -> StopInfo | None:
    """Settle the prompt task after teardown; a still-pending task is
    cancelled. Returns the StopInfo when the turn actually resolved."""
    if not prompt_task.done():
        prompt_task.cancel()
    try:
        return await prompt_task
    except (asyncio.CancelledError, ProtocolError):
        return None


async def _cancel_ladder(
    client: AgentProcessClient,
    session_id: str,
    prompt_task: asyncio.Task[StopInfo],
    grace_seconds: float,
) -> tuple[int | None, StopInfo | None]:
    """Teardown ladder steps 1-2 (ACP cancel + grace, still draining events),
    then hand the rest (group TERM/KILL/reap) to ``client.shutdown``."""
    await client.cancel(session_id)
    stop: StopInfo | None = None
    with suppress(TimeoutError, ProtocolError):
        stop = await asyncio.wait_for(asyncio.shield(prompt_task), timeout=max(grace_seconds, 0.0))
    exit_code = await client.shutdown(0.0)
    late = await _reap_prompt(prompt_task)
    return exit_code, stop if stop is not None else late


def _stop_outcome(
    stop: StopInfo,
    *,
    exit_code: int | None,
    handshake: HandshakeInfo,
    session_id: str,
    recorder: RunRecorder,
) -> _StepOutcome:
    reason = stop.stop_reason
    if reason == "end_turn":
        return _StepOutcome(
            status=StepStatus.SUCCESS,
            stop_reason=reason,
            exit_code=exit_code,
            handshake=handshake,
            session_id=session_id,
        )
    if reason == "cancelled":
        error: ZiggyError = CancelledError(
            "agent reported the turn as cancelled", details={"stop_reason": reason}
        )
        _emit_error(recorder, error, session_id=session_id)
        return _StepOutcome(
            status=StepStatus.CANCELLED,
            error=error,
            stop_reason=reason,
            exit_code=exit_code,
            handshake=handshake,
            session_id=session_id,
            capture_degraded=True,
        )
    error = ProtocolError(
        f"turn ended without completing: {reason}", details={"stop_reason": reason}
    )
    _emit_error(recorder, error, session_id=session_id)
    return _StepOutcome(
        status=StepStatus.FAILED,
        error=error,
        stop_reason=reason,
        exit_code=exit_code,
        handshake=handshake,
        session_id=session_id,
    )


async def _drive_step(
    spec: RunSpec,
    recorder: RunRecorder,
    hooks: RecordingHooks,
    cancel_event: asyncio.Event | None,
) -> _StepOutcome:
    """Launch, handshake, prompt, and tear down one agent subprocess."""
    recorder.emit(
        event_type="agent_launching",
        step_id=MAIN_STEP_ID,
        attempt_no=1,
        payload={"command": spec.command, "args": list(spec.args)},
    )
    try:
        client = await AgentProcessClient.launch(
            command=spec.command, args=spec.args, env=spec.env, cwd=spec.cwd, hooks=hooks
        )
    except AgentLaunchError as exc:
        _emit_error(recorder, exc)
        return _StepOutcome(status=StepStatus.FAILED, error=exc)
    recorder.emit(
        event_type="agent_launched",
        step_id=MAIN_STEP_ID,
        attempt_no=1,
        payload={"pid": client.pid, "pgid": client.pgid},
    )

    shutdown_done = False

    async def shutdown(grace: float = 0.0) -> int | None:
        nonlocal shutdown_done
        shutdown_done = True
        return await client.shutdown(grace)

    try:
        try:
            handshake = await client.initialize()
        except ProtocolError as exc:
            _emit_error(recorder, exc)
            exit_code = await shutdown()
            return _StepOutcome(
                status=StepStatus.FAILED,
                error=exc,
                exit_code=exit_code,
                capture_degraded=True,
            )
        recorder.emit(
            event_type="handshake",
            step_id=MAIN_STEP_ID,
            attempt_no=1,
            payload=asdict(handshake),
        )

        try:
            session_id = await client.new_session(spec.cwd)
        except ProtocolError as exc:
            _emit_error(recorder, exc)
            exit_code = await shutdown()
            return _StepOutcome(
                status=StepStatus.FAILED,
                error=exc,
                exit_code=exit_code,
                handshake=handshake,
                capture_degraded=True,
            )
        hooks.session_id = session_id
        recorder.emit(
            event_type="session_created",
            step_id=MAIN_STEP_ID,
            attempt_no=1,
            session_id=session_id,
            payload={"session_id": session_id, "cwd": spec.cwd},
        )
        recorder.emit(
            event_type="prompt_started",
            step_id=MAIN_STEP_ID,
            attempt_no=1,
            session_id=session_id,
            payload={"text": spec.prompt},
        )

        prompt_task = asyncio.create_task(client.prompt(session_id, spec.prompt))
        cancel_task: asyncio.Task[bool] | None = None
        waiters: set[asyncio.Task] = {prompt_task}
        if cancel_event is not None:
            cancel_task = asyncio.create_task(cancel_event.wait())
            waiters.add(cancel_task)
        try:
            done, _ = await asyncio.wait(
                waiters,
                timeout=spec.step_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if cancel_task is not None and not cancel_task.done():
                cancel_task.cancel()
                with suppress(asyncio.CancelledError):
                    await cancel_task

        if prompt_task in done:
            try:
                stop = prompt_task.result()
            except ProtocolError as exc:
                _emit_error(recorder, exc, session_id=session_id)
                exit_code = await shutdown()
                recorder.emit(
                    event_type="terminated",
                    step_id=MAIN_STEP_ID,
                    attempt_no=1,
                    session_id=session_id,
                    payload={"exit_code": exit_code, "reason": "protocol_error"},
                )
                return _StepOutcome(
                    status=StepStatus.FAILED,
                    error=exc,
                    exit_code=exit_code,
                    handshake=handshake,
                    session_id=session_id,
                    capture_degraded=True,
                )
            exit_code = await shutdown()
            recorder.emit(
                event_type="terminated",
                step_id=MAIN_STEP_ID,
                attempt_no=1,
                session_id=session_id,
                payload={"exit_code": exit_code, "reason": "turn_complete"},
            )
            return _stop_outcome(
                stop,
                exit_code=exit_code,
                handshake=handshake,
                session_id=session_id,
                recorder=recorder,
            )

        cancelled = cancel_task is not None and cancel_task in done
        reason = "cancel" if cancelled else "timeout"
        recorder.emit(
            event_type="cancel_requested",
            step_id=MAIN_STEP_ID,
            attempt_no=1,
            session_id=session_id,
            payload={"reason": reason, "grace_seconds": spec.cancel_grace_seconds},
        )
        shutdown_done = True  # the ladder owns teardown from here
        exit_code, stop = await _cancel_ladder(
            client, session_id, prompt_task, spec.cancel_grace_seconds
        )
        recorder.emit(
            event_type="terminated",
            step_id=MAIN_STEP_ID,
            attempt_no=1,
            session_id=session_id,
            payload={"exit_code": exit_code, "reason": reason},
        )
        stop_reason = stop.stop_reason if stop is not None else None
        if cancelled:
            error: ZiggyError = CancelledError(
                "run cancelled", details={"stop_reason": stop_reason}
            )
            _emit_error(recorder, error, session_id=session_id)
            return _StepOutcome(
                status=StepStatus.CANCELLED,
                error=error,
                stop_reason=stop_reason,
                exit_code=exit_code,
                handshake=handshake,
                session_id=session_id,
                capture_degraded=True,
            )
        error = StepTimeoutError(
            f"step '{MAIN_STEP_ID}' exceeded {spec.step_timeout_seconds}s",
            details={
                "timeout_seconds": spec.step_timeout_seconds,
                "stop_reason": stop_reason,
            },
        )
        _emit_error(recorder, error, session_id=session_id)
        return _StepOutcome(
            status=StepStatus.FAILED,
            error=error,
            stop_reason=stop_reason,
            exit_code=exit_code,
            handshake=handshake,
            session_id=session_id,
            capture_degraded=True,
        )
    finally:
        if not shutdown_done:
            await client.shutdown(0.0)


def _degrade_capture(result: RunResult) -> None:
    for entry in result.capture.values():
        if _CAPTURE_SEVERITY[entry.status] < _CAPTURE_SEVERITY[CaptureStatus.PARTIAL]:
            entry.status = CaptureStatus.PARTIAL


def _persist(result: RunResult, writer: RunDirWriter, index_db: Path) -> None:
    """Atomic manifest first; index row only once the manifest is durable."""
    result.persisted = True
    result.result_path = str(writer.run_dir / "result.json")
    result.events_path = str(writer.events_path)
    try:
        writer.write_result(result.model_dump_json(indent=2))
    except PersistenceError as exc:
        result.persisted = False
        result.result_path = None
        result.errors.append(exc.to_model())
        return
    try:
        with RunIndex(index_db) as index:
            index.insert_or_replace(
                IndexRow(
                    run_id=result.run_id,
                    kind=result.kind.value,
                    target=result.target,
                    status=result.status.value,
                    started_at=result.started_at,
                    ended_at=result.ended_at,
                    duration_ms=result.duration_ms,
                    workspace=result.workspace,
                    result_path=result.result_path,
                )
            )
    except PersistenceError as exc:
        result.errors.append(exc.to_model())


async def execute_run(
    spec: RunSpec,
    *,
    render_cb: RenderCallback | None = None,
    cancel_event: asyncio.Event | None = None,
) -> RunResult:
    """Execute one direct agent run; always returns the full RunResult.

    The run id and in-memory RunResult scaffolding exist before anything
    fallible (REQ-004); store bootstrap failure degrades to in-memory
    recording rather than aborting the run. ``cancel_event`` is the caller's
    cancellation signal (Ctrl-C in the CLI); setting it triggers the full
    teardown ladder and a ``cancelled`` terminal state.
    """
    run_id = new_run_id()
    clock = MonotonicClock()
    started_at = utc_now_iso()
    step = StepResult(step_id=MAIN_STEP_ID, agent=spec.agent_name, status=StepStatus.FAILED)
    result = RunResult(
        run_id=run_id,
        kind=RunKind.AGENT,
        target=spec.agent_name,
        status=RunStatus.FAILED,
        started_at=started_at,
        workspace=spec.cwd,
        capture_profile=spec.capture_profile,
        persisted=False,
        policy=PolicyProvenance(
            policy_name=DEFAULT_DENY_POLICY_NAME,
            ceiling_source="default",
            rules=[{"rule_id": DEFAULT_DENY_RULE_ID, "action": "deny", "surface": "all"}],
            enforcement="advisory",
            enforcement_scope_default=EnforcementScope.ACP_MEDIATED,
        ),
        steps={MAIN_STEP_ID: step},
    )

    writer: RunDirWriter | None = None
    index_db: Path | None = None
    if not spec.no_save:
        try:
            store = RunStore(spec.store_root)
            writer = store.open_run(run_id)
            index_db = store.index_db_path
        except PersistenceError as exc:
            result.errors.append(exc.to_model())

    redactor = Redactor(secret_values=_secret_env_values(spec.env))
    recorder = RunRecorder(
        run_id=run_id,
        store_writer=writer,
        redactor=redactor,
        capture_profile=spec.capture_profile,
        limits=spec.limits,
        render_cb=render_cb,
        clock=clock,
    )
    hooks = RecordingHooks(recorder=recorder, step_id=MAIN_STEP_ID, attempt_no=1)

    recorder.emit(
        event_type="run_started",
        payload={
            "kind": RunKind.AGENT.value,
            "target": spec.agent_name,
            "workspace": spec.cwd,
            "capture_profile": spec.capture_profile.value,
        },
    )
    recorder.emit(
        event_type="policy_resolved",
        payload={
            "policy_name": DEFAULT_DENY_POLICY_NAME,
            "rule_id": DEFAULT_DENY_RULE_ID,
            "enforcement": "advisory",
        },
    )
    recorder.emit(
        event_type="step_started",
        step_id=MAIN_STEP_ID,
        attempt_no=1,
        payload={"agent": spec.agent_name},
    )

    attempt = Attempt(attempt_no=1, status=StepStatus.FAILED, started_at=utc_now_iso())
    attempt_start_ms = clock.elapsed_ms()

    outcome = await _drive_step(spec, recorder, hooks, cancel_event)

    attempt.ended_at = utc_now_iso()
    attempt.duration_ms = clock.elapsed_ms() - attempt_start_ms
    attempt.status = outcome.status
    attempt.stop_reason = outcome.stop_reason
    attempt.exit_code = outcome.exit_code
    if outcome.error is not None:
        attempt.errors = [outcome.error.to_model()]
        step.errors = [outcome.error.to_model()]

    if outcome.handshake is not None:
        step.agent_info = AgentInfo(
            name=spec.agent_name,
            provider=spec.provider,
            protocol_version=outcome.handshake.protocol_version,
            agent_name=outcome.handshake.agent_name,
            agent_title=outcome.handshake.agent_title,
            agent_version=outcome.handshake.agent_version,
            capabilities=outcome.handshake.capabilities,
            auth_methods=outcome.handshake.auth_methods,
        )

    redacted_prompt, _ = redactor.redact_text(spec.prompt)
    step.inputs_resolved = {"prompt": redacted_prompt}
    step.input_sources = {"prompt": "direct:prompt"}
    # Re-redact the assembled transcript: a secret split across stream chunks
    # is invisible per-chunk but contiguous (and matchable) once concatenated.
    assembled_text, _ = redactor.redact_text(recorder.text_output(MAIN_STEP_ID))
    step.outputs = {"text": assembled_text}
    step.tool_calls = recorder.tool_calls(MAIN_STEP_ID)
    step.file_changes = recorder.file_changes(MAIN_STEP_ID)
    step.permission_decisions = recorder.permission_decisions(MAIN_STEP_ID)
    step.attempts = [attempt]
    step.status = outcome.status
    result.status = _RUN_STATUS_OF_STEP[outcome.status]

    recorder.emit(
        event_type="step_finished",
        step_id=MAIN_STEP_ID,
        attempt_no=1,
        session_id=outcome.session_id,
        payload={"status": outcome.status.value, "duration_ms": attempt.duration_ms},
    )
    recorder.emit(event_type="run_finished", payload={"status": result.status.value})
    await recorder.finalize()

    result.capture = recorder.capture_summary()
    if outcome.capture_degraded:
        _degrade_capture(result)
    result.redaction = recorder.redaction_summary()
    usage = recorder.usage_summary()
    if usage is not None and usage.provider is None:
        usage.provider = spec.provider
    result.usage = usage
    if spec.provider:
        result.egress = [EgressRecord(step_id=MAIN_STEP_ID, provider=spec.provider)]
    result.ended_at = utc_now_iso()
    result.duration_ms = clock.elapsed_ms()

    for perr in recorder.persistence_errors:
        result.errors.append(perr.to_model())

    if writer is not None and index_db is not None:
        try:
            _persist(result, writer, index_db)
        finally:
            writer.finalize()
    return result
