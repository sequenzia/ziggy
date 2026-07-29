"""Shared enums and status semantics (single source of truth)."""

from __future__ import annotations

from enum import StrEnum


class RunKind(StrEnum):
    AGENT = "agent"
    WORKFLOW = "workflow"
    ORCHESTRATOR = "orchestrator"


class RunStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


class StepStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


class StepType(StrEnum):
    AGENT = "agent"


class CaptureStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    DERIVED = "derived"
    UNAVAILABLE = "unavailable"


class CaptureProfile(StrEnum):
    METADATA = "metadata"
    STANDARD = "standard"
    DEBUG = "debug"

    def rank(self) -> int:
        return {"metadata": 0, "standard": 1, "debug": 2}[self.value]


class EnforcementScope(StrEnum):
    ACP_MEDIATED = "acp_mediated"
    AGENT_REPORTED = "agent_reported"
    OS_ENFORCED = "os_enforced"


class PermissionDecisionKind(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"


class ConfigSource(StrEnum):
    DEFAULT = "default"
    USER = "user"
    ENV = "env"
    PROJECT = "project"


class ProjectAction(StrEnum):
    """What happened to a project-scope config value during the monotonic merge."""

    NONE = "none"
    APPLIED = "applied"
    TIGHTENED = "tightened"
    IGNORED = "ignored"
    REJECTED = "rejected"


#: Step statuses that count as "ran and completed the work".
STEP_OK = frozenset({StepStatus.SUCCESS})
#: Step statuses that are terminal without the step ever running.
STEP_NEVER_RAN = frozenset({StepStatus.BLOCKED, StepStatus.SKIPPED})

RUN_TERMINAL = frozenset(RunStatus)
STEP_TERMINAL = frozenset(StepStatus)
