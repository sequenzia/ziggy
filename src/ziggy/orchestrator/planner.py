"""Planning run + repair + orchestrated execution (REQ-013, spec §5.8).

:func:`run_orchestration` drives one full ``ziggy orchestrate`` run — planning
turn, at most ONE repair turn, security validation, egress gate, and (unless
``--plan-only`` / ``auto_execute = false``) execution of the validated plan —
under ONE run id, ONE :class:`~ziggy.events.RunRecorder`, one ``events.jsonl``.
Like ``execute_run``/``execute_workflow`` it ALWAYS returns the full
:class:`RunResult`: run-level failures (lease busy, invalid plan,
unacknowledged egress, execution-mapping refusal) live inside the result as
typed errors, never as raised exceptions. The caller maps error codes to exit
codes (``TrustPolicyError``/``OrchestratorPlanInvalid`` shapes → exit 2 class).

Planning isolation (reduced exposure — never an OS sandbox):

- The planner runs in an EMPTY ``tempfile.mkdtemp(prefix='ziggy-plan-')``
  working directory that Ziggy creates, supplies no workspace contents to,
  and removes in a ``finally``.
- Its environment is the documented minimal baseline plus the planner's
  explicit trusted-config values only (composed by ``prepare_orchestration``;
  no ``inherit_env`` passthrough).
- Its mediation policy is :class:`PlanningMediationPolicy`: mediated reads
  are allowed ONLY inside the temp dir (guarded containment with the temp dir
  as the workspace), EVERY mediated write is denied unconditionally by an
  overriding rule (``planning-write-deny`` — not a directory trick, a
  categorical deny), and the terminal surface is denied (no allowlist).
  Permission requests during planning are decided locally by this policy and
  never forwarded upstream.
- ACP mediation is observable governance: an uncontained planner's DIRECT
  local tools are outside this policy, which is exactly why the
  ``allow_uncontained_planner`` acknowledgement (recorded with ``advisory``
  enforcement) and the pre-launch workspace lease exist. v0.1 reality: both
  builtins are ``direct_tools_assumed``, so the acknowledgement is always
  required in practice; the contained branch (lease acquired only before the
  execution stage) exists for future capability-matrix updates.

Repair: ONE repair prompt in the SAME planner session (a second prompt turn on
the live subprocess) whenever the first response fails parsing OR security
validation — one unified repair, with the bounded error list embedded in a
fixed template. A second invalid response records ``OrchestratorPlanInvalid``
with ``PlanValidation{attempt_count: 2, repair_requested: true, valid: false}``
and NO execution agent ever launches. The planner subprocess is shut down
after planning, before any execution agent starts.

Planning IS egress: the user goal and the bounded catalog go to the planner
provider, recorded as ``EgressRecord{step_id: 'plan', input_sources:
['goal', 'catalog']}`` on every run. The plan-derived provider set (planner
included) must be acknowledged before execution; an unacknowledged crossing
stops the run with the plan preserved in the result and a rerun hint.

Plan-step transcript note: parsing consumes the RECORDED (redacted) planner
text. A plan that only parses un-redacted would mean the planner emitted a
seeded secret — failing closed there is deliberate.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ziggy.acp import AgentProcessClient
from ziggy.engine.hooks import PolicyHooks
from ziggy.engine.runner import (
    RenderCallback,
    StepOutcome,
    _cancel_ladder,
    degrade_capture,
    persist_result,
    settle_step_result,
)
from ziggy.errors import (
    AgentLaunchError,
    CancelledError,
    EgressNotAcknowledgedError,
    OrchestratorPlanInvalid,
    PersistenceError,
    ProtocolError,
    StepTimeoutError,
    WorkspaceBusyError,
    ZiggyError,
)
from ziggy.events import RunRecorder
from ziggy.ids import MonotonicClock, new_run_id, utc_now_iso
from ziggy.models.common import RunKind, RunStatus, StepStatus
from ziggy.models.plan import PlanValidation
from ziggy.models.result import Attempt, EgressRecord, RunResult, StepResult
from ziggy.orchestrator.execute import (
    build_execution,
    run_execution,
    scaffold_step_results,
)
from ziggy.orchestrator.validate import bound_errors, parse_plan, validate_plan
from ziggy.policy import Decision, MediationPolicy
from ziggy.redact import Redactor
from ziggy.store import RunDirWriter, RunStore
from ziggy.workflows.egress import is_acknowledged
from ziggy.workflows.runner import aggregate_status

if TYPE_CHECKING:
    from ziggy.acp import HandshakeInfo, StopInfo
    from ziggy.engine.hooks import MetadataLoggerLike, PermissionDecider
    from ziggy.engine.lease import AcquiredLease
    from ziggy.engine.prepare import PreparedOrchestration
    from ziggy.models.plan import OrchestratorPlan
    from ziggy.orchestrator.validate import ValidationReport

__all__ = [
    "PLANNING_POLICY_NAME",
    "PLAN_STEP_ID",
    "RULE_PLANNING_WRITE_DENY",
    "PlanningMediationPolicy",
    "run_orchestration",
]

#: The implicit planning step's id (REQ-013; executed steps are 'execute/*').
PLAN_STEP_ID = "plan"

#: Policy name of the planning isolation profile (stable string).
PLANNING_POLICY_NAME = "planning-isolation"

#: Rule id of the categorical planning write denial (stable string).
RULE_PLANNING_WRITE_DENY = "planning-write-deny"

#: Fixed repair-prompt frame (deterministic; only the bounded error list and
#: this template ever reach the repair turn — never the raw invalid response).
_REPAIR_HEADER = "Your previous response was not a valid plan. Problems found:"
_REPAIR_FOOTER = (
    "Return the corrected plan now as RAW JSON only: exactly one JSON object "
    "matching one of the three plan shapes from the original instructions, "
    "with no prose, no markdown, and no code fences."
)


class PlanningMediationPolicy(MediationPolicy):
    """Planning isolation profile: deny ALL writes, deny terminal, reads only
    inside the planning temp dir.

    Built via ``guarded(workspace=<temp dir>, step_dir=<temp dir>)`` with no
    profile: mediated reads use guarded containment against the EMPTY temp
    dir (nothing else is readable through Ziggy), the terminal allowlist is
    empty (every terminal op denies), and this override denies EVERY mediated
    write unconditionally — including writes inside the temp dir — so the
    deny-all-writes requirement never depends on a directory layout.
    ``decide_permission`` routes write-kind permission requests through this
    same override.
    """

    def decide_fs_write(self, path: str) -> Decision:
        return Decision(
            allowed=False,
            rule_id=RULE_PLANNING_WRITE_DENY,
            policy_name=self.policy_name,
            policy_source="default",
            reason=(
                f"write of {path!r} denied: the planning isolation profile "
                "denies ALL filesystem writes"
            ),
        )


def _repair_prompt(errors: list[str]) -> str:
    lines = [_REPAIR_HEADER]
    lines.extend(f"- {entry}" for entry in bound_errors(errors))
    lines.extend(["", _REPAIR_FOOTER])
    return "\n".join(lines)


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


# ---------------------------------------------------------------------------
# planning session (launch → up to two prompt turns → teardown)


_TurnKind = Literal[
    "end_turn", "incomplete", "agent_cancelled", "protocol_error", "cancelled", "timeout"
]


@dataclass(slots=True)
class _TurnResult:
    """Outcome of one planning prompt turn.

    ``tore_down`` is True when the cancel/timeout ladder already terminated
    the subprocess (no further turns are possible).
    """

    kind: _TurnKind
    stop_reason: str | None = None
    error: ZiggyError | None = None
    exit_code: int | None = None
    tore_down: bool = False


@dataclass(slots=True)
class _PlanningResult:
    """Everything the planning session produced."""

    outcome: StepOutcome
    plan: OrchestratorPlan | None = None
    report: ValidationReport | None = None
    attempt_count: int = 1
    repair_requested: bool = False
    errors: list[str] = field(default_factory=list)


async def _prompt_turn(
    client: AgentProcessClient,
    session_id: str,
    text: str,
    *,
    timeout_seconds: float,
    grace_seconds: float,
    cancel_event: asyncio.Event | None,
    recorder: RunRecorder,
    logger: MetadataLoggerLike | None,
) -> _TurnResult:
    """One prompt turn with the engine's timeout/cancel semantics.

    On timeout or external cancellation the full teardown ladder runs here
    (ACP cancel → grace → group TERM/KILL) and ``tore_down`` is set.
    """
    prompt_task = asyncio.create_task(client.prompt(session_id, text))
    cancel_task: asyncio.Task[bool] | None = None
    waiters: set[asyncio.Task[Any]] = {prompt_task}
    if cancel_event is not None:
        cancel_task = asyncio.create_task(cancel_event.wait())
        waiters.add(cancel_task)
    try:
        done, _ = await asyncio.wait(
            waiters,
            timeout=max(timeout_seconds, 0.0),
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()
            with suppress(asyncio.CancelledError):
                await cancel_task

    if prompt_task in done:
        try:
            stop: StopInfo = prompt_task.result()
        except ProtocolError as exc:
            return _TurnResult(kind="protocol_error", error=exc)
        if stop.stop_reason == "end_turn":
            return _TurnResult(kind="end_turn", stop_reason=stop.stop_reason)
        if stop.stop_reason == "cancelled":
            return _TurnResult(kind="agent_cancelled", stop_reason=stop.stop_reason)
        return _TurnResult(kind="incomplete", stop_reason=stop.stop_reason)

    cancelled = cancel_task is not None and cancel_task in done
    reason = "cancel" if cancelled else "timeout"
    recorder.emit(
        event_type="cancel_requested",
        step_id=PLAN_STEP_ID,
        attempt_no=1,
        session_id=session_id,
        payload={"reason": reason, "grace_seconds": grace_seconds},
    )
    _mlog(
        logger,
        "cancel_requested",
        run_id=recorder.run_id,
        step_id=PLAN_STEP_ID,
        reason_code=reason,
    )
    exit_code, stop_info = await _cancel_ladder(client, session_id, prompt_task, grace_seconds)
    return _TurnResult(
        kind="cancelled" if cancelled else "timeout",
        stop_reason=stop_info.stop_reason if stop_info is not None else None,
        exit_code=exit_code,
        tore_down=True,
    )


def _turn_failure(
    turn: _TurnResult,
    *,
    prepared: PreparedOrchestration,
    recorder: RunRecorder,
    handshake: HandshakeInfo | None,
    session_id: str | None,
) -> StepOutcome:
    """Map a non-``end_turn`` turn result onto the engine's outcome shapes."""
    if turn.kind == "protocol_error":
        error: ZiggyError = turn.error or ProtocolError("planning turn failed")
        status = StepStatus.FAILED
    elif turn.kind == "incomplete":
        error = ProtocolError(
            f"turn ended without completing: {turn.stop_reason}",
            details={"stop_reason": turn.stop_reason},
        )
        status = StepStatus.FAILED
    elif turn.kind in ("cancelled", "agent_cancelled"):
        error = CancelledError("planning cancelled", details={"stop_reason": turn.stop_reason})
        status = StepStatus.CANCELLED
    else:  # timeout
        error = StepTimeoutError(
            f"step '{PLAN_STEP_ID}' exceeded {prepared.step_timeout_seconds}s",
            details={
                "timeout_seconds": prepared.step_timeout_seconds,
                "stop_reason": turn.stop_reason,
            },
        )
        status = StepStatus.FAILED
    _emit_error(recorder, error, step_id=PLAN_STEP_ID, attempt_no=1)
    return StepOutcome(
        status=status,
        error=error,
        stop_reason=turn.stop_reason,
        exit_code=turn.exit_code,
        handshake=handshake,
        session_id=session_id,
        capture_degraded=True,
    )


async def _run_planning(
    prepared: PreparedOrchestration,
    *,
    temp_dir: Path,
    recorder: RunRecorder,
    cancel_event: asyncio.Event | None,
) -> _PlanningResult:
    """Launch the planner in the isolation profile and obtain a valid plan.

    Owns the whole planning subprocess lifecycle: launch, handshake, session,
    the first prompt turn, the optional repair turn in the SAME session, and
    teardown. Never raises for agent-side failure — the returned
    ``_PlanningResult.outcome`` carries the typed error.
    """
    planner = prepared.planner_agent
    logger = prepared.logger
    policy = PlanningMediationPolicy.guarded(
        workspace=temp_dir,
        step_dir=temp_dir,
        profile=None,
        profile_name=PLANNING_POLICY_NAME,
    )
    hooks = PolicyHooks(
        recorder=recorder,
        policy=policy,
        logger=logger,
        step_id=PLAN_STEP_ID,
        attempt_no=1,
    )
    recorder.emit(
        event_type="agent_launching",
        step_id=PLAN_STEP_ID,
        attempt_no=1,
        payload={"command": planner.command, "args": list(planner.args)},
    )
    try:
        client = await AgentProcessClient.launch(
            command=planner.command,
            args=list(planner.args),
            env=dict(prepared.planning_env),
            cwd=str(temp_dir),
            hooks=hooks,
        )
    except AgentLaunchError as exc:
        _emit_error(recorder, exc, step_id=PLAN_STEP_ID, attempt_no=1)
        _mlog(
            logger,
            "agent_launch_failed",
            run_id=recorder.run_id,
            step_id=PLAN_STEP_ID,
            agent=planner.name,
            level="error",
            reason_code=exc.code,
        )
        return _PlanningResult(
            outcome=StepOutcome(status=StepStatus.FAILED, error=exc),
            errors=["plan: planning agent failed to launch"],
        )
    recorder.emit(
        event_type="agent_launched",
        step_id=PLAN_STEP_ID,
        attempt_no=1,
        payload={"pid": client.pid, "pgid": client.pgid},
    )
    _mlog(
        logger, "agent_launched", run_id=recorder.run_id, step_id=PLAN_STEP_ID, agent=planner.name
    )

    deadline = time.monotonic() + prepared.step_timeout_seconds
    tore_down = False
    try:
        try:
            handshake = await client.initialize()
        except ProtocolError as exc:
            _emit_error(recorder, exc, step_id=PLAN_STEP_ID, attempt_no=1)
            exit_code = await client.shutdown(0.0)
            tore_down = True
            return _PlanningResult(
                outcome=StepOutcome(
                    status=StepStatus.FAILED,
                    error=exc,
                    exit_code=exit_code,
                    capture_degraded=True,
                ),
                errors=["plan: planning agent handshake failed"],
            )
        recorder.emit(
            event_type="handshake",
            step_id=PLAN_STEP_ID,
            attempt_no=1,
            payload=asdict(handshake),
        )
        _mlog(logger, "handshake", run_id=recorder.run_id, step_id=PLAN_STEP_ID, agent=planner.name)

        try:
            session_id = await client.new_session(str(temp_dir))
        except ProtocolError as exc:
            _emit_error(recorder, exc, step_id=PLAN_STEP_ID, attempt_no=1)
            exit_code = await client.shutdown(0.0)
            tore_down = True
            return _PlanningResult(
                outcome=StepOutcome(
                    status=StepStatus.FAILED,
                    error=exc,
                    exit_code=exit_code,
                    handshake=handshake,
                    capture_degraded=True,
                ),
                errors=["plan: planning session could not be created"],
            )
        hooks.session_id = session_id
        recorder.emit(
            event_type="session_created",
            step_id=PLAN_STEP_ID,
            attempt_no=1,
            session_id=session_id,
            payload={"session_id": session_id, "cwd": str(temp_dir)},
        )
        _mlog(
            logger,
            "session_created",
            run_id=recorder.run_id,
            step_id=PLAN_STEP_ID,
            agent=planner.name,
        )

        plan: OrchestratorPlan | None = None
        report: ValidationReport | None = None
        errors: list[str] = []
        repair_requested = False
        attempt_count = 1
        prompt_text = prepared.meta_prompt
        last_stop: str | None = None

        for planning_attempt in (1, 2):
            attempt_count = planning_attempt
            recorder.emit(
                event_type="prompt_started",
                step_id=PLAN_STEP_ID,
                attempt_no=1,
                session_id=session_id,
                payload={"text": prompt_text, "planning_attempt": planning_attempt},
            )
            _mlog(
                logger,
                "prompt_started",
                run_id=recorder.run_id,
                step_id=PLAN_STEP_ID,
                agent=planner.name,
                count=planning_attempt,
            )
            text_before = len(recorder.text_output(PLAN_STEP_ID))
            turn = await _prompt_turn(
                client,
                session_id,
                prompt_text,
                timeout_seconds=deadline - time.monotonic(),
                grace_seconds=prepared.grace_seconds,
                cancel_event=cancel_event,
                recorder=recorder,
                logger=logger,
            )
            if turn.kind != "end_turn":
                if not turn.tore_down:
                    turn.exit_code = await client.shutdown(0.0)
                tore_down = True
                recorder.emit(
                    event_type="terminated",
                    step_id=PLAN_STEP_ID,
                    attempt_no=1,
                    session_id=session_id,
                    payload={"exit_code": turn.exit_code, "reason": turn.kind},
                )
                _mlog(
                    logger,
                    "terminated",
                    run_id=recorder.run_id,
                    step_id=PLAN_STEP_ID,
                    agent=planner.name,
                    exit_code=turn.exit_code,
                    reason_code=turn.kind,
                )
                return _PlanningResult(
                    outcome=_turn_failure(
                        turn,
                        prepared=prepared,
                        recorder=recorder,
                        handshake=handshake,
                        session_id=session_id,
                    ),
                    attempt_count=planning_attempt,
                    repair_requested=repair_requested,
                    errors=bound_errors([*errors, "plan: planning turn did not complete"]),
                )
            last_stop = turn.stop_reason

            # Parse + validate the RECORDED (redacted) turn text.
            turn_text = recorder.text_output(PLAN_STEP_ID)[text_before:]
            candidate, parse_errors = parse_plan(turn_text)
            if candidate is None:
                errors = parse_errors
            else:
                candidate_report = validate_plan(
                    candidate,
                    prepared.catalog,
                    prepared.resolved,
                    prepared.registry,
                    planner_provider=prepared.planner_provider,
                    goal=prepared.goal,
                )
                if candidate_report.valid:
                    plan = candidate
                    report = candidate_report
                    break
                errors = candidate_report.errors
            if planning_attempt == 1:
                repair_requested = True
                _mlog(
                    logger,
                    "plan_repair_requested",
                    run_id=recorder.run_id,
                    step_id=PLAN_STEP_ID,
                    agent=planner.name,
                    level="warning",
                    count=len(errors),
                )
                prompt_text = _repair_prompt(errors)

        exit_code = await client.shutdown(0.0)
        tore_down = True
        recorder.emit(
            event_type="terminated",
            step_id=PLAN_STEP_ID,
            attempt_no=1,
            session_id=session_id,
            payload={"exit_code": exit_code, "reason": "turn_complete"},
        )
        _mlog(
            logger,
            "terminated",
            run_id=recorder.run_id,
            step_id=PLAN_STEP_ID,
            agent=planner.name,
            exit_code=exit_code,
            reason_code="turn_complete",
            stop_reason=last_stop,
        )
        return _PlanningResult(
            outcome=StepOutcome(
                status=StepStatus.SUCCESS,
                stop_reason=last_stop,
                exit_code=exit_code,
                handshake=handshake,
                session_id=session_id,
            ),
            plan=plan,
            report=report,
            attempt_count=attempt_count,
            repair_requested=repair_requested,
            errors=[] if plan is not None else bound_errors(errors),
        )
    finally:
        if not tore_down:
            await client.shutdown(0.0)


# ---------------------------------------------------------------------------
# run driver


def _acquire_lease(
    prepared: PreparedOrchestration,
    store: RunStore | None,
    run_id: str,
    recorder: RunRecorder,
    result: RunResult,
) -> AcquiredLease | None:
    """Acquire the workspace lease; record failure on the run and return None."""
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
        return None
    except OSError as exc:  # store root not even creatable: fail closed
        error = PersistenceError(
            f"cannot prepare lease directory: {exc}",
            details={"error": exc.__class__.__name__},
        )
        _emit_error(recorder, error)
        result.errors.append(error.to_model())
        return None
    recorder.emit(
        event_type="lease_acquired",
        payload={"workspace": lease.lease.workspace, "lease_path": str(lease.path)},
    )
    _mlog(prepared.logger, "lease_acquired", run_id=run_id)
    return lease


def _plan_egress_message(provider_set: frozenset[str]) -> EgressNotAcknowledgedError:
    ordered = sorted(provider_set)
    joined = ",".join(ordered)
    return EgressNotAcknowledgedError(
        f"the validated plan sends data across providers {{{', '.join(ordered)}}} "
        "(planner included) and this exact provider set is not acknowledged; "
        f"re-run with --acknowledge-egress {joined} or add {ordered} to "
        "[egress] acknowledged_provider_sets in trusted user config",
        details={"provider_set": ordered, "hint": f"--acknowledge-egress {joined}"},
    )


async def run_orchestration(
    prepared: PreparedOrchestration,
    *,
    render_cb: RenderCallback | None = None,
    cancel_event: asyncio.Event | None = None,
    decide_permission: PermissionDecider | None = None,
) -> RunResult:
    """Execute one prepared orchestration; always returns the full RunResult.

    Structure of the result: kind ``orchestrator``, target = planner agent
    name, an implicit ``plan`` StepResult, ``execute/<id>`` StepResults when
    execution ran, ``RunResult.plan``/``plan_validation`` per REQ-013, and the
    planning egress record always present. Status: aggregate over all steps
    (workflow semantics; a valid plan with ``--plan-only``/``auto_execute =
    false`` is ``success``); any run-level gate failure (lease, invalid plan,
    unacknowledged egress, execution-mapping refusal) makes the run ``failed``.

    ``decide_permission`` is the server bridge's permission-decision override
    for EXECUTION steps; planning permission requests are always decided
    locally by the planning isolation policy.
    """
    run_id = new_run_id()
    clock = MonotonicClock()
    started_at = utc_now_iso()
    planner = prepared.planner_agent

    plan_validation = PlanValidation(
        attempt_count=1, repair_requested=False, valid=False, errors=[]
    )
    plan_step = StepResult(
        step_id=PLAN_STEP_ID,
        agent=planner.name,
        status=StepStatus.SKIPPED,
        input_sources={"goal": "goal", "catalog": "catalog"},
    )
    plan_egress = EgressRecord(
        step_id=PLAN_STEP_ID,
        provider=prepared.planner_provider,
        input_sources=["goal", "catalog"],
    )
    result = RunResult(
        run_id=run_id,
        kind=RunKind.ORCHESTRATOR,
        target=planner.name,
        status=RunStatus.FAILED,
        started_at=started_at,
        workspace=str(prepared.workspace),
        capture_profile=prepared.capture_profile,
        persisted=False,
        config_fingerprint=prepared.config_fingerprint,
        policy=prepared.policy_provenance,
        steps={PLAN_STEP_ID: plan_step},
        plan_validation=plan_validation,
        egress=[plan_egress],
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
            "kind": RunKind.ORCHESTRATOR.value,
            "target": planner.name,
            "workspace": str(prepared.workspace),
            "capture_profile": prepared.capture_profile.value,
            "plan_only": prepared.plan_only,
            "auto_execute": prepared.auto_execute,
        },
    )
    _mlog(
        prepared.logger,
        "run_started",
        run_id=run_id,
        agent=planner.name,
        kind=RunKind.ORCHESTRATOR.value,
        target=planner.name,
    )
    if prepared.config_fingerprint is not None:
        recorder.emit(
            event_type="config_resolved",
            payload={"fingerprint": prepared.config_fingerprint},
        )
    recorder.emit(
        event_type="policy_resolved",
        payload={
            "policy_name": prepared.policy_provenance.policy_name,
            "ceiling_source": prepared.policy_provenance.ceiling_source,
            "rule_count": len(prepared.policy_provenance.rules),
            "enforcement": prepared.policy_provenance.enforcement,
        },
    )
    # Planning IS egress: the goal and bounded catalog go to the planner
    # provider on every run (REQ-013).
    recorder.emit(
        event_type="egress_notice",
        payload={
            "step_id": PLAN_STEP_ID,
            "provider": prepared.planner_provider,
            "input_sources": ["goal", "catalog"],
            "planning": True,
        },
    )
    _mlog(
        prepared.logger,
        "egress_notice",
        run_id=run_id,
        provider_set=[prepared.planner_provider],
    )
    if prepared.uncontained:
        # Trusted-user acknowledgement of the advisory planner boundary.
        recorder.emit(
            event_type="egress_notice",
            payload={
                "uncontained_planner_ack": True,
                "enforcement": "advisory",
                "agent": planner.name,
                "acknowledged_by": "config:orchestrator.allow_uncontained_planner",
            },
        )

    cancelled = False
    capture_degraded = False
    run_fatal = False
    lease: AcquiredLease | None = None
    try:
        if prepared.lease_required_before_planning:
            # Uncontained planner: hold the workspace lease from BEFORE the
            # planner launch through execution / plan-only completion.
            lease = _acquire_lease(prepared, store, run_id, recorder, result)
            if lease is None:
                run_fatal = True
                plan_validation.errors = bound_errors(
                    ["plan: workspace lease unavailable; planning did not run"]
                )

        if not run_fatal:
            temp_dir = Path(tempfile.mkdtemp(prefix="ziggy-plan-"))
            plan_step.status = StepStatus.FAILED  # provisional while active
            recorder.emit(
                event_type="step_started",
                step_id=PLAN_STEP_ID,
                attempt_no=1,
                payload={"agent": planner.name, "provider": prepared.planner_provider},
            )
            attempt = Attempt(attempt_no=1, status=StepStatus.FAILED, started_at=utc_now_iso())
            attempt_start_ms = clock.elapsed_ms()
            try:
                planning = await _run_planning(
                    prepared,
                    temp_dir=temp_dir,
                    recorder=recorder,
                    cancel_event=cancel_event,
                )
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)

            settle_step_result(
                step=plan_step,
                attempt=attempt,
                outcome=planning.outcome,
                duration_ms=clock.elapsed_ms() - attempt_start_ms,
                recorder=recorder,
                redactor=redactor,
                agent_name=planner.name,
                provider=prepared.planner_provider,
            )
            goal_redacted, _ = redactor.redact_text(prepared.goal)
            prompt_redacted, _ = redactor.redact_text(prepared.meta_prompt)
            plan_step.inputs_resolved = {"goal": goal_redacted, "prompt": prompt_redacted}
            capture_degraded = capture_degraded or planning.outcome.capture_degraded
            if planning.outcome.status is StepStatus.CANCELLED:
                cancelled = True

            plan_validation.attempt_count = planning.attempt_count
            plan_validation.repair_requested = planning.repair_requested
            plan_validation.valid = planning.plan is not None
            plan_validation.errors = list(planning.errors)

            if planning.outcome.status is StepStatus.SUCCESS and planning.plan is None:
                # The planner completed its turn(s) but never produced a valid
                # plan: OrchestratorPlanInvalid, run failed, NO execution.
                invalid = OrchestratorPlanInvalid(
                    "planner did not produce a valid plan"
                    + (" after one repair attempt" if planning.repair_requested else ""),
                    details={
                        "attempt_count": planning.attempt_count,
                        "errors": plan_validation.errors,
                    },
                )
                plan_step.status = StepStatus.FAILED
                plan_step.errors.append(invalid.to_model())
                result.errors.append(invalid.to_model())
                _emit_error(recorder, invalid, step_id=PLAN_STEP_ID, attempt_no=1)
                run_fatal = True

            recorder.emit(
                event_type="step_finished",
                step_id=PLAN_STEP_ID,
                attempt_no=1,
                session_id=planning.outcome.session_id,
                payload={
                    "status": plan_step.status.value,
                    "duration_ms": attempt.duration_ms,
                    "plan_valid": plan_validation.valid,
                    "attempt_count": plan_validation.attempt_count,
                    "repair_requested": plan_validation.repair_requested,
                    "errors": plan_validation.errors,
                    # REQ-013: structural validation only — generated prompts
                    # remain untrusted model output.
                    "semantic_safety": "not_validated",
                    "generated_prompts": "untrusted-model-output",
                },
            )
            _mlog(
                prepared.logger,
                "plan_validation",
                run_id=run_id,
                step_id=PLAN_STEP_ID,
                agent=planner.name,
                status="valid" if plan_validation.valid else "invalid",
                count=plan_validation.attempt_count,
            )
            _mlog(
                prepared.logger,
                "step_finished",
                run_id=run_id,
                step_id=PLAN_STEP_ID,
                agent=planner.name,
                status=plan_step.status.value,
                duration_ms=attempt.duration_ms,
            )

            if planning.plan is not None and planning.report is not None and not cancelled:
                result.plan = planning.plan
                report = planning.report
                ack = is_acknowledged(
                    report.provider_set, prepared.resolved, prepared.acknowledge_egress
                )
                if report.provider_set:
                    plan_egress.acknowledged_by = ack

                if report.provider_set and ack is None:
                    # Unacknowledged provider crossing: execution stops BEFORE
                    # any planned agent launches (exit-2 class + rerun hint);
                    # the validated plan stays in the result.
                    gate_error = _plan_egress_message(report.provider_set)
                    _emit_error(recorder, gate_error)
                    result.errors.append(gate_error.to_model())
                    run_fatal = True
                elif prepared.plan_only or not prepared.auto_execute:
                    pass  # valid plan, no execution requested: finalize below
                else:
                    if lease is None:
                        # Contained planner: the lease is needed only for the
                        # execution stage (unreachable for v0.1 builtins).
                        lease = _acquire_lease(prepared, store, run_id, recorder, result)
                        if lease is None:
                            run_fatal = True
                    if lease is not None:
                        try:
                            execution = build_execution(
                                planning.plan,
                                prepared,
                                acknowledged_by=ack if report.provider_set else None,
                            )
                        except ZiggyError as exc:
                            _emit_error(recorder, exc)
                            result.errors.append(exc.to_model())
                            run_fatal = True
                        else:
                            scaffold_step_results(execution, result)
                            result.egress.extend(execution.egress_records)
                            if report.provider_set:
                                recorder.emit(
                                    event_type="egress_notice",
                                    payload={
                                        "provider_set": sorted(report.provider_set),
                                        "acknowledged_by": ack,
                                        "records": [
                                            record.model_dump()
                                            for record in execution.egress_records
                                        ],
                                    },
                                )
                                _mlog(
                                    prepared.logger,
                                    "egress_notice",
                                    run_id=run_id,
                                    provider_set=sorted(report.provider_set),
                                )
                            exec_cancelled, exec_degraded = await run_execution(
                                execution,
                                prepared,
                                result,
                                recorder=recorder,
                                redactor=redactor,
                                clock=clock,
                                cancel_event=cancel_event,
                                decide_permission=decide_permission,
                            )
                            cancelled = cancelled or exec_cancelled
                            capture_degraded = capture_degraded or exec_degraded
    finally:
        if lease is not None:
            lease.release()
            recorder.emit(
                event_type="lease_released",
                payload={"workspace": lease.lease.workspace},
            )
            _mlog(prepared.logger, "lease_released", run_id=run_id)

    result.status = aggregate_status(
        (step.status for step in result.steps.values()), cancelled=cancelled
    )
    if run_fatal and result.status is RunStatus.SUCCESS:
        result.status = RunStatus.FAILED
    recorder.emit(event_type="run_finished", payload={"status": result.status.value})
    await recorder.finalize()

    result.capture = recorder.capture_summary()
    if capture_degraded:
        degrade_capture(result)
    result.redaction = recorder.redaction_summary()
    usage = recorder.usage_summary()
    if usage is not None and usage.provider is None:
        usage.provider = prepared.planner_provider
    result.usage = usage
    result.ended_at = utc_now_iso()
    result.duration_ms = clock.elapsed_ms()
    _mlog(
        prepared.logger,
        "run_finished",
        run_id=run_id,
        agent=planner.name,
        kind=RunKind.ORCHESTRATOR.value,
        target=planner.name,
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
                agent=planner.name,
                path_ref=result.result_path,
            )
        else:
            _mlog(
                prepared.logger,
                "run_persist_failed",
                run_id=run_id,
                agent=planner.name,
                level="warning",
                reason_code="PersistenceError",
            )
    return result
