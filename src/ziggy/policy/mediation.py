"""Guarded mediation policy engine (REQ-008).

Pure decision surface: the engine's hooks call ``decide_*`` and get back a
:class:`Decision`; recording, wire replies, and actually serving requests stay
with the caller. Guarded semantics (spec §5.5):

- client-mediated **reads** auto-approved inside the canonical workspace;
- client-mediated **writes** auto-approved inside the canonical step working
  directory;
- client-mediated **terminal** execution denied unless a trusted USER-scope
  allowlist rule matches (exact ``argv[0]`` + args prefix);
- sensitive-path globs deny mediated access (reads *and* writes — spec §6.2
  words this as "access") even inside the allowed directories;
- project scope can only *add* deny rules (``project_denials``), checked
  before any allow rule; deny always wins; unknown request kinds fall through
  to ``unmatched-default-deny``.

``decide_*`` methods never raise on hostile input — every failure mode
becomes a deny :class:`Decision`. All decisions carry
``enforcement_scope=acp_mediated``: this is observable governance over
ACP-routed requests, never an OS sandbox (docs/phase0/trust-boundary.md).

Config/profile inputs are typed structurally (:class:`PolicyProfileLike`,
:class:`TerminalRuleLike`) matching the phase2 config contracts, and the
``ziggy.acp`` request types are annotation-only imports, keeping this module
free of runtime dependencies beyond ``models`` per the layering table.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from ziggy.models.common import EnforcementScope
from ziggy.models.result import PolicyProvenance
from ziggy.policy.paths import (
    DEFAULT_SENSITIVE_GLOBS,
    REASON_OUTSIDE_BASE,
    PathAmbiguity,
    glob_matches,
    resolve_contained,
)

if TYPE_CHECKING:
    from ziggy.acp.types import PermissionRequestN, TerminalRequestN

#: Name of the only builtin policy in v0.1.
GUARDED_POLICY_NAME = "guarded"

# Stable rule ids (recorded in events/RunResult; never rename).
RULE_READ_IN_WORKSPACE_ALLOW = "read-in-workspace-allow"
RULE_WRITE_IN_STEPDIR_ALLOW = "write-in-stepdir-allow"
RULE_TERMINAL_USER_ALLOWLIST = "terminal-user-allowlist"
RULE_SENSITIVE_PATH_DENY = "sensitive-path-deny"
RULE_OUTSIDE_WORKSPACE_DENY = "outside-workspace-deny"
RULE_OUTSIDE_STEPDIR_DENY = "outside-stepdir-deny"
RULE_TERMINAL_DEFAULT_DENY = "terminal-default-deny"
RULE_UNMATCHED_DEFAULT_DENY = "unmatched-default-deny"
RULE_PATH_AMBIGUITY_DENY = "path-ambiguity-deny"

# Stable policy_source values.
SOURCE_DEFAULT = "default"
SOURCE_USER = "user"
SOURCE_PROJECT_DENIAL = "project-denial"

#: ToolCallUpdate kinds that map to the fs-write decision.
_WRITE_KINDS = frozenset({"edit", "delete", "move"})


def project_denial_rule_id(index: int) -> str:
    """``project-denial:<n>`` — n is the rule's index in the denial list."""
    return f"project-denial:{index}"


class TerminalRuleLike(Protocol):
    """Structural shape of ``config.TerminalRule``: exact argv0 + args prefix."""

    command: str
    args_prefix: Sequence[str]


class PolicyProfileLike(Protocol):
    """Structural shape of ``config.PolicyProfile`` (USER scope only)."""

    terminal_allowlist: Sequence[TerminalRuleLike]
    deny_paths: Sequence[str]


class ProjectDenialLike(Protocol):
    """Structural shape of ``config.ProjectDenial`` (``args_prefix`` optional:
    a terminal denial without one denies every invocation of that argv0)."""

    kind: str
    pattern: str


@dataclass(frozen=True, slots=True)
class ProjectDenial:
    """One deny-only rule contributed by project scope.

    ``kind='path'``: ``pattern`` is a workspace-relative deny glob (same
    semantics as sensitive globs) applied to mediated reads and writes.
    ``kind='terminal'``: ``pattern`` is an exact ``argv[0]``; the rule denies
    any invocation whose args start with ``args_prefix`` (empty prefix denies
    every invocation of that command). Unknown kinds are validated away by the
    config layer and can never match here.
    """

    kind: Literal["path", "terminal"]
    pattern: str
    args_prefix: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Decision:
    """Outcome of one mediation decision (recorded verbatim)."""

    allowed: bool
    rule_id: str
    policy_name: str
    policy_source: str  # default | user | project-denial
    reason: str
    enforcement_scope: EnforcementScope = EnforcementScope.ACP_MEDIATED


class MediationPolicy:
    """Guarded mediation decisions for one run step.

    Build via :meth:`guarded`. Instances are immutable after construction and
    safe to share across requests of the same step.
    """

    def __init__(
        self,
        *,
        workspace: Path,
        step_dir: Path,
        policy_name: str,
        terminal_allowlist: tuple[tuple[str, tuple[str, ...]], ...],
        project_denials: tuple[ProjectDenial, ...],
        default_globs: tuple[str, ...],
        user_globs: tuple[str, ...],
        has_user_rules: bool,
    ) -> None:
        self._workspace = workspace
        self._step_dir = step_dir
        self._policy_name = policy_name
        self._terminal_allowlist = terminal_allowlist
        self._project_denials = project_denials
        self._default_globs = default_globs
        self._user_globs = user_globs
        self._has_user_rules = has_user_rules

    @classmethod
    def guarded(
        cls,
        *,
        workspace: Path,
        step_dir: Path,
        profile: PolicyProfileLike | None = None,
        project_denials: Iterable[ProjectDenial | ProjectDenialLike] = (),
        sensitive_globs: Iterable[str] = (),
        profile_name: str | None = None,
    ) -> MediationPolicy:
        """Build the guarded policy for one step.

        ``workspace``/``step_dir`` come from the trusted engine; ``step_dir``
        must be contained in ``workspace`` (a caller bug here raises
        :class:`PathAmbiguity` — fail closed, this input is not agent data).
        ``profile`` is a USER-scope profile (terminal allowlist + extra deny
        globs); ``profile_name`` names it for provenance (defaults to
        ``guarded``). ``sensitive_globs`` are extra USER-scope deny globs on
        top of :data:`DEFAULT_SENSITIVE_GLOBS`; ``project_denials`` are the
        project-scope deny-only additions.
        """
        resolved_workspace = Path(os.path.realpath(workspace))
        resolved_step_dir = resolve_contained(resolved_workspace, step_dir)
        user_globs = tuple(sensitive_globs)
        allowlist: tuple[tuple[str, tuple[str, ...]], ...] = ()
        if profile is not None:
            allowlist = tuple(
                (rule.command, tuple(rule.args_prefix)) for rule in profile.terminal_allowlist
            )
            user_globs = (*tuple(profile.deny_paths), *user_globs)
        return cls(
            workspace=resolved_workspace,
            step_dir=resolved_step_dir,
            policy_name=profile_name or GUARDED_POLICY_NAME,
            terminal_allowlist=allowlist,
            project_denials=tuple(_normalize_denial(denial) for denial in project_denials),
            default_globs=DEFAULT_SENSITIVE_GLOBS,
            user_globs=user_globs,
            has_user_rules=profile is not None or bool(user_globs),
        )

    # ------------------------------------------------------------ properties

    @property
    def policy_name(self) -> str:
        return self._policy_name

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def step_dir(self) -> Path:
        return self._step_dir

    @property
    def terminal_allowlist(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        return self._terminal_allowlist

    @property
    def project_denials(self) -> tuple[ProjectDenial, ...]:
        return self._project_denials

    @property
    def default_sensitive_globs(self) -> tuple[str, ...]:
        return self._default_globs

    @property
    def user_sensitive_globs(self) -> tuple[str, ...]:
        return self._user_globs

    @property
    def has_user_rules(self) -> bool:
        return self._has_user_rules

    # ------------------------------------------------------------- decisions

    def decide_fs_read(self, path: str) -> Decision:
        """Mediated read: allowed anywhere inside the workspace unless denied."""
        resolved = self._resolve_in_workspace(path)
        if isinstance(resolved, Decision):
            return resolved
        denial = self._path_denial(resolved, path)
        if denial is not None:
            return denial
        return self._make(
            True,
            RULE_READ_IN_WORKSPACE_ALLOW,
            SOURCE_DEFAULT,
            f"read of {path!r} resolves inside the workspace",
        )

    def decide_fs_write(self, path: str) -> Decision:
        """Mediated write: allowed only inside the step working directory."""
        resolved = self._resolve_in_workspace(path)
        if isinstance(resolved, Decision):
            return resolved
        denial = self._path_denial(resolved, path)
        if denial is not None:
            return denial
        if not resolved.is_relative_to(self._step_dir):
            return self._make(
                False,
                RULE_OUTSIDE_STEPDIR_DENY,
                SOURCE_DEFAULT,
                f"write of {path!r} is inside the workspace but outside the step working directory",
            )
        return self._make(
            True,
            RULE_WRITE_IN_STEPDIR_ALLOW,
            SOURCE_DEFAULT,
            f"write of {path!r} resolves inside the step working directory",
        )

    def decide_terminal(self, req: TerminalRequestN) -> Decision:
        """Mediated terminal op: only ``create`` carries a matchable command.

        Lifecycle ops (``output``/``wait_for_exit``/``kill``/``release``) on a
        terminal the engine already created are governed by that create
        decision; a stateless decision on them is a fail-closed default deny.
        """
        op = getattr(req, "op", None)
        payload = getattr(req, "payload", None)
        if op != "create":
            return self._make(
                False,
                RULE_TERMINAL_DEFAULT_DENY,
                SOURCE_DEFAULT,
                f"terminal op {op!r} carries no command; guarded policy denies by default",
            )
        if not isinstance(payload, dict):
            return self._make(
                False,
                RULE_TERMINAL_DEFAULT_DENY,
                SOURCE_DEFAULT,
                "terminal create payload is malformed; guarded policy denies by default",
            )
        return self._terminal_decision(payload.get("command"), payload.get("args"))

    def decide_permission(self, req: PermissionRequestN) -> Decision:
        """Resolve an ACP ``session/request_permission`` by guarded policy.

        The request carries a ToolCallUpdate dump; the decision kind derives
        from ``tool_call.kind`` + ``locations``: ``read`` → fs-read per
        location; ``edit``/``delete``/``move`` → fs-write per location;
        ``execute`` → terminal decision against the raw command when present
        in the tool call, else default deny. Any denied location denies the
        request overall (that rule is recorded); everything else — unknown or
        missing kinds, missing/empty/malformed locations — is
        ``unmatched-default-deny``.
        """
        tool_call = getattr(req, "tool_call", None)
        if not isinstance(tool_call, dict):
            return self._unmatched("permission request carries no tool call payload")
        kind = tool_call.get("kind")
        if kind == "execute":
            command, args = _extract_command(tool_call)
            if command is None:
                return self._make(
                    False,
                    RULE_TERMINAL_DEFAULT_DENY,
                    SOURCE_DEFAULT,
                    "execute permission request carries no raw command; "
                    "guarded policy denies terminal execution by default",
                )
            return self._terminal_decision(command, args)
        decide: Callable[[str], Decision]
        if kind == "read":
            decide = self.decide_fs_read
        elif kind in _WRITE_KINDS:
            decide = self.decide_fs_write
        else:
            return self._unmatched(f"tool kind {kind!r} is not mapped to a guarded rule")
        locations = _location_paths(tool_call)
        if locations is None:
            return self._unmatched("tool call locations are malformed")
        if not locations:
            return self._unmatched(f"tool kind {kind!r} carries no locations")
        decision = self._unmatched("no location was evaluated")  # unreachable placeholder
        for path in locations:
            decision = decide(path)
            if not decision.allowed:
                return decision
        return decision

    # --------------------------------------------------------------- helpers

    def _make(self, allowed: bool, rule_id: str, source: str, reason: str) -> Decision:
        return Decision(
            allowed=allowed,
            rule_id=rule_id,
            policy_name=self._policy_name,
            policy_source=source,
            reason=reason,
        )

    def _unmatched(self, reason: str) -> Decision:
        return self._make(False, RULE_UNMATCHED_DEFAULT_DENY, SOURCE_DEFAULT, reason)

    def _resolve_in_workspace(self, path: str) -> Path | Decision:
        try:
            return resolve_contained(self._workspace, path)
        except PathAmbiguity as exc:
            if exc.reason == REASON_OUTSIDE_BASE:
                return self._make(
                    False,
                    RULE_OUTSIDE_WORKSPACE_DENY,
                    SOURCE_DEFAULT,
                    f"path {path!r} resolves outside the workspace",
                )
            return self._make(
                False,
                RULE_PATH_AMBIGUITY_DENY,
                SOURCE_DEFAULT,
                f"path {path!r} failed canonical containment ({exc.reason}): {exc}",
            )

    def _path_denial(self, resolved: Path, requested: str) -> Decision | None:
        """Deny rules for a workspace-contained path; None means no deny hit."""
        rel = resolved.relative_to(self._workspace)
        for index, denial in enumerate(self._project_denials):
            if denial.kind == "path" and glob_matches(denial.pattern, rel):
                return self._make(
                    False,
                    project_denial_rule_id(index),
                    SOURCE_PROJECT_DENIAL,
                    f"path {requested!r} matches project deny glob {denial.pattern!r}",
                )
        for pattern in self._default_globs:
            if glob_matches(pattern, rel):
                return self._make(
                    False,
                    RULE_SENSITIVE_PATH_DENY,
                    SOURCE_DEFAULT,
                    f"path {requested!r} matches sensitive glob {pattern!r}",
                )
        for pattern in self._user_globs:
            if glob_matches(pattern, rel):
                return self._make(
                    False,
                    RULE_SENSITIVE_PATH_DENY,
                    SOURCE_USER,
                    f"path {requested!r} matches user sensitive glob {pattern!r}",
                )
        return None

    def _terminal_decision(self, command: object, args_raw: object) -> Decision:
        if not isinstance(command, str) or not command:
            return self._make(
                False,
                RULE_TERMINAL_DEFAULT_DENY,
                SOURCE_DEFAULT,
                "terminal request carries no command string; guarded policy denies by default",
            )
        if args_raw is None:
            args: list[str] = []
        elif isinstance(args_raw, list) and all(isinstance(a, str) for a in args_raw):
            args = list(args_raw)
        else:
            return self._make(
                False,
                RULE_TERMINAL_DEFAULT_DENY,
                SOURCE_DEFAULT,
                f"terminal args for {command!r} are malformed; guarded policy denies by default",
            )
        for index, denial in enumerate(self._project_denials):
            if (
                denial.kind == "terminal"
                and denial.pattern == command
                and _args_prefix_matches(denial.args_prefix, args)
            ):
                return self._make(
                    False,
                    project_denial_rule_id(index),
                    SOURCE_PROJECT_DENIAL,
                    f"command {command!r} matches project terminal denial",
                )
        for allowed_command, prefix in self._terminal_allowlist:
            if allowed_command == command and _args_prefix_matches(prefix, args):
                return self._make(
                    True,
                    RULE_TERMINAL_USER_ALLOWLIST,
                    SOURCE_USER,
                    f"command {command!r} matches the trusted user terminal allowlist",
                )
        return self._make(
            False,
            RULE_TERMINAL_DEFAULT_DENY,
            SOURCE_DEFAULT,
            f"command {command!r} matches no user terminal allowlist rule; "
            "guarded policy denies terminal execution by default",
        )


def _normalize_denial(denial: ProjectDenial | ProjectDenialLike) -> ProjectDenial:
    if isinstance(denial, ProjectDenial):
        return denial
    return ProjectDenial(
        kind=denial.kind,  # type: ignore[arg-type]  # config validates the Literal
        pattern=denial.pattern,
        args_prefix=tuple(getattr(denial, "args_prefix", ())),
    )


def _args_prefix_matches(prefix: Sequence[str], args: list[str]) -> bool:
    return list(prefix) == args[: len(prefix)]


def _extract_command(tool_call: dict[str, Any]) -> tuple[str | None, object]:
    """Raw command (+ args, unvalidated) from an execute-kind tool call.

    ``raw_input`` (the SDK dump of the agent's tool input) wins over a
    top-level ``command`` key; absence of both means the request is not
    matchable and the caller default-denies.
    """
    raw_input = tool_call.get("raw_input")
    for source in (raw_input, tool_call):
        if isinstance(source, dict):
            command = source.get("command")
            if isinstance(command, str) and command:
                return command, source.get("args")
    return None, None


def _location_paths(tool_call: dict[str, Any]) -> list[str] | None:
    """Location path strings from a tool call dump; None when malformed.

    Accepts SDK-shaped entries (``{"path": str, ...}``) and bare strings.
    A missing ``locations`` key is an empty list (mapped to unmatched deny by
    the caller); any entry without a usable path poisons the whole list.
    """
    raw = tool_call.get("locations")
    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    paths: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            paths.append(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("path"), str):
            paths.append(entry["path"])
        else:
            return None
    return paths


def build_policy_provenance(
    policy: MediationPolicy, *, ceiling_source: str | None = None
) -> PolicyProvenance:
    """Snapshot the effective policy for the RunResult (``policy`` field).

    ``ceiling_source`` is where the policy ceiling came from (``default`` |
    ``user`` | ``env``); when omitted it is inferred: ``user`` if any
    user-scope rules (profile / extra globs) contributed, else ``default``.
    Enforcement is always reported ``advisory`` — ACP mediation is observable
    governance, never an OS sandbox.
    """
    if ceiling_source is None:
        ceiling_source = SOURCE_USER if policy.has_user_rules else SOURCE_DEFAULT
    rules: list[dict[str, Any]] = [
        {"rule_id": RULE_READ_IN_WORKSPACE_ALLOW, "effect": "allow", "source": SOURCE_DEFAULT},
        {"rule_id": RULE_WRITE_IN_STEPDIR_ALLOW, "effect": "allow", "source": SOURCE_DEFAULT},
        {
            "rule_id": RULE_SENSITIVE_PATH_DENY,
            "effect": "deny",
            "source": SOURCE_DEFAULT,
            "patterns": list(policy.default_sensitive_globs),
        },
    ]
    if policy.user_sensitive_globs:
        rules.append(
            {
                "rule_id": RULE_SENSITIVE_PATH_DENY,
                "effect": "deny",
                "source": SOURCE_USER,
                "patterns": list(policy.user_sensitive_globs),
            }
        )
    for command, prefix in policy.terminal_allowlist:
        rules.append(
            {
                "rule_id": RULE_TERMINAL_USER_ALLOWLIST,
                "effect": "allow",
                "source": SOURCE_USER,
                "command": command,
                "args_prefix": list(prefix),
            }
        )
    for index, denial in enumerate(policy.project_denials):
        entry: dict[str, Any] = {
            "rule_id": project_denial_rule_id(index),
            "effect": "deny",
            "source": "project",
            "kind": denial.kind,
            "pattern": denial.pattern,
        }
        if denial.kind == "terminal":
            entry["args_prefix"] = list(denial.args_prefix)
        rules.append(entry)
    rules.extend(
        {"rule_id": rule_id, "effect": "deny", "source": SOURCE_DEFAULT}
        for rule_id in (
            RULE_OUTSIDE_WORKSPACE_DENY,
            RULE_OUTSIDE_STEPDIR_DENY,
            RULE_TERMINAL_DEFAULT_DENY,
            RULE_PATH_AMBIGUITY_DENY,
            RULE_UNMATCHED_DEFAULT_DENY,
        )
    )
    tightened_by = (
        [f"project-denials:{len(policy.project_denials)}"] if policy.project_denials else []
    )
    return PolicyProvenance(
        policy_name=policy.policy_name,
        ceiling_source=ceiling_source,
        rules=rules,
        tightened_by=tightened_by,
        enforcement="advisory",
    )
