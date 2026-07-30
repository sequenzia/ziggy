# Phase 4/5 Contracts — ACP Server Mode & Constrained Orchestrator

Extends ARCHITECTURE.md, phase2-contracts.md, phase3-contracts.md.

## Phase 4 — `ziggy serve` (REQ-012)

### ziggy/acp/server.py (SDK boundary, agent side)

`serve_stdio(agent_impl: ZiggyAgentProtocol) -> None` wrapping `acp.run_agent`;
`ZiggyAgentProtocol` = native-typed protocol the server app implements
(initialize/new_session/set_config_option/prompt/cancel/authenticate), so
`server/` stays SDK-free. The wrapper owns SDK model construction:
- `initialize` → InitializeResponse(protocol_version=1, agent_info
  Implementation(name='ziggy', version), agentCapabilities: loadSession=False,
  promptCapabilities all False except embedded_context=False — text-only).
  Record client_capabilities (fs/terminal support) for the permission bridge
  fallback decision and client_info for logs.
- `new_session` → NewSessionResponse(session_id, config_options=[
  SessionConfigOptionSelect(id='route', name='Route', value=default_route,
  options=[orchestrator, agent:<each trusted agent>, workflow:<each discovered
  trusted workflow>])]) — v1 session-config surface, domain stays neutral.
- `set_config_option(config_id='route', value=...)` → route change; unknown
  id/value → RequestError.invalid_params. Route cannot touch policy/ceilings.
- Outbound: `emit_update(session_id, native_ev, *, run_id, step_id)` →
  session/update with `_meta={'ziggy': {'run_id':…, 'step_id':…}}`;
  `forward_permission(session_id, native_req, context_label) ->
  PermissionReply` mapping SDK response; RequestError(method_not_found) ⇒
  raises `ClientPermissionUnsupported` (server app falls back).

### server/app.py

- `ZiggyServer(resolved_config, workspace_override=None)` implementing the
  native protocol. Sessions: dict session_id → SessionState{route, cwd
  (canonicalized client cwd), active_task}.
- `max_active_runs` (default 1) across the process; excess `session/prompt` ⇒
  RequestError.internal_error(data={'code':'ServerBusyError', …}) — typed busy,
  no queueing. Workspace lease still applies per run (WorkspaceBusyError same
  shape).
- prompt flow: extract text blocks (join) → route:
  `agent:<name>` → prepare_run + execute_run;
  `workflow:<name>` → prepare_workflow + execute_workflow;
  `orchestrator` → Phase 5 entry (until Phase 5 lands: RequestError
  internal_error code='CapabilityError' detail 'orchestrator not configured').
  render_cb → emit_update re-emission (message/tool/plan/permission-decision/
  step-transition/limit events normalized to agent_message_chunk for text and
  tool_call/tool_call_update passthrough; non-text events that have no v1
  mapping are emitted as agent_message_chunk summaries only when human-relevant:
  step_started/step_finished/egress_notice/truncation/error).
- Permission bridge: policy ceiling FIRST — deny ⇒ record local deny (client
  never asked, client_response=None). Allow ⇒ forward with context label
  (agent/step) when client supports it; client deny ⇒ deny
  (client_response='denied'); client allow ⇒ allow (client_response=
  'approved'); `ClientPermissionUnsupported` ⇒ mark session no-forwarding,
  emit one visible fallback notice update, resolve THIS and later requests via
  guarded local mediation (fallback recorded per decision:
  policy_source+='(guarded-fallback)').
- `cancel(session_id)` → cancel_event of active run → normal ladder; prompt
  returns stop_reason='cancelled'.
- Client EOF/disconnect (run_agent returns / connection error): cancel active
  runs, persist, release leases, bounded teardown, process exit.
- prompt return mapping: run success|partial → end_turn; cancelled →
  'cancelled'; failed → end_turn AFTER emitting a final error-summary update
  (v1 has no error stop reason; JSON-RPC error is reserved for
  transport/validation failures, not completed-but-failed runs — document).
  Server-mode runs persist identical RunResults (kind agent/workflow/
  orchestrator, not a special server kind).
- Session resume/load not advertised; load_session etc. → method_not_found.

### CLI: `ziggy serve` command — plain stderr logging only, stdout belongs to ACP.

### Tests: loopback fixture

`tests/integration/test_server_loopback.py`: spawn `ziggy serve` as subprocess
via `acp`-free raw NDJSON client (reuse mock-client helpers from test_mocks) in
a tmp ZIGGY_HOME whose config registers mock raw_agent; scenarios: initialize
handshake shape; direct-agent route run streams updates + persists RunResult;
route switch via set_config_option; workflow route; busy (second concurrent
prompt) typed error; cancellation mid-run; permission forwarding (mock agent
permission scenario → server forwards → client approves/denies → decision
recorded with client_response); client without permission support (respond
method_not_found) → guarded fallback notice + local decision; disconnect
(close stdin) → server exits, run persisted cancelled, lease released.
Also `--json`-style assertion that server stdout carries ONLY JSON-RPC frames.

## Phase 5 — Constrained Orchestrator (REQ-013)

### orchestrator/catalog.py

`build_catalog(resolved, registry, workspace) -> Catalog`:
- eligible agents: names in `orchestrator.eligible_agents` that exist in
  registry AND have `orchestration_eligible=True` in their (user-scope)
  AgentConfig; mismatch ⇒ ConfigError at doctor/orchestrate time.
- trusted workflows: `orchestrator.trusted_workflows` entries {path, sha256}:
  canonical path inside workspace or user workflows dir; file content sha256
  must equal pin, else the workflow silently DROPS OUT of the catalog with a
  recorded warning (spec: changed hash drops until re-approved) — not an error.
- Catalog text for the meta-prompt: per agent {name, provider, capability
  one-liner}; per workflow {name, description, variables schema}; descriptions
  are untrusted → wrapped in `<<<ziggy:untrusted-description>>>` delimiters.

### orchestrator/planner.py

- Gate: planner AgentConfig.direct_tools_assumed and NOT
  allow_uncontained_planner ⇒ TrustPolicyError before launch (exit 2). With
  acknowledgement: record {'uncontained_planner_ack': true, enforcement
  'advisory'} in policy provenance AND acquire the workspace lease before
  planner launch, holding through execution/plan-only completion.
  (Contained planners: lease acquired only for the execution stage. v0.1
  reality: every builtin is direct_tools_assumed ⇒ ack required — document.)
- Planning run: empty `tempfile.mkdtemp(prefix='ziggy-plan-')` cwd; env =
  compose_child_env minus inherit extras beyond baseline (HOME, PATH + api key);
  policy: planning profile = deny ALL fs writes, deny terminal, allow fs reads
  ONLY within the temp dir; capture per config; step_id 'plan'. Prompt =
  meta-prompt (goal + catalog + limits + JSON output instructions incl. the
  exact three-variant schema and 'JSON only, no prose' instruction).
  Cleanup temp dir in finally.
- Parse: strip markdown fences if present; locate first balanced JSON object;
  `TypeAdapter(OrchestratorPlan).validate_json` — parse/shape failure produces
  bounded error list (≤10 errors, each ≤200 chars, no raw response echo).
- Security validation (orchestrator/validate.py) on shape-valid plans:
  single_agent/inline agents ∈ catalog eligible set; named_workflow ∈ catalog
  trusted set + variables validate against VariableDef schema (typed, sizes,
  required); inline: ≤ orchestrator.max_inline_steps (≤8 default), unique ids,
  id not colliding with 'plan', deps exist + acyclic (reuse scheduler topo),
  inputs 'goal'|deps-only, prompt+goal composed sizes within engine limits,
  provider set of the planned execution computed (planner provider included in
  run-level egress) + acknowledgement enforced BEFORE execution.
- Repair: on invalid (parse or validation): ONE repair prompt in the SAME
  session listing bounded errors; second failure ⇒ OrchestratorPlanInvalid,
  PlanValidation{attempt_count:2, repair_requested:true, valid:false}. No
  execution agent ever launches on an invalid plan.

### orchestrator/execute.py

Valid plan → execution under the ORIGINAL ceilings (nothing from the plan can
touch config/policy/limits):
- single_agent → internal one-step graph, step id `execute/main`.
- named_workflow → prepare_workflow with plan variables; step ids
  `execute/<declared-id>`.
- inline_agent_workflow → InlineStep list → internal WorkflowDef-equivalent:
  each step prompt validated by interpolate rules with inputs mapping
  ('goal' → pseudo-var vars.goal carrying the original user goal; step refs
  as normal); untrusted-input delimiters as Phase 3; step ids `execute/<id>`.
- One run_id/recorder for plan + execution; kind='orchestrator', target=
  planner agent name; RunResult.plan + plan_validation set; 'plan' StepResult
  (the planning agent turn) + execute/* steps. `--plan-only` / auto_execute
  false ⇒ stop after validation, run status: success if plan valid.
  Generated prompts recorded as untrusted model output (event
  `plan`-step metadata notes semantic safety NOT validated).

### CLI + server

`ziggy orchestrate GOAL [--plan-only --json ...]`; server 'orchestrator' route
calls the same entry. Config `orchestrator.agent` unset ⇒ ConfigError exit 2.

### Tests

Hostile-plan suite (spec §10.2 'Hostile orchestrator output') via mock planner
agent scenarios that return: scripts/command fields/env/paths/policy/resource
keys (each rejected by extra='forbid' → bounded errors), >8 steps, unknown/
non-eligible agents, nested orchestration attempt, template expressions,
unacknowledged provider crossing, invalid then invalid again (no execution,
attempt_count 2), invalid then valid (repair works, execution runs), each
valid plan type end-to-end against mock execution agents (serial order
asserted), plan-only with suspicious-but-valid prompt labeled untrusted,
planning isolation (mock planner records its cwd/env via env_echo-style
scenario → assert empty temp cwd + minimal env + no workspace contents),
uncontained gate (default refuse, project config cannot enable, user ack
records advisory + lease held from before planner launch), cancellation
mid-planning and mid-execution.
