"""Serial deterministic DAG scheduling (REQ-010, spec §5.6/§6.4) — pure logic.

No I/O, no clocks except the injectable :class:`WorkflowDeadline`, no agent
knowledge: unknown *agent* names are the schema/registry layer's concern; this
module only reasons about step ids and edges.

Edges are the union of each step's ``depends_on`` (pure ordering) and the
steps referenced by its ``inputs`` (data dependencies of the form
``steps.<id>.outputs.<name>``; ``vars.<name>`` sources are user values, never
edges). :func:`topo_order` is Kahn's algorithm with **declaration order**
(the dict order of the YAML ``steps`` mapping) as the tie-break, so the
execution order is stable across runs and machines. Cycles — including
2-cycles and self-loops — and references to unknown steps are
:class:`~ziggy.errors.ValidationError`\\ s at load time.

:func:`propagate_failure` implements the REQ-010 failure semantics for the
not-yet-run remainder of a precomputed order: transitive dependents of the
failed step become ``blocked``, every other remaining step becomes
``skipped``; already-completed steps are never touched (they are not in the
remainder).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from ziggy.errors import ValidationError
from ziggy.models.common import StepStatus
from ziggy.models.workflow import WorkflowDef

__all__ = [
    "WorkflowDeadline",
    "input_source_step",
    "propagate_failure",
    "step_edges",
    "topo_order",
]


def input_source_step(source: str) -> str | None:
    """Step id referenced by a ``steps.<id>.outputs.<name>`` input source.

    Returns ``None`` for ``vars.<name>`` sources (and anything else the
    schema would have rejected) — those never create edges.
    """
    parts = source.split(".")
    if len(parts) == 4 and parts[0] == "steps" and parts[2] == "outputs":
        return parts[1]
    return None


def step_edges(workflow: WorkflowDef) -> dict[str, tuple[str, ...]]:
    """Dependency edges per step: union of ``depends_on`` + ``inputs`` step refs.

    Maps every step id to the tuple of step ids it depends on (its
    predecessors), deduplicated, in declaration order (``depends_on`` entries
    first, then ``inputs`` sources). Raises :class:`ValidationError` — with
    every violation collected into one path-precise message — for references
    to unknown steps and for self-dependencies (via either edge kind).
    """
    problems: list[str] = []
    edges: dict[str, tuple[str, ...]] = {}
    for step_id, step in workflow.steps.items():
        deps: list[str] = []
        seen: set[str] = set()
        for ref in step.depends_on:
            where = f"steps.{step_id}.depends_on"
            if ref not in workflow.steps:
                problems.append(f"{where}: references unknown step {ref!r}")
                continue
            if ref == step_id:
                problems.append(f"{where}: step depends on itself")
                continue
            if ref not in seen:
                seen.add(ref)
                deps.append(ref)
        for local_name, source in step.inputs.items():
            src_id = input_source_step(source)
            if src_id is None:
                continue
            where = f"steps.{step_id}.inputs.{local_name}"
            if src_id not in workflow.steps:
                problems.append(f"{where}: references unknown step {src_id!r}")
                continue
            if src_id == step_id:
                problems.append(f"{where}: step cannot consume its own output")
                continue
            if src_id not in seen:
                seen.add(src_id)
                deps.append(src_id)
        edges[step_id] = tuple(deps)
    if problems:
        raise ValidationError("; ".join(problems))
    return edges


def _cycle_members(edges: Mapping[str, tuple[str, ...]], remaining: Sequence[str]) -> list[str]:
    """The remaining steps that actually sit on a dependency cycle.

    A step is a cycle member iff it can reach itself through dependency
    edges restricted to the unschedulable remainder. Kahn's leftover also
    contains innocent steps *downstream* of a cycle; naming only the true
    members keeps the error actionable.
    """
    remaining_set = set(remaining)

    def reaches(start: str) -> bool:
        stack = [dep for dep in edges.get(start, ()) if dep in remaining_set]
        visited: set[str] = set()
        while stack:
            node = stack.pop()
            if node == start:
                return True
            if node in visited:
                continue
            visited.add(node)
            stack.extend(dep for dep in edges.get(node, ()) if dep in remaining_set)
        return False

    members = [step_id for step_id in remaining if reaches(step_id)]
    return members or list(remaining)


def topo_order(workflow: WorkflowDef) -> list[str]:
    """Stable serial execution order: Kahn with declaration-order tie-break.

    At every point the declaration-earliest step whose dependencies have all
    been scheduled is chosen, so the result is fully deterministic for a given
    workflow definition. Raises :class:`ValidationError` for unknown/self
    references (via :func:`step_edges`) and for dependency cycles, naming the
    cycle's member steps in declaration order.
    """
    edges = step_edges(workflow)
    declaration = list(workflow.steps)
    scheduled: set[str] = set()
    order: list[str] = []
    while len(order) < len(declaration):
        ready = next(
            (
                step_id
                for step_id in declaration
                if step_id not in scheduled and all(dep in scheduled for dep in edges[step_id])
            ),
            None,
        )
        if ready is None:
            remaining = [step_id for step_id in declaration if step_id not in scheduled]
            members = _cycle_members(edges, remaining)
            raise ValidationError(
                "steps: dependency cycle detected among steps: " + ", ".join(members),
                details={"cycle": members},
            )
        scheduled.add(ready)
        order.append(ready)
    return order


def propagate_failure(
    order: Sequence[str],
    edges: Mapping[str, Iterable[str]],
    failed_step: str,
) -> dict[str, StepStatus]:
    """Terminal statuses for the not-yet-run steps after ``failed_step`` fails.

    ``order`` is the precomputed topological order and ``edges`` the
    dependency mapping from :func:`step_edges`. Every step *after* the failed
    one in ``order`` is returned: transitive dependents of the failure —
    directly or through another blocked step — map to
    :attr:`StepStatus.BLOCKED`, all other remaining steps to
    :attr:`StepStatus.SKIPPED`. Steps at or before the failure point are not
    in the result (already-terminal steps stay unchanged).
    """
    if failed_step not in order:
        raise ValueError(f"failed step {failed_step!r} is not in the execution order")
    position = order.index(failed_step)
    poisoned: set[str] = {failed_step}
    statuses: dict[str, StepStatus] = {}
    for step_id in order[position + 1 :]:
        if any(dep in poisoned for dep in edges.get(step_id, ())):
            poisoned.add(step_id)
            statuses[step_id] = StepStatus.BLOCKED
        else:
            statuses[step_id] = StepStatus.SKIPPED
    return statuses


@dataclass
class WorkflowDeadline:
    """Monotonic total-workflow deadline bookkeeping (REQ-010).

    Anchored at construction time. ``clock`` is injectable for deterministic
    tests and defaults to :func:`time.monotonic` (durations never come from
    wall-clock time). The runner checks :meth:`exceeded` before each step and
    clamps every step's timeout with :meth:`clamp` so the deadline is also
    enforced *around* the active step: a deadline crossing mid-step surfaces
    as that step's timeout path.
    """

    timeout_seconds: float
    clock: Callable[[], float] = time.monotonic
    _started_at: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._started_at = self.clock()

    def elapsed_seconds(self) -> float:
        return max(self.clock() - self._started_at, 0.0)

    def remaining_seconds(self) -> float:
        return max(self.timeout_seconds - self.elapsed_seconds(), 0.0)

    def exceeded(self) -> bool:
        return self.remaining_seconds() <= 0.0

    def clamp(self, step_timeout_seconds: float) -> float:
        """Effective timeout for the next step: never past the deadline."""
        return min(step_timeout_seconds, self.remaining_seconds())
