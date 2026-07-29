"""RunResult manifest contract (§5.3, §7.3)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from ziggy.errors import TypedError
from ziggy.models.common import (
    CaptureProfile,
    EnforcementScope,
    RunKind,
    RunStatus,
    StepStatus,
    StepType,
)
from ziggy.models.events import (
    CaptureSummaryEntry,
    FileChange,
    PermissionDecision,
    RedactionSummary,
    ToolCallRecord,
)
from ziggy.models.plan import OrchestratorPlan, PlanValidation


class AgentInfo(BaseModel):
    """Handshake-derived identity/capability snapshot for one step's agent."""

    model_config = ConfigDict(extra="forbid")

    name: str  # ziggy-registered agent name
    provider: str | None = None  # e.g. anthropic|openai|custom
    protocol_version: int | None = None
    agent_name: str | None = None  # agent-reported Implementation.name
    agent_title: str | None = None
    agent_version: str | None = None
    capabilities: dict[str, Any] = {}
    auth_methods: list[dict[str, Any]] = []
    direct_tools_assumed: bool = True  # conservative until live probes prove otherwise
    mediation: str = "advisory"  # advisory | os_enforced (never os_enforced in v0.1)


class Attempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_no: int
    status: StepStatus
    started_at: str
    ended_at: str | None = None
    duration_ms: int | None = None
    stop_reason: str | None = None
    exit_code: int | None = None
    errors: list[TypedError] = []


class StepResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    step_type: StepType = StepType.AGENT
    agent: str | None = None
    agent_info: AgentInfo | None = None
    status: StepStatus
    inputs_resolved: dict[str, Any] = {}  # concrete post-interpolation values (redacted)
    input_sources: dict[str, str] = {}  # input name -> declared var / upstream output path
    attempts: list[Attempt] = []  # 0 before launch; exactly 1 in MVP
    outputs: dict[str, Any] = {}  # standard: text; plus artifact refs
    tool_calls: list[ToolCallRecord] = []
    file_changes: list[FileChange] = []
    permission_decisions: list[PermissionDecision] = []
    errors: list[TypedError] = []


class EgressRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    provider: str
    input_sources: list[str] = []  # which upstream outputs/vars were sent
    acknowledged_by: str | None = None  # config | flag:--acknowledge-egress | None


class UsageSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str | None = None
    units: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float | None = None
    currency: str | None = None
    capture_status: str = "unavailable"
    ceiling_enforceable: bool = False


class PolicyProvenance(BaseModel):
    """Effective mediation policy: ceiling, applied rules, sources, scope."""

    model_config = ConfigDict(extra="forbid")

    policy_name: str
    ceiling_source: str  # default|user|env
    rules: list[dict[str, Any]] = []
    tightened_by: list[str] = []  # project/workflow/step contributors (deny-only)
    enforcement: str = "advisory"
    enforcement_scope_default: EnforcementScope = EnforcementScope.ACP_MEDIATED


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    run_id: str
    kind: RunKind
    target: str
    status: RunStatus
    started_at: str
    ended_at: str | None = None  # absent only while incomplete
    duration_ms: int | None = None
    workspace: str
    capture_profile: CaptureProfile = CaptureProfile.STANDARD
    persisted: bool = True
    config_fingerprint: str | None = None  # absent only if config validation failed
    policy: PolicyProvenance | None = None  # absent only if trust/policy resolution failed
    steps: dict[str, StepResult]
    plan: OrchestratorPlan | None = None
    plan_validation: PlanValidation | None = None
    errors: list[TypedError] = []
    capture: dict[str, CaptureSummaryEntry] = {}
    redaction: RedactionSummary = RedactionSummary()
    egress: list[EgressRecord] = []
    usage: UsageSummary | None = None
    result_path: str | None = None  # absent when --no-save
    events_path: str | None = None

    @model_validator(mode="after")
    def _check_contract(self) -> RunResult:
        if not self.steps:
            msg = "RunResult must contain at least one step"
            raise ValueError(msg)
        if self.kind is RunKind.ORCHESTRATOR and self.plan_validation is None:
            msg = "orchestrator runs must record plan_validation"
            raise ValueError(msg)
        return self
