"""Direct-run engine: config-driven run preparation, execution, mediation hooks.

Phase 3 adds the shared per-step executor (:func:`execute_step` driven by a
:class:`StepExecutionContext`) used by both direct runs and the workflow
runner, and the workspace lease (``ziggy.engine.lease``, imported directly by
its consumers).
"""

from ziggy.engine.env import BASELINE_ENV_VARS, compose_child_env
from ziggy.engine.hooks import PolicyHooks, RecordingHooks
from ziggy.engine.prepare import PreparedRun, RunOverrides, prepare_run
from ziggy.engine.runner import (
    MAIN_STEP_ID,
    RunSpec,
    StepExecutionContext,
    StepOutcome,
    execute_run,
    execute_step,
)

__all__ = [
    "BASELINE_ENV_VARS",
    "MAIN_STEP_ID",
    "PolicyHooks",
    "PreparedRun",
    "RecordingHooks",
    "RunOverrides",
    "RunSpec",
    "StepExecutionContext",
    "StepOutcome",
    "compose_child_env",
    "execute_run",
    "execute_step",
    "prepare_run",
]
