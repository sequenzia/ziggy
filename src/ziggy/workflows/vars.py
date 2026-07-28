"""Typed workflow variable resolution and the secret-allowance gate (REQ-009/011).

``--var k=v`` values arrive as strings and are parsed strictly per the
declared :class:`~ziggy.models.workflow.VariableDef` type: ``string``
verbatim, ``integer``/``boolean`` strict decimal / ``true``|``false``,
``json`` via ``json.loads``. Unknown names, missing required variables, type
parse failures, default/type mismatches, and encoded-size (UTF-8 bytes over
``max_bytes``) violations are all :class:`~ziggy.errors.ValidationError`
before anything executes.

Secret variables (REQ-011): interpolating a secret variable into a step's
prompt requires a trusted user-scope
``workflows.secret_variable_allowances = { <var> = ["<provider>", ...] }``
entry covering the destination step's provider — otherwise
:func:`check_secret_allowances` fails before execution. Secret values also
register as exact-match redaction values (:func:`secret_redaction_values`) so
they are redacted from persisted artifacts such as ``inputs_resolved``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ziggy.errors import ValidationError
from ziggy.models.workflow import VariableDef, WorkflowDef
from ziggy.workflows.interpolate import value_to_text, vars_referenced_by_step

_INTEGER_RE = re.compile(r"^[+-]?[0-9]+$")
_BOOLEANS = {"true": True, "false": False}


def parse_var_args(pairs: Sequence[str]) -> dict[str, str]:
    """Split raw ``--var name=value`` arguments into a name -> raw-value map.

    The value keeps everything after the first ``=`` verbatim. Missing ``=``,
    empty names, and repeated names are usage errors.
    """
    out: dict[str, str] = {}
    for pair in pairs:
        name, sep, raw = pair.partition("=")
        if not sep or not name:
            raise ValidationError(f"--var {pair!r}: expected <name>=<value>")
        if name in out:
            raise ValidationError(f"--var {name!r}: variable given more than once")
        out[name] = raw
    return out


def validate_variables(workflow: WorkflowDef, cli_vars: dict[str, str]) -> dict[str, Any]:
    """Resolve CLI values + defaults into typed values for interpolation.

    Returns a mapping of variable name to typed value (``str``/``int``/
    ``bool``/parsed JSON). Optional variables with neither a value nor a
    default are absent from the result. All problems are collected and raised
    together as one :class:`ValidationError`.
    """
    problems: list[str] = []
    unknown = sorted(set(cli_vars) - set(workflow.variables))
    for name in unknown:
        problems.append(f"--var {name}: unknown variable (not declared by the workflow)")

    values: dict[str, Any] = {}
    for name, decl in workflow.variables.items():
        if name in cli_vars:
            raw = cli_vars[name]
            raw_bytes = len(raw.encode("utf-8"))
            if raw_bytes > decl.max_bytes:
                problems.append(
                    f"variables.{name}: value is {raw_bytes} bytes "
                    f"(UTF-8); max_bytes is {decl.max_bytes}"
                )
                continue
            try:
                values[name] = _parse_typed(name, decl, raw)
            except ValidationError as exc:
                problems.append(exc.message)
        elif decl.default is not None:
            try:
                values[name] = _check_default(name, decl)
            except ValidationError as exc:
                problems.append(exc.message)
        elif decl.required:
            problems.append(f"variables.{name}: required variable not provided (--var {name}=...)")
    if problems:
        raise ValidationError("; ".join(problems))
    return values


def _parse_typed(name: str, decl: VariableDef, raw: str) -> Any:
    if decl.type == "string":
        return raw
    if decl.type == "integer":
        if not _INTEGER_RE.match(raw):
            raise ValidationError(f"variables.{name}: expected an integer, got a non-integer value")
        return int(raw)
    if decl.type == "boolean":
        if raw not in _BOOLEANS:
            raise ValidationError(f"variables.{name}: expected 'true' or 'false'")
        return _BOOLEANS[raw]
    # json
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"variables.{name}: invalid JSON: {exc}") from None


def _check_default(name: str, decl: VariableDef) -> Any:
    default = decl.default
    ok = (
        (decl.type == "string" and isinstance(default, str))
        or (decl.type == "integer" and isinstance(default, int) and not isinstance(default, bool))
        or (decl.type == "boolean" and isinstance(default, bool))
        or decl.type == "json"
    )
    if not ok:
        raise ValidationError(
            f"variables.{name}: default value does not match declared type '{decl.type}'"
        )
    default_bytes = len(value_to_text(default).encode("utf-8"))
    if default_bytes > decl.max_bytes:
        raise ValidationError(
            f"variables.{name}: default is {default_bytes} bytes (UTF-8); "
            f"max_bytes is {decl.max_bytes}"
        )
    return default


def check_secret_allowances(
    workflow: WorkflowDef,
    *,
    step_providers: Mapping[str, str],
    allowances: Mapping[str, Sequence[str]],
) -> None:
    """Gate secret-variable interpolation per destination provider (REQ-011).

    ``step_providers`` maps every step id to its provider identity;
    ``allowances`` is trusted user-scope
    ``workflows.secret_variable_allowances``. Any secret variable flowing
    into a step whose provider is not explicitly allowed for that variable
    fails before execution. Fails closed on a missing provider mapping.
    """
    problems: list[str] = []
    for step_id, step in workflow.steps.items():
        secret_names = sorted(
            name
            for name in vars_referenced_by_step(step)
            if name in workflow.variables and workflow.variables[name].secret
        )
        if not secret_names:
            continue
        provider = step_providers.get(step_id)
        for name in secret_names:
            if provider is None:
                problems.append(
                    f"steps.{step_id}: secret variable {name!r} flows to a step "
                    "with no resolved provider identity"
                )
            elif provider not in allowances.get(name, ()):
                problems.append(
                    f"steps.{step_id}: secret variable {name!r} is not allowed "
                    f"for provider {provider!r} (add workflows."
                    f'secret_variable_allowances.{name} = ["{provider}"] to '
                    "trusted user config)"
                )
    if problems:
        raise ValidationError("; ".join(problems))


def secret_redaction_values(
    workflow: WorkflowDef, values: Mapping[str, Any]
) -> list[tuple[str, str]]:
    """Exact-match ``(kind, value)`` redaction pairs for resolved secret vars.

    Feed these into the run's :class:`~ziggy.redact.Redactor` so secret
    variable values are redacted from every persisted artifact, including
    ``inputs_resolved``.
    """
    pairs: list[tuple[str, str]] = []
    for name in sorted(workflow.variables):
        if workflow.variables[name].secret and name in values:
            text = value_to_text(values[name])
            if text:
                pairs.append((f"var:{name}", text))
    return pairs
