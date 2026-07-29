"""Plan parsing + security validation for the constrained orchestrator (REQ-013).

Two halves, both fail-closed and both producing BOUNDED error lists
(:data:`MAX_PLAN_ERRORS` entries of at most :data:`MAX_PLAN_ERROR_CHARS`
characters each) that are safe to persist and to feed back in a repair prompt:

- :func:`parse_plan` turns raw planner text into a shape-valid
  :data:`~ziggy.models.plan.OrchestratorPlan` (markdown fences stripped, first
  balanced top-level JSON object extracted with string/escape-aware brace
  matching, strict pydantic validation). Parse errors are rendered ONLY from
  pydantic ``loc``/``type`` — never from pydantic ``msg`` or ``input``, both
  of which can echo raw model output.
- :func:`validate_plan` security-validates a shape-valid plan against the
  trusted-user catalog and the ORIGINAL resource ceilings and computes the
  provider set of the planned execution. It returns a
  :class:`ValidationReport`; the caller gates (``gate_egress``-style) and
  decides repair/execution. Nothing here launches anything.

Error text policy: error entries may reference key paths (``steps.0.env``),
schema-constrained identifiers (step ids, template token names — both limited
to identifier character sets by the plan schema), and derived numbers (byte
counts). They never include free-form model output values (agent names,
workflow names, prompts, input sources, rationale text).

Inline dependency semantics (documented precisely, per contract): a step's
effective dependency set is the UNION of its ``depends_on`` list and the steps
referenced by its ``inputs`` (``steps.<id>.outputs.text`` — a data edge
implies the ordering dependency, exactly like YAML workflows in
``ziggy.workflows.scheduler``). An input reference to a step that is not
listed in ``depends_on`` is therefore legal in itself; it is an error when the
referenced step does not exist, is the step itself, or when the implied edge
makes the combined graph cyclic.

Validation constrains plan STRUCTURE and execution authority only; it never
claims a natural-language prompt is semantically safe (generated prompts stay
untrusted model output downstream).
"""

from __future__ import annotations

import contextlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from ziggy.errors import ConfigError, ValidationError
from ziggy.models.plan import (
    InlineAgentWorkflowPlan,
    InlineStep,
    NamedWorkflowPlan,
    OrchestratorPlan,
    SingleAgentPlan,
)
from ziggy.models.workflow import StepDef, WorkflowDef
from ziggy.workflows.egress import is_acknowledged, required_provider_set, step_provider
from ziggy.workflows.interpolate import TOKEN_RE, value_to_text, wrap_untrusted
from ziggy.workflows.scheduler import topo_order
from ziggy.workflows.schema import load_workflow
from ziggy.workflows.vars import check_secret_allowances

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from ziggy.agents.registry import AgentRegistry
    from ziggy.config import ResolvedConfig
    from ziggy.models.agent import AgentConfig
    from ziggy.orchestrator.catalog import Catalog, CatalogWorkflow

__all__ = [
    "MAX_PLAN_ERRORS",
    "MAX_PLAN_ERROR_CHARS",
    "ValidationReport",
    "bound_errors",
    "parse_plan",
    "validate_plan",
]

#: Bounds for every error list produced by this module (contract: <=10 errors,
#: each <=200 characters, never echoing the raw planner response).
MAX_PLAN_ERRORS = 10
MAX_PLAN_ERROR_CHARS = 200

_PLAN_ADAPTER: TypeAdapter[Any] = TypeAdapter(OrchestratorPlan)

#: First markdown code fence (```json or bare ```), non-greedy body.
_FENCE_RE = re.compile(r"```[ \t]*(?:json)?[ \t]*\r?\n(.*?)```", re.IGNORECASE | re.DOTALL)

#: Brace-shaped template syntax; mirrors ``ziggy.workflows.interpolate``'s
#: private suspect pattern (a lone ``}}`` is legitimate literal text).
_SUSPECT_RE = re.compile(r"\{\{|\{%|%\}")

_VARIANT_TAGS = frozenset({"single_agent", "named_workflow", "inline_agent_workflow"})

#: Exact allowed model_dump keys per variant — defense in depth against future
#: drift of ``ziggy.models.plan`` (the parse-time ``extra='forbid'`` guarantee
#: is what keeps hostile fields out; this check keeps the MODEL honest).
_ALLOWED_PLAN_KEYS: dict[str, frozenset[str]] = {
    "single_agent": frozenset({"plan_type", "rationale", "agent", "prompt"}),
    "named_workflow": frozenset({"plan_type", "rationale", "workflow_name", "variables"}),
    "inline_agent_workflow": frozenset({"plan_type", "rationale", "steps"}),
}
_ALLOWED_STEP_KEYS = frozenset({"id", "agent", "prompt", "inputs", "depends_on"})

#: Context-free renderings per pydantic error ``type``. pydantic ``msg`` text
#: is NEVER used: several types (``union_tag_invalid``, ``value_error``, ...)
#: interpolate raw input values into their messages.
_PARSE_PHRASES: dict[str, str] = {
    "extra_forbidden": "extra fields forbidden",
    "missing": "field required",
    "union_tag_invalid": "must be 'single_agent', 'named_workflow', or 'inline_agent_workflow'",
    "union_tag_not_found": "a JSON object with a 'plan_type' field is required",
    "json_invalid": "invalid JSON",
    "model_type": "expected an object",
    "dict_type": "expected an object",
    "list_type": "expected an array",
    "string_type": "expected a string",
    "string_too_long": "string exceeds the maximum length",
    "string_pattern_mismatch": "does not match the required id pattern",
    "too_short": "too few items",
    "value_error": "input sources must be 'goal' or 'steps.<id>.outputs.text'",
}


def bound_errors(errors: Iterable[str]) -> list[str]:
    """Clamp an error sequence to the contract bounds.

    At most :data:`MAX_PLAN_ERRORS` entries; each entry hard-truncated to
    :data:`MAX_PLAN_ERROR_CHARS` characters (``...`` marker within the bound).
    """
    bounded: list[str] = []
    for entry in errors:
        if len(bounded) >= MAX_PLAN_ERRORS:
            break
        if len(entry) > MAX_PLAN_ERROR_CHARS:
            entry = entry[: MAX_PLAN_ERROR_CHARS - 3] + "..."
        bounded.append(entry)
    return bounded


# ---------------------------------------------------------------------------
# parse half


def _balanced_spans(text: str) -> Iterable[str]:
    """Yield each balanced top-level ``{...}`` span, string/escape aware.

    Scanning is JSON-oriented: only double-quoted strings suspend brace
    counting, and a backslash escapes the next character inside a string. An
    opening brace whose object never closes ends the scan (everything after it
    is inside that unbalanced object, so no further TOP-LEVEL span exists).
    """
    index = 0
    length = len(text)
    while True:
        start = text.find("{", index)
        if start == -1:
            return
        depth = 0
        in_string = False
        escaped = False
        for pos in range(start, length):
            char = text[pos]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    yield text[start : pos + 1]
                    index = pos + 1
                    break
        else:
            return


def _extract_json_object(raw_text: str) -> str | None:
    """Locate the plan candidate: fenced content first, then the full text.

    Returns the first balanced top-level span that parses as JSON; if none
    parses, the first balanced span (so strict validation reports a bounded
    'invalid JSON' error); ``None`` when no balanced object exists at all.
    """
    sources: list[str] = []
    fenced = _FENCE_RE.search(raw_text)
    if fenced is not None:
        sources.append(fenced.group(1))
    sources.append(raw_text)

    first_balanced: str | None = None
    for source in sources:
        for span in _balanced_spans(source):
            if first_balanced is None:
                first_balanced = span
            try:
                json.loads(span)
            except json.JSONDecodeError:
                continue
            return span
    return first_balanced


def _render_parse_error(error: Mapping[str, Any]) -> str:
    """One bounded, context-free entry from a pydantic error dict.

    Uses ``loc`` and ``type`` only. The leading discriminated-union variant
    tag is dropped from the path (``('single_agent', 'env')`` renders as
    ``env``), matching the schema the planner was shown.
    """
    loc = [str(part) for part in error.get("loc", ())]
    if loc and loc[0] in _VARIANT_TAGS:
        loc = loc[1:]
    error_type = str(error.get("type", "unknown"))
    phrase = _PARSE_PHRASES.get(error_type, f"invalid value ({error_type})")
    if loc:
        path = ".".join(loc)
    elif error_type.startswith("union_tag"):
        path = "plan_type"
    else:
        path = "plan"
    return f"{path}: {phrase}"


def parse_plan(raw_text: str) -> tuple[OrchestratorPlan | None, list[str]]:
    """Parse raw planner output into a shape-valid plan, or bounded errors.

    Strips a markdown fence if present, extracts the first balanced top-level
    JSON object (prose before/after is ignored), and validates it strictly
    against the three-variant plan schema. Returns ``(plan, [])`` on success
    or ``(None, errors)`` — errors bounded per :func:`bound_errors` and never
    echoing the raw response.
    """
    candidate = _extract_json_object(raw_text)
    if candidate is None:
        return None, ["plan: planner output contains no JSON object"]
    try:
        plan = _PLAN_ADAPTER.validate_json(candidate, strict=True)
    except PydanticValidationError as exc:
        return None, bound_errors(_render_parse_error(error) for error in exc.errors())
    return plan, []


# ---------------------------------------------------------------------------
# validation half


@dataclass(frozen=True)
class ValidationReport:
    """Outcome of :func:`validate_plan`.

    ``errors`` empty means the plan is valid. ``provider_set`` is the exact
    cross-provider set of the planned execution INCLUDING the planner (empty
    when nothing crosses); ``acknowledged_by`` is the trusted-config
    acknowledgement per :func:`ziggy.workflows.egress.is_acknowledged`
    (``'config'`` or ``None`` — the per-invocation flag is applied by the
    caller, which owns the gate).
    """

    errors: list[str]
    provider_set: frozenset[str]
    acknowledged_by: str | None

    @property
    def valid(self) -> bool:
        return not self.errors


def _provider_of(registry: AgentRegistry, agent_name: str) -> str | None:
    """Egress identity of a registered agent; ``None`` when unregistered."""
    try:
        agent: AgentConfig = registry.get(agent_name)
    except ConfigError:
        return None
    return step_provider(agent)


def _schema_drift_errors(plan: Any) -> list[str]:
    """Defense in depth: re-dump the model and require EXACTLY the known keys.

    ``extra='forbid'`` at parse time is what blocks hostile fields; this check
    catches future drift of ``ziggy.models.plan`` (a newly added field would
    otherwise flow to execution without a validation rule). Key names here
    come from the model definition, never from planner input.
    """
    plan_type = getattr(plan, "plan_type", None)
    allowed = _ALLOWED_PLAN_KEYS.get(plan_type) if isinstance(plan_type, str) else None
    if allowed is None:
        return ["plan_type: unknown plan variant"]
    dump = plan.model_dump()
    errors = [
        f"plan: unexpected field {key!r} (plan schema drift)" for key in sorted(set(dump) - allowed)
    ]
    if plan_type == "inline_agent_workflow":
        for index, step_dump in enumerate(dump.get("steps") or []):
            if not isinstance(step_dump, dict):
                continue
            errors.extend(
                f"steps.{index}: unexpected field {key!r} (plan schema drift)"
                for key in sorted(set(step_dump) - _ALLOWED_STEP_KEYS)
            )
    return errors


def _template_errors(
    prompt: str,
    declared_inputs: Mapping[str, str],
    where: str,
) -> tuple[list[str], Counter[str]]:
    """Restricted template check with context-free messages.

    Same grammar as :func:`ziggy.workflows.interpolate.validate_template`
    (tokens scrubbed, remaining brace syntax rejected) but rendered without
    prompt snippets. Only ``{{ inputs.<name> }}`` tokens naming DECLARED
    inputs are allowed in inline prompts; ``vars.*`` tokens never are.

    Returns ``(errors, goal_occurrences)`` where ``goal_occurrences`` counts
    token occurrences per input whose declared source is ``'goal'`` (for the
    worst-case size estimate).
    """
    errors: list[str] = []
    goal_occurrences: Counter[str] = Counter()
    scrubbed = list(prompt)
    for match in TOKEN_RE.finditer(prompt):
        namespace, name = match.group(1), match.group(2)
        if namespace == "vars":
            errors.append(
                f"{where}: 'vars.*' tokens are not allowed "
                "(only '{{ inputs.<name> }}' tokens naming declared inputs)"
            )
        elif name not in declared_inputs:
            errors.append(f"{where}: references undeclared input '{name}'")
        elif declared_inputs[name] == "goal":
            goal_occurrences[name] += 1
        for index in range(match.start(), match.end()):
            scrubbed[index] = " "
    if _SUSPECT_RE.search("".join(scrubbed)):
        errors.append(
            f"{where}: unsupported template syntax "
            "(only '{{ inputs.<name> }}' tokens naming declared inputs are allowed; "
            "no expressions, filters, loops, or other template constructs)"
        )
    return errors, goal_occurrences


def _validate_single_agent(
    plan: SingleAgentPlan,
    eligible: frozenset[str],
    resolved: ResolvedConfig,
) -> list[str]:
    errors: list[str] = []
    if plan.agent not in eligible:
        errors.append("agent: not in the orchestration-eligible agent set")
    if not plan.prompt.strip():
        errors.append("prompt: must be non-empty")
    if _SUSPECT_RE.search(plan.prompt):
        errors.append(
            "prompt: template syntax is not allowed in a single_agent prompt "
            "(it is sent verbatim; there are no inputs to interpolate)"
        )
    max_prompt_bytes = resolved.config.engine.max_prompt_bytes
    prompt_bytes = len(plan.prompt.encode("utf-8"))
    if prompt_bytes > max_prompt_bytes:
        errors.append(
            f"prompt: {prompt_bytes} bytes (UTF-8) exceeds "
            f"engine.max_prompt_bytes ({max_prompt_bytes})"
        )
    return errors


def _plan_value_matches(var_type: str, value: Any) -> bool:
    """Exact-typed match for JSON-native plan variable values.

    Unlike ``--var`` CLI strings (parsed by ``workflows.vars._parse_typed``),
    plan variables arrive as JSON values, so NO string coercion is applied:
    ``string`` requires a JSON string, ``integer`` a JSON integer (bool is
    never an integer), ``boolean`` a JSON boolean, ``json`` any JSON value.
    """
    if var_type == "string":
        return isinstance(value, str)
    if var_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if var_type == "boolean":
        return isinstance(value, bool)
    return True  # json


def _validate_plan_variables(workflow: WorkflowDef, values: Mapping[str, Any]) -> list[str]:
    """REQ-013 named_workflow variable checks against the declared schema.

    Mirrors ``workflows.vars.validate_variables`` semantics (unknown names,
    required, ``max_bytes`` as UTF-8 bytes of the canonical insertion text)
    with exact-typed JSON values instead of CLI string parsing.
    """
    errors: list[str] = []
    errors.extend(
        f"variables.{name}: unknown variable (not declared by the workflow)"
        for name in sorted(set(values) - set(workflow.variables))
    )
    for name, decl in workflow.variables.items():
        if name in values:
            value = values[name]
            if not _plan_value_matches(decl.type, value):
                errors.append(f"variables.{name}: expected a value of type '{decl.type}'")
                continue
            value_bytes = len(value_to_text(value).encode("utf-8"))
            if value_bytes > decl.max_bytes:
                errors.append(
                    f"variables.{name}: value is {value_bytes} bytes (UTF-8); "
                    f"max_bytes is {decl.max_bytes}"
                )
        elif decl.required:
            errors.append(f"variables.{name}: required variable not provided by the plan")
    return errors


def _load_trusted_workflow(entry: CatalogWorkflow) -> tuple[WorkflowDef | None, list[str]]:
    """Re-load a catalog workflow definition from its pinned path, fail-closed.

    The content hash was verified against the trusted-user pin when the
    catalog was built; a file that no longer loads cleanly here fails
    validation rather than silently passing (the path itself is
    catalog-resolved, never planner input).
    """
    try:
        return load_workflow(entry.path), []
    except ValidationError:
        return None, [
            "workflow_name: trusted workflow could not be re-loaded for validation "
            "(file changed or became unreadable after catalog build)"
        ]


def _validate_named_workflow(
    plan: NamedWorkflowPlan,
    workflows: Mapping[str, CatalogWorkflow],
    resolved: ResolvedConfig,
    registry: AgentRegistry,
) -> tuple[list[str], WorkflowDef | None]:
    if plan.workflow_name not in workflows:
        return ["workflow_name: not in the trusted workflow catalog"], None
    workflow, errors = _load_trusted_workflow(workflows[plan.workflow_name])
    if workflow is None:
        return errors, None

    errors.extend(_validate_plan_variables(workflow, plan.variables))

    step_providers: dict[str, str] = {}
    for step_id, step in workflow.steps.items():
        provider = _provider_of(registry, step.agent)
        if provider is None:
            # Trusted workflow content, catalog-approved: step ids are safe paths.
            errors.append(f"steps.{step_id}: workflow step agent is not registered")
        else:
            step_providers[step_id] = provider
    try:
        # Same gate as prepare_workflow: secret variables flowing into a step
        # require a trusted user-scope allowance for that step's provider;
        # missing provider identity fails closed inside the helper.
        check_secret_allowances(
            workflow,
            step_providers=step_providers,
            allowances=resolved.config.workflows.secret_variable_allowances,
        )
    except ValidationError as exc:
        errors.extend(exc.message.split("; "))
    return errors, workflow


def _synthetic_workflow(steps: list[InlineStep]) -> WorkflowDef:
    """InlineStep graph as a WorkflowDef so scheduler helpers apply as-is.

    ``'goal'`` input sources map to the ``vars.goal`` pseudo-variable exactly
    as execution will map them (user data — never a dependency edge). Prompts
    are replaced with a placeholder: the scheduler never reads them, and
    inline prompts are template-checked separately with context-free messages
    (``StepDef`` also requires a non-empty prompt while ``InlineStep`` does
    not — emptiness is reported as its own validation error).
    """
    return WorkflowDef(
        version=1,
        name="inline-plan",
        steps={
            step.id: StepDef(
                agent=step.agent,
                prompt="-",
                inputs={
                    name: ("vars.goal" if source == "goal" else source)
                    for name, source in step.inputs.items()
                },
                depends_on=list(step.depends_on),
            )
            for step in steps
        },
    )


def _validate_inline(
    plan: InlineAgentWorkflowPlan,
    eligible: frozenset[str],
    resolved: ResolvedConfig,
    goal: str,
) -> list[str]:
    errors: list[str] = []
    max_inline_steps = resolved.config.orchestrator.max_inline_steps
    if len(plan.steps) > max_inline_steps:
        errors.append(
            f"steps: {len(plan.steps)} steps exceeds "
            f"orchestrator.max_inline_steps ({max_inline_steps})"
        )

    max_prompt_bytes = resolved.config.engine.max_prompt_bytes
    goal_bytes = len(goal.encode("utf-8"))
    seen_ids: dict[str, int] = {}
    duplicate_ids = False
    for index, step in enumerate(plan.steps):
        # Step ids are schema-bounded identifiers (^[a-zA-Z][a-zA-Z0-9_-]{0,63}$)
        # and safe to reference; positions are used for all other step errors.
        if step.id == "plan":
            errors.append(
                f"steps.{index}.id: 'plan' is reserved for the planning step "
                "(executed steps are namespaced 'execute/<id>')"
            )
        if step.id in seen_ids:
            duplicate_ids = True
            errors.append(
                f"steps.{index}.id: duplicate step id (also declared at steps.{seen_ids[step.id]})"
            )
        else:
            seen_ids[step.id] = index

        if step.agent not in eligible:
            errors.append(f"steps.{index}.agent: not in the orchestration-eligible agent set")

        if not step.prompt.strip():
            errors.append(f"steps.{index}.prompt: must be non-empty")
        template_errors, goal_occurrences = _template_errors(
            step.prompt, step.inputs, f"steps.{index}.prompt"
        )
        errors.extend(template_errors)

        for name, source in step.inputs.items():
            if source == "goal":
                continue
            parts = source.split(".")
            shape_ok = len(parts) == 4 and parts[0] == "steps" and parts[2] == "outputs"
            if not shape_ok or parts[3] != "text":
                errors.append(
                    f"steps.{index}.inputs.{name}: only 'goal' or "
                    "'steps.<id>.outputs.text' sources are allowed"
                )

        # Worst-case composed size: each '{{ inputs.<name> }}' occurrence of a
        # goal-sourced input expands to the goal plus the untrusted-input
        # delimiter overhead (conservative upper bound: execution inserts the
        # goal as a vars.goal pseudo-variable). Step-output inputs have
        # unknown size here; the runner re-enforces the composed ceiling at
        # render time.
        estimated = len(step.prompt.encode("utf-8"))
        for name, count in goal_occurrences.items():
            overhead = len(wrap_untrusted(name, "goal", "").encode("utf-8"))
            estimated += count * (goal_bytes + overhead)
        if estimated > max_prompt_bytes:
            errors.append(
                f"steps.{index}.prompt: estimated composed prompt is {estimated} "
                f"bytes (worst-case goal interpolation); engine.max_prompt_bytes "
                f"is {max_prompt_bytes}"
            )

    if not duplicate_ids:
        # depends_on UNION input-implied edges: existence, self-dependency, and
        # acyclicity via the workflow scheduler (declaration-order Kahn).
        # Scheduler messages reference schema-bounded step ids only.
        try:
            topo_order(_synthetic_workflow(plan.steps))
        except ValidationError as exc:
            errors.extend(exc.message.split("; "))
    return errors


def _crossing_set(pairs: Iterable[tuple[str | None, str | None]]) -> set[str]:
    """Providers participating in any pair whose two sides differ."""
    crossing: set[str] = set()
    for sender, receiver in pairs:
        if sender is not None and receiver is not None and sender != receiver:
            crossing.update((sender, receiver))
    return crossing


def _plan_provider_set(
    plan: Any,
    workflow: WorkflowDef | None,
    registry: AgentRegistry,
    planner_provider: str,
) -> frozenset[str]:
    """Cross-provider set of the planned execution, planner included.

    The plan itself (selected targets, generated prompts, variables) is
    planner output that parameterizes EVERY execution step, so the planner
    counts as a data sender to each step; intra-plan ``steps.<id>.outputs.text``
    edges cross exactly as in :func:`ziggy.workflows.egress.required_provider_set`.
    Empty when every resolvable participant shares one provider. Agents that
    fail registry lookup contribute nothing (their absence is already a
    validation error, and errors block execution regardless).
    """
    pairs: list[tuple[str | None, str | None]] = []
    crossing: set[str] = set()
    if isinstance(plan, SingleAgentPlan):
        pairs.append((planner_provider, _provider_of(registry, plan.agent)))
    elif isinstance(plan, NamedWorkflowPlan):
        if workflow is not None:
            for step in workflow.steps.values():
                pairs.append((planner_provider, _provider_of(registry, step.agent)))
            # Unregistered step agents raise ConfigError here; their absence
            # is already a validation error and errors block execution.
            with contextlib.suppress(ConfigError):
                crossing |= required_provider_set(workflow, registry)
    elif isinstance(plan, InlineAgentWorkflowPlan):
        providers: dict[str, str | None] = {}
        for step in plan.steps:
            provider = _provider_of(registry, step.agent)
            providers.setdefault(step.id, provider)
            pairs.append((planner_provider, provider))
        for step in plan.steps:
            receiver = providers.get(step.id)
            for source in step.inputs.values():
                parts = source.split(".")
                if len(parts) == 4 and parts[0] == "steps" and parts[2] == "outputs":
                    pairs.append((providers.get(parts[1]), receiver))
    crossing |= _crossing_set(pairs)
    return frozenset(crossing)


def validate_plan(
    plan: OrchestratorPlan,
    catalog: Catalog,
    resolved: ResolvedConfig,
    registry: AgentRegistry,
    *,
    planner_provider: str,
    goal: str,
) -> ValidationReport:
    """Security-validate a shape-valid plan; empty ``errors`` means valid.

    Checks per REQ-013: eligibility of every targeted agent against the
    trusted-user catalog, trusted-workflow membership + typed variable
    validation, inline graph bounds/uniqueness/acyclicity (data edges imply
    dependencies), restricted input-only template use, composed prompt size
    under the ORIGINAL ``engine.max_prompt_bytes`` ceiling (worst-case goal
    interpolation for inline steps), and a model-drift re-dump check.

    ``planner_provider`` is the planning agent's egress identity (the plan is
    planner output flowing into every execution step); ``goal`` is the
    original user goal (needed for the worst-case size estimate). The
    returned :class:`ValidationReport` carries the provider set and the
    trusted-CONFIG acknowledgement only — the caller applies any
    per-invocation flag and owns the gate. Nothing is launched here.
    """
    eligible = frozenset(agent.name for agent in catalog.eligible_agents)
    workflows: dict[str, CatalogWorkflow] = {wf.name: wf for wf in catalog.workflows}

    errors: list[str] = _schema_drift_errors(plan)
    named_def: WorkflowDef | None = None
    if isinstance(plan, SingleAgentPlan):
        errors.extend(_validate_single_agent(plan, eligible, resolved))
    elif isinstance(plan, NamedWorkflowPlan):
        named_errors, named_def = _validate_named_workflow(plan, workflows, resolved, registry)
        errors.extend(named_errors)
    elif isinstance(plan, InlineAgentWorkflowPlan):
        errors.extend(_validate_inline(plan, eligible, resolved, goal))
    else:  # pragma: no cover - unreachable behind parse_plan
        errors.append("plan_type: unknown plan variant")

    provider_set = _plan_provider_set(plan, named_def, registry, planner_provider)
    return ValidationReport(
        errors=bound_errors(errors),
        provider_set=provider_set,
        acknowledged_by=is_acknowledged(provider_set, resolved, None),
    )
