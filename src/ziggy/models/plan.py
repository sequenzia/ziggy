"""Orchestrator plan contract (§5.8): three strict variants, nothing else.

The restricted inline schema is intentionally NOT the repository workflow
schema: no working directories, environment, credentials, policy, resources,
scripts, or nesting can be expressed at all.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_RATIONALE_CHARS = 2000


class SingleAgentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_type: Literal["single_agent"]
    rationale: str = Field(max_length=MAX_RATIONALE_CHARS)
    agent: str
    prompt: str


class NamedWorkflowPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_type: Literal["named_workflow"]
    rationale: str = Field(max_length=MAX_RATIONALE_CHARS)
    workflow_name: str
    variables: dict[str, Any] = {}


class InlineStep(BaseModel):
    """Exactly {id, agent, prompt, inputs, depends_on} — nothing else parses."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    agent: str
    prompt: str
    inputs: dict[str, str] = {}  # local name -> "goal" | "steps.<id>.outputs.text"
    depends_on: list[str] = []

    @field_validator("inputs")
    @classmethod
    def _input_sources_shape(cls, v: dict[str, str]) -> dict[str, str]:
        for name, source in v.items():
            if source == "goal":
                continue
            parts = source.split(".")
            if len(parts) == 4 and parts[0] == "steps" and parts[2] == "outputs":
                continue
            msg = (
                f"input {name!r}: source must be 'goal' or "
                f"'steps.<id>.outputs.<name>', got {source!r}"
            )
            raise ValueError(msg)
        return v


class InlineAgentWorkflowPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_type: Literal["inline_agent_workflow"]
    rationale: str = Field(max_length=MAX_RATIONALE_CHARS)
    steps: list[InlineStep] = Field(min_length=1)


OrchestratorPlan = Annotated[
    SingleAgentPlan | NamedWorkflowPlan | InlineAgentWorkflowPlan,
    Field(discriminator="plan_type"),
]


class PlanValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_count: int = Field(ge=1, le=2)
    repair_requested: bool = False
    errors: list[str] = []  # bounded, redacted summaries — never the raw model response
    valid: bool
