"""Run preparation: resolved config + CLI overrides -> executable RunSpec.

``prepare_run`` is the seam between the trusted configuration layer and the
direct-run engine (REQ-003/007/008/016). It owns every pre-launch decision:

- **Agent resolution** — the agent must exist in the registry built from the
  trusted user config (builtins + ``[agents.*]``); unknown names raise
  ``ConfigError`` before anything else is touched.
- **Prompt ceiling** — the UTF-8 byte size of the prompt must not exceed
  ``engine.max_prompt_bytes`` (``ResourceLimitError``).
- **Capture profile** — a CLI ``--capture`` override is *direct user intent*
  and may exceed the configured value (REQ-006: higher capture must be
  selected by trusted user config **or an explicit CLI flag**); only project
  scope is bound by the tighten-only merge, which the config loader already
  enforced.
- **Child environment** — composed via :func:`ziggy.engine.env.compose_child_env`;
  a named-but-unset ``api_key_env`` raises ``ConfigError`` before launch.
- **Timeout** — the CLI override may only lower the user-scope ceiling:
  ``min(override, engine.default_step_timeout_seconds)``.
- **Mediation policy** — guarded policy for the run, with
  ``step_dir == workspace`` for direct runs (one implicit step working in the
  workspace), the user profile selected by ``permissions.default_policy``,
  and the project-scope deny-only additions.
- **Metadata logger** — a real :class:`MetadataLogger` (with config
  retention) or a :class:`NullLogger` when the run is unsaved
  (``--no-save`` or ``results.persist = false``).
- **Redaction seeding** — exact values of ``api_key_env`` plus
  ``redaction.extra_value_env_vars`` present in the parent environment, and
  the config custom patterns, passed through to the run's Redactor.
- **Egress acknowledgement** — records *how* the agent provider's egress was
  acknowledged (``flag:--acknowledge-egress`` wins over ``config``); a direct
  single-agent run has no cross-provider flow, so absence never blocks here.

Phase 3 adds :func:`prepare_workflow` — the same seam for workflow runs
(REQ-009/010/011): discovery → schema → typed vars (with the secret-allowance
gate) → interpolation validation → limits preflight → egress preflight
(fail-closed BEFORE any launch) → per-step child env + guarded policy →
metadata logger. The result is the launch-ready
:class:`~ziggy.workflows.runner.PreparedWorkflow` consumed by
``execute_workflow``. NOTE: ``ziggy.workflows.runner`` is imported lazily
inside the function — a module-scope import would create an import cycle
through ``ziggy.engine.__init__ -> prepare -> workflows.runner ->
ziggy.engine.runner``.

Phase 5 adds :func:`prepare_orchestration` — the pre-launch seam for
``ziggy orchestrate`` (REQ-013). It owns every trusted-config decision that
must happen before the planning agent can even be considered:

- ``orchestrator.agent`` unset or unknown ⇒ ``ConfigError`` (exit 2). The
  planner does NOT need ``orchestration_eligible`` — that flag is the trusted
  user allowlist for agents a model-generated plan may INVOKE; who plans is
  the separate ``orchestrator.agent`` decision.
- The uncontained-planner gate: a planner with ``direct_tools_assumed`` (both
  v0.1 builtins, and every custom agent) is refused with ``TrustPolicyError``
  (exit 2) unless trusted user config sets
  ``orchestrator.allow_uncontained_planner = true`` (USER_ONLY — project
  scope cannot set it). The acknowledgement is recorded in the run's policy
  provenance with ``advisory`` enforcement.
- Catalog build (``ConfigError`` for eligible-agent incoherence) and the
  deterministic meta-prompt, whose composed UTF-8 size (goal included) must
  fit ``engine.max_prompt_bytes`` (``ResourceLimitError``).
- The planning child environment: the documented minimal baseline plus the
  planner's explicit trusted-config values ONLY — composed via
  ``compose_child_env`` over a stripped AgentConfig copy whose
  ``inherit_env`` passthrough list is cleared, so no parent variable beyond
  the baseline (HOME/PATH/TERM/LANG) reaches the planner. The agent's
  literal ``env`` table and ``api_key_env`` remain: both are explicit
  trusted-user declarations, not parent-environment inheritance.
- Redaction seeding: the planner's credential plus best-effort ``api_key_env``
  values of every registered agent present in the parent env (execution
  targets are chosen by the plan, after the run's Redactor exists) plus the
  configured extra values/patterns.

The orchestrator catalog module is imported lazily to keep
``ziggy.engine.__init__`` import-time free of the orchestrator package.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ziggy.agents import AgentRegistry
from ziggy.config import ResolvedConfig, ZiggyConfig
from ziggy.engine.env import compose_child_env
from ziggy.engine.hooks import MetadataLoggerLike
from ziggy.engine.lease import LeaseManager
from ziggy.engine.runner import RunSpec
from ziggy.errors import ConfigError, ResourceLimitError, TrustPolicyError, ValidationError
from ziggy.events import EventLimits
from ziggy.models.agent import AgentConfig
from ziggy.models.common import CaptureProfile
from ziggy.models.result import PolicyProvenance
from ziggy.models.workflow import StepDef
from ziggy.policy import (
    GUARDED_POLICY_NAME,
    SOURCE_USER,
    MediationPolicy,
    PathAmbiguity,
    build_policy_provenance,
)
from ziggy.redact import CustomPattern
from ziggy.store.logs import open_metadata_logger
from ziggy.workflows.discovery import resolve as resolve_workflow
from ziggy.workflows.egress import (
    build_egress_records,
    gate_egress,
    is_acknowledged,
    required_provider_set,
    step_provider,
)
from ziggy.workflows.interpolate import validate_templates
from ziggy.workflows.scheduler import topo_order
from ziggy.workflows.vars import (
    check_secret_allowances,
    secret_redaction_values,
    validate_variables,
)

if TYPE_CHECKING:
    from ziggy.orchestrator.catalog import Catalog
    from ziggy.workflows.runner import PreparedWorkflow

#: EgressRecord.acknowledged_by values (stable strings).
ACK_BY_FLAG = "flag:--acknowledge-egress"
ACK_BY_CONFIG = "config"

#: Rule id of the uncontained-planner acknowledgement recorded in the
#: orchestrator run's policy provenance (REQ-013; stable string).
RULE_UNCONTAINED_PLANNER_ACK = "uncontained-planner-ack"


@dataclass(slots=True)
class RunOverrides:
    """Per-invocation user controls from CLI flags (REQ-003).

    ``capture`` and ``timeout_seconds`` are ``None`` when the flag was not
    given (config values apply). ``acknowledge_egress`` is the provider list
    from ``--acknowledge-egress p1,p2``. ``plan_only`` is the orchestrator
    ``--plan-only`` flag (REQ-013): validate and return the plan without
    launching execution.
    """

    no_save: bool = False
    capture: CaptureProfile | None = None
    timeout_seconds: float | None = None
    acknowledge_egress: list[str] | None = None
    plan_only: bool = False


@dataclass(slots=True)
class PreparedRun:
    """Everything the CLI needs to execute and render one direct run.

    ``spec`` already carries ``policy``, ``policy_provenance``, ``logger``,
    and ``config_fingerprint`` for :func:`ziggy.engine.runner.execute_run`;
    the top-level fields exist so callers can inspect them without digging
    through the spec.
    """

    spec: RunSpec
    policy: MediationPolicy
    logger: MetadataLoggerLike
    policy_provenance: PolicyProvenance
    agent_config: AgentConfig
    config_fingerprint: str


def _egress_acknowledgement(
    config: ZiggyConfig, provider: str | None, flag_providers: list[str] | None
) -> str | None:
    """How the agent provider's egress was acknowledged, if at all."""
    if provider is None:
        return None
    if flag_providers and provider in flag_providers:
        return ACK_BY_FLAG
    if any(provider in provider_set for provider_set in config.egress.acknowledged_provider_sets):
        return ACK_BY_CONFIG
    return None


def prepare_run(
    resolved: ResolvedConfig,
    *,
    agent_name: str,
    prompt: str,
    workspace: Path,
    overrides: RunOverrides,
    base_env: Mapping[str, str] | None = None,
) -> PreparedRun:
    """Validate and assemble one direct run from config + CLI overrides.

    Raises ``ConfigError`` (unknown agent, missing ``api_key_env``) or
    ``ResourceLimitError`` (prompt over ``engine.max_prompt_bytes``) before
    any subprocess or filesystem side effect. The only side effect on success
    is opening the metadata logger (which prunes expired log files) — and
    none at all for unsaved runs (``NullLogger``).

    ``base_env`` defaults to ``os.environ``; it is the parent environment
    used for child-env composition and secret-value resolution (injectable
    for tests).
    """
    env_map: Mapping[str, str] = os.environ if base_env is None else base_env
    config = resolved.config

    registry = AgentRegistry.from_config(resolved)
    agent_config = registry.get(agent_name)  # ConfigError on unknown agent

    prompt_bytes = len(prompt.encode("utf-8"))
    if prompt_bytes > config.engine.max_prompt_bytes:
        raise ResourceLimitError(
            f"prompt is {prompt_bytes} bytes; engine.max_prompt_bytes is "
            f"{config.engine.max_prompt_bytes}",
            details={
                "prompt_bytes": prompt_bytes,
                "max_prompt_bytes": config.engine.max_prompt_bytes,
            },
        )

    # CLI flag is direct user intent and may exceed the configured profile.
    capture = overrides.capture if overrides.capture is not None else config.results.capture

    env, secret_values = compose_child_env(agent_config, env_map)  # ConfigError pre-launch
    for name in config.redaction.extra_value_env_vars:
        value = env_map.get(name)
        if value:
            secret_values.append((f"env:{name}", value))
    redaction_patterns = [
        CustomPattern(kind=p.kind, regex=p.regex, max_width=p.max_width)
        for p in config.redaction.patterns
    ]

    ceiling = float(config.engine.default_step_timeout_seconds)
    if overrides.timeout_seconds is None:
        timeout = ceiling
    else:
        timeout = min(float(overrides.timeout_seconds), ceiling)

    policy = MediationPolicy.guarded(
        workspace=workspace,
        step_dir=workspace,  # direct runs: one implicit step working in the workspace
        profile=config.permissions.profiles.get(config.permissions.default_policy),
        project_denials=config.permissions.project_denials,
        profile_name=config.permissions.default_policy,
    )
    policy_provenance = build_policy_provenance(policy)

    no_save = overrides.no_save or not config.results.persist
    store_root = (
        Path(config.results.store_path).expanduser()
        if config.results.store_path is not None
        else None
    )
    logger = open_metadata_logger(
        store_root, no_save=no_save, retention_days=config.logs.retention_days
    )

    spec = RunSpec(
        agent_name=agent_name,
        command=agent_config.command,
        args=list(agent_config.args),
        env=env,
        cwd=str(workspace),
        prompt=prompt,
        capture_profile=capture,
        no_save=no_save,
        step_timeout_seconds=timeout,
        cancel_grace_seconds=float(config.engine.cancel_grace_seconds),
        provider=agent_config.provider,
        limits=EventLimits(
            max_event_bytes_per_step=config.engine.max_event_bytes_per_step,
            max_artifact_bytes_per_run=config.engine.max_artifact_bytes_per_run,
        ),
        store_root=store_root,
        secret_values=secret_values,
        redaction_patterns=redaction_patterns or None,
        policy=policy,
        policy_provenance=policy_provenance,
        logger=logger,
        config_fingerprint=resolved.fingerprint,
        egress_acknowledged_by=_egress_acknowledgement(
            config, agent_config.provider, overrides.acknowledge_egress
        ),
    )
    return PreparedRun(
        spec=spec,
        policy=policy,
        logger=logger,
        policy_provenance=policy_provenance,
        agent_config=agent_config,
        config_fingerprint=resolved.fingerprint,
    )


def _step_policy(
    config: ZiggyConfig, workspace: Path, step_id: str, step: StepDef
) -> MediationPolicy:
    """Guarded mediation policy for one workflow step (REQ-008/009).

    ``step_dir`` is the step's ``working_dir`` (re-proven canonically inside
    the workspace, fail-closed) or the workspace itself. A step may reference
    a trusted user-defined ``policy_profile``; an unknown name is a
    ``ValidationError`` — a workflow can never invent policy authority.
    Project denials always apply (deny-only tightening).
    """
    profile_name = step.policy_profile or config.permissions.default_policy
    profile = config.permissions.profiles.get(profile_name)
    if (
        step.policy_profile is not None
        and profile is None
        and step.policy_profile != GUARDED_POLICY_NAME
    ):
        raise ValidationError(
            f"steps.{step_id}.policy_profile: unknown policy profile "
            f"{step.policy_profile!r} (profiles are defined in trusted user "
            "config under [permissions.profiles])",
            details={"step_id": step_id, "profile": step.policy_profile},
        )
    step_dir = Path(step.working_dir) if step.working_dir is not None else workspace
    try:
        return MediationPolicy.guarded(
            workspace=workspace,
            step_dir=step_dir,
            profile=profile,
            project_denials=config.permissions.project_denials,
            profile_name=profile_name,
        )
    except PathAmbiguity as exc:
        raise ValidationError(
            f"steps.{step_id}.working_dir: {step.working_dir!r} does not "
            f"resolve canonically inside the workspace: {exc}",
            details={"step_id": step_id, "reason": exc.reason},
        ) from exc


def prepare_workflow(
    resolved: ResolvedConfig,
    *,
    name_or_path: str,
    cli_vars: dict[str, str],
    workspace: Path,
    overrides: RunOverrides,
    base_env: Mapping[str, str] | None = None,
) -> PreparedWorkflow:
    """Validate and assemble one workflow run from config + CLI inputs.

    Every pre-launch decision happens here, in order: discovery/direct-path
    resolution (``ValidationError`` for unknown names, duplicates, escaping
    paths), template validation, step-count ceiling
    (``engine.max_workflow_steps`` → ``ResourceLimitError``), topological
    order (cycles/unknown refs), per-step agent lookup (``ConfigError``),
    typed variable resolution + the secret-allowance gate vs each destination
    step's provider, per-step child env (missing ``api_key_env`` →
    ``ConfigError``) and guarded policy (step ``working_dir`` proven inside
    the workspace), and the egress preflight — an unacknowledged
    cross-provider set raises ``EgressNotAcknowledgedError`` (exit 2) HERE,
    before any launch. The only side effect on success is opening the
    metadata logger (none at all for unsaved runs).

    ``secret_values`` on the returned PreparedWorkflow are the complete
    exact-match redaction seeds — per-step ``api_key_env`` values, config
    ``redaction.extra_value_env_vars``, and resolved secret workflow
    variables; the workflow runner has no name-heuristic fallback.
    """
    # Lazy import: a module-scope import would create a cycle through
    # ziggy.engine.__init__ -> prepare -> workflows.runner -> engine.runner.
    from ziggy.workflows.runner import EgressPlan, PreparedStep, PreparedWorkflow

    env_map: Mapping[str, str] = os.environ if base_env is None else base_env
    config = resolved.config

    discovered = resolve_workflow(name_or_path, workspace)
    workflow = discovered.definition
    validate_templates(workflow)

    max_steps = config.engine.max_workflow_steps
    if len(workflow.steps) > max_steps:
        raise ResourceLimitError(
            f"workflow {workflow.name!r} declares {len(workflow.steps)} steps; "
            f"engine.max_workflow_steps is {max_steps}",
            details={"step_count": len(workflow.steps), "max_workflow_steps": max_steps},
        )
    order = topo_order(workflow)

    registry = AgentRegistry.from_config(resolved)
    agent_configs = {
        step_id: registry.get(step.agent) for step_id, step in workflow.steps.items()
    }  # ConfigError on unknown agent
    providers = {step_id: step_provider(cfg) for step_id, cfg in agent_configs.items()}

    variables = validate_variables(workflow, cli_vars)
    check_secret_allowances(
        workflow,
        step_providers=providers,
        allowances=config.workflows.secret_variable_allowances,
    )

    # CLI flag is direct user intent and may exceed the configured profile.
    capture = overrides.capture if overrides.capture is not None else config.results.capture

    secret_values: list[tuple[str, str]] = []
    seen_secrets: set[tuple[str, str]] = set()

    def add_secret(pair: tuple[str, str]) -> None:
        if pair not in seen_secrets:
            seen_secrets.add(pair)
            secret_values.append(pair)

    ceiling = float(config.engine.default_step_timeout_seconds)
    steps: dict[str, PreparedStep] = {}
    for step_id, step in workflow.steps.items():
        agent_config = agent_configs[step_id]
        env, step_secrets = compose_child_env(agent_config, env_map)  # ConfigError pre-launch
        for pair in step_secrets:
            add_secret(pair)
        timeout = (
            min(float(step.timeout_seconds), ceiling)
            if step.timeout_seconds is not None
            else ceiling
        )
        steps[step_id] = PreparedStep(
            step_id=step_id,
            agent_config=agent_config,
            provider=providers[step_id],
            env=env,
            policy=_step_policy(config, workspace, step_id, step),
            timeout_seconds=timeout,
        )

    for name in config.redaction.extra_value_env_vars:
        value = env_map.get(name)
        if value:
            add_secret((f"env:{name}", value))
    for pair in secret_redaction_values(workflow, variables):
        add_secret(pair)
    redaction_patterns = [
        CustomPattern(kind=p.kind, regex=p.regex, max_width=p.max_width)
        for p in config.redaction.patterns
    ]

    # Egress preflight: headless + unacknowledged crossing fails (exit 2)
    # before any launch. The per-invocation flag wins over config.
    provider_set = required_provider_set(workflow, registry)
    acknowledged_by = is_acknowledged(provider_set, resolved, overrides.acknowledge_egress)
    gate_egress(provider_set, acknowledged_by)
    egress = EgressPlan(
        provider_set=provider_set,
        acknowledged_by=acknowledged_by,
        records=build_egress_records(workflow, registry, acknowledged_by),
    )

    # Run-level provenance mirrors direct runs: the workspace-scoped default
    # policy; per-step policies (profiles, working_dir) live on the steps.
    run_policy = MediationPolicy.guarded(
        workspace=workspace,
        step_dir=workspace,
        profile=config.permissions.profiles.get(config.permissions.default_policy),
        project_denials=config.permissions.project_denials,
        profile_name=config.permissions.default_policy,
    )
    policy_provenance = build_policy_provenance(run_policy)

    no_save = overrides.no_save or not config.results.persist
    store_root = (
        Path(config.results.store_path).expanduser()
        if config.results.store_path is not None
        else None
    )
    logger = open_metadata_logger(
        store_root, no_save=no_save, retention_days=config.logs.retention_days
    )

    return PreparedWorkflow(
        resolved=resolved,
        workflow=workflow,
        variables=variables,
        workspace=workspace,
        order=order,
        steps=steps,
        egress=egress,
        limits=EventLimits(
            max_event_bytes_per_step=config.engine.max_event_bytes_per_step,
            max_artifact_bytes_per_run=config.engine.max_artifact_bytes_per_run,
        ),
        capture_profile=capture,
        no_save=no_save,
        logger=logger,
        store_root=store_root,
        config_fingerprint=resolved.fingerprint,
        policy_provenance=policy_provenance,
        secret_values=secret_values,
        redaction_patterns=redaction_patterns,
        workflow_timeout_seconds=float(config.engine.default_workflow_timeout_seconds),
    )


# ---------------------------------------------------------------------------
# Phase 5: orchestration preparation (REQ-013)


@dataclass(slots=True)
class PreparedOrchestration:
    """Everything ``ziggy.orchestrator.planner.run_orchestration`` needs.

    Built exclusively by :func:`prepare_orchestration` from trusted config +
    CLI flags. ``planning_env`` is the COMPLETE planning child environment
    (documented minimal baseline + the planner's explicit trusted-config
    values; no ``inherit_env`` passthrough). ``meta_prompt`` is the rendered
    deterministic planning prompt, already proven under
    ``engine.max_prompt_bytes``. ``base_env`` is the captured parent
    environment mapping used later to compose EXECUTION step environments
    (the plan decides the targets, so those envs cannot be composed here).

    ``uncontained`` mirrors the planner's ``direct_tools_assumed``;
    ``lease_required_before_planning`` is true exactly when the planner is
    uncontained (spec: the workspace lease must be held from before planner
    launch through completion). v0.1 reality: both builtins and every custom
    agent are ``direct_tools_assumed``, so reaching this object at all means
    the trusted user acknowledged ``orchestrator.allow_uncontained_planner``;
    the contained branch exists for future capability-matrix updates.
    """

    resolved: ResolvedConfig
    registry: AgentRegistry
    planner_agent: AgentConfig
    planner_provider: str
    catalog: Catalog
    goal: str
    meta_prompt: str
    meta_prompt_limits: dict[str, int]
    workspace: Path
    plan_only: bool
    auto_execute: bool
    uncontained: bool
    lease_required_before_planning: bool
    planning_env: dict[str, str]
    acknowledge_egress: list[str] | None
    base_env: dict[str, str]
    capture_profile: CaptureProfile
    no_save: bool
    store_root: Path | None
    logger: MetadataLoggerLike
    limits: EventLimits
    config_fingerprint: str
    policy_provenance: PolicyProvenance
    secret_values: list[tuple[str, str]]
    redaction_patterns: list[CustomPattern]
    step_timeout_seconds: float
    execution_deadline_seconds: float
    grace_seconds: float
    max_prompt_bytes: int
    lease_manager: LeaseManager = field(default_factory=LeaseManager)


def prepare_orchestration(
    resolved: ResolvedConfig,
    *,
    goal: str,
    workspace: Path,
    overrides: RunOverrides,
    base_env: Mapping[str, str] | None = None,
) -> PreparedOrchestration:
    """Validate and assemble one orchestration run from config + CLI inputs.

    Raises before any subprocess or filesystem side effect (the only success
    side effect is opening the metadata logger):

    - ``ConfigError`` (exit 2): ``orchestrator.agent`` unset, planner not
      registered, catalog eligible-agent incoherence, or a named-but-unset
      planner ``api_key_env``.
    - ``TrustPolicyError`` (exit 2): the planner has assumed direct local
      tools and ``orchestrator.allow_uncontained_planner`` is not set in
      trusted user config (project scope can never set it).
    - ``ResourceLimitError``: the composed meta-prompt (goal included)
      exceeds ``engine.max_prompt_bytes``.

    The planner does NOT need ``orchestration_eligible``: that flag allowlists
    agents a model-generated plan may invoke as execution targets; the planner
    itself is chosen directly by trusted user config (``orchestrator.agent``).
    """
    # Lazy import: keeps ziggy.engine import-time independent of the
    # orchestrator package (mirrors the workflows.runner lazy import above).
    from ziggy.orchestrator.catalog import build_catalog, render_meta_prompt

    env_map: Mapping[str, str] = os.environ if base_env is None else base_env
    config = resolved.config
    orch = config.orchestrator

    if not orch.agent:
        raise ConfigError(
            "orchestrator.agent is not configured; set orchestrator.agent to a "
            "trusted user-registered agent name to enable `ziggy orchestrate`",
            details={"key": "orchestrator.agent"},
        )
    registry = AgentRegistry.from_config(resolved)
    planner = registry.get(orch.agent)  # ConfigError on unknown agent

    uncontained = planner.direct_tools_assumed
    if uncontained and not orch.allow_uncontained_planner:
        raise TrustPolicyError(
            f"planner agent '{planner.name}' is assumed to have direct filesystem/"
            "shell tools that Ziggy cannot disable or OS-contain; planning is "
            "refused by default. To accept this advisory boundary, set "
            "orchestrator.allow_uncontained_planner = true in trusted USER "
            "config (project config can never set it).",
            details={
                "agent": planner.name,
                "key": "orchestrator.allow_uncontained_planner",
                "enforcement": "advisory",
            },
        )

    catalog = build_catalog(resolved, registry, workspace)  # ConfigError on incoherence

    meta_prompt_limits = {
        "max_inline_steps": int(orch.max_inline_steps),
        "max_prompt_bytes": int(config.engine.max_prompt_bytes),
    }
    meta_prompt = render_meta_prompt(catalog, goal, meta_prompt_limits)
    prompt_bytes = len(meta_prompt.encode("utf-8"))
    if prompt_bytes > config.engine.max_prompt_bytes:
        raise ResourceLimitError(
            f"planning meta-prompt is {prompt_bytes} bytes (goal is "
            f"{len(goal.encode('utf-8'))} bytes); engine.max_prompt_bytes is "
            f"{config.engine.max_prompt_bytes}",
            details={
                "prompt_bytes": prompt_bytes,
                "goal_bytes": len(goal.encode("utf-8")),
                "max_prompt_bytes": config.engine.max_prompt_bytes,
            },
        )

    # Planning env: minimal baseline + the planner's explicit trusted-config
    # values only. The stripped copy clears the inherit_env passthrough list;
    # the literal env table and api_key_env are explicit trusted-user
    # declarations (not parent-env inheritance) and survive.
    stripped = planner.model_copy(update={"inherit_env": []})
    planning_env, secret_values = compose_child_env(stripped, env_map)  # ConfigError pre-launch

    # Redaction seeding must happen now (one Redactor per run), but execution
    # agents are chosen by the plan later: seed best-effort with every
    # registered agent's credential value present in the parent env.
    seen: set[tuple[str, str]] = set(secret_values)
    for agent in registry.list():
        if agent.api_key_env:
            value = env_map.get(agent.api_key_env, "")
            pair = (f"env:{agent.api_key_env}", value)
            if value and pair not in seen:
                seen.add(pair)
                secret_values.append(pair)
    for name in config.redaction.extra_value_env_vars:
        value = env_map.get(name, "")
        pair = (f"env:{name}", value)
        if value and pair not in seen:
            seen.add(pair)
            secret_values.append(pair)
    redaction_patterns = [
        CustomPattern(kind=p.kind, regex=p.regex, max_width=p.max_width)
        for p in config.redaction.patterns
    ]

    # CLI flag is direct user intent and may exceed the configured profile.
    capture = overrides.capture if overrides.capture is not None else config.results.capture

    # Run-level provenance mirrors workflow runs (guarded workspace policy for
    # the execution stage; the planning step builds its own isolation policy
    # at run time around the temp dir). The uncontained acknowledgement is
    # recorded here with advisory enforcement (REQ-013).
    run_policy = MediationPolicy.guarded(
        workspace=workspace,
        step_dir=workspace,
        profile=config.permissions.profiles.get(config.permissions.default_policy),
        project_denials=config.permissions.project_denials,
        profile_name=config.permissions.default_policy,
    )
    policy_provenance = build_policy_provenance(run_policy)
    if uncontained:
        policy_provenance.rules.append(
            {
                "rule_id": RULE_UNCONTAINED_PLANNER_ACK,
                "effect": "acknowledge",
                "source": SOURCE_USER,
                "uncontained_planner_ack": True,
                "enforcement": "advisory",
                "agent": planner.name,
            }
        )

    ceiling = float(config.engine.default_step_timeout_seconds)
    if overrides.timeout_seconds is None:
        step_timeout = ceiling
    else:
        step_timeout = min(float(overrides.timeout_seconds), ceiling)

    no_save = overrides.no_save or not config.results.persist
    store_root = (
        Path(config.results.store_path).expanduser()
        if config.results.store_path is not None
        else None
    )
    logger = open_metadata_logger(
        store_root, no_save=no_save, retention_days=config.logs.retention_days
    )

    return PreparedOrchestration(
        resolved=resolved,
        registry=registry,
        planner_agent=planner,
        planner_provider=step_provider(planner),
        catalog=catalog,
        goal=goal,
        meta_prompt=meta_prompt,
        meta_prompt_limits=meta_prompt_limits,
        workspace=workspace,
        plan_only=overrides.plan_only,
        auto_execute=bool(orch.auto_execute),
        uncontained=uncontained,
        lease_required_before_planning=uncontained,
        planning_env=planning_env,
        acknowledge_egress=overrides.acknowledge_egress,
        base_env=dict(env_map),
        capture_profile=capture,
        no_save=no_save,
        store_root=store_root,
        logger=logger,
        limits=EventLimits(
            max_event_bytes_per_step=config.engine.max_event_bytes_per_step,
            max_artifact_bytes_per_run=config.engine.max_artifact_bytes_per_run,
        ),
        config_fingerprint=resolved.fingerprint,
        policy_provenance=policy_provenance,
        secret_values=secret_values,
        redaction_patterns=redaction_patterns,
        step_timeout_seconds=step_timeout,
        execution_deadline_seconds=float(config.engine.default_workflow_timeout_seconds),
        grace_seconds=float(config.engine.cancel_grace_seconds),
        max_prompt_bytes=int(config.engine.max_prompt_bytes),
    )
