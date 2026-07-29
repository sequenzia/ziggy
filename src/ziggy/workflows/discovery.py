"""Workflow discovery and name/path resolution (REQ-009).

Search order: ``<workspace>/.ziggy/workflows/*.{yaml,yml}`` (project scope),
then ``$ZIGGY_HOME/workflows/`` (user scope, default ``~/.ziggy/workflows``).
A workflow's identity is its YAML ``name`` field, which ``load_workflow``
already forces to equal the filename stem.

Duplicate names — across scopes or within one scope (``foo.yaml`` next to
``foo.yml``) — are a :class:`~ziggy.errors.ValidationError` naming both
paths. The only bypass is invoking a workflow by **direct path**, which never
consults discovery but must canonicalize (fail-closed, via
:func:`ziggy.policy.paths.resolve_contained`) inside the invocation workspace
OR inside the user workflows directory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ziggy.errors import ValidationError
from ziggy.models.workflow import WorkflowDef
from ziggy.policy.paths import PathAmbiguity, resolve_contained
from ziggy.store.runstore import ziggy_home
from ziggy.workflows.schema import load_workflow

#: ``source_scope`` values (stable strings, recorded in results).
SCOPE_PROJECT = "project"
SCOPE_USER = "user"

_WORKFLOW_SUFFIXES = (".yaml", ".yml")
#: A bare workflow name (same shape as ``WorkflowDef.name``); anything that
#: does not match — separators, dots, extensions — is treated as a direct path.
_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")


@dataclass(frozen=True, slots=True)
class DiscoveredWorkflow:
    """One resolved workflow: identity, origin, and validated definition."""

    name: str
    path: Path
    source_scope: str  # SCOPE_PROJECT | SCOPE_USER
    definition: WorkflowDef


def user_workflows_dir() -> Path:
    """User-scope workflow search path (``$ZIGGY_HOME``-aware)."""
    return ziggy_home() / "workflows"


def project_workflows_dir(workspace: Path) -> Path:
    """Project-scope workflow search path inside the invocation workspace."""
    return workspace / ".ziggy" / "workflows"


def _workflow_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        entry
        for entry in directory.iterdir()
        if entry.is_file() and entry.suffix in _WORKFLOW_SUFFIXES
    )


def discover(workspace: Path) -> dict[str, DiscoveredWorkflow]:
    """All discoverable workflows by name; duplicates fail with both paths.

    Every file found is fully validated (``load_workflow``), so a broken
    workflow anywhere on the search path surfaces here, before execution.
    """
    found: dict[str, DiscoveredWorkflow] = {}
    scopes = (
        (SCOPE_PROJECT, project_workflows_dir(workspace)),
        (SCOPE_USER, user_workflows_dir()),
    )
    for scope, directory in scopes:
        for file in _workflow_files(directory):
            definition = load_workflow(file)
            existing = found.get(definition.name)
            if existing is not None:
                raise ValidationError(
                    f"duplicate workflow name {definition.name!r}: "
                    f"{existing.path} and {file} (use a direct path to "
                    "disambiguate)"
                )
            found[definition.name] = DiscoveredWorkflow(
                name=definition.name,
                path=file,
                source_scope=scope,
                definition=definition,
            )
    return found


def resolve(name_or_path: str, workspace: Path) -> DiscoveredWorkflow:
    """Resolve a workflow reference: bare name via discovery, else direct path.

    A bare name consults :func:`discover` (duplicate names error there). A
    direct path bypasses discovery but must canonicalize inside the invocation
    workspace or inside the user workflows directory; anything else —
    escapes, symlink tricks, unrelated absolute paths — is rejected.
    """
    if _NAME_RE.match(name_or_path):
        discovered = discover(workspace)
        workflow = discovered.get(name_or_path)
        if workflow is None:
            raise ValidationError(
                f"workflow {name_or_path!r} not found in "
                f"{project_workflows_dir(workspace)} or {user_workflows_dir()}"
            )
        return workflow
    return _resolve_direct_path(name_or_path, workspace)


def _resolve_direct_path(raw: str, workspace: Path) -> DiscoveredWorkflow:
    candidate = Path(raw).expanduser()
    bases = (
        (SCOPE_PROJECT, workspace),
        (SCOPE_USER, user_workflows_dir()),
    )
    contained: list[tuple[str, Path]] = []
    for scope, base in bases:
        try:
            contained.append((scope, resolve_contained(base, candidate)))
        except PathAmbiguity:
            continue
    if not contained:
        raise ValidationError(
            f"workflow path {raw!r} must resolve canonically inside the "
            f"workspace ({workspace}) or the user workflows directory "
            f"({user_workflows_dir()})"
        )
    for scope, resolved in contained:
        if resolved.is_file():
            definition = load_workflow(resolved)
            return DiscoveredWorkflow(
                name=definition.name,
                path=resolved,
                source_scope=scope,
                definition=definition,
            )
    raise ValidationError(f"workflow file not found: {contained[0][1]}")
