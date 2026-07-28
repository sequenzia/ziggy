"""Typed error taxonomy (§7.3 TypedError).

Every failure Ziggy reports is one of these exceptions; each serializes to the
``TypedError`` model embedded in RunResults. ``details`` must already be safe to
persist (no secrets, no full payloads).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class TypedError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any] = {}


class ZiggyError(Exception):
    """Base for all typed Ziggy errors."""

    code = "ZiggyError"
    #: default CLI exit code for this error class (§5.2)
    exit_code = 1

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_model(self) -> TypedError:
        return TypedError(code=self.code, message=self.message, details=self.details)


class AgentLaunchError(ZiggyError):
    code = "AgentLaunchError"


class ProtocolError(ZiggyError):
    code = "ProtocolError"


class CapabilityError(ZiggyError):
    code = "CapabilityError"


class PermissionDeniedError(ZiggyError):
    code = "PermissionDeniedError"


class StepTimeoutError(ZiggyError):
    code = "StepTimeoutError"


class ResourceLimitError(ZiggyError):
    code = "ResourceLimitError"


class ValidationError(ZiggyError):
    """Schema/structure validation failure (workflow YAML, plan shape, vars)."""

    code = "ValidationError"
    exit_code = 2


class ConfigError(ZiggyError):
    code = "ConfigError"
    exit_code = 2


class TrustPolicyError(ZiggyError):
    """Project/plan/client attempted to exceed trusted user authority."""

    code = "TrustPolicyError"
    exit_code = 2


class OrchestratorPlanInvalid(ZiggyError):
    code = "OrchestratorPlanInvalid"


class ServerBusyError(ZiggyError):
    code = "ServerBusyError"


class WorkspaceBusyError(ZiggyError):
    code = "WorkspaceBusyError"


class PersistenceError(ZiggyError):
    code = "PersistenceError"


class CancelledError(ZiggyError):
    code = "CancelledError"
    exit_code = 130


class AbandonedError(ZiggyError):
    code = "AbandonedError"


class EgressNotAcknowledgedError(TrustPolicyError):
    """Cross-provider egress required but not acknowledged (headless fail, exit 2)."""

    code = "TrustPolicyError"


ERROR_CLASSES: dict[str, type[ZiggyError]] = {
    cls.code: cls
    for cls in (
        AgentLaunchError,
        ProtocolError,
        CapabilityError,
        PermissionDeniedError,
        StepTimeoutError,
        ResourceLimitError,
        ValidationError,
        ConfigError,
        TrustPolicyError,
        OrchestratorPlanInvalid,
        ServerBusyError,
        WorkspaceBusyError,
        PersistenceError,
        CancelledError,
        AbandonedError,
    )
}
