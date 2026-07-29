"""Canonical event pipeline: seq, redact, persist, aggregate, fan-out (REQ-004)."""

from ziggy.events.pipeline import (
    ARTIFACT_CLASSES,
    DEFAULT_LIMITS,
    EVENT_TYPES,
    EventLimits,
    RunRecorder,
)

__all__ = [
    "ARTIFACT_CLASSES",
    "DEFAULT_LIMITS",
    "EVENT_TYPES",
    "EventLimits",
    "RunRecorder",
]
