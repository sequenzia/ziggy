"""Agent registration contract (§7.3 AgentConfig). Trusted user scope only."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    builtin: bool = False
    command: str
    args: list[str] = []
    env: dict[str, str] = {}  # explicit literal env (non-secret; validated)
    inherit_env: list[str] = []  # names inherited from parent env
    working_dir: str | None = None
    api_key_env: str | None = None  # env var NAME, never a value
    provider: str | None = None  # anthropic|openai|custom (egress identity)
    permission_policy: str | None = None  # named policy profile (user-defined)
    orchestration_eligible: bool = False  # may model-generated plans invoke it
    #: conservative default: assume direct (non-mediated) local tools until proven
    direct_tools_assumed: bool = True


class WorkspaceLease(BaseModel):
    """Cross-process single-mutator lease (§7.3); stored outside the project."""

    model_config = ConfigDict(extra="forbid")

    canonical_workspace_hash: str
    workspace: str
    run_id: str
    owner_pid: int
    owner_process_start: str  # opaque platform token proving pid identity
    owner_process_group: int
    acquired_at: str
