# Phase 3 Contracts — Constrained Workflow Engine

Extends ARCHITECTURE.md + phase2-contracts.md. Implements REQ-009/010/011.

## Modules

- `workflows/schema.py` — YAML load/validate → `WorkflowDef` (models exist).
  yaml.safe_load ONLY. Path-precise errors (`ValidationError`) carrying file +
  key path. A `type:` other than `agent` (script/shell/python/...) must produce
  the specific message "step type '<t>' is not supported in schema version 1
  (deferred post-MVP)" — not an anonymous enum error.
- `workflows/discovery.py` — search `./.ziggy/workflows/*.{yaml,yml}` then
  `~/.ziggy/workflows/` (ZIGGY_HOME-aware). Name = YAML `name` field (must match
  filename stem; mismatch = ValidationError). Duplicate name across scopes or
  within scope ⇒ error naming both paths, unless invoked by direct path.
  Direct path must canonicalize inside the invocation workspace OR inside the
  user workflows dir (REQ-009: project workflow files resolved canonically
  inside the workspace).
- `workflows/interpolate.py` — restricted value-only templating:
  - Tokens: `{{ vars.<name> }}` and `{{ inputs.<name> }}` only (regex
    `\{\{\s*(vars|inputs)\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}`). Anything else
    brace-shaped (`{{ x }}`, `{% %}`, filters, attribute access) ⇒ ValidationError
    at workflow validation time.
  - `validate_template(prompt, declared_vars, declared_inputs)` at load;
    `render(prompt, vars, inputs)` at run — pure string substitution, single
    pass over the template (values are NEVER re-scanned for tokens).
  - Step-output input values are wrapped in deterministic delimiters:
    `<<<ziggy:untrusted-input name="<n>" source="steps.<id>.outputs.text">>>`
    newline, raw value, newline, `<<<ziggy:end-untrusted-input name="<n>">>>`.
    Variables are inserted verbatim (user-provided, not model output).
- `workflows/vars.py` — typed `--var k=v` parsing per VariableDef: string
  verbatim; integer/boolean strict parse; json via json.loads. Unknown name /
  missing required / max_bytes (encoded UTF-8) violations ⇒ ValidationError.
  Secret vars: interpolation into a step prompt requires
  `workflows.secret_variable_allowances = { <var> = ["<provider>", ...] }`
  (USER_ONLY config, new field) covering the destination step's provider;
  otherwise ValidationError before execution. Secret var values register as
  exact-match redaction values and are redacted from inputs_resolved.
- `workflows/scheduler.py` — serial deterministic execution:
  - Edges = union of `depends_on` + steps referenced by `inputs`. Kahn topo
    with declaration order as the tie-break (dict order of steps mapping).
    Cycle/unknown refs ⇒ ValidationError at load.
  - Execution loop runs steps strictly one at a time in the precomputed order.
    First step failure: transitive dependents → `blocked`; every other
    not-yet-run step → `skipped`; loop ends. Cancellation: active step
    `cancelled`, not-yet-run → `skipped`, run `cancelled`. Workflow deadline
    (monotonic) checked before each step and enforced around the active step
    (deadline exceeded mid-step = that step's timeout path, remaining skipped,
    run `partial`/`failed` per normal aggregation + StepTimeoutError at run level).
  - Aggregate: all success → success; any success + any failure/blocked/skipped
    → partial; no success → failed (cancelled/abandoned override per status rules).
- `workflows/egress.py` — provider flow: step provider from AgentConfig.provider
  (fallback "custom:<agent-name>"). Crossing exists iff a data edge connects
  steps with different providers OR a secret/var flows to any provider (vars
  don't count as crossings; only inter-provider step-output flow does).
  `required_provider_set(workflow, registry) -> frozenset[str]` = providers of
  all steps participating in any cross-provider data edge. Acknowledgement:
  exact set match against `egress.acknowledged_provider_sets` (order-free) or
  `--acknowledge-egress p1,p2`. Headless + unacknowledged ⇒
  EgressNotAcknowledgedError (exit 2) BEFORE any launch. EgressRecord emitted
  per step with input lineage; `egress_notice` event before execution.
- `engine/lease.py` — WorkspaceLease per ARCHITECTURE/spec §6.4:
  `acquire(store, workspace, run_id) -> Lease` writes
  `leases/<sha256(canonical workspace)>.json` via O_EXCL (0600) with
  {workspace, run_id, owner_pid, owner_process_start (psutil-free: read process
  start time from `ps -o lstart= -p PID` output hash or /proc equivalent — on
  macOS use `os.stat('/proc')`-free fallback: store (pid, pid_create_check())
  where pid_create_check = hash of `ps -p PID -o pid,lstart` output), pgid,
  acquired_at}. Existing lease: owner alive (kill(pid,0) ok AND start marker
  matches) ⇒ WorkspaceBusyError(details incl. run_id). Provably dead (ESRCH on
  pid AND on recorded pgid via killpg probe) ⇒ replace atomically. Ambiguous
  (EPERM, marker mismatch-but-alive-pid, unreadable file) ⇒ WorkspaceBusyError.
  `release()` removes only if file still contains our run_id. Acquired before
  any agent launch in EVERY run kind (direct, workflow, orchestrated) —
  project config cannot disable. Release on terminal state/teardown (finally).
- `workflows/runner.py` — `execute_workflow(prepared: PreparedWorkflow, *,
  render_cb, cancel_event) -> RunResult`:
  One run_id, one RunRecorder, one events.jsonl for the whole run. Per step:
  compose prompt (interpolate) → size ceiling check → fresh AgentProcessClient
  (own subprocess/session, step-scoped MediationPolicy with step working_dir)
  → step events with step_id set → StepResult from recorder aggregations.
  Reuse the same internal step-execution helper as direct runs — refactor
  `engine/runner.py` to expose `execute_step(...)` shared by both paths without
  breaking its public `execute_run` contract or Phase-1/2 tests.
- `engine/prepare.py` gains `prepare_workflow(resolved, *, name_or_path, vars,
  workspace, overrides) -> PreparedWorkflow` (discovery → schema → vars →
  interpolation validation → limits preflight → egress preflight → per-step
  policy construction → lease manager).

## CLI additions

`ziggy workflow run <name|path> [--var k=v]... [--json --no-save --capture
--plain --acknowledge-egress SET]` and `ziggy workflow list [--json]`
(discovered workflows: name, source path, description, variables). Exit codes
per global mapping; validation errors exit 2 with path-precise messages.

## Test obligations (Phase-3 gate)

Deterministic mock-driven state machine scenarios (§10.2 critical path 1):
4-step DAG failure propagation (blocked vs skipped vs unchanged), cancellation
mid-workflow, per-step timeout, workflow deadline, partial aggregation,
resource ceilings (step count, prompt bytes, var bytes), duplicate/cyclic/
unknown-ref rejection, template injection attempt via step output containing
`{{ vars.x }}` (must appear literally in downstream prompt, never interpolated,
wrapped in untrusted delimiters), egress crossing detection + acknowledgement
(config and flag) + headless failure, lease conflict (two concurrent workflow
runs same workspace), hostile workflow YAML additions to the security suite
(script step type, absolute working_dir, symlink working_dir escape, policy
field attempts, oversized vars). The `review-and-fix` example from the spec
must be committed under `examples/workflows/review-and-fix.yaml` and validate.
