"""Direct-run engine (REQ-004/005, §5.1, §6.4).

``execute_run`` drives one agent through one implicit step named ``main`` and
always returns a full :class:`RunResult`; run-level failures live inside the
result as typed errors, never as raised exceptions. The caller (CLI in Phase
2) maps result contents to exit codes.

Phase 2 wires config, policy, and metadata logs through :class:`RunSpec`
(see the dataclass docstring): a spec built by ``prepare_run`` carries the
guarded mediation policy (served by :class:`PolicyHooks`), exact-value
redaction seeds, the config fingerprint, and a metadata logger emitting
lifecycle lines (launch, handshake, prompt-start, permission decisions,
cancellation/termination, persistence). A plain Phase-1 spec behaves exactly
as before.

Phase 3 extracts the per-step core into :func:`execute_step` — launch,
handshake, session, prompt, and teardown of ONE agent subprocess, driven by a
:class:`StepExecutionContext` — so direct runs and workflow steps share one
execution path. ``execute_run`` keeps its exact public contract and delegates
to ``execute_step`` for its ``main`` step; the workflow runner
(``ziggy.workflows.runner``) builds one context per workflow step.

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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ziggy.acp import AgentProcessClient, HandshakeInfo, StopInfo
from ziggy.engine.hooks import (
    DEFAULT_DENY_POLICY_NAME,
    DEFAULT_DENY_RULE_ID,
    MetadataLoggerLike,
    PermissionDecider,
    PolicyHooks,
    RecordingHooks,
)
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
from ziggy.policy import MediationPolicy, build_policy_provenance
from ziggy.redact import CustomPattern, Redactor
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

    Phase 2 constructs this from merged trusted config + CLI flags via
    :func:`ziggy.engine.prepare.prepare_run`; direct construction with only
    the Phase-1 fields remains supported (every Phase-2 field defaults to the
    Phase-1 behavior). ``env`` is the complete explicit subprocess environment
    (nothing is inherited implicitly). ``store_root=None`` uses the RunStore
    default (``$ZIGGY_HOME`` or ``~/.ziggy``).

    Phase-2 extensions:

    - ``secret_values``: exact-match ``(kind, value)`` pairs for the run's
      Redactor (``api_key_env`` value + config ``redaction.extra_value_env_vars``).
      ``None`` keeps the Phase-1 env-var *name* heuristic as a fallback.
    - ``redaction_patterns``: config custom patterns, passed to the Redactor.
    - ``policy``: guarded mediation policy; when present the run uses
      :class:`PolicyHooks` instead of the Phase-1 default-deny hooks.
    - ``policy_provenance``: RunResult policy snapshot (derived from
      ``policy`` when omitted).
    - ``logger``: metadata logger for lifecycle log lines (``None`` = silent).
    - ``config_fingerprint``: resolved-config fingerprint for the RunResult.
    - ``egress_acknowledged_by``: how the provider egress was acknowledged
      (``config`` | ``flag:--acknowledge-egress``), recorded on EgressRecord.
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
    secret_values: list[tuple[str, str]] | None = None
    redaction_patterns: list[CustomPattern] | None = None
    policy: MediationPolicy | None = None
    policy_provenance: PolicyProvenance | None = None
    logger: MetadataLoggerLike | None = None
    config_fingerprint: str | None = None
    egress_acknowledged_by: str | None = None


@dataclass(slots=True)
class StepExecutionContext:
    """Everything :func:`execute_step` needs to run ONE agent step (Phase 3).

    The context is deliberately launch-shaped (command/args/env/cwd), not
    config-shaped: the caller — ``execute_run`` for the implicit ``main`` step,
    ``ziggy.workflows.runner`` per workflow step — has already resolved agent
    registration, per-step policy, timeouts, and the composed prompt.

    ``session_label`` is the human-readable agent label attached to metadata
    log lines and step events (the ziggy-registered agent name in practice).
    ``recorder`` is the run-wide :class:`RunRecorder`; every event emitted for
    this step carries ``step_id``/``attempt_no`` from here. ``policy=None``
    selects the Phase-1 default-deny hooks (no mediated serving).
    """

    step_id: str
    command: str
    args: list[str]
    env: dict[str, str]
    cwd: str
    prompt: str
    timeout_seconds: float
    grace_seconds: float
    recorder: RunRecorder
    session_label: str
    policy: MediationPolicy | None = None
    logger: MetadataLoggerLike | None = None
    cancel_event: asyncio.Event | None = None
    attempt_no: int = 1
    #: Phase-4 server bridge: overrides how permission requests are decided
    #: (policy ceiling + upstream-client forwarding); ``None`` keeps the local
    #: policy decision.
    decide_permission: PermissionDecider | None = None
    _hooks: RecordingHooks | None = field(default=None, repr=False, init=False)

    def hooks(self) -> RecordingHooks:
        """The step's mediation hooks (policy-backed when a policy is set)."""
        if self._hooks is None:
            if self.policy is not None:
                self._hooks = PolicyHooks(
                    recorder=self.recorder,
                    policy=self.policy,
                    logger=self.logger,
                    step_id=self.step_id,
                    attempt_no=self.attempt_no,
                    decide_permission=self.decide_permission,
                )
            else:
                self._hooks = RecordingHooks(
                    recorder=self.recorder,
                    step_id=self.step_id,
                    attempt_no=self.attempt_no,
                    decide_permission=self.decide_permission,
                )
        return self._hooks


@dataclass(slots=True)
class StepOutcome:
    """Terminal outcome of one :func:`execute_step` invocation."""

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


def _mlog(
    logger: MetadataLoggerLike | None,
    event: str,
    *,
    run_id: str,
    step_id: str | None = None,
    agent: str | None = None,
    level: str = "info",
    **detail: Any,
) -> None:
    """Best-effort metadata-log line (REQ-016): write failures never disturb
    the run; detail-allowlist misuse (``ValueError``) stays loud."""
    if logger is None:
        return
    with suppress(PersistenceError):
        logger.log(event, run_id=run_id, step_id=step_id, agent=agent, level=level, **detail)


def _emit_error(
    recorder: RunRecorder,
    error: ZiggyError,
    *,
    step_id: str,
    attempt_no: int = 1,
    session_id: str | None = None,
) -> None:
    recorder.emit(
        event_type="error",
        step_id=step_id,
        attempt_no=attempt_no,
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
    ctx: StepExecutionContext,
    exit_code: int | None,
    handshake: HandshakeInfo,
    session_id: str,
) -> StepOutcome:
    reason = stop.stop_reason
    if reason == "end_turn":
        return StepOutcome(
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
        _emit_error(
            ctx.recorder,
            error,
            step_id=ctx.step_id,
            attempt_no=ctx.attempt_no,
            session_id=session_id,
        )
        return StepOutcome(
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
    _emit_error(
        ctx.recorder,
        error,
        step_id=ctx.step_id,
        attempt_no=ctx.attempt_no,
        session_id=session_id,
    )
    return StepOutcome(
        status=StepStatus.FAILED,
        error=error,
        stop_reason=reason,
        exit_code=exit_code,
        handshake=handshake,
        session_id=session_id,
    )


async def execute_step(ctx: StepExecutionContext) -> StepOutcome:
    """Launch, handshake, prompt, and tear down one agent subprocess.

    The shared per-step core of direct runs and workflow steps: a fresh
    subprocess + ACP session per invocation, the full teardown ladder on
    timeout/cancel, and every event recorded with the context's
    ``step_id``/``attempt_no``. Never raises for agent-side failure — the
    outcome carries the typed error; only caller bugs (bad context) escape.
    The caller emits ``step_started``/``step_finished`` and owns
    StepResult/Attempt bookkeeping (see :func:`settle_step_result`).
    """
    recorder = ctx.recorder
    hooks = ctx.hooks()
    recorder.emit(
        event_type="agent_launching",
        step_id=ctx.step_id,
        attempt_no=ctx.attempt_no,
        payload={"command": ctx.command, "args": list(ctx.args)},
    )
    try:
        client = await AgentProcessClient.launch(
            command=ctx.command, args=ctx.args, env=ctx.env, cwd=ctx.cwd, hooks=hooks
        )
    except AgentLaunchError as exc:
        _emit_error(recorder, exc, step_id=ctx.step_id, attempt_no=ctx.attempt_no)
        _mlog(
            ctx.logger,
            "agent_launch_failed",
            run_id=recorder.run_id,
            step_id=ctx.step_id,
            agent=ctx.session_label,
            level="error",
            reason_code=exc.code,
        )
        return StepOutcome(status=StepStatus.FAILED, error=exc)
    recorder.emit(
        event_type="agent_launched",
        step_id=ctx.step_id,
        attempt_no=ctx.attempt_no,
        payload={"pid": client.pid, "pgid": client.pgid},
    )
    _mlog(
        ctx.logger,
        "agent_launched",
        run_id=recorder.run_id,
        step_id=ctx.step_id,
        agent=ctx.session_label,
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
            _emit_error(recorder, exc, step_id=ctx.step_id, attempt_no=ctx.attempt_no)
            exit_code = await shutdown()
            return StepOutcome(
                status=StepStatus.FAILED,
                error=exc,
                exit_code=exit_code,
                capture_degraded=True,
            )
        recorder.emit(
            event_type="handshake",
            step_id=ctx.step_id,
            attempt_no=ctx.attempt_no,
            payload=asdict(handshake),
        )
        _mlog(
            ctx.logger,
            "handshake",
            run_id=recorder.run_id,
            step_id=ctx.step_id,
            agent=ctx.session_label,
        )

        try:
            session_id = await client.new_session(ctx.cwd)
        except ProtocolError as exc:
            _emit_error(recorder, exc, step_id=ctx.step_id, attempt_no=ctx.attempt_no)
            exit_code = await shutdown()
            return StepOutcome(
                status=StepStatus.FAILED,
                error=exc,
                exit_code=exit_code,
                handshake=handshake,
                capture_degraded=True,
            )
        hooks.session_id = session_id
        recorder.emit(
            event_type="session_created",
            step_id=ctx.step_id,
            attempt_no=ctx.attempt_no,
            session_id=session_id,
            payload={"session_id": session_id, "cwd": ctx.cwd},
        )
        _mlog(
            ctx.logger,
            "session_created",
            run_id=recorder.run_id,
            step_id=ctx.step_id,
            agent=ctx.session_label,
        )
        recorder.emit(
            event_type="prompt_started",
            step_id=ctx.step_id,
            attempt_no=ctx.attempt_no,
            session_id=session_id,
            payload={"text": ctx.prompt},
        )
        _mlog(
            ctx.logger,
            "prompt_started",
            run_id=recorder.run_id,
            step_id=ctx.step_id,
            agent=ctx.session_label,
        )

        prompt_task = asyncio.create_task(client.prompt(session_id, ctx.prompt))
        cancel_task: asyncio.Task[bool] | None = None
        waiters: set[asyncio.Task] = {prompt_task}
        if ctx.cancel_event is not None:
            cancel_task = asyncio.create_task(ctx.cancel_event.wait())
            waiters.add(cancel_task)
        try:
            done, _ = await asyncio.wait(
                waiters,
                timeout=ctx.timeout_seconds,
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
                _emit_error(
                    recorder,
                    exc,
                    step_id=ctx.step_id,
                    attempt_no=ctx.attempt_no,
                    session_id=session_id,
                )
                exit_code = await shutdown()
                recorder.emit(
                    event_type="terminated",
                    step_id=ctx.step_id,
                    attempt_no=ctx.attempt_no,
                    session_id=session_id,
                    payload={"exit_code": exit_code, "reason": "protocol_error"},
                )
                _mlog(
                    ctx.logger,
                    "terminated",
                    run_id=recorder.run_id,
                    step_id=ctx.step_id,
                    agent=ctx.session_label,
                    exit_code=exit_code,
                    reason_code="protocol_error",
                )
                return StepOutcome(
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
                step_id=ctx.step_id,
                attempt_no=ctx.attempt_no,
                session_id=session_id,
                payload={"exit_code": exit_code, "reason": "turn_complete"},
            )
            _mlog(
                ctx.logger,
                "terminated",
                run_id=recorder.run_id,
                step_id=ctx.step_id,
                agent=ctx.session_label,
                exit_code=exit_code,
                reason_code="turn_complete",
                stop_reason=stop.stop_reason,
            )
            return _stop_outcome(
                stop,
                ctx=ctx,
                exit_code=exit_code,
                handshake=handshake,
                session_id=session_id,
            )

        cancelled = cancel_task is not None and cancel_task in done
        reason = "cancel" if cancelled else "timeout"
        recorder.emit(
            event_type="cancel_requested",
            step_id=ctx.step_id,
            attempt_no=ctx.attempt_no,
            session_id=session_id,
            payload={"reason": reason, "grace_seconds": ctx.grace_seconds},
        )
        _mlog(
            ctx.logger,
            "cancel_requested",
            run_id=recorder.run_id,
            step_id=ctx.step_id,
            agent=ctx.session_label,
            reason_code=reason,
        )
        shutdown_done = True  # the ladder owns teardown from here
        exit_code, stop = await _cancel_ladder(client, session_id, prompt_task, ctx.grace_seconds)
        recorder.emit(
            event_type="terminated",
            step_id=ctx.step_id,
            attempt_no=ctx.attempt_no,
            session_id=session_id,
            payload={"exit_code": exit_code, "reason": reason},
        )
        _mlog(
            ctx.logger,
            "terminated",
            run_id=recorder.run_id,
            step_id=ctx.step_id,
            agent=ctx.session_label,
            exit_code=exit_code,
            reason_code=reason,
        )
        stop_reason = stop.stop_reason if stop is not None else None
        if cancelled:
            error: ZiggyError = CancelledError(
                "run cancelled", details={"stop_reason": stop_reason}
            )
            _emit_error(
                recorder,
                error,
                step_id=ctx.step_id,
                attempt_no=ctx.attempt_no,
                session_id=session_id,
            )
            return StepOutcome(
                status=StepStatus.CANCELLED,
                error=error,
                stop_reason=stop_reason,
                exit_code=exit_code,
                handshake=handshake,
                session_id=session_id,
                capture_degraded=True,
            )
        error = StepTimeoutError(
            f"step '{ctx.step_id}' exceeded {ctx.timeout_seconds}s",
            details={
                "timeout_seconds": ctx.timeout_seconds,
                "stop_reason": stop_reason,
            },
        )
        _emit_error(
            recorder,
            error,
            step_id=ctx.step_id,
            attempt_no=ctx.attempt_no,
            session_id=session_id,
        )
        return StepOutcome(
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


def settle_step_result(
    *,
    step: StepResult,
    attempt: Attempt,
    outcome: StepOutcome,
    duration_ms: int,
    recorder: RunRecorder,
    redactor: Redactor,
    agent_name: str,
    provider: str | None,
) -> None:
    """Fill one StepResult + Attempt pair from a settled :class:`StepOutcome`.

    Shared by ``execute_run`` and the workflow runner: attempt bookkeeping,
    typed errors, handshake-derived :class:`AgentInfo`, and the recorder
    aggregations (tool calls, file changes, permission decisions). The
    assembled transcript is re-redacted before becoming ``outputs['text']``:
    a secret split across stream chunks is invisible per-chunk but contiguous
    (and matchable) once concatenated. ``inputs_resolved``/``input_sources``
    stay with the caller — they are run-kind specific.
    """
    attempt.ended_at = utc_now_iso()
    attempt.duration_ms = duration_ms
    attempt.status = outcome.status
    attempt.stop_reason = outcome.stop_reason
    attempt.exit_code = outcome.exit_code
    if outcome.error is not None:
        attempt.errors = [outcome.error.to_model()]
        step.errors = [outcome.error.to_model()]

    if outcome.handshake is not None:
        step.agent_info = AgentInfo(
            name=agent_name,
            provider=provider,
            protocol_version=outcome.handshake.protocol_version,
            agent_name=outcome.handshake.agent_name,
            agent_title=outcome.handshake.agent_title,
            agent_version=outcome.handshake.agent_version,
            capabilities=outcome.handshake.capabilities,
            auth_methods=outcome.handshake.auth_methods,
        )

    assembled_text, _ = redactor.redact_text(recorder.text_output(step.step_id))
    step.outputs = {"text": assembled_text}
    step.tool_calls = recorder.tool_calls(step.step_id)
    step.file_changes = recorder.file_changes(step.step_id)
    step.permission_decisions = recorder.permission_decisions(step.step_id)
    step.attempts = [attempt]
    step.status = outcome.status


def degrade_capture(result: RunResult) -> None:
    """Degrade every capture entry to at least ``partial`` (interrupted turn)."""
    for entry in result.capture.values():
        if _CAPTURE_SEVERITY[entry.status] < _CAPTURE_SEVERITY[CaptureStatus.PARTIAL]:
            entry.status = CaptureStatus.PARTIAL


def persist_result(result: RunResult, writer: RunDirWriter, index_db: Path) -> None:
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
    decide_permission: PermissionDecider | None = None,
) -> RunResult:
    """Execute one direct agent run; always returns the full RunResult.

    The run id and in-memory RunResult scaffolding exist before anything
    fallible (REQ-004); store bootstrap failure degrades to in-memory
    recording rather than aborting the run. ``cancel_event`` is the caller's
    cancellation signal (Ctrl-C in the CLI); setting it triggers the full
    teardown ladder and a ``cancelled`` terminal state. ``decide_permission``
    is the Phase-4 server bridge's permission-decision override, threaded to
    the step's mediation hooks (``None`` keeps local policy decisions).
    """
    run_id = new_run_id()
    clock = MonotonicClock()
    started_at = utc_now_iso()
    step = StepResult(step_id=MAIN_STEP_ID, agent=spec.agent_name, status=StepStatus.FAILED)
    if spec.policy is not None:
        policy_provenance = spec.policy_provenance or build_policy_provenance(spec.policy)
    else:
        policy_provenance = PolicyProvenance(
            policy_name=DEFAULT_DENY_POLICY_NAME,
            ceiling_source="default",
            rules=[{"rule_id": DEFAULT_DENY_RULE_ID, "action": "deny", "surface": "all"}],
            enforcement="advisory",
            enforcement_scope_default=EnforcementScope.ACP_MEDIATED,
        )
    result = RunResult(
        run_id=run_id,
        kind=RunKind.AGENT,
        target=spec.agent_name,
        status=RunStatus.FAILED,
        started_at=started_at,
        workspace=spec.cwd,
        capture_profile=spec.capture_profile,
        persisted=False,
        config_fingerprint=spec.config_fingerprint,
        policy=policy_provenance,
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

    # Exact-value seeding (Phase 2) wins; the env-var *name* heuristic remains
    # the Phase-1 fallback when no resolved secret list was provided.
    secret_values = (
        spec.secret_values if spec.secret_values is not None else _secret_env_values(spec.env)
    )
    redactor = Redactor(secret_values=secret_values, custom_patterns=spec.redaction_patterns or ())
    recorder = RunRecorder(
        run_id=run_id,
        store_writer=writer,
        redactor=redactor,
        capture_profile=spec.capture_profile,
        limits=spec.limits,
        render_cb=render_cb,
        clock=clock,
    )

    recorder.emit(
        event_type="run_started",
        payload={
            "kind": RunKind.AGENT.value,
            "target": spec.agent_name,
            "workspace": spec.cwd,
            "capture_profile": spec.capture_profile.value,
        },
    )
    _mlog(
        spec.logger,
        "run_started",
        run_id=run_id,
        agent=spec.agent_name,
        kind=RunKind.AGENT.value,
        target=spec.agent_name,
    )
    if spec.config_fingerprint is not None:
        recorder.emit(
            event_type="config_resolved",
            payload={"fingerprint": spec.config_fingerprint},
        )
    if spec.policy is not None:
        recorder.emit(
            event_type="policy_resolved",
            payload={
                "policy_name": policy_provenance.policy_name,
                "ceiling_source": policy_provenance.ceiling_source,
                "rule_count": len(policy_provenance.rules),
                "enforcement": policy_provenance.enforcement,
            },
        )
    else:
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

    outcome = await execute_step(
        StepExecutionContext(
            step_id=MAIN_STEP_ID,
            command=spec.command,
            args=list(spec.args),
            env=spec.env,
            cwd=spec.cwd,
            prompt=spec.prompt,
            timeout_seconds=spec.step_timeout_seconds,
            grace_seconds=spec.cancel_grace_seconds,
            recorder=recorder,
            session_label=spec.agent_name,
            policy=spec.policy,
            logger=spec.logger,
            cancel_event=cancel_event,
            attempt_no=1,
            decide_permission=decide_permission,
        )
    )

    settle_step_result(
        step=step,
        attempt=attempt,
        outcome=outcome,
        duration_ms=clock.elapsed_ms() - attempt_start_ms,
        recorder=recorder,
        redactor=redactor,
        agent_name=spec.agent_name,
        provider=spec.provider,
    )
    redacted_prompt, _ = redactor.redact_text(spec.prompt)
    step.inputs_resolved = {"prompt": redacted_prompt}
    step.input_sources = {"prompt": "direct:prompt"}
    result.status = _RUN_STATUS_OF_STEP[outcome.status]

    recorder.emit(
        event_type="step_finished",
        step_id=MAIN_STEP_ID,
        attempt_no=1,
        session_id=outcome.session_id,
        payload={"status": outcome.status.value, "duration_ms": attempt.duration_ms},
    )
    _mlog(
        spec.logger,
        "step_finished",
        run_id=run_id,
        step_id=MAIN_STEP_ID,
        agent=spec.agent_name,
        status=outcome.status.value,
        duration_ms=attempt.duration_ms,
    )
    recorder.emit(event_type="run_finished", payload={"status": result.status.value})
    await recorder.finalize()

    result.capture = recorder.capture_summary()
    if outcome.capture_degraded:
        degrade_capture(result)
    result.redaction = recorder.redaction_summary()
    usage = recorder.usage_summary()
    if usage is not None and usage.provider is None:
        usage.provider = spec.provider
    result.usage = usage
    if spec.provider:
        result.egress = [
            EgressRecord(
                step_id=MAIN_STEP_ID,
                provider=spec.provider,
                acknowledged_by=spec.egress_acknowledged_by,
            )
        ]
    result.ended_at = utc_now_iso()
    result.duration_ms = clock.elapsed_ms()
    _mlog(
        spec.logger,
        "run_finished",
        run_id=run_id,
        agent=spec.agent_name,
        status=result.status.value,
        duration_ms=result.duration_ms,
    )

    for perr in recorder.persistence_errors:
        result.errors.append(perr.to_model())

    if writer is not None and index_db is not None:
        try:
            persist_result(result, writer, index_db)
        finally:
            writer.finalize()
        if result.persisted:
            _mlog(
                spec.logger,
                "run_persisted",
                run_id=run_id,
                agent=spec.agent_name,
                path_ref=result.result_path,
            )
        else:
            _mlog(
                spec.logger,
                "run_persist_failed",
                run_id=run_id,
                agent=spec.agent_name,
                level="warning",
                reason_code="PersistenceError",
            )
    return result
