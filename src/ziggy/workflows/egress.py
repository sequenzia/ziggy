"""Cross-provider egress computation for workflows (REQ-011, §5.6).

Provider flow is derived exclusively from *data edges*: ``inputs`` entries of
the form ``steps.<id>.outputs.<name>``. ``depends_on`` is pure ordering and
carries no data, so it can never create egress; ``vars.<name>`` inputs are
user-provided values, not another provider's output, so they never count as
crossings either (secret-variable allowances are enforced separately in
``workflows/vars.py``).

A *crossing* exists iff a data edge connects two steps whose providers differ.
When a workflow crosses providers, the exact provider set must be acknowledged
— via trusted user config (``[egress] acknowledged_provider_sets``) or the
per-invocation ``--acknowledge-egress`` flag — before any agent launch;
headless + unacknowledged fails with :class:`EgressNotAcknowledgedError`
(exit 2) via :func:`gate_egress`.

Acknowledgement matching is EXACT set equality: order-free, duplicate-free,
and never satisfied by a subset or superset.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ziggy.errors import EgressNotAcknowledgedError
from ziggy.models.result import EgressRecord

if TYPE_CHECKING:
    from ziggy.agents import AgentRegistry
    from ziggy.config import ResolvedConfig
    from ziggy.models.agent import AgentConfig
    from ziggy.models.workflow import StepDef, WorkflowDef

#: ``EgressRecord.acknowledged_by`` values (stable strings; mirror
#: ``ziggy.engine.prepare`` — not imported to keep workflows -> engine acyclic).
ACK_BY_CONFIG = "config"
ACK_BY_FLAG = "flag:--acknowledge-egress"


def step_provider(agent_config: AgentConfig) -> str:
    """Egress identity of one step's agent: declared provider, else a stable
    ``custom:<agent-name>`` fallback so unlabelled agents are never conflated
    with each other or with a known provider."""
    if agent_config.provider:
        return agent_config.provider
    return f"custom:{agent_config.name}"


def _source_step_id(source: str) -> str | None:
    """Step id for a ``steps.<id>.outputs.<name>`` input source, else None."""
    parts = source.split(".")
    if len(parts) == 4 and parts[0] == "steps" and parts[2] == "outputs":
        return parts[1]
    return None


def _step_output_sources(step: StepDef) -> list[str]:
    """The step's upstream-output input sources, in declaration order."""
    return [source for source in step.inputs.values() if _source_step_id(source) is not None]


def _providers_by_step(workflow: WorkflowDef, registry: AgentRegistry) -> dict[str, str]:
    return {
        step_id: step_provider(registry.get(step.agent)) for step_id, step in workflow.steps.items()
    }


def required_provider_set(workflow: WorkflowDef, registry: AgentRegistry) -> frozenset[str]:
    """Providers of all steps participating in any cross-provider data edge.

    Empty frozenset when nothing crosses (single-provider workflows, or
    workflows whose only edges are ``depends_on`` / ``vars.*``): no
    acknowledgement is required.
    """
    providers = _providers_by_step(workflow, registry)
    crossing: set[str] = set()
    for step_id, step in workflow.steps.items():
        receiver = providers[step_id]
        for source in step.inputs.values():
            src_id = _source_step_id(source)
            if src_id is None:  # vars.* input: never a crossing
                continue
            sender = providers.get(src_id)
            if sender is None:  # unknown step ref: schema/scheduler reject it
                continue
            if sender != receiver:
                crossing.add(sender)
                crossing.add(receiver)
    return frozenset(crossing)


def is_acknowledged(
    provider_set: frozenset[str],
    resolved_config: ResolvedConfig,
    flag_value: list[str] | None,
) -> str | None:
    """How (and whether) the exact crossing provider set is acknowledged.

    Returns ``"flag:--acknowledge-egress"`` when the flag names exactly this
    set, ``"config"`` when ``egress.acknowledged_provider_sets`` contains
    exactly this set, else ``None``. The explicit per-invocation flag wins when
    both match. Matching is exact set equality — order and duplicates are
    irrelevant; subsets and supersets never match. An empty ``provider_set``
    (no crossing) needs no acknowledgement and always returns ``None``.
    """
    target = set(provider_set)
    if not target:
        return None
    if flag_value is not None:
        flagged = {entry.strip() for entry in flag_value if entry.strip()}
        if flagged == target:
            return ACK_BY_FLAG
    acknowledged_sets = resolved_config.config.egress.acknowledged_provider_sets
    for entry in acknowledged_sets:
        if {provider.strip() for provider in entry if provider.strip()} == target:
            return ACK_BY_CONFIG
    return None


def build_egress_records(
    workflow: WorkflowDef,
    registry: AgentRegistry,
    acknowledged_by: str | None,
) -> list[EgressRecord]:
    """One :class:`EgressRecord` per step that RECEIVES upstream outputs.

    ``input_sources`` are the raw ``steps.<id>.outputs.<name>`` strings in
    declaration order (``vars.*`` inputs are not egress lineage). Steps with
    no upstream-output inputs produce no record. ``acknowledged_by`` is
    stamped only on steps that actually receive another provider's output;
    single-provider workflows therefore keep per-step provider lineage with
    ``acknowledged_by=None`` and never trigger the crossing gate.
    """
    providers = _providers_by_step(workflow, registry)
    records: list[EgressRecord] = []
    for step_id, step in workflow.steps.items():
        upstream = _step_output_sources(step)
        if not upstream:
            continue
        receiver = providers[step_id]
        crosses = any(
            providers.get(src_id, receiver) != receiver
            for source in upstream
            if (src_id := _source_step_id(source)) is not None
        )
        records.append(
            EgressRecord(
                step_id=step_id,
                provider=receiver,
                input_sources=upstream,
                acknowledged_by=acknowledged_by if crosses else None,
            )
        )
    return records


def gate_egress(provider_set: frozenset[str], acknowledged_by: str | None) -> None:
    """Fail closed BEFORE any launch when a crossing is unacknowledged.

    No-op when nothing crosses (empty set) or the set was acknowledged.
    Raises :class:`EgressNotAcknowledgedError` (exit 2) naming the exact
    sorted provider set with a rerun hint.
    """
    if not provider_set or acknowledged_by is not None:
        return
    ordered = sorted(provider_set)
    joined = ",".join(ordered)
    raise EgressNotAcknowledgedError(
        f"workflow sends step outputs across providers {{{', '.join(ordered)}}} "
        "and this exact provider set is not acknowledged; re-run with "
        f"--acknowledge-egress {joined} or add {ordered} to "
        "[egress] acknowledged_provider_sets in trusted user config",
        details={
            "provider_set": ordered,
            "hint": f"--acknowledge-egress {joined}",
        },
    )
