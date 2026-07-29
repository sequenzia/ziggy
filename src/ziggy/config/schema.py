"""Config schema (REQ-007): the full TOML -> pydantic tree.

Structures are normative per ``docs/design/phase2-contracts.md`` '## config/'.
Every model forbids unknown keys; the loader converts pydantic validation
failures into path-precise ``ConfigError`` messages carrying file provenance,
so raw pydantic errors (which may echo input values) never surface directly.

Deliberate omissions (contract decisions, do not "fix"):

- ``PolicyProfile.allow_read_outside_workspace`` is rejected for v0.1 — the
  guarded policy semantics are fixed and user scope may not widen reads.
- ``AgentEntry`` has no ``acknowledged_egress``; egress acknowledgements live
  in the top-level ``[egress]`` table (``EgressConfig``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ziggy.models.common import CaptureProfile


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EngineConfig(_Section):
    """Resource ceilings; every numeric field is a TIGHTEN_MIN candidate."""

    max_workflow_steps: int = 16
    max_prompt_bytes: int = 262144
    default_step_timeout_seconds: int = 1800
    default_workflow_timeout_seconds: int = 3600
    cancel_grace_seconds: float = 5.0
    max_event_bytes_per_step: int = 10 * 2**20
    max_artifact_bytes_per_run: int = 50 * 2**20


class TerminalRule(_Section):
    """Trusted terminal allowlist entry: exact argv[0] plus argument prefix."""

    command: str
    args_prefix: list[str] = []


class PolicyProfile(_Section):
    """User-defined mediation profile: extra denials + terminal allowlist."""

    terminal_allowlist: list[TerminalRule] = []
    deny_paths: list[str] = []  # extra sensitive-path globs


class ProjectDenial(_Section):
    """Project-scope-only additional deny rule (may only tighten policy)."""

    kind: Literal["path", "terminal"]
    pattern: str


class PermissionsConfig(_Section):
    default_policy: str = "guarded"  # 'guarded' is the only builtin name in v0.1
    profiles: dict[str, PolicyProfile] = {}
    #: Populated exclusively from project scope; the loader rejects it in
    #: user scope and rejects every other permissions key in project scope.
    project_denials: list[ProjectDenial] = []


class ResultsConfig(_Section):
    persist: bool = True
    capture: CaptureProfile = CaptureProfile.STANDARD
    #: USER_ONLY. Deletion policy is not a security ceiling a project may
    #: "tighten": a smaller retention window destroys audit evidence sooner, so
    #: project scope may not touch it at all (loader rejects it). ``ge=1``
    #: forbids a zero/negative window that would make every completed run
    #: eligible for deletion immediately.
    retention_days: int = Field(default=30, ge=1)
    auto_prune: bool = False
    store_path: str | None = None  # USER_ONLY


class ServerConfig(_Section):
    max_active_runs: int = 1


class TrustedWorkflow(_Section):
    """Canonical path + content hash entry for orchestrator auto-selection."""

    path: str
    sha256: str


class OrchestratorConfig(_Section):
    agent: str | None = None
    max_inline_steps: int = 8
    auto_execute: bool = True
    allow_uncontained_planner: bool = False
    eligible_agents: list[str] = []
    trusted_workflows: list[TrustedWorkflow] = []


class CustomPatternCfg(_Section):
    """User redaction pattern; regex validity is checked by the loader."""

    kind: str
    regex: str
    max_width: int | None = None


class RedactionConfig(_Section):
    extra_value_env_vars: list[str] = []  # env var NAMES whose values redact
    patterns: list[CustomPatternCfg] = []


class LogsConfig(_Section):
    retention_days: int = 30


class WorkflowsConfig(_Section):
    default_name: str | None = None  # PROJECT_OK
    #: USER_ONLY: secret variable name -> provider names allowed to receive its
    #: value via prompt interpolation (REQ-011). Unlisted pairs are denied.
    secret_variable_allowances: dict[str, list[str]] = {}


class AgentEntry(_Section):
    """Trusted-user agent registration. The dict key is the agent name.

    ``command`` may be omitted only for the builtin names (claude, codex),
    whose pinned launch defaults come from ``agents/builtins.py``.
    """

    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}  # explicit literal env (non-secret; validated)
    inherit_env: list[str] = []
    working_dir: str | None = None
    api_key_env: str | None = None  # env var NAME, never a value
    provider: str | None = None
    orchestration_eligible: bool = False


class EgressConfig(_Section):
    acknowledged_provider_sets: list[list[str]] = []


class ZiggyConfig(_Section):
    """Root of the config tree; ``schema_version = 1`` is required."""

    schema_version: Literal[1]
    engine: EngineConfig = EngineConfig()
    agents: dict[str, AgentEntry] = {}
    permissions: PermissionsConfig = PermissionsConfig()
    results: ResultsConfig = ResultsConfig()
    server: ServerConfig = ServerConfig()
    orchestrator: OrchestratorConfig = OrchestratorConfig()
    redaction: RedactionConfig = RedactionConfig()
    logs: LogsConfig = LogsConfig()
    workflows: WorkflowsConfig = WorkflowsConfig()
    egress: EgressConfig = EgressConfig()
