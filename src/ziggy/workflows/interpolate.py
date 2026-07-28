"""Restricted value-only prompt templating (REQ-009/010).

The only template tokens that exist are ``{{ vars.<name> }}`` and
``{{ inputs.<name> }}`` (:data:`TOKEN_RE`). There are no expressions, filters,
loops, includes, attribute access, or function calls; anything else
brace-shaped is a :class:`~ziggy.errors.ValidationError` at workflow
validation time, never silently passed through.

Validation and rendering are split deliberately:

- :func:`validate_template` / :func:`validate_templates` run at **load** time
  and reject undeclared references and unsupported syntax.
- :func:`render` / :func:`render_prompt` run at **run** time and are pure
  string substitution in a single pass over the template — substituted values
  are NEVER re-scanned for tokens, so agent output containing ``{{ vars.x }}``
  lands literally in the downstream prompt (template injection is inert).

Step-output input values are model output and therefore untrusted: they are
wrapped in the deterministic :func:`wrap_untrusted` delimiters. Variable
values are user-provided and inserted verbatim.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterator, Mapping
from typing import Any

from ziggy.errors import ValidationError
from ziggy.models.workflow import StepDef, WorkflowDef

#: The complete restricted template grammar. Nothing else is a token.
TOKEN_RE: re.Pattern[str] = re.compile(r"\{\{\s*(vars|inputs)\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

#: Brace-shaped syntax that survives token removal is a violation. A lone
#: ``}}`` is NOT matched: it is legitimate in literal text such as JSON.
_SUSPECT_RE: re.Pattern[str] = re.compile(r"\{\{|\{%|%\}")

_UNTRUSTED_OPEN = '<<<ziggy:untrusted-input name="{name}" source="{source}">>>'
_UNTRUSTED_CLOSE = '<<<ziggy:end-untrusted-input name="{name}">>>'

#: Prefix of an input source that refers to another step's output.
_STEP_SOURCE_PREFIX = "steps."
_VAR_SOURCE_PREFIX = "vars."


def wrap_untrusted(name: str, source: str, value: str) -> str:
    """Wrap one step-output value in the deterministic untrusted delimiters."""
    return (
        _UNTRUSTED_OPEN.format(name=name, source=source)
        + "\n"
        + value
        + "\n"
        + _UNTRUSTED_CLOSE.format(name=name)
    )


def template_references(prompt: str) -> Iterator[tuple[str, str]]:
    """Yield ``(namespace, name)`` for every valid token in ``prompt``."""
    for match in TOKEN_RE.finditer(prompt):
        yield match.group(1), match.group(2)


def vars_referenced_by_step(step: StepDef) -> frozenset[str]:
    """Variable names whose values reach ``step``'s rendered prompt.

    Covers direct ``{{ vars.<name> }}`` tokens plus ``{{ inputs.<x> }}``
    tokens whose declared source is ``vars.<name>``.
    """
    names: set[str] = set()
    for namespace, name in template_references(step.prompt):
        if namespace == "vars":
            names.add(name)
        else:
            source = step.inputs.get(name)
            if source is not None and source.startswith(_VAR_SOURCE_PREFIX):
                names.add(source.split(".", 1)[1])
    return frozenset(names)


def validate_template(
    prompt: str,
    declared_vars: Collection[str],
    declared_inputs: Collection[str],
    *,
    where: str = "prompt",
) -> None:
    """Reject unsupported template syntax and undeclared references.

    ``where`` is the key path prefixed to every message (e.g.
    ``steps.plan.prompt``). All violations in the template are collected and
    reported together.
    """
    problems: list[str] = []
    scrubbed = list(prompt)
    for match in TOKEN_RE.finditer(prompt):
        namespace, name = match.group(1), match.group(2)
        if namespace == "vars" and name not in declared_vars:
            problems.append(f"{where}: undeclared variable 'vars.{name}' referenced")
        elif namespace == "inputs" and name not in declared_inputs:
            problems.append(f"{where}: undeclared input 'inputs.{name}' referenced")
        for index in range(match.start(), match.end()):
            scrubbed[index] = " "
    remainder = "".join(scrubbed)
    for match in _SUSPECT_RE.finditer(remainder):
        snippet = prompt[match.start() : match.start() + 32].splitlines()[0]
        problems.append(
            f"{where}: unsupported template syntax near {snippet!r} "
            "(only '{{ vars.<name> }}' and '{{ inputs.<name> }}' are allowed; "
            "no expressions, filters, loops, includes, attribute access, or "
            "function calls)"
        )
    if problems:
        raise ValidationError("; ".join(problems))


def validate_templates(workflow: WorkflowDef) -> None:
    """Validate every step prompt of ``workflow`` (called at load time).

    Also rejects step inputs whose declared source references an undeclared
    variable (``inputs: {x: vars.<missing>}``).
    """
    problems: list[str] = []
    for step_id, step in workflow.steps.items():
        try:
            validate_template(
                step.prompt,
                workflow.variables.keys(),
                step.inputs.keys(),
                where=f"steps.{step_id}.prompt",
            )
        except ValidationError as exc:
            problems.append(exc.message)
        for local_name, source in step.inputs.items():
            if source.startswith(_VAR_SOURCE_PREFIX):
                var_name = source.split(".", 1)[1]
                if var_name not in workflow.variables:
                    problems.append(
                        f"steps.{step_id}.inputs.{local_name}: references "
                        f"undeclared variable {var_name!r}"
                    )
    if problems:
        raise ValidationError("; ".join(problems))


def value_to_text(value: Any) -> str:
    """Canonical prompt-insertion text for a typed variable value.

    Strings verbatim; booleans as ``true``/``false``; integers in decimal;
    json values as compact-ish JSON (round-trips through ``json.loads``).
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def render(prompt: str, vars: Mapping[str, str], inputs: Mapping[str, str]) -> str:
    """Pure single-pass string substitution of validated tokens.

    Values are inserted exactly once and never re-scanned for tokens. A
    referenced name without a value fails closed (:class:`ValidationError`) —
    load-time validation plus variable resolution should make that unreachable.
    """

    def _substitute(match: re.Match[str]) -> str:
        namespace, name = match.group(1), match.group(2)
        mapping = vars if namespace == "vars" else inputs
        if name not in mapping:
            raise ValidationError(f"no value available for template reference '{namespace}.{name}'")
        return mapping[name]

    return TOKEN_RE.sub(_substitute, prompt)


def render_prompt(step: StepDef, vars: Mapping[str, Any], inputs: Mapping[str, str]) -> str:
    """Compose one step's final prompt from typed vars and resolved inputs.

    ``vars`` holds typed variable values (output of ``validate_variables``);
    ``inputs`` holds raw upstream text keyed by the step's local input names.
    Step-output inputs are wrapped in untrusted delimiters; variable values
    (including inputs whose source is ``vars.<name>``) are inserted verbatim.
    """
    var_texts = {name: value_to_text(value) for name, value in vars.items()}
    input_texts: dict[str, str] = {}
    for local_name, source in step.inputs.items():
        if source.startswith(_VAR_SOURCE_PREFIX):
            var_name = source.split(".", 1)[1]
            if var_name not in var_texts:
                raise ValidationError(f"input {local_name!r}: no value for source {source!r}")
            input_texts[local_name] = var_texts[var_name]
        else:
            if local_name not in inputs:
                raise ValidationError(
                    f"input {local_name!r}: no resolved value for source {source!r}"
                )
            input_texts[local_name] = wrap_untrusted(local_name, source, inputs[local_name])
    return render(step.prompt, var_texts, input_texts)
