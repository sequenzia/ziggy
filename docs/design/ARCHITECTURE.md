# Ziggy v0.1 — Architecture & Module Contracts

Normative for all implementation work. Read together with:
`specs/ziggy-mvp-SPEC.md` (requirements), `docs/phase0/*` (SDK facts, trust
vocabulary, process lifecycle). On conflict: spec wins for behavior, this doc
wins for structure/naming, Phase 0 docs win for SDK facts.

## Package layout (src/ziggy/)

| Module | Responsibility | May import from |
|--------|----------------|-----------------|
| `ids.py` | ULID run ids, UTC + monotonic timing helpers | stdlib, python-ulid |
| `errors.py` | TypedError taxonomy: exception classes + serializable model | models/common |
| `models/` | Pydantic contracts: enums, events, result, workflow, plan, agent | pydantic only |
| `redact/` | Bounded streaming redactor + RedactionSummary | models |
| `events/` | RunRecorder: canonical event pipeline (seq, redact, persist, fan-out) | models, redact, store(writer handle) |
| `acp/` | **ONLY module importing the `acp` SDK.** Native types + client/server adapters | models, errors; SDK inside only |
| `agents/` | Builtin launch metadata (pinned), AgentRegistry from config | models, config |
| `config/` | TOML schema, env overrides, field-specific monotonic merge, provenance | models, errors |
| `policy/` | Guarded mediation engine + canonical path logic | models, config |
| `store/` | Run directory writer, SQLite index, metadata logs, retention/prune | models, errors |
| `engine/` | Direct-run engine, workspace lease, teardown ladder orchestration | everything above |
| `workflows/` | YAML schema/discovery/interpolation/serial scheduler/egress | models, config, engine |
| `orchestrator/` | Catalog, planning run, strict validation, repair, execution mapping | workflows, engine, config |
| `server/` | `ziggy serve` ACP-agent app, routing, permission bridge | acp, engine, workflows, orchestrator |
| `cli/` | typer app, rich/plain rendering, doctor | everything |

Dependency rule: strictly top-to-bottom in this table; no upward imports.
`from acp import ...` appears **only** under `src/ziggy/acp/` (enforced by test).

## Core dataflow (one pipeline, all run kinds)

```
spawn agent ── acp adapter ──> native AgentEvent stream ──> RunRecorder.emit()
                                                             ├─ redactor (bounded, streaming)
                                                             ├─ events.jsonl append (unless --no-save)
                                                             ├─ live render queue (CLI/rich or server re-emit)
                                                             └─ in-memory aggregation → StepResult/RunResult
permission request ──> policy engine ──> decision (+ recorded PermissionDecision event)
fs/terminal request ──> policy + path checks ──> served/denied (+ recorded event)
```

RunResult manifest is written once, atomically, at terminal state; the SQLite
index row is inserted only after `result.json` is durable. `result.json` holds
summaries/references, never the full event payloads.

## Native domain types (ziggy.acp public surface — SDK-free)

```python
@dataclass HandshakeInfo: protocol_version:int; agent_name:str; agent_version:str|None;
                          agent_title:str|None; capabilities:dict; auth_methods:list[dict]
# AgentEvent union (events emitted while a prompt turn is active):
MessageChunkEvent(role:'agent'|'user', text:str, thought:bool)
ToolCallEvent(tool_call_id, phase:'start'|'update', title|None, kind|None, status|None,
              locations:list[str], has_content:bool, raw:dict)       # raw kept only for debug capture
PlanEvent(entries:list[{content,priority,status}])
UsageEvent(used:int|None, size:int|None, cost:float|None, currency:str|None, raw:dict)
ModeEvent(kind:str, payload:dict)          # current_mode/config_option/session_info/available_commands
UnknownUpdateEvent(update_type:str, payload:dict)
StopInfo(stop_reason:str)                  # end_turn|max_tokens|max_turn_requests|refusal|cancelled
PermissionRequestN(session_id, tool_call:dict, options:list[PermissionOptionN])
PermissionOptionN(option_id:str, name:str, kind:str)   # allow_once|allow_always|reject_once|reject_always
FsReadRequestN(path:str, line:int|None, limit:int|None) / FsWriteRequestN(path:str, content:str)
TerminalRequestN(op:str, payload:dict)
```

`AgentProcessClient` (ziggy/acp/client.py) contract:

```python
class MediationHooks(Protocol):            # implemented by engine
    async def on_event(self, ev: AgentEvent) -> None
    async def resolve_permission(self, req: PermissionRequestN) -> PermissionReply
        # PermissionReply(kind:'selected'|'cancelled', option_id:str|None)
    async def read_text_file(self, req) -> str            # raises PolicyDenied
    async def write_text_file(self, req) -> None          # raises PolicyDenied
    async def handle_terminal(self, req) -> TerminalReply # or raises PolicyDenied/Unsupported

class AgentProcessClient:
    @classmethod async def launch(cls, *, command, args, env, cwd, hooks,
                                  raw_frame_cb=None, stream_limit=None) -> AgentProcessClient
        # start_new_session=True; raises AgentLaunchError
    handshake: HandshakeInfo               # after .initialize()
    async def initialize(self) -> HandshakeInfo           # ProtocolError on failure/mismatch
    async def new_session(self, cwd: str) -> str          # session_id
    async def prompt(self, session_id: str, text: str) -> StopInfo
    async def cancel(self, session_id: str) -> None       # ACP notification only
    async def shutdown(self, grace_seconds: float) -> int|None
        # full teardown ladder (docs/phase0/process-lifecycle.md); returns exit code
    @property pid/pgid/returncode
```

The adapter maps SDK errors: spawn failure→`AgentLaunchError`; version/handshake
problems→`ProtocolError`; RequestError from agent→`ProtocolError` (with code);
`ConnectionError`/EOF mid-turn→`ProtocolError` + partial capture.
`raw_frame_cb` receives `(direction:str, frame:dict)` for debug capture only.

## RunRecorder (events/pipeline.py) contract

```python
class RunRecorder:
    def __init__(self, *, run_id, store_writer|None, redactor, capture_profile,
                 limits: EventLimits, render_cb: Callable[[EventEnvelope], None]|None)
    def emit(self, *, event_type:str, step_id:str|None, attempt_no:int|None,
             session_id:str|None, payload:dict, capture_status:str='complete') -> EventEnvelope
        # assigns seq + ts + monotonic_offset_ms, redacts payload text fields,
        # enforces per-step byte ceilings (switches to metadata-only continuation
        # with truncation metadata), appends to events.jsonl, fans out to render_cb
    def tool_calls(step_id) / file_changes(step_id) / permission_decisions(step_id)  # aggregates
    def capture_summary() -> dict[str, CaptureSummary-shaped]
    def redaction_summary() -> RedactionSummary
    async def finalize(self) -> None      # flush + fsync events.jsonl
```

Event `event_type` names (stable, snake_case): `run_started, config_resolved,
policy_resolved, agent_launching, agent_launched, handshake, session_created,
prompt_started, message_chunk, thought_chunk, tool_call, tool_call_update, plan,
usage, mode, unknown_update, permission_requested, permission_decided, fs_read,
fs_write, terminal_op, file_change, step_started, step_finished, egress_notice,
lease_acquired, lease_released, cancel_requested, terminated, truncation,
error, run_finished, raw_frame (debug only)`.

## Status semantics (single source: models/common.py)

- RunStatus: success | failed | partial | cancelled | abandoned
- StepStatus: success | failed | blocked | skipped | cancelled | abandoned
- Workflow aggregate: first failure stops new scheduling; transitive dependents
  → blocked; other pending → skipped; run = partial if ≥1 step succeeded else failed.
  Cancellation: active step → cancelled, pending → skipped, run → cancelled.
- Capture status: complete | partial | derived | unavailable
- Enforcement scope: acp_mediated | agent_reported | os_enforced
- Capture profile: metadata | standard | debug

## Persistence layout

```
~/.ziggy/
  config.toml                    # trusted user scope
  runs/<run-id>/result.json      # atomic: tmp + fsync + rename (0600)
  runs/<run-id>/events.jsonl     # append-only, redacted, incremental (0600)
  runs/<run-id>/changes/ artifacts/
  runs/index.db                  # SQLite WAL; derived only
  leases/<sha256-of-canonical-workspace>.json
  logs/ziggy-YYYY-MM-DD.jsonl    # metadata only, daily rotation
  workflows/                     # user workflow search path
```

Index DDL (v1):
```sql
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL,
  status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
  duration_ms INTEGER, workspace TEXT NOT NULL, result_path TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
```
`meta['schema_version']='1'`. Init/migration inside one transaction
(`BEGIN IMMEDIATE`). Readers tolerate current + previous schema; future → explicit error.

## Config merge semantics (config/loader.py)

Each config field carries a merge rule:
- `USER_ONLY` — project value ⇒ `ConfigError` (agents.*, orchestrator trust
  fields, server limits, storage paths, redaction additions?, log retention,
  inherit_env, api_key_env, allow_uncontained_planner, trusted_workflows,
  eligible_agents, egress acknowledgements)
- `TIGHTEN_MIN` — numeric ceilings; project may only lower (timeouts, byte/step limits)
- `TIGHTEN_POLICY` — permission policy; project may only pick stricter/add denials
- `PROJECT_OK` — workflow defaults (e.g. `workflows.default_name`), capture may
  only move toward less capture (standard→metadata ok, →debug rejected)

Provenance per effective field: `{value, source: default|user|env|project,
project_action: none|applied|tightened|ignored|rejected}`. Env overrides
(`ZIGGY_SECTION__KEY`) are user-scope. Unknown keys anywhere ⇒ `ConfigError`
with TOML path. `schema_version = 1` required.

## Error taxonomy (errors.py)

Exceptions all subclass `ZiggyError(code:str, message:str, details:dict)`, and
serialize to `TypedError{code, message, details}`:
`AgentLaunchError, ProtocolError, CapabilityError, PermissionDeniedError,
StepTimeoutError, ResourceLimitError, ValidationError, ConfigError,
TrustPolicyError, OrchestratorPlanInvalid, ServerBusyError, WorkspaceBusyError,
PersistenceError, CancelledError, AbandonedError`.
CLI maps: ConfigError/TrustPolicyError/ValidationError(usage)/missing egress ack → exit 2;
user cancellation → 130; other failures → 1.

## Conventions

- Python 3.12+, `from __future__ import annotations`, full type hints, ruff-clean.
- pydantic v2 everywhere; models are `model_config = ConfigDict(extra='forbid')`.
- All timestamps: `datetime.now(UTC)` ISO-8601 Z; durations from `time.monotonic()`.
- asyncio-only concurrency; no threads except where stdlib forces (none expected).
- Tests: pytest + pytest-asyncio (asyncio_mode=auto), tmp_path-based stores;
  `ZIGGY_HOME` env var overrides `~/.ziggy` root for tests (resolved in store/config).
- Never `print()` outside cli/; engine communicates via events + return values.
- Log lines are metadata-only (no prompts/payloads/paths beyond run dir refs).
- Security posture words: see docs/phase0/trust-boundary.md terminology rules.
