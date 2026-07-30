"""Config loading (REQ-007): scopes, env overrides, monotonic merge, provenance.

Scopes and precedence (field-specific, never blanket):

- **user** — ``$ZIGGY_HOME/config.toml`` (default ``~/.ziggy/config.toml``),
  trusted. ``ZIGGY_<SECTION>__<KEY>`` environment variables are user-scope
  overrides applied over the user file.
- **project** — ``<workspace>/.ziggy/config.toml``, untrusted. May only
  select workflow defaults, tighten ceilings/capture, and add permission
  denials. Everything else present in project scope is a ``ConfigError``
  raised before any project value is applied (fail closed).

Every effective leaf field carries provenance ``{source, project_action}``;
the resolved config exposes a stable sha256 fingerprint over the canonical
JSON of the (non-secret) config plus provenance for RunResult embedding.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
import types
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, get_args, get_origin

from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError

from ziggy.agents.builtins import BUILTIN_AGENTS
from ziggy.config.schema import ZiggyConfig
from ziggy.errors import ConfigError
from ziggy.models.common import CaptureProfile, ConfigSource, ProjectAction
from ziggy.redact import CustomPattern, Redactor

#: Agent names whose launch defaults ship pinned in ``agents/builtins.py``;
#: only these may omit ``command`` in config. Derived from the single source of
#: truth so adding a built-in cannot drift the two lists apart.
BUILTIN_AGENT_NAMES: frozenset[str] = frozenset(BUILTIN_AGENTS)

_AGENT_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
#: ``api_key_env`` must be an env var NAME, never a value.
_ENV_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
#: ``ZIGGY_<SECTION>__<KEY>`` (``ZIGGY_HOME`` etc. have no ``__`` and never match).
_ENV_OVERRIDE_RE = re.compile(r"^ZIGGY_([A-Z0-9]+)__([A-Z0-9_]+)$")


class MergeRule(StrEnum):
    """Per-field monotonic merge rule (docs/design/phase2-contracts.md)."""

    USER_ONLY = "user_only"
    TIGHTEN_MIN = "tighten_min"
    TIGHTEN_CAPTURE = "tighten_capture"
    PROJECT_DENIALS = "project_denials"
    PROJECT_OK = "project_ok"


_TIGHTEN_MIN_PATHS: frozenset[str] = frozenset(
    {
        "engine.max_workflow_steps",
        "engine.max_prompt_bytes",
        "engine.default_step_timeout_seconds",
        "engine.default_workflow_timeout_seconds",
        "engine.max_event_bytes_per_step",
        "engine.max_artifact_bytes_per_run",
        # NOTE: results.retention_days is deliberately NOT here. It is a
        # deletion window, not a security ceiling: letting an untrusted project
        # config "tighten" it to a smaller value would destroy audit evidence
        # sooner ('ziggy runs prune' would delete history the user meant to
        # keep). The fail-closed default below keeps it USER_ONLY, so project
        # scope is rejected outright rather than silently lowering it.
    }
)
_CAPTURE_PATH = "results.capture"
_DENIALS_PATH = "permissions.project_denials"
_PROJECT_OK_PATHS: frozenset[str] = frozenset({"workflows.default_name"})
# NOTE: workflows.secret_variable_allowances.* is deliberately unlisted — the
# fail-closed default below keeps it USER_ONLY (project scope rejected).


def merge_rule_for(path: str) -> MergeRule:
    """Merge rule for a leaf field path. Anything unlisted is USER_ONLY (fail closed)."""
    if path in _TIGHTEN_MIN_PATHS:
        return MergeRule.TIGHTEN_MIN
    if path == _CAPTURE_PATH:
        return MergeRule.TIGHTEN_CAPTURE
    if path == _DENIALS_PATH:
        return MergeRule.PROJECT_DENIALS
    if path in _PROJECT_OK_PATHS:
        return MergeRule.PROJECT_OK
    return MergeRule.USER_ONLY


class ProvenanceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: ConfigSource
    project_action: ProjectAction = ProjectAction.NONE


@dataclass
class ResolvedConfig:
    """Effective merged config + per-field provenance + stable fingerprint."""

    config: ZiggyConfig
    provenance: dict[str, ProvenanceEntry]
    fingerprint: str
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> list[dict[str, Any]]:
        """Rows for ``ziggy config show``: path, value, source, project_action."""
        leaves = _dump_leaves(self.config.model_dump(mode="json"))
        return [
            {
                "path": path,
                "value": leaves.get(path),
                "source": self.provenance[path].source.value,
                "project_action": self.provenance[path].project_action.value,
            }
            for path in sorted(self.provenance)
        ]


def load_config(
    workspace: Path | None,
    *,
    user_path: Path | None = None,
    project_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> ResolvedConfig:
    """Load, merge, and validate user + project config into a ResolvedConfig.

    Both files are optional (schema defaults apply). Any violation —
    unknown keys, parse errors, literal secrets, forbidden project keys,
    project attempts to raise a ceiling — raises :class:`ConfigError` with
    the TOML path and file provenance in the message.
    """
    env_map: Mapping[str, str] = os.environ if env is None else env
    user_file = user_path if user_path is not None else _default_user_path(env_map)
    if project_path is not None:
        project_file: Path | None = project_path
    elif workspace is not None:
        project_file = workspace / ".ziggy" / "config.toml"
    else:
        project_file = None

    user_raw = _parse_toml(user_file, "user") if user_file and user_file.is_file() else None
    project_raw = (
        _parse_toml(project_file, "project") if project_file and project_file.is_file() else None
    )

    _reject_user_scope_denials(user_raw, user_file)
    overlay, env_by_path = _env_overrides(env_map)
    _reject_secret_literals(user_raw, user_file, overlay, env_by_path, project_raw, project_file)

    user_desc = f"user config {user_file}" if user_raw is not None else "user-scope configuration"
    base = user_raw if user_raw is not None else {"schema_version": 1}
    merged_user = _deep_merge(base, overlay)
    effective = _validate(merged_user, user_desc, env_by_path)

    final_dict = effective.model_dump(mode="json")
    actions: dict[str, ProjectAction] = {}
    warnings: list[str] = []
    if project_raw is not None:
        assert project_file is not None
        _apply_project(project_raw, project_file, final_dict, actions, warnings)

    final = _validate(final_dict, "merged configuration", env_by_path)
    _semantic_checks(final, user_desc)

    provenance = _build_provenance(final, user_raw, env_by_path, actions)
    fingerprint = _fingerprint(final, provenance)
    return ResolvedConfig(
        config=final, provenance=provenance, fingerprint=fingerprint, warnings=warnings
    )


# ---------------------------------------------------------------------------
# scope resolution + parsing


def _default_user_path(env_map: Mapping[str, str]) -> Path:
    home = env_map.get("ZIGGY_HOME")
    root = Path(home).expanduser() if home else Path.home() / ".ziggy"
    return root / "config.toml"


def _parse_toml(path: Path, scope: str) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{scope} config {path}: TOML parse error: {exc}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{scope} config {path}: unreadable: {exc}") from exc


def _reject_user_scope_denials(user_raw: dict[str, Any] | None, user_file: Path | None) -> None:
    permissions = (user_raw or {}).get("permissions")
    if isinstance(permissions, dict) and "project_denials" in permissions:
        raise ConfigError(
            f"user config {user_file}: permissions.project_denials is allowed only in "
            "project scope (<workspace>/.ziggy/config.toml)"
        )


# ---------------------------------------------------------------------------
# environment overrides (user scope)


def _env_overrides(
    env_map: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Overlay dict from ``ZIGGY_<SECTION>__<KEY>`` vars + leaf-path -> var name."""
    overlay: dict[str, Any] = {}
    by_path: dict[str, str] = {}
    for name in sorted(env_map):
        match = _ENV_OVERRIDE_RE.match(name)
        if not match:
            continue
        section, key = match.group(1).lower(), match.group(2).lower()
        section_field = ZiggyConfig.model_fields.get(section)
        if section_field is None:
            raise ConfigError(f"{name}: unknown config section '{section}'")
        section_ann = _strip_optional(section_field.annotation)
        if not (isinstance(section_ann, type) and issubclass(section_ann, BaseModel)):
            raise ConfigError(
                f"{name}: section '{section}' cannot be set via environment overrides "
                "(lists/tables are unsupported)"
            )
        key_field = section_ann.model_fields.get(key)
        if key_field is None:
            raise ConfigError(f"{name}: unknown config key '{section}.{key}'")
        value = _coerce_env(name, key_field.annotation, env_map[name])
        overlay.setdefault(section, {})[key] = value
        by_path[f"{section}.{key}"] = name
    return overlay, by_path


def _coerce_env(name: str, annotation: Any, raw: str) -> Any:
    """Coerce an env override string to the target field type; fail typed.

    Error messages never echo the raw value (it could be secret-shaped).
    """
    ann = _strip_optional(annotation)
    origin = get_origin(ann)
    if (
        origin in (list, dict)
        or ann in (list, dict)
        or (isinstance(ann, type) and issubclass(ann, BaseModel))
    ):
        raise ConfigError(f"{name}: list/table values cannot be set via environment overrides")
    if ann is bool:
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ConfigError(f"{name}: expected a boolean (true/false)")
    if ann is int:
        try:
            return int(raw.strip())
        except ValueError:
            raise ConfigError(f"{name}: expected an integer") from None
    if ann is float:
        try:
            return float(raw.strip())
        except ValueError:
            raise ConfigError(f"{name}: expected a number") from None
    if isinstance(ann, type) and issubclass(ann, StrEnum):
        try:
            return ann(raw.strip())
        except ValueError:
            allowed = ", ".join(member.value for member in ann)
            raise ConfigError(f"{name}: invalid value; expected one of: {allowed}") from None
    if ann is str:
        return raw
    raise ConfigError(f"{name}: unsupported value type for environment override")


def _strip_optional(annotation: Any) -> Any:
    if get_origin(annotation) is types.UnionType:
        non_none = [a for a in get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return annotation


# ---------------------------------------------------------------------------
# secret-literal scan (both scopes; never echoes values)


def _reject_secret_literals(
    user_raw: dict[str, Any] | None,
    user_file: Path | None,
    overlay: dict[str, Any],
    env_by_path: dict[str, str],
    project_raw: dict[str, Any] | None,
    project_file: Path | None,
) -> None:
    scanner = Redactor()
    hits: list[str] = []
    if user_raw is not None:
        _scan_value(user_raw, "", f"user config {user_file}", scanner, hits)
    for path, value in _leaves(overlay).items():
        label = f"environment override {env_by_path.get(path, path)}"
        _scan_value(value, path, label, scanner, hits)
    if project_raw is not None:
        _scan_value(project_raw, "", f"project config {project_file}", scanner, hits)
    if hits:
        raise ConfigError(
            "literal secret values are not allowed in config (reference env var names "
            "instead): " + "; ".join(hits)
        )


def _scan_value(value: Any, path: str, label: str, scanner: Redactor, hits: list[str]) -> None:
    if isinstance(value, str):
        _, counts = scanner.redact_text(value)
        if counts:
            kinds = ",".join(sorted(counts))
            hits.append(f"{label}: {path or '<root>'}: value matches secret pattern(s) [{kinds}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            _scan_value(item, child, label, scanner, hits)
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            _scan_value(item, f"{path}[{idx}]", label, scanner, hits)


# ---------------------------------------------------------------------------
# validation helpers


def _validate(data: dict[str, Any], source_desc: str, env_by_path: dict[str, str]) -> ZiggyConfig:
    try:
        return ZiggyConfig.model_validate(data)
    except PydanticValidationError as exc:
        raise _to_config_error(exc, source_desc, env_by_path) from exc


def _to_config_error(
    exc: PydanticValidationError, source_desc: str, env_by_path: dict[str, str]
) -> ConfigError:
    """Path-precise ConfigError; input values are intentionally not echoed."""
    items: list[str] = []
    for err in exc.errors(include_url=False, include_context=False, include_input=False):
        path = ".".join(str(part) for part in err["loc"]) or "<root>"
        if err["type"] == "extra_forbidden":
            message = "unknown configuration key"
        elif err["type"] == "missing" and path == "schema_version":
            message = "schema_version = 1 is required"
        else:
            message = err["msg"]
        origin = env_by_path.get(path)
        where = f"{path} (from {origin})" if origin else path
        items.append(f"{where}: {message}")
    return ConfigError(f"{source_desc}: " + "; ".join(items))


def _schema_allows(parts: tuple[str, ...]) -> bool:
    """Whether a dotted leaf path is representable in the schema at all."""
    node: Any = ZiggyConfig
    index = 0
    while index < len(parts):
        if isinstance(node, type) and issubclass(node, BaseModel):
            model_field = node.model_fields.get(parts[index])
            if model_field is None:
                return False
            node = _strip_optional(model_field.annotation)
            index += 1
            continue
        if get_origin(node) is dict:
            args = get_args(node)
            node = _strip_optional(args[1]) if len(args) == 2 else object
            index += 1  # any key name is allowed for dict fields
            continue
        return False  # scalar/list leaf but path parts remain
    return True


# ---------------------------------------------------------------------------
# project-scope merge (monotonic; fail closed)


def _apply_project(
    project_raw: dict[str, Any],
    project_file: Path,
    final_dict: dict[str, Any],
    actions: dict[str, ProjectAction],
    warnings: list[str],
) -> None:
    desc = f"project config {project_file}"
    if project_raw.get("schema_version") != 1:
        raise ConfigError(f"{desc}: schema_version = 1 is required")

    leaves = _leaves({k: v for k, v in project_raw.items() if k != "schema_version"})

    # Pass 1 — collect ALL unknown/forbidden paths; raise before applying anything.
    problems: list[str] = []
    for path in sorted(leaves):
        if not _schema_allows(tuple(path.split("."))):
            problems.append(f"{path}: unknown configuration key")
        elif merge_rule_for(path) is MergeRule.USER_ONLY:
            problems.append(f"{path}: forbidden in project scope (user-scope only)")
    if problems:
        raise ConfigError(f"{desc}: " + "; ".join(problems))

    # Structural/type validation of the (now known-allowed) project values.
    project_dump = _validate(project_raw, desc, {}).model_dump(mode="json")

    # Pass 2 — evaluate merges; collect ceiling violations, then apply or raise.
    ceiling_problems: list[str] = []
    for path in sorted(leaves):
        rule = merge_rule_for(path)
        value = _get_path(project_dump, path)
        if rule is MergeRule.TIGHTEN_MIN:
            eff = _get_path(final_dict, path)
            if value < eff:
                _set_path(final_dict, path, value)
                actions[path] = ProjectAction.TIGHTENED
            elif value == eff:
                actions[path] = ProjectAction.IGNORED
            else:
                ceiling_problems.append(
                    f"{path}: project value {value} may not raise the user-scope ceiling {eff}"
                )
        elif rule is MergeRule.TIGHTEN_CAPTURE:
            proj = CaptureProfile(value)
            eff_profile = CaptureProfile(_get_path(final_dict, path))
            if proj.rank() < eff_profile.rank():
                _set_path(final_dict, path, proj.value)
                actions[path] = ProjectAction.TIGHTENED
            elif proj.rank() == eff_profile.rank():
                actions[path] = ProjectAction.IGNORED
            else:
                actions[path] = ProjectAction.REJECTED
                warnings.append(
                    f"{desc}: results.capture={proj.value} rejected by user-scope "
                    f"ceiling ({eff_profile.value})"
                )
        elif rule in (MergeRule.PROJECT_DENIALS, MergeRule.PROJECT_OK):
            _set_path(final_dict, path, value)
            actions[path] = ProjectAction.APPLIED
    if ceiling_problems:
        raise ConfigError(f"{desc}: " + "; ".join(ceiling_problems))


# ---------------------------------------------------------------------------
# semantic (post-merge) checks — all USER_ONLY fields


def _semantic_checks(config: ZiggyConfig, user_desc: str) -> None:
    problems: list[str] = []
    for name, entry in config.agents.items():
        if not _AGENT_NAME_RE.match(name):
            problems.append(f"agents.{name}: invalid agent name")
        if entry.command is None and name not in BUILTIN_AGENT_NAMES:
            builtins = ", ".join(sorted(BUILTIN_AGENT_NAMES))
            problems.append(
                f"agents.{name}: 'command' is required for custom agents "
                f"(only builtins [{builtins}] may omit it)"
            )
        if entry.api_key_env is not None and not _ENV_VAR_NAME_RE.match(entry.api_key_env):
            problems.append(
                f"agents.{name}.api_key_env: must be an environment variable NAME "
                "matching ^[A-Z][A-Z0-9_]*$ (value not shown)"
            )
    permissions = config.permissions
    if (
        permissions.default_policy != "guarded"
        and permissions.default_policy not in permissions.profiles
    ):
        problems.append(
            f"permissions.default_policy: unknown policy profile '{permissions.default_policy}'"
        )
    known_agents = set(config.agents) | set(BUILTIN_AGENT_NAMES)
    orchestrator = config.orchestrator
    if orchestrator.agent is not None and orchestrator.agent not in known_agents:
        problems.append(f"orchestrator.agent: unknown agent '{orchestrator.agent}'")
    for agent_name in orchestrator.eligible_agents:
        if agent_name not in known_agents:
            problems.append(f"orchestrator.eligible_agents: unknown agent '{agent_name}'")
    if problems:
        raise ConfigError(f"{user_desc}: " + "; ".join(problems))

    try:
        Redactor(
            custom_patterns=[
                CustomPattern(kind=p.kind, regex=p.regex, max_width=p.max_width)
                for p in config.redaction.patterns
            ]
        )
    except (re.error, ValueError) as exc:
        raise ConfigError(
            f"{user_desc}: redaction.patterns: invalid custom pattern: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# provenance + fingerprint


def _build_provenance(
    config: ZiggyConfig,
    user_raw: dict[str, Any] | None,
    env_by_path: dict[str, str],
    actions: dict[str, ProjectAction],
) -> dict[str, ProvenanceEntry]:
    user_paths = set(_leaves(user_raw)) if user_raw is not None else set()
    project_applied = {
        path
        for path, action in actions.items()
        if action in (ProjectAction.APPLIED, ProjectAction.TIGHTENED)
    }
    provenance: dict[str, ProvenanceEntry] = {}
    for path in _dump_leaves(config.model_dump(mode="json")):
        if path in project_applied:
            source = ConfigSource.PROJECT
        elif path in env_by_path:
            source = ConfigSource.ENV
        elif path in user_paths:
            source = ConfigSource.USER
        else:
            source = ConfigSource.DEFAULT
        provenance[path] = ProvenanceEntry(
            source=source, project_action=actions.get(path, ProjectAction.NONE)
        )
    return provenance


def _fingerprint(config: ZiggyConfig, provenance: dict[str, ProvenanceEntry]) -> str:
    canonical = json.dumps(
        {
            "config": config.model_dump(mode="json"),
            "provenance": {
                path: {
                    "source": entry.source.value,
                    "project_action": entry.project_action.value,
                }
                for path, entry in provenance.items()
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# small tree utilities


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _leaves(tree: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Dotted leaf paths of a raw TOML tree; empty tables contribute nothing."""
    out: dict[str, Any] = {}
    for key, value in tree.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            out.update(_leaves(value, f"{path}."))
        elif isinstance(value, dict):
            continue
        else:
            out[path] = value
    return out


def _dump_leaves(tree: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Leaf paths of a model dump; empty dicts are kept as leaves."""
    out: dict[str, Any] = {}
    for key, value in tree.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            out.update(_dump_leaves(value, f"{path}."))
        else:
            out[path] = value
    return out


def _get_path(tree: dict[str, Any], path: str) -> Any:
    node: Any = tree
    for part in path.split("."):
        node = node[part]
    return node


def _set_path(tree: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = tree
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
