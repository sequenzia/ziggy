"""Workflow YAML loading and validation (REQ-009).

``load_workflow`` is the single entry point from a file path to a validated
:class:`~ziggy.models.workflow.WorkflowDef`:

- ``yaml.safe_load`` ONLY — anchors/aliases resolve, arbitrary-object tags do
  not construct anything and fail as parse errors. A construction-free
  ``yaml.compose`` pass additionally rejects duplicate mapping keys (YAML
  silently keeps the last, which would let ``steps: {a: ..., a: ...}`` shadow
  an earlier definition).
- Every failure is a :class:`~ziggy.errors.ValidationError` carrying the file
  and the dotted key path (path-precise, before any step runs).
- A step ``type`` other than ``agent`` (script/shell/python/...) produces the
  specific deferred-post-MVP message, not an anonymous enum error.
- The workflow ``name`` must match the filename stem (discovery identity).
- Template validation (:func:`~ziggy.workflows.interpolate.validate_templates`)
  runs at load so unsupported syntax and undeclared references never reach
  execution.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError as PydanticValidationError

from ziggy.errors import ValidationError
from ziggy.models.workflow import WorkflowDef
from ziggy.workflows.interpolate import validate_templates


def load_workflow(path: Path | str) -> WorkflowDef:
    """Load and fully validate one workflow YAML file.

    Raises :class:`ValidationError` (exit 2) with ``workflow <path>: <key
    path>: <message>`` precision for every violation; never raises raw
    pydantic/yaml errors.

    This is READ + delegate: :func:`load_workflow_bytes` owns the parse body so
    a caller that must hash the file first can parse the EXACT bytes it hashed
    (no second, unverified re-open — the trusted-workflow TOCTOU guard).
    """
    file = Path(path)
    try:
        content = file.read_bytes()
    except OSError as exc:
        raise ValidationError(f"workflow {file}: unreadable: {exc}") from exc
    return load_workflow_bytes(content, file)


def load_workflow_bytes(content: bytes, display_path: Path | str) -> WorkflowDef:
    """Load and fully validate a workflow definition from ALREADY-READ bytes.

    ``display_path`` supplies the filename identity (the ``name`` must match its
    stem) and error provenance. Parsing the caller-supplied bytes — rather than
    re-opening ``display_path`` — is what lets a trusted-workflow re-verification
    hash and execute the very same bytes (see
    ``ziggy.orchestrator.execute._reverify_trusted_workflow``).
    """
    file = Path(display_path)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"workflow {file}: unreadable: {exc}") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValidationError(f"workflow {file}: YAML parse error: {exc}") from exc

    if not isinstance(data, dict):
        raise ValidationError(
            f"workflow {file}: top-level YAML must be a mapping (got {type(data).__name__})"
        )

    _reject_duplicate_mapping_keys(text, file)
    _reject_non_agent_step_types(data, file)

    try:
        workflow = WorkflowDef.model_validate(data)
    except PydanticValidationError as exc:
        raise _to_validation_error(exc, file) from exc

    if workflow.name != file.stem:
        raise ValidationError(
            f"workflow {file}: name: {workflow.name!r} must match the filename stem {file.stem!r}"
        )

    try:
        validate_templates(workflow)
    except ValidationError as exc:
        raise ValidationError(f"workflow {file}: {exc.message}", details=exc.details) from exc

    return workflow


def _reject_duplicate_mapping_keys(text: str, file: Path) -> None:
    """Reject YAML mappings that declare the same key twice (REQ-009).

    ``yaml.safe_load`` silently keeps the LAST value for a duplicated key, so
    duplicate step ids (or variables, or inputs) would shadow each other
    without this construction-free ``yaml.compose`` pass over the node graph.
    Aliases share node objects, so visited nodes are tracked by identity to
    stay linear (and terminate) even for recursive anchors.
    """
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:  # pragma: no cover - safe_load already parsed it
        return
    problems: list[str] = []
    visited: set[int] = set()

    def walk(node: yaml.Node | None, path: str) -> None:
        if node is None or id(node) in visited:
            return
        visited.add(id(node))
        if isinstance(node, yaml.MappingNode):
            seen: dict[str, int] = {}
            for key_node, value_node in node.value:
                key = key_node.value if isinstance(key_node, yaml.ScalarNode) else None
                if not isinstance(key, str):
                    walk(value_node, path)
                    continue
                key_path = f"{path}.{key}" if path else key
                line = key_node.start_mark.line + 1
                if key in seen:
                    problems.append(
                        f"{key_path}: duplicate mapping key (lines "
                        f"{seen[key]} and {line}; YAML would silently keep "
                        "the last value)"
                    )
                else:
                    seen[key] = line
                walk(value_node, key_path)
        elif isinstance(node, yaml.SequenceNode):
            for index, item in enumerate(node.value):
                walk(item, f"{path}[{index}]")

    walk(root, "")
    if problems:
        raise ValidationError(f"workflow {file}: " + "; ".join(problems))


def _reject_non_agent_step_types(data: dict[str, Any], file: Path) -> None:
    """Precise rejection of script/shell/python/... step types (REQ-009).

    Runs on the raw mapping BEFORE pydantic so the user sees the deliberate
    schema-v1 deferral message instead of an anonymous literal-value error.
    """
    steps = data.get("steps")
    if not isinstance(steps, dict):
        return
    for step_id, step in steps.items():
        if not isinstance(step, dict):
            continue
        step_type = step.get("type", "agent")
        if step_type != "agent":
            raise ValidationError(
                f"workflow {file}: steps.{step_id}.type: step type "
                f"'{step_type}' is not supported in schema version 1 "
                "(deferred post-MVP)"
            )


def _to_validation_error(exc: PydanticValidationError, file: Path) -> ValidationError:
    """Path-precise ValidationError; input values are intentionally not echoed."""
    items: list[str] = []
    for err in exc.errors(include_url=False, include_context=False, include_input=False):
        key_path = ".".join(str(part) for part in err["loc"]) or "<root>"
        if err["type"] == "extra_forbidden":
            message = "unknown key"
        elif key_path == "version":
            message = "schema version must be the literal 1"
        else:
            message = err["msg"]
        items.append(f"{key_path}: {message}")
    return ValidationError(f"workflow {file}: " + "; ".join(items))
