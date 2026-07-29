"""Validated-plan → execution mapping for the constrained orchestrator (REQ-013).

A valid plan executes under the ORIGINAL trusted ceilings — nothing a plan
contains can touch config, policy, or limits. :func:`build_execution` maps the
three plan variants onto one internal, RAW-id keyed ``WorkflowDef`` so the
serial engine machinery (scheduler order, failure propagation, restricted
interpolation, guarded per-step policy) applies unchanged:

- ``single_agent`` → a one-step graph with raw id ``main`` (result step id
  ``execute/main``); the plan prompt is sent verbatim (the validator rejected
  every template token, so interpolation is an identity pass).
- ``named_workflow`` → the trusted catalog workflow, re-loaded from its
  catalog-pinned path with the content hash RE-VERIFIED against the trusted
  user pin at execution-build time (TOCTOU guard: a file that changed after
  the catalog was built raises ``TrustPolicyError`` and nothing launches).
  Plan variables are typed JSON values (already validated); defaults fill in
  exactly as for CLI runs.
- ``inline_agent_workflow`` → a synthesized step graph. The ``'goal'`` input
  source maps to the ``vars.goal`` pseudo-variable carrying the ORIGINAL user
  goal: the goal is USER input, not model output, so it interpolates verbatim
  per the Phase-3 variable rules. ``steps.<id>.outputs.text`` inputs thread
  upstream step output wrapped in the untrusted-input delimiters, exactly like
  YAML workflows.

Result/step namespacing: every executed step appears in the RunResult (and in
every event) as ``execute/<raw-id>``; the raw ids drive the scheduler. The
plan's prompts remain untrusted model output — structural validation never
claims semantic prompt safety — and every executed step still runs under the
guarded workspace policy and the run's resource ceilings.

Named-workflow plan variables are planner output, not user CLI input. They
interpolate verbatim like all workflow variables: sizes/types were validated
against the declared schema, and substituted values are never re-scanned for
tokens, so template injection through a variable value is inert. A plan value
for a ``secret``-declared variable is NOT added to the redaction seeds: it
arrived as planner model output and is already part of the recorded plan-step
transcript, so treating it as a redactable secret would be meaningless.

Egress lineage: every executed step receives the plan (planner output) as a
prompt/parameter source, so each per-step :class:`EgressRecord` lists the
synthetic source ``"plan"`` first, followed by any raw
``steps.<id>.outputs.text`` sources it consumes.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ziggy.engine.env import compose_child_env
from ziggy.engine.prepare import PreparedOrchestration, _step_policy
from ziggy.engine.runner import (
    StepExecutionContext,
    execute_step,
    settle_step_result,
)
from ziggy.errors import (
    PersistenceError,
    ResourceLimitError,
    StepTimeoutError,
    TrustPolicyError,
    ValidationError,
    ZiggyError,
)
from ziggy.ids import utc_now_iso
from ziggy.models.common import StepStatus
from ziggy.models.plan import (
    InlineAgentWorkflowPlan,
    NamedWorkflowPlan,
    OrchestratorPlan,
    SingleAgentPlan,
)
from ziggy.models.result import Attempt, EgressRecord, RunResult, StepResult
from ziggy.models.workflow import StepDef, VariableDef, WorkflowDef
from ziggy.policy import MediationPolicy, PathAmbiguity, resolve_contained
from ziggy.workflows.discovery import user_workflows_dir
from ziggy.workflows.egress import step_provider
from ziggy.workflows.interpolate import render_prompt, validate_templates, value_to_text
from ziggy.workflows.scheduler import (
    WorkflowDeadline,
    input_source_step,
    propagate_failure,
    step_edges,
    topo_order,
)
from ziggy.workflows.schema import load_workflow
from ziggy.workflows.vars import check_secret_allowances

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ziggy.engine.hooks import MetadataLoggerLike, PermissionDecider
    from ziggy.events import RunRecorder
    from ziggy.ids import MonotonicClock
    from ziggy.models.agent import AgentConfig
    from ziggy.orchestrator.catalog import CatalogWorkflow
    from ziggy.redact import Redactor

__all__ = [
    "EXECUTE_PREFIX",
    "PLAN_INPUT_SOURCE",
    "ExecutionPlan",
    "PreparedExecutionStep",
    "build_execution",
    "run_execution",
    "scaffold_step_results",
]

#: Result/event step-id namespace prefix for executed plan steps.
EXECUTE_PREFIX = "execute/"

#: Synthetic egress input source naming the validated plan itself (planner
#: output parameterizes every executed step).
PLAN_INPUT_SOURCE = "plan"

#: Internal WorkflowDef names for the synthesized graphs (never user-visible
#: as targets; the RunResult target stays the planner agent name).
_SINGLE_AGENT_NAME = "plan-single-agent"
_INLINE_NAME = "plan-inline"

#: Raw id of the single_agent step (namespaced to ``execute/main``).
_SINGLE_AGENT_STEP_ID = "main"


@dataclass(slots=True)
class PreparedExecutionStep:
    """Launch-ready material for one executed plan step.

    ``raw_id`` keys the internal graph (scheduler, edges); ``step_id`` is the
    namespaced ``execute/<raw-id>`` used in the RunResult and every event.
    """

    raw_id: str
    step_id: str
    agent_config: AgentConfig
    provider: str
    env: dict[str, str]
    policy: MediationPolicy
    timeout_seconds: float


@dataclass(slots=True)
class ExecutionPlan:
    """The execution work a validated plan maps to.

    ``workflow`` is the internal RAW-id keyed graph; ``variables`` holds the
    typed values it interpolates (``{"goal": <original user goal>}`` for
    inline plans, resolved declared variables for named workflows, empty for
    single_agent). ``egress_records`` carry namespaced step ids and are ready
    to append to ``RunResult.egress``.
    """

    workflow: WorkflowDef
    variables: dict[str, Any]
    order: list[str]
    steps: dict[str, PreparedExecutionStep]
    egress_records: list[EgressRecord]


def _reverify_trusted_workflow(
    prepared: PreparedOrchestration, entry: CatalogWorkflow
) -> WorkflowDef:
    """Re-load a catalog workflow, re-proving the trusted-user hash pin.

    TOCTOU guard: the pin was verified when the catalog was built, but the
    file may have changed between planning and execution. The CURRENT content
    hash must still equal a trusted ``orchestrator.trusted_workflows`` pin
    whose path resolves to the same catalog-pinned file; anything else —
    unreadable file, no matching pin, stale hash, failed re-load — raises
    ``TrustPolicyError`` and no execution agent launches.
    """
    try:
        content = entry.path.read_bytes()
    except OSError as exc:
        raise TrustPolicyError(
            f"trusted workflow {entry.name!r} could not be re-read at execution "
            f"time: {exc.__class__.__name__}",
            details={"workflow": entry.name},
        ) from exc
    digest = hashlib.sha256(content).hexdigest()
    user_dir = user_workflows_dir()
    for pin in prepared.resolved.config.orchestrator.trusted_workflows:
        if pin.sha256.strip().lower() != digest:
            continue
        for base in (prepared.workspace, user_dir):
            try:
                resolved_path = resolve_contained(base, pin.path)
            except PathAmbiguity:
                continue
            if resolved_path == entry.path:
                try:
                    workflow = load_workflow(entry.path)
                except ValidationError as exc:
                    raise TrustPolicyError(
                        f"trusted workflow {entry.name!r} failed to re-load at "
                        f"execution time: {exc.message}",
                        details={"workflow": entry.name},
                    ) from exc
                if workflow.name != entry.name:
                    break  # renamed since catalog build: fail closed below
                return workflow
    raise TrustPolicyError(
        f"trusted workflow {entry.name!r} changed after the catalog was built "
        "(content hash no longer matches the trusted user pin); execution "
        "refused until the pin is re-approved",
        details={"workflow": entry.name},
    )


def _resolve_plan_variables(workflow: WorkflowDef, provided: Mapping[str, Any]) -> dict[str, Any]:
    """Typed variable values for a named-workflow plan (defaults filled).

    Plan variables are JSON values already validated against the declared
    schema by ``validate_plan``; this re-checks fail-closed (a validator/
    execution drift would otherwise run with unchecked values) and fills
    declared defaults exactly as CLI runs do.
    """
    problems: list[str] = []
    values: dict[str, Any] = {}
    for name in sorted(set(provided) - set(workflow.variables)):
        problems.append(f"variables.{name}: unknown variable (not declared by the workflow)")
    for name, decl in workflow.variables.items():
        if name in provided:
            value = provided[name]
            value_bytes = len(value_to_text(value).encode("utf-8"))
            if value_bytes > decl.max_bytes:
                problems.append(
                    f"variables.{name}: value is {value_bytes} bytes (UTF-8); "
                    f"max_bytes is {decl.max_bytes}"
                )
            else:
                values[name] = value
        elif decl.default is not None:
            values[name] = decl.default
        elif decl.required:
            problems.append(f"variables.{name}: required variable not provided by the plan")
    if problems:
        raise ValidationError("; ".join(problems))
    return values


def _inline_workflow(plan: InlineAgentWorkflowPlan) -> WorkflowDef:
    """Synthesize the internal graph for an inline plan.

    ``'goal'`` input sources become the ``vars.goal`` pseudo-variable (user
    data, interpolated verbatim); step-output sources pass through unchanged
    (wrapped in untrusted delimiters at render time). The ``goal`` variable is
    DECLARED on the synthetic definition so template validation accepts the
    mapped input sources; its size is governed by the composed-prompt ceiling
    at render time (the goal already fit the planning meta-prompt), not by
    the declarative ``max_bytes`` default. Anything the strict plan schema
    let through that the WorkflowDef contract rejects fails closed.
    """
    try:
        return WorkflowDef(
            version=1,
            name=_INLINE_NAME,
            variables={"goal": VariableDef(type="string")},
            steps={
                step.id: StepDef(
                    agent=step.agent,
                    prompt=step.prompt,
                    inputs={
                        name: ("vars.goal" if source == "goal" else source)
                        for name, source in step.inputs.items()
                    },
                    depends_on=list(step.depends_on),
                )
                for step in plan.steps
            },
        )
    except (ValueError, TypeError) as exc:  # pydantic ValidationError included
        raise ValidationError(
            f"inline plan could not be mapped to an executable step graph: {exc.__class__.__name__}"
        ) from exc


def _plan_workflow(
    plan: OrchestratorPlan, prepared: PreparedOrchestration
) -> tuple[WorkflowDef, dict[str, Any]]:
    """Internal (workflow, variables) pair for any validated plan variant."""
    if isinstance(plan, SingleAgentPlan):
        try:
            workflow = WorkflowDef(
                version=1,
                name=_SINGLE_AGENT_NAME,
                steps={_SINGLE_AGENT_STEP_ID: StepDef(agent=plan.agent, prompt=plan.prompt)},
            )
        except (ValueError, TypeError) as exc:
            raise ValidationError(
                "single_agent plan could not be mapped to an executable step: "
                f"{exc.__class__.__name__}"
            ) from exc
        return workflow, {}
    if isinstance(plan, NamedWorkflowPlan):
        entries = {wf.name: wf for wf in prepared.catalog.workflows}
        entry = entries.get(plan.workflow_name)
        if entry is None:  # validator guarantees membership; fail closed anyway
            raise TrustPolicyError(
                "planned workflow is not in the trusted catalog",
                details={"reason": "not-in-catalog"},
            )
        workflow = _reverify_trusted_workflow(prepared, entry)
        max_steps = prepared.resolved.config.engine.max_workflow_steps
        if len(workflow.steps) > max_steps:
            raise ResourceLimitError(
                f"trusted workflow {workflow.name!r} declares {len(workflow.steps)} "
                f"steps; engine.max_workflow_steps is {max_steps}",
                details={"step_count": len(workflow.steps), "max_workflow_steps": max_steps},
            )
        return workflow, _resolve_plan_variables(workflow, plan.variables)
    if isinstance(plan, InlineAgentWorkflowPlan):
        return _inline_workflow(plan), {"goal": prepared.goal}
    raise ValidationError("unknown plan variant cannot be executed")


def build_execution(
    plan: OrchestratorPlan,
    prepared: PreparedOrchestration,
    *,
    acknowledged_by: str | None,
) -> ExecutionPlan:
    """Map one VALIDATED plan onto launch-ready execution work.

    Raises (``TrustPolicyError`` / ``ConfigError`` / ``ValidationError`` /
    ``ResourceLimitError``) BEFORE anything launches; the caller records the
    typed error on the run and never starts execution. ``acknowledged_by`` is
    the resolved egress acknowledgement stamped on the per-step records
    (``None`` when the planned execution crosses no providers).
    """
    workflow, variables = _plan_workflow(plan, prepared)
    validate_templates(workflow)
    order = topo_order(workflow)

    config = prepared.resolved.config
    steps: dict[str, PreparedExecutionStep] = {}
    providers: dict[str, str] = {}
    for raw_id, step_def in workflow.steps.items():
        agent_config = prepared.registry.get(step_def.agent)  # ConfigError pre-launch
        provider = step_provider(agent_config)
        providers[raw_id] = provider
        env, _secrets = compose_child_env(agent_config, prepared.base_env)  # ConfigError
        timeout = (
            min(float(step_def.timeout_seconds), prepared.step_timeout_seconds)
            if step_def.timeout_seconds is not None
            else prepared.step_timeout_seconds
        )
        steps[raw_id] = PreparedExecutionStep(
            raw_id=raw_id,
            step_id=EXECUTE_PREFIX + raw_id,
            agent_config=agent_config,
            provider=provider,
            env=env,
            policy=_step_policy(config, prepared.workspace, raw_id, step_def),
            timeout_seconds=timeout,
        )

    # Same gate as prepare_workflow: secret variables flowing into a step need
    # a trusted user-scope allowance for that step's provider (named-workflow
    # plans only; inline/single graphs declare no variables).
    check_secret_allowances(
        workflow,
        step_providers=providers,
        allowances=config.workflows.secret_variable_allowances,
    )

    records = [
        EgressRecord(
            step_id=EXECUTE_PREFIX + raw_id,
            provider=providers[raw_id],
            input_sources=[
                PLAN_INPUT_SOURCE,
                *(
                    source
                    for source in workflow.steps[raw_id].inputs.values()
                    if input_source_step(source) is not None
                ),
            ],
            acknowledged_by=acknowledged_by,
        )
        for raw_id in order
    ]
    return ExecutionPlan(
        workflow=workflow,
        variables=variables,
        order=order,
        steps=steps,
        egress_records=records,
    )


def scaffold_step_results(execution: ExecutionPlan, result: RunResult) -> None:
    """Add one ``skipped`` StepResult scaffold per executed step (namespaced).

    Every declared step is present in the result in a terminal state from the
    moment execution is committed to; the run loop upgrades them.
    """
    for raw_id in execution.order:
        step_def = execution.workflow.steps[raw_id]
        step_id = EXECUTE_PREFIX + raw_id
        result.steps[step_id] = StepResult(
            step_id=step_id,
            agent=step_def.agent,
            status=StepStatus.SKIPPED,
            input_sources=dict(step_def.inputs),
        )


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


def _resolve_step_dir(workspace: Path, raw_id: str, working_dir: str | None) -> Path:
    """Fail-closed re-resolution of a step's working directory at run time."""
    if working_dir is None:
        return Path(os.path.realpath(workspace))
    try:
        return resolve_contained(workspace, working_dir)
    except PathAmbiguity as exc:
        raise ValidationError(
            f"steps.{raw_id}.working_dir: {working_dir!r} does not resolve "
            f"canonically inside the workspace: {exc}",
            details={"step_id": raw_id, "reason": exc.reason},
        ) from exc


def _upstream_inputs(step_def: StepDef, result: RunResult) -> dict[str, str]:
    """Raw upstream output text per local input name (namespaced lookup)."""
    upstream: dict[str, str] = {}
    for local_name, source in step_def.inputs.items():
        src_id = input_source_step(source)
        if src_id is None:
            continue
        producer = result.steps.get(EXECUTE_PREFIX + src_id)
        value = producer.outputs.get("text", "") if producer is not None else ""
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
    """Record the run-level StepTimeoutError for a crossed execution deadline."""
    details: dict[str, Any] = {"deadline_seconds": deadline_seconds}
    if active_step is not None:
        details["step_id"] = active_step
    error = StepTimeoutError(f"execution deadline of {deadline_seconds}s exceeded", details=details)
    _emit_error(recorder, error)
    result.errors.append(error.to_model())
    _mlog(
        logger,
        "workflow_deadline_exceeded",
        run_id=result.run_id,
        level="warning",
        reason_code=error.code,
    )


def _propagate(
    execution: ExecutionPlan,
    result: RunResult,
    edges: Mapping[str, tuple[str, ...]],
    failed_raw_id: str,
) -> None:
    """Apply blocked/skipped propagation over the namespaced result steps."""
    for raw_id, status in propagate_failure(execution.order, edges, failed_raw_id).items():
        result.steps[EXECUTE_PREFIX + raw_id].status = status


async def run_execution(
    execution: ExecutionPlan,
    prepared: PreparedOrchestration,
    result: RunResult,
    *,
    recorder: RunRecorder,
    redactor: Redactor,
    clock: MonotonicClock,
    cancel_event: asyncio.Event | None = None,
    decide_permission: PermissionDecider | None = None,
) -> tuple[bool, bool]:
    """Serial execution of the mapped plan steps; returns
    ``(cancelled, capture_degraded)``.

    Identical semantics to the workflow runner's step loop (one step at a
    time in the precomputed order, first failure stops new scheduling with
    blocked/skipped propagation, deadline enforced around the active step,
    cancellation marks the active step cancelled and the remainder skipped),
    with every event and StepResult keyed by the namespaced ``execute/<id>``
    step id. Mutates ``result.steps`` in place; the caller owns run-level
    aggregation, the plan step, and persistence.
    """
    edges = step_edges(execution.workflow)
    deadline = WorkflowDeadline(timeout_seconds=prepared.execution_deadline_seconds)
    cancelled = False
    capture_degraded = False
    pending = list(execution.order)
    while pending:
        raw_id = pending[0]
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            recorder.emit(
                event_type="cancel_requested",
                payload={"reason": "cancel", "scope": "orchestrator-execution"},
            )
            break
        if deadline.exceeded():
            _deadline_stop(
                result,
                recorder,
                prepared.logger,
                deadline.timeout_seconds,
                active_step=None,
            )
            break
        pending.pop(0)
        step_def = execution.workflow.steps[raw_id]
        prep = execution.steps[raw_id]
        step_result = result.steps[prep.step_id]
        step_result.status = StepStatus.FAILED  # provisional while active
        recorder.emit(
            event_type="step_started",
            step_id=prep.step_id,
            attempt_no=1,
            payload={
                "agent": step_def.agent,
                "provider": prep.provider,
                # REQ-013: the prompt this step runs was generated by the
                # planner; structural validation never proved it semantically
                # safe.
                "prompt_origin": "orchestrator-plan",
                "prompt_trust": "untrusted-model-output",
            },
        )

        try:
            cwd = _resolve_step_dir(prepared.workspace, raw_id, step_def.working_dir)
            upstream = _upstream_inputs(step_def, result)
            prompt = render_prompt(step_def, execution.variables, upstream)
            prompt_bytes = len(prompt.encode("utf-8"))
            if prompt_bytes > prepared.max_prompt_bytes:
                raise ResourceLimitError(
                    f"steps.{raw_id}: composed prompt is {prompt_bytes} bytes; "
                    f"engine.max_prompt_bytes is {prepared.max_prompt_bytes}",
                    details={
                        "step_id": prep.step_id,
                        "prompt_bytes": prompt_bytes,
                        "max_prompt_bytes": prepared.max_prompt_bytes,
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
            _propagate(execution, result, edges, raw_id)
            break

        attempt = Attempt(attempt_no=1, status=StepStatus.FAILED, started_at=utc_now_iso())
        attempt_start_ms = clock.elapsed_ms()
        outcome = await execute_step(
            StepExecutionContext(
                step_id=prep.step_id,
                command=prep.agent_config.command,
                args=list(prep.agent_config.args),
                env=dict(prep.env),
                cwd=str(cwd),
                prompt=prompt,
                timeout_seconds=deadline.clamp(prep.timeout_seconds),
                grace_seconds=prepared.grace_seconds,
                recorder=recorder,
                session_label=step_def.agent,
                policy=prep.policy,
                logger=prepared.logger,
                cancel_event=cancel_event,
                attempt_no=1,
                decide_permission=decide_permission,
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
            step_def, execution.variables, upstream, prompt, redactor
        )
        capture_degraded = capture_degraded or outcome.capture_degraded
        recorder.emit(
            event_type="step_finished",
            step_id=prep.step_id,
            attempt_no=1,
            session_id=outcome.session_id,
            payload={"status": outcome.status.value, "duration_ms": attempt.duration_ms},
        )
        _mlog(
            prepared.logger,
            "step_finished",
            run_id=recorder.run_id,
            step_id=prep.step_id,
            agent=step_def.agent,
            status=outcome.status.value,
            duration_ms=attempt.duration_ms,
        )

        if outcome.status is StepStatus.SUCCESS:
            continue
        if outcome.status is StepStatus.CANCELLED:
            cancelled = True  # active step cancelled; remainder stays skipped
            break
        if isinstance(outcome.error, StepTimeoutError) and deadline.exceeded():
            _deadline_stop(
                result,
                recorder,
                prepared.logger,
                deadline.timeout_seconds,
                active_step=prep.step_id,
            )
        else:
            _propagate(execution, result, edges, raw_id)
        break
    return cancelled, capture_degraded
