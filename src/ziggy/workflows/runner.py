"""Serial workflow execution (REQ-010/011, spec §5.6, phase-3 contracts).

``execute_workflow`` drives one validated, fully-prepared workflow through the
same event/result pipeline as direct runs: ONE run id, ONE
:class:`~ziggy.events.RunRecorder`, one ``events.jsonl`` for the whole run.
Like ``execute_run`` it always returns the full :class:`RunResult` — run-level
failures (lease busy, deadline, persistence) live inside the result as typed
errors, never as raised exceptions — and persistence is identical to direct
runs (atomic manifest, then index row).

Per step, strictly one at a time in the precomputed topological order:

1. workflow deadline check (monotonic, before each step),
2. prompt composition via restricted interpolation — typed variables verbatim,
   upstream step outputs wrapped in untrusted delimiters, values never
   re-scanned for tokens,
3. composed-prompt byte ceiling (``engine.max_prompt_bytes``,
   ``ResourceLimitError`` — knowable only now that upstream output exists),
4. :func:`ziggy.engine.runner.execute_step` with a fresh subprocess/session
   and the step-scoped mediation policy (step ``working_dir`` re-resolved
   fail-closed via ``resolve_contained``),
5. StepResult assembly from the recorder's step-scoped aggregations.

The first failed step stops new scheduling: its transitive dependents become
``blocked``, every other not-yet-run step ``skipped``. Cancellation marks the
active step ``cancelled``, the remainder ``skipped``, and the run
``cancelled``. A workflow-deadline crossing surfaces as the active step's
timeout path plus a run-level ``StepTimeoutError``, with the remainder
``skipped``.

The cross-process workspace lease is acquired BEFORE the first agent launch
and released in a ``finally`` — no launch ever happens without it, and a busy
or unprovable lease fails the run with every step left in a terminal
never-ran state.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ziggy.config import ResolvedConfig
from ziggy.engine.hooks import MetadataLoggerLike
from ziggy.engine.lease import AcquiredLease, LeaseManager
from ziggy.engine.runner import (
    RenderCallback,
    StepExecutionContext,
    degrade_capture,
    execute_step,
    persist_result,
    settle_step_result,
)
from ziggy.errors import (
    PersistenceError,
    ResourceLimitError,
    StepTimeoutError,
    ValidationError,
    WorkspaceBusyError,
    ZiggyError,
)
from ziggy.events import EventLimits, RunRecorder
from ziggy.ids import MonotonicClock, new_run_id, utc_now_iso
from ziggy.models.agent import AgentConfig
from ziggy.models.common import CaptureProfile, RunKind, RunStatus, StepStatus
from ziggy.models.result import (
    Attempt,
    EgressRecord,
    PolicyProvenance,
    RunResult,
    StepResult,
)
from ziggy.models.workflow import StepDef, WorkflowDef
from ziggy.policy import MediationPolicy, PathAmbiguity, resolve_contained
from ziggy.redact import CustomPattern, Redactor
from ziggy.store import RunDirWriter, RunStore
from ziggy.workflows.interpolate import render_prompt, value_to_text
from ziggy.workflows.scheduler import (
    WorkflowDeadline,
    input_source_step,
    propagate_failure,
    step_edges,
)

__all__ = [
    "EgressPlan",
    "PreparedStep",
    "PreparedWorkflow",
    "aggregate_status",
    "execute_workflow",
]


@dataclass(slots=True)
class PreparedStep:
    """Launch-ready material for one workflow step (built by prepare).

    ``env`` is the complete explicit child environment for this step's agent;
    ``policy`` is the step-scoped guarded mediation policy (step working_dir
    as the write scope); ``timeout_seconds`` is already clamped to the user
    ceiling; ``provider`` is the step's egress identity
    (``AgentConfig.provider`` or the ``custom:<agent>`` fallback).
    """

    step_id: str
    agent_config: AgentConfig
    provider: str
    env: dict[str, str]
    policy: MediationPolicy
    timeout_seconds: float


@dataclass(slots=True)
class EgressPlan:
    """Precomputed provider-flow facts for the run (REQ-011).

    ``provider_set`` is the exact crossing set (empty = no crossing, nothing
    to acknowledge); ``acknowledged_by`` records HOW it was acknowledged
    (``config`` | ``flag:--acknowledge-egress`` | ``None``); ``records`` are
    the per-step lineage records stamped into the RunResult. The
    fail-before-launch gate (``gate_egress``) runs in prepare, not here.
    """

    provider_set: frozenset[str]
    acknowledged_by: str | None
    records: list[EgressRecord]


@dataclass(slots=True)
class PreparedWorkflow:
    """Everything ``execute_workflow`` needs; built by
    ``ziggy.engine.prepare.prepare_workflow`` (discovery → schema → vars →
    interpolation validation → limits preflight → egress preflight → per-step
    policy construction → lease manager).

    ``order`` is the precomputed stable topological order
    (:func:`ziggy.workflows.scheduler.topo_order`); ``steps`` must cover every
    step in ``workflow.steps``. ``variables`` holds the TYPED resolved values
    (``validate_variables`` output). ``secret_values`` are the exact-match
    redaction seeds (api_key_env values, ``redaction.extra_value_env_vars``,
    secret workflow variables) — the workflow runner has NO name-heuristic
    fallback; prepare must supply them. ``workflow_timeout_seconds=None``
    uses ``engine.default_workflow_timeout_seconds`` from ``resolved``.
    """

    resolved: ResolvedConfig
    workflow: WorkflowDef
    variables: dict[str, Any]
    workspace: Path
    order: list[str]
    steps: dict[str, PreparedStep]
    egress: EgressPlan
    limits: EventLimits
    capture_profile: CaptureProfile
    no_save: bool
    logger: MetadataLoggerLike | None
    store_root: Path | None
    config_fingerprint: str | None
    policy_provenance: PolicyProvenance | None
    secret_values: list[tuple[str, str]] = field(default_factory=list)
    redaction_patterns: list[CustomPattern] = field(default_factory=list)
    workflow_timeout_seconds: float | None = None
    lease_manager: LeaseManager = field(default_factory=LeaseManager)


def aggregate_status(statuses: Iterable[StepStatus], *, cancelled: bool = False) -> RunStatus:
    """REQ-010 aggregation: the workflow-level status from step statuses.

    Cancellation overrides (run ``cancelled`` when the run was cancelled or
    any step ended ``cancelled``, regardless of earlier successes); otherwise
    all success → ``success``; any success beside any failure/blocked/skipped
    → ``partial``; no success → ``failed``.
    """
    settled = list(statuses)
    if cancelled or any(status is StepStatus.CANCELLED for status in settled):
        return RunStatus.CANCELLED
    if settled and all(status is StepStatus.SUCCESS for status in settled):
        return RunStatus.SUCCESS
    if any(status is StepStatus.SUCCESS for status in settled):
        return RunStatus.PARTIAL
    return RunStatus.FAILED


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
    """Best-effort metadata-log line; write failures never disturb the run."""
    if logger is None:
        return
    with suppress(PersistenceError):
        logger.log(event, run_id=run_id, step_id=step_id, agent=agent, level=level, **detail)


def _emit_error(
    recorder: RunRecorder,
    error: ZiggyError,
    *,
    step_id: str | None = None,
    attempt_no: int | None = None,
) -> None:
    recorder.emit(
        event_type="error",
        step_id=step_id,
        attempt_no=attempt_no,
        payload={"code": error.code, "message": error.message, "details": error.details},
    )


def _resolve_step_dir(workspace: Path, step_id: str, working_dir: str | None) -> Path:
    """Fail-closed re-resolution of a step's working directory at run time."""
    if working_dir is None:
        return Path(os.path.realpath(workspace))
    try:
        return resolve_contained(workspace, working_dir)
    except PathAmbiguity as exc:
        raise ValidationError(
            f"steps.{step_id}.working_dir: {working_dir!r} does not resolve "
            f"canonically inside the workspace: {exc}",
            details={"step_id": step_id, "reason": getattr(exc, "reason", None)},
        ) from exc


def _upstream_inputs(step_def: StepDef, steps: Mapping[str, StepResult]) -> dict[str, str]:
    """Raw upstream output text per local input name (``steps.*`` sources only).

    By the time a step runs, every upstream producer has succeeded (otherwise
    this step would have been blocked), so its ``outputs['text']`` exists —
    already redacted by the event pipeline, which is the conservative value to
    thread downstream.
    """
    upstream: dict[str, str] = {}
    for local_name, source in step_def.inputs.items():
        src_id = input_source_step(source)
        if src_id is None:
            continue
        value = steps[src_id].outputs.get("text", "") if src_id in steps else ""
        upstream[local_name] = value if isinstance(value, str) else str(value)
    return upstream


def _resolved_inputs(
    step_def: StepDef,
    variables: Mapping[str, Any],
    upstream: Mapping[str, str],
    prompt: str,
    redactor: Redactor,
) -> dict[str, Any]:
    """Redacted concrete post-interpolation values for ``inputs_resolved``."""
    resolved: dict[str, Any] = {}
    for local_name, source in step_def.inputs.items():
        if local_name in upstream:
            value = upstream[local_name]
        else:
            var_name = source.split(".", 1)[1]
            if var_name not in variables:
                continue
            value = value_to_text(variables[var_name])
        redacted, _ = redactor.redact_text(value)
        resolved[local_name] = redacted
    redacted_prompt, _ = redactor.redact_text(prompt)
    resolved["prompt"] = redacted_prompt
    return resolved


def _fail_before_launch(
    step_result: StepResult,
    error: ZiggyError,
    *,
    recorder: RunRecorder,
    logger: MetadataLoggerLike | None,
    agent: str,
) -> None:
    """Mark a step failed for a pre-launch violation (no attempt, no process)."""
    step_result.errors = [error.to_model()]
    step_result.status = StepStatus.FAILED
    _emit_error(recorder, error, step_id=step_result.step_id, attempt_no=1)
    recorder.emit(
        event_type="step_finished",
        step_id=step_result.step_id,
        attempt_no=1,
        payload={"status": StepStatus.FAILED.value, "duration_ms": 0},
    )
    _mlog(
        logger,
        "step_finished",
        run_id=recorder.run_id,
        step_id=step_result.step_id,
        agent=agent,
        level="warning",
        status=StepStatus.FAILED.value,
        duration_ms=0,
    )


def _deadline_stop(
    result: RunResult,
    recorder: RunRecorder,
    logger: MetadataLoggerLike | None,
    deadline_seconds: float,
    *,
    active_step: str | None,
) -> None:
    """Record the run-level StepTimeoutError for a crossed workflow deadline."""
    details: dict[str, Any] = {"deadline_seconds": deadline_seconds}
    if active_step is not None:
        details["step_id"] = active_step
    error = StepTimeoutError(f"workflow deadline of {deadline_seconds}s exceeded", details=details)
    _emit_error(recorder, error)
    result.errors.append(error.to_model())
    _mlog(
        logger,
        "workflow_deadline_exceeded",
        run_id=result.run_id,
        level="warning",
        reason_code=error.code,
    )


async def _run_steps(
    prepared: PreparedWorkflow,
    result: RunResult,
    *,
    recorder: RunRecorder,
    redactor: Redactor,
    clock: MonotonicClock,
    cancel_event: asyncio.Event | None,
    deadline: WorkflowDeadline,
    grace_seconds: float,
    max_prompt_bytes: int,
) -> tuple[bool, bool]:
    """Serial step loop; returns ``(cancelled, capture_degraded)``.

    Mutates ``result.steps`` in place. Every step not reached keeps its
    ``skipped`` scaffold status; failure propagation upgrades transitive
    dependents to ``blocked``.
    """
    wf = prepared.workflow
    steps = result.steps
    edges = step_edges(wf)
    cancelled = False
    capture_degraded = False
    pending = list(prepared.order)
    while pending:
        step_id = pending[0]
        if cancel_event is not None and cancel_event.is_set():
            # Cancel arrived between steps: nothing active, remainder skipped.
            cancelled = True
            recorder.emit(
                event_type="cancel_requested",
                payload={"reason": "cancel", "scope": "workflow"},
            )
            break
        if deadline.exceeded():
            _deadline_stop(
                result, recorder, prepared.logger, deadline.timeout_seconds, active_step=None
            )
            break
        pending.pop(0)
        step_def = wf.steps[step_id]
        prep = prepared.steps[step_id]
        step_result = steps[step_id]
        step_result.status = StepStatus.FAILED  # provisional while active
        recorder.emit(
            event_type="step_started",
            step_id=step_id,
            attempt_no=1,
            payload={"agent": step_def.agent, "provider": prep.provider},
        )

        try:
            cwd = _resolve_step_dir(prepared.workspace, step_id, step_def.working_dir)
            upstream = _upstream_inputs(step_def, steps)
            prompt = render_prompt(step_def, prepared.variables, upstream)
            prompt_bytes = len(prompt.encode("utf-8"))
            if prompt_bytes > max_prompt_bytes:
                raise ResourceLimitError(
                    f"steps.{step_id}: composed prompt is {prompt_bytes} bytes; "
                    f"engine.max_prompt_bytes is {max_prompt_bytes}",
                    details={
                        "step_id": step_id,
                        "prompt_bytes": prompt_bytes,
                        "max_prompt_bytes": max_prompt_bytes,
                    },
                )
        except (ValidationError, ResourceLimitError) as exc:
            _fail_before_launch(
                step_result,
                exc,
                recorder=recorder,
                logger=prepared.logger,
                agent=step_def.agent,
            )
            for sid, status in propagate_failure(prepared.order, edges, step_id).items():
                steps[sid].status = status
            break

        attempt = Attempt(attempt_no=1, status=StepStatus.FAILED, started_at=utc_now_iso())
        attempt_start_ms = clock.elapsed_ms()
        outcome = await execute_step(
            StepExecutionContext(
                step_id=step_id,
                command=prep.agent_config.command,
                args=list(prep.agent_config.args),
                env=dict(prep.env),
                cwd=str(cwd),
                prompt=prompt,
                timeout_seconds=deadline.clamp(prep.timeout_seconds),
                grace_seconds=grace_seconds,
                recorder=recorder,
                session_label=step_def.agent,
                policy=prep.policy,
                logger=prepared.logger,
                cancel_event=cancel_event,
                attempt_no=1,
            )
        )
        settle_step_result(
            step=step_result,
            attempt=attempt,
            outcome=outcome,
            duration_ms=clock.elapsed_ms() - attempt_start_ms,
            recorder=recorder,
            redactor=redactor,
            agent_name=step_def.agent,
            provider=prep.provider,
        )
        step_result.inputs_resolved = _resolved_inputs(
            step_def, prepared.variables, upstream, prompt, redactor
        )
        capture_degraded = capture_degraded or outcome.capture_degraded
        recorder.emit(
            event_type="step_finished",
            step_id=step_id,
            attempt_no=1,
            session_id=outcome.session_id,
            payload={"status": outcome.status.value, "duration_ms": attempt.duration_ms},
        )
        _mlog(
            prepared.logger,
            "step_finished",
            run_id=recorder.run_id,
            step_id=step_id,
            agent=step_def.agent,
            status=outcome.status.value,
            duration_ms=attempt.duration_ms,
        )

        if outcome.status is StepStatus.SUCCESS:
            continue
        if outcome.status is StepStatus.CANCELLED:
            cancelled = True  # active step cancelled; remainder stays skipped
            break
        # Failed. A timeout that crossed the workflow deadline is the deadline
        # path (remainder skipped + run-level error); every other failure uses
        # blocked/skipped propagation. First failure stops new scheduling.
        if isinstance(outcome.error, StepTimeoutError) and deadline.exceeded():
            _deadline_stop(
                result,
                recorder,
                prepared.logger,
                deadline.timeout_seconds,
                active_step=step_id,
            )
        else:
            for sid, status in propagate_failure(prepared.order, edges, step_id).items():
                steps[sid].status = status
        break
    return cancelled, capture_degraded


async def execute_workflow(
    prepared: PreparedWorkflow,
    *,
    render_cb: RenderCallback | None = None,
    cancel_event: asyncio.Event | None = None,
) -> RunResult:
    """Execute one prepared workflow; always returns the full RunResult.

    One run id / recorder / ``events.jsonl`` for the whole run; every declared
    step is present in the result in a terminal state. The workspace lease is
    acquired before any agent launch (project config cannot disable it) and
    released in a ``finally``; a busy or ambiguous lease fails the run with a
    ``WorkspaceBusyError`` recorded and no step ever started. Persistence and
    degradation behavior are identical to :func:`ziggy.engine.runner.execute_run`.
    """
    run_id = new_run_id()
    clock = MonotonicClock()
    started_at = utc_now_iso()
    wf = prepared.workflow
    engine_cfg = prepared.resolved.config.engine
    grace_seconds = float(engine_cfg.cancel_grace_seconds)
    deadline_seconds = (
        float(prepared.workflow_timeout_seconds)
        if prepared.workflow_timeout_seconds is not None
        else float(engine_cfg.default_workflow_timeout_seconds)
    )
    max_prompt_bytes = int(engine_cfg.max_prompt_bytes)

    steps: dict[str, StepResult] = {
        step_id: StepResult(
            step_id=step_id,
            agent=step_def.agent,
            status=StepStatus.SKIPPED,
            input_sources=dict(step_def.inputs),
        )
        for step_id, step_def in wf.steps.items()
    }
    result = RunResult(
        run_id=run_id,
        kind=RunKind.WORKFLOW,
        target=wf.name,
        status=RunStatus.FAILED,
        started_at=started_at,
        workspace=str(prepared.workspace),
        capture_profile=prepared.capture_profile,
        persisted=False,
        config_fingerprint=prepared.config_fingerprint,
        policy=prepared.policy_provenance,
        steps=steps,
        egress=[record.model_copy(deep=True) for record in prepared.egress.records],
    )

    store: RunStore | None = None
    writer: RunDirWriter | None = None
    index_db: Path | None = None
    if not prepared.no_save:
        try:
            store = RunStore(prepared.store_root)
            writer = store.open_run(run_id)
            index_db = store.index_db_path
        except PersistenceError as exc:
            store = None
            result.errors.append(exc.to_model())

    redactor = Redactor(
        secret_values=prepared.secret_values,
        custom_patterns=prepared.redaction_patterns,
    )
    recorder = RunRecorder(
        run_id=run_id,
        store_writer=writer,
        redactor=redactor,
        capture_profile=prepared.capture_profile,
        limits=prepared.limits,
        render_cb=render_cb,
        clock=clock,
    )

    recorder.emit(
        event_type="run_started",
        payload={
            "kind": RunKind.WORKFLOW.value,
            "target": wf.name,
            "workspace": str(prepared.workspace),
            "capture_profile": prepared.capture_profile.value,
            "step_order": list(prepared.order),
        },
    )
    _mlog(
        prepared.logger,
        "run_started",
        run_id=run_id,
        kind=RunKind.WORKFLOW.value,
        target=wf.name,
        count=len(prepared.order),
    )
    if prepared.config_fingerprint is not None:
        recorder.emit(
            event_type="config_resolved",
            payload={"fingerprint": prepared.config_fingerprint},
        )
    if prepared.policy_provenance is not None:
        recorder.emit(
            event_type="policy_resolved",
            payload={
                "policy_name": prepared.policy_provenance.policy_name,
                "ceiling_source": prepared.policy_provenance.ceiling_source,
                "rule_count": len(prepared.policy_provenance.rules),
                "enforcement": prepared.policy_provenance.enforcement,
            },
        )
    if prepared.egress.provider_set:
        recorder.emit(
            event_type="egress_notice",
            payload={
                "provider_set": sorted(prepared.egress.provider_set),
                "acknowledged_by": prepared.egress.acknowledged_by,
                "records": [record.model_dump() for record in prepared.egress.records],
            },
        )
        _mlog(
            prepared.logger,
            "egress_notice",
            run_id=run_id,
            provider_set=sorted(prepared.egress.provider_set),
        )

    cancelled = False
    capture_degraded = False
    lease: AcquiredLease | None = None
    try:
        try:
            lease_store = store if store is not None else RunStore(prepared.store_root)
            lease = prepared.lease_manager.acquire(lease_store, prepared.workspace, run_id)
        except (WorkspaceBusyError, PersistenceError) as exc:
            _emit_error(recorder, exc)
            result.errors.append(exc.to_model())
            _mlog(
                prepared.logger,
                "lease_unavailable",
                run_id=run_id,
                level="warning",
                reason_code=exc.code,
            )
        except OSError as exc:  # store root not even creatable: fail closed
            error = PersistenceError(
                f"cannot prepare lease directory: {exc}",
                details={"error": exc.__class__.__name__},
            )
            _emit_error(recorder, error)
            result.errors.append(error.to_model())
        else:
            recorder.emit(
                event_type="lease_acquired",
                payload={"workspace": lease.lease.workspace, "lease_path": str(lease.path)},
            )
            _mlog(prepared.logger, "lease_acquired", run_id=run_id)
            cancelled, capture_degraded = await _run_steps(
                prepared,
                result,
                recorder=recorder,
                redactor=redactor,
                clock=clock,
                cancel_event=cancel_event,
                deadline=WorkflowDeadline(timeout_seconds=deadline_seconds),
                grace_seconds=grace_seconds,
                max_prompt_bytes=max_prompt_bytes,
            )
    finally:
        if lease is not None:
            lease.release()
            recorder.emit(
                event_type="lease_released",
                payload={"workspace": lease.lease.workspace},
            )
            _mlog(prepared.logger, "lease_released", run_id=run_id)

    result.status = aggregate_status((step.status for step in steps.values()), cancelled=cancelled)
    recorder.emit(event_type="run_finished", payload={"status": result.status.value})
    await recorder.finalize()

    result.capture = recorder.capture_summary()
    if capture_degraded:
        degrade_capture(result)
    result.redaction = recorder.redaction_summary()
    result.usage = recorder.usage_summary()
    result.ended_at = utc_now_iso()
    result.duration_ms = clock.elapsed_ms()
    _mlog(
        prepared.logger,
        "run_finished",
        run_id=run_id,
        kind=RunKind.WORKFLOW.value,
        target=wf.name,
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
                prepared.logger,
                "run_persisted",
                run_id=run_id,
                path_ref=result.result_path,
            )
        else:
            _mlog(
                prepared.logger,
                "run_persist_failed",
                run_id=run_id,
                level="warning",
                reason_code="PersistenceError",
            )
    return result
