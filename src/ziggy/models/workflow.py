"""Workflow YAML v1 contract (§5.6): agent-only steps, typed variables,
declared inputs, restricted value-only interpolation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

STEP_ID_PATTERN = r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$"
VAR_NAME_PATTERN = r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$"
DEFAULT_VAR_MAX_BYTES = 65536


class VariableDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["string", "integer", "boolean", "json"] = "string"
    required: bool = False
    default: Any | None = None
    secret: bool = False
    max_bytes: int = Field(default=DEFAULT_VAR_MAX_BYTES, ge=1)
    description: str | None = None

    @model_validator(mode="after")
    def _required_has_no_default(self) -> VariableDef:
        if self.required and self.default is not None:
            msg = "a required variable cannot also declare a default"
            raise ValueError(msg)
        return self


class StepDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # MVP: agent-only. 'type' accepted so schema v1 can REJECT script/shell/python
    # with a precise message (workflows/schema.py), not an unknown-key error.
    type: Literal["agent"] = "agent"
    agent: str
    prompt: str = Field(min_length=1)
    inputs: dict[str, str] = {}  # local name -> "steps.<id>.outputs.<name>" | "vars.<name>"
    depends_on: list[str] = []
    working_dir: str | None = None  # must canonicalize inside workspace
    timeout_seconds: int | None = Field(default=None, ge=1)  # <= user ceiling
    policy_profile: str | None = None  # trusted user-defined; deny-only additions

    @field_validator("inputs")
    @classmethod
    def _input_source_shape(cls, v: dict[str, str]) -> dict[str, str]:
        for name, source in v.items():
            parts = source.split(".")
            if len(parts) == 2 and parts[0] == "vars":
                continue
            if len(parts) == 4 and parts[0] == "steps" and parts[2] == "outputs":
                continue
            msg = (
                f"input {name!r}: source must be 'vars.<name>' or "
                f"'steps.<id>.outputs.<name>', got {source!r}"
            )
            raise ValueError(msg)
        return v


class WorkflowDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    name: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    description: str | None = None
    variables: dict[str, VariableDef] = {}
    steps: dict[str, StepDef] = Field(min_length=1)

    @field_validator("variables")
    @classmethod
    def _var_names(cls, v: dict[str, VariableDef]) -> dict[str, VariableDef]:
        import re

        for name in v:
            if not re.match(VAR_NAME_PATTERN, name):
                msg = f"invalid variable name {name!r}"
                raise ValueError(msg)
        return v

    @field_validator("steps")
    @classmethod
    def _step_ids(cls, v: dict[str, StepDef]) -> dict[str, StepDef]:
        import re

        for step_id in v:
            if not re.match(STEP_ID_PATTERN, step_id):
                msg = f"invalid step id {step_id!r}"
                raise ValueError(msg)
        return v
