# Phase 2 Contracts — Config/Trust, Mediation Policy, CLI, Doctor, Logs

Extends ARCHITECTURE.md. Normative structures for Phase 2 modules.

## config/ — schema (pydantic, `extra='forbid'` everywhere)

```python
class EngineConfig:      max_workflow_steps:int=16; max_prompt_bytes:int=262144
                         default_step_timeout_seconds:int=1800
                         default_workflow_timeout_seconds:int=3600
                         cancel_grace_seconds:float=5.0
                         max_event_bytes_per_step:int=10*2**20
                         max_artifact_bytes_per_run:int=50*2**20
class PermissionsConfig: default_policy:str='guarded'    # 'guarded' is the only builtin name v0.1
                         profiles:dict[str,PolicyProfile]={}   # user-defined, deny/allow rules
class PolicyProfile:     terminal_allowlist:list[TerminalRule]=[]
                         deny_paths:list[str]=[]         # extra sensitive-path globs
                         allow_read_outside_workspace:bool=False   # user scope may widen reads? NO — v0.1 keeps guarded semantics; field rejected for now (do not implement)
class TerminalRule:      command:str; args_prefix:list[str]=[]     # exact argv[0] + prefix match
class ResultsConfig:     persist:bool=True; capture:CaptureProfile='standard'
                         retention_days:int=30; auto_prune:bool=False
                         store_path:str|None=None        # USER_ONLY
class ServerConfig:      max_active_runs:int=1
class OrchestratorConfig: agent:str|None=None; max_inline_steps:int=8
                         auto_execute:bool=True; allow_uncontained_planner:bool=False
                         eligible_agents:list[str]=[]
                         trusted_workflows:list[TrustedWorkflow]=[]  # {path, sha256}
class RedactionConfig:   extra_value_env_vars:list[str]=[]  # env var NAMES whose values redact
                         patterns:list[CustomPatternCfg]=[] # {kind, regex, max_width?}
class LogsConfig:        retention_days:int=30
class WorkflowsConfig:   default_name:str|None=None       # PROJECT_OK
class AgentEntry:        command:str|None; args:list[str]=[]; env:dict[str,str]={}
                         inherit_env:list[str]=[]; working_dir:str|None
                         api_key_env:str|None; provider:str|None
                         orchestration_eligible:bool=False
                         acknowledged_egress:list[list[str]]=[]  # provider sets, USER_ONLY (top-level [egress] section alt: keep here? -> put on ResultsConfig? Decision: top-level `[egress] acknowledged_provider_sets = [["anthropic","openai"]]`)
class EgressConfig:      acknowledged_provider_sets:list[list[str]]=[]
class ZiggyConfig:       schema_version:Literal[1]; engine; agents:dict[str,AgentEntry]
                         permissions; results; server; orchestrator; redaction; logs; workflows; egress
```

## Merge rules (field-path → rule)

- `USER_ONLY` (project presence ⇒ rejected, recorded, `ConfigError` listing path):
  `agents.*` (entire table), `orchestrator.*` except noted, `server.*`,
  `results.store_path`, `results.persist`, `logs.*`, `redaction.*`,
  `egress.*`, `permissions.profiles` (profile definitions).
- `TIGHTEN_MIN` (project may lower only): `engine.max_workflow_steps`,
  `engine.max_prompt_bytes`, `engine.default_step_timeout_seconds`,
  `engine.default_workflow_timeout_seconds`, `engine.max_event_bytes_per_step`,
  `engine.max_artifact_bytes_per_run`, `results.retention_days`.
- `TIGHTEN_CAPTURE`: `results.capture` — project may move toward LESS capture
  only (standard→metadata OK; anything→debug rejected; metadata→standard rejected).
- `TIGHTEN_POLICY`: `permissions.default_policy` — project may reference a
  profile name ONLY to *add* deny constraints via `permissions.project_denials`
  (list of deny rules); it cannot select/replace profiles or create approvals.
- `PROJECT_OK`: `workflows.default_name`.
- Env overrides `ZIGGY_SECTION__KEY=value` (user scope), parsed to field type;
  lists/dicts unsupported via env (ConfigError).

Loader outputs `ResolvedConfig`: `.config` (ZiggyConfig), `.provenance`
(dict field-path → {source, project_action}), `.fingerprint` (sha256 of
canonical non-secret dump + provenance), `.warnings`. Secret-looking literal
values anywhere in config ⇒ `ConfigError` (run Redactor built-ins over every
string value). Registered agent validation: builtin names (claude, codex, opencode, devin) may
omit command (defaults from agents/builtins.py); custom agents require command.

## Child environment composition (engine/env.py)

`compose_child_env(agent: AgentConfig, base=os.environ) -> dict`:
minimal baseline = {HOME, PATH, TERM?, LANG?} (documented) + agent.inherit_env
(names present in parent) + agent.env literals + api_key_env value if set.
Missing api_key_env value ⇒ ConfigError BEFORE launch (per §5.1 error table).
Test: env_echo mock scenario shows exactly composed keys.

## policy/ — guarded mediation

```python
class MediationPolicy:
    @classmethod def guarded(cls, *, workspace: Path, step_dir: Path, profile: PolicyProfile|None,
                             project_denials, sensitive_globs) -> MediationPolicy
    def decide_permission(self, req: PermissionRequestN) -> Decision
    def decide_fs_read(self, path: str) -> Decision
    def decide_fs_write(self, path: str) -> Decision
    def decide_terminal(self, req: TerminalRequestN) -> Decision
class Decision: allowed: bool; rule_id: str; policy_name: str; policy_source: str
                reason: str; enforcement_scope = acp_mediated
```

Rule ids (stable strings, recorded): `read-in-workspace-allow`,
`write-in-stepdir-allow`, `terminal-user-allowlist`, `sensitive-path-deny`,
`outside-workspace-deny`, `outside-stepdir-deny`, `terminal-default-deny`,
`unmatched-default-deny`, `project-denial:<n>`, `path-ambiguity-deny`.
Deny always wins; intersection semantics; unknown request kinds ⇒
`unmatched-default-deny`.

Path logic (policy/paths.py): `resolve_contained(base: Path, candidate: str) -> Path`
— canonicalize via `Path.resolve(strict=False)` + `os.path.realpath` of deepest
existing ancestor; symlink escape / `..` escape / non-relative ⇒ raise
`PathAmbiguity`; fail closed. Sensitive default globs: `**/.env`,
`**/.env.*`, `**/*_key`, `**/*.pem`, `**/id_rsa*`, `**/.aws/**`, `**/.ssh/**`,
`**/.ziggy/config.toml` + user config additions.

Permission-request mapping: ACP permission requests carry a ToolCallUpdate;
derive request kind from `tool_call.kind` + locations: read→fs_read decision
per location, edit/delete/move→fs_write decision per location,
execute→terminal decision, everything else/no locations ⇒ unmatched default
deny. If ANY location denies, deny overall (record which rule).

## cli/ — typer surface (Phase 2 subset)

`ziggy run <agent> <prompt>` `[--json] [--no-save] [--capture ...]
[--plain] [--timeout N] [--acknowledge-egress p1,p2]`,
`ziggy agents list`, `ziggy runs list [--failed --kind --agent --since N]`,
`ziggy runs show <run-id>`, `ziggy runs reindex`,
`ziggy runs prune [--older-than D] [--dry-run]`,
`ziggy config show` / `ziggy config validate`, `ziggy doctor [--json --agent X --all]`.
stdout/stderr contract: `--json` ⇒ ONLY final RunResult JSON on stdout, all
progress/diagnostics stderr. Rich rendering honors `--plain`, `NO_COLOR`,
non-TTY. Exit codes via `ZiggyError.exit_code`; cancellation 130 via SIGINT
handler setting cancel_event; second SIGINT hard-exits.
Renderer consumes EventEnvelopes from RunRecorder render_cb; summary table at
end: status, duration, files changed, permissions denied, result path.

## doctor checks (each `{name, status: pass|fail|warn|skip, detail, hint}`)

config-load, config-forbidden-project-keys, agent-command-resolvable (shutil.which,
no download; `npx --no-install` probe for builtins), api-key-env-set (presence
only), acp-handshake (live `initialize` per requested agent, then clean shutdown),
capability-summary, direct-tools-advisory (from capability matrix assumption),
orchestrator-planning-eligibility, trusted-workflow-hashes, server-readiness
(route config + lease dir writable + max_active_runs), store-writable,
index-integrity. Default scope: builtins; `--agent X`, `--all` widen.

## store/logs.py — metadata logger

JSONL `~/.ziggy/logs/ziggy-YYYY-MM-DD.jsonl`, fields
`{ts, level, event, run_id?, step_id?, agent?, detail?}` — metadata only (no
prompts/payloads/secret names). Lifecycle events per §5.10. Daily file naming =
rotation; retention prune of files older than logs.retention_days on logger
open. `--no-save` ⇒ logger disabled for that run.
