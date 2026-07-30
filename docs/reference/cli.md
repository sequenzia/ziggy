# CLI Reference

Ziggy's command surface is a [Typer](https://typer.tiangolo.com/) app exposed two ways — both run the identical application object (`ziggy.cli.main:app`):

```bash
ziggy <command> [args] [options]      # console script from the installed package
python -m ziggy <command> [args]      # equivalent, no script shim needed
```

Invoking `ziggy` with no arguments prints help and exits — the same is true for each sub-app group (`agents`, `runs`, `config`, `workflow`, `schemas`). Every command supports `--help`.

!!! note "There is no `--version` flag"
    The root app registers no version callback. The only root-level options are Typer's built-in `--install-completion`, `--show-completion`, and `--help`. Everything else is per-command — there are no global flags that apply across commands.

Ziggy is always invoked *from the workspace you want it to act on*: every command resolves configuration and policy against `Path.cwd()`. There is no `--workspace` flag.

## Commands at a glance

| Command | Purpose |
| --- | --- |
| `ziggy run <agent> <prompt>` | One-shot headless run against a registered agent |
| `ziggy orchestrate <goal>` | Plan-then-execute a goal via the configured planner |
| `ziggy workflow run <name\|path>` | Execute one constrained workflow serially |
| `ziggy workflow list` | Show discovered workflows (project scope, then user scope) |
| `ziggy agents list` | Registered agents plus their last-handshake capability summary |
| `ziggy runs list` | Browse persisted runs from the derived index |
| `ziggy runs show <run-id>` | Inspect one persisted `RunResult` manifest |
| `ziggy runs reindex` | Rebuild the derived index from durable manifests |
| `ziggy runs prune` | Delete expired run directories (the only deletion mechanism) |
| `ziggy config show` | Effective merged config with per-field provenance |
| `ziggy config validate` | Validate config; exit 2 on a path-precise `ConfigError` |
| `ziggy schemas dump` | Write the versioned JSON Schema artifacts |
| `ziggy doctor` | Environment diagnostics with per-check pass/fail and hints |
| `ziggy serve` | Serve Ziggy itself as an ACP agent on stdio |

## Common run flags

The three run-producing commands — [`run`](#ziggy-run), [`orchestrate`](#ziggy-orchestrate), and [`workflow run`](#ziggy-workflow-run) — share these flags with identical semantics.

| Flag | Type | Default | Meaning |
| --- | --- | --- | --- |
| `--json` | flag | off | stdout carries **only** the final `RunResult` JSON (2-space indented); all progress, summaries, and warnings move to stderr |
| `--no-save` | flag | off | Do not persist this run. `persisted` is `false`, `result_path` is `null`, and no run directory is created |
| `--capture` | `metadata` \| `standard` \| `debug` | `results.capture` (default `standard`) | Capture profile for this invocation |
| `--plain` | flag | off | Line-oriented output with no ANSI escapes |
| `--acknowledge-egress` | comma-separated string | unset | Provider set whose cross-provider egress you acknowledge for this invocation |

!!! warning "`--capture` raises; `--timeout` only lowers"
    These two flags are **not** symmetric, and it is a common misreading.

    `--capture` is treated as direct user intent and **may exceed** the configured `results.capture` — asking for `debug` on a `standard` config works. The tighten-only merge rule binds *project-scope config*, not your command line.

    `--timeout` (available on `ziggy run` only) is clamped: the effective value is `min(--timeout, engine.default_step_timeout_seconds)`. Passing a larger number than the configured ceiling silently keeps the ceiling.

### `--acknowledge-egress` matching is exact

The flag must name the **exact** crossing provider set — matching is set equality, so order and duplicates are irrelevant but subsets and supersets never match. When both the flag and `[egress] acknowledged_provider_sets` in config would match, the flag wins and the run records `flag:--acknowledge-egress` as the acknowledgement source.

An unacknowledged crossing fails **before any agent launches**, with a rerun hint naming the exact set:

```text
error [TrustPolicyError]: ... rerun with --acknowledge-egress anthropic,openai
```

Acknowledgement records that a crossing happened and that you accepted it. It does not and cannot un-send data. See [Trust and policy](trust-and-policy.md).

---

## `ziggy run`

One-shot headless run against a named agent.

```bash
ziggy run <agent> <prompt> [--json] [--no-save] [--capture PROFILE] [--plain]
                           [--timeout SECONDS] [--acknowledge-egress PROVIDERS]
```

| Argument | Type | Meaning |
| --- | --- | --- |
| `agent` | string (required) | Registered agent name — a v0.1 builtin or an `[agents.<name>]` entry in trusted user config |
| `prompt` | string (required) | The one-shot prompt text |

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `--timeout` | float, `>= 0.0` | `engine.default_step_timeout_seconds` | Step timeout in seconds; may only **lower** the configured ceiling |

Plus every flag in [Common run flags](#common-run-flags).

```bash
# interactive: streamed agent text, then a run summary table
ziggy run claude "summarize the changes on this branch"

# headless: RunResult JSON on stdout, progress on stderr
ziggy run claude "review src/ziggy/policy for fail-open paths" --json > result.json

# raise capture above the configured profile, and tighten the step deadline
ziggy run codex "explain the failing test" --capture debug --timeout 90

# scratch run: nothing written to the run store
ziggy run claude "what version of the protocol do you speak?" --no-save
```

An unknown agent name fails with a `ConfigError` before anything launches (exit 2). A prompt larger than `engine.max_prompt_bytes` fails with a `ResourceLimitError`.

!!! note "Cancelling a run"
    The first `Ctrl-C` sets the engine's cancel event and walks the teardown ladder — the run reaches terminal status `cancelled`, persists a durable manifest, and the CLI exits **130**. A second `Ctrl-C` hard-exits immediately via `os._exit(130)`, skipping teardown entirely (nothing further is persisted or cleaned up).

See [Running agents](../guides/running-agents.md).

## `ziggy orchestrate`

Plan-then-execute one goal via the configured orchestrator.

```bash
ziggy orchestrate <goal> [--plan-only] [--json] [--no-save] [--capture PROFILE]
                         [--plain] [--acknowledge-egress PROVIDERS]
```

| Argument | Type | Meaning |
| --- | --- | --- |
| `goal` | string (required) | Natural-language goal handed to the configured planner |

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `--plan-only` | flag | off | Validate and return the plan without launching execution |

Plus every flag in [Common run flags](#common-run-flags). There is no `--timeout` here; step deadlines come from `engine.default_step_timeout_seconds`.

```bash
# plan and execute
ziggy orchestrate "add retry-with-backoff to the ACP client and cover it with tests"

# validate the plan only — nothing is executed
ziggy orchestrate "migrate the store to WAL mode" --plan-only

# machine-readable: the plan is embedded in the RunResult on stdout
ziggy orchestrate "triage the open bug reports" --plan-only --json | jq '.plan'
```

Under `--json`, stdout is the `RunResult` — the validated plan lives at `.plan` and the validation record at `.plan_validation`. It is not a bare plan document.

Without `--json`, a compact plan summary is printed to stdout after the run summary:

```text
--- plan ---
type: single_agent
rationale: <first 200 characters of the planner's rationale>
steps: execute/main (agent: mock-exec)
```

!!! note "Why the summary is deliberately thin"
    The rationale and every generated prompt are planner **model output** whose semantics were never validated. The summary truncates the rationale at 200 characters and never echoes generated prompts; the full redacted plan lives in the persisted `RunResult`.

`orchestrator.agent` must be set in trusted user config, and a planner assumed to run direct (non-ACP) local tools is refused unless `orchestrator.allow_uncontained_planner = true` — both are exit 2. An invalid plan (`OrchestratorPlanInvalid`) is exit 1. `--plan-only`, and a config with `auto_execute = false`, both produce a *successful* run (exit 0) that stops after validation.

See [Orchestration](../guides/orchestration.md).

## `ziggy workflow run`

Execute one constrained workflow serially in deterministic order.

```bash
ziggy workflow run <name-or-path> [--var NAME=VALUE ...] [--json] [--no-save]
                                  [--capture PROFILE] [--plain]
                                  [--acknowledge-egress PROVIDERS]
```

| Argument | Type | Meaning |
| --- | --- | --- |
| `name_or_path` | string (required) | A discovered workflow name, or a direct path to a YAML file |

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `--var` | `NAME=VALUE`, repeatable | none | One typed workflow variable. The value keeps everything after the first `=` verbatim |

Plus every flag in [Common run flags](#common-run-flags).

```bash
# by discovered name
ziggy workflow run release-notes

# typed variables: integers, JSON, and strings containing '=' all work
ziggy workflow run vars-demo --var n=5 --var 'data={"k": [1, 2]}'

# by direct path (bypasses name discovery)
ziggy workflow run ./.ziggy/workflows/two-step.yaml --json
```

Per-step timeouts come from the workflow YAML, each clamped to `engine.default_step_timeout_seconds`. There is no `--timeout` flag on this command.

These are all usage errors (exit 2), reported before any agent launches: a malformed pair (`--var oops` → `expected <name>=<value>`), a repeated variable name, an undeclared variable, a value that fails its declared type (`--var n=abc` on an integer), an unknown workflow name, and an unacknowledged provider crossing.

See [Workflows](../guides/workflows.md).

## `ziggy workflow list`

Discovered workflows: project scope (`<workspace>/.ziggy/workflows/`) first, then user scope (`$ZIGGY_HOME/workflows/`), sorted by name.

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `--json` | flag | off | Emit discovered workflows as JSON |

```bash
ziggy workflow list
ziggy workflow list --json | jq '.[] | select(.scope == "user") | .name'
```

The table columns are `name`, `scope`, `path`, `description`, `variables`. In the variables column, `*` marks a required variable and `(secret)` marks a secret one — for example `n:integer*, token:string (secret)`. With nothing discovered, the command prints `no workflows found` and exits 0.

!!! warning "Duplicate workflow names are a hard error"
    A name defined in more than one place — across scopes, or as `foo.yaml` next to `foo.yml` — is a `ValidationError` (exit 2) naming **both** paths. It affects `workflow list` and `workflow run` alike. Invoking by direct path is the only bypass.

## `ziggy agents list`

Registered agents with the capability summary from their most recent persisted handshake. No options.

```bash
ziggy agents list
```

Columns: `name`, `builtin`, `provider`, `command`, `orchestration`, `capabilities`. The `capabilities` column is `-` until that agent has completed at least one persisted run; it is read from the newest indexed run's manifest, not from a live probe. For a live handshake, use [`ziggy doctor`](#ziggy-doctor).

## `ziggy runs list`

List persisted runs from the derived SQLite index, newest first (capped at 200 rows).

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `--failed` | flag | off | Only runs that ended without completing all requested work |
| `--kind` | string | unset | Filter by run kind (`agent`, `workflow`, `orchestrator`, …) |
| `--agent` | string | unset | Filter by agent/workflow target name |
| `--since` | string | unset | ISO-8601 date/datetime, or a relative `<N>d` |
| `--json` | flag | off | Emit rows as JSON |

```bash
ziggy runs list
ziggy runs list --failed --since 7d
ziggy runs list --kind workflow --agent release-notes
ziggy runs list --since 2026-07-01 --json | jq -r '.[].run_id'
```

!!! note "`--failed` excludes cancelled runs"
    `--failed` matches exactly `failed`, `partial`, and `abandoned` — the statuses where work did not complete. A run you cancelled yourself is **not** a failure and will not appear. List those with `--kind` plus your own filtering, or read `.status` from `--json` output.

`--since` accepts a full ISO-8601 date or datetime (a naive datetime is interpreted as UTC) or the relative form `<N>d`, such as `7d` for the last seven days. Anything else is a usage error (exit 2). When the index has not been created yet, the command prints `no runs recorded` and exits 0.

## `ziggy runs show`

Inspect one persisted `RunResult` manifest.

```bash
ziggy runs show <run-id> [--json]
```

| Argument / Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `run_id` | string (required) | — | Run id (ULID) |
| `--json` | flag | off | Emit the raw `result.json` manifest |

```bash
ziggy runs show 01JZ8QK3M4N5P6R7S8T9V0W1X2
ziggy runs show 01JZ8QK3M4N5P6R7S8T9V0W1X2 --json | jq '.steps'
```

The human view reads directly from the durable manifest and covers identity and timing, workspace, persistence, config fingerprint, the resolved policy line, per-artifact capture completeness with an explicit `truncation:` line, per-step status with file changes and policy decisions, egress records with their acknowledgement, and run-level errors. Every policy decision carries its `enforcement_scope`.

A missing manifest is a `PersistenceError` — **exit 1**, not 2.

See [Runs and audit](../guides/runs-and-audit.md).

## `ziggy runs reindex`

Rebuild the derived index from durable `result.json` manifests. No options.

```bash
ziggy runs reindex
```

The index is a *derived* cache — deleting `runs/index.db` loses nothing durable. Reindexing also finalizes runs that were interrupted mid-flight, reporting them as `abandoned`:

```text
finalized 2 interrupted run(s) as abandoned
indexed 41 run(s)
```

## `ziggy runs prune`

Explicitly delete expired run directories. This is the **only** deletion mechanism in Ziggy — nothing else removes run history.

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `--older-than` | int, `>= 0` | `results.retention_days` (default `30`) | Delete runs that ended more than DAYS days ago |
| `--dry-run` | flag | off | List candidates without deleting anything |
| `--yes` | flag | off | Actually delete — **required** for any deletion to occur |
| `--all-workspaces` | flag | off | Prune matching runs from every workspace in the store |

```bash
# always start here: see exactly what would go
ziggy runs prune --dry-run

# delete this workspace's runs older than the configured retention window
ziggy runs prune --yes

# tighter window, still scoped to this workspace
ziggy runs prune --older-than 7 --yes

# opt in to every workspace in the global store
ziggy runs prune --all-workspaces --dry-run
```

!!! warning "Two defaults worth internalizing before you run this"
    **1. `--yes` is required.** Without it, `prune` lists the candidate ids on stdout, prints a refusal hint on stderr, deletes nothing, and exits **2**. This is a headless-safe default: an unattended script that forgets `--yes` fails loudly rather than deleting audit evidence.

    **2. The default scope is the current workspace only.** The run store is *global* (`$ZIGGY_HOME/runs`), so an unscoped prune would destroy audit history belonging to every other project on the machine. By default, only runs whose manifest workspace resolves to the current working directory are candidates; `--all-workspaces` opts out of that protection. The active scope is always printed first, before anything else:

    ```text
    scope: workspace /Users/you/dev/repos/ziggy
    ```

Several things are never pruned, by construction: symlinks under `runs/` (they are `lstat`-checked, never followed, never deleted), directories without a durable `result.json` (in-flight or crashed runs), and — in the default scoped mode — any manifest with no attributable workspace. When nothing matches, the command prints `no completed runs older than N day(s)` and exits 0. `--older-than 0` sets the cutoff to now, making every completed run in scope a candidate.

Deleted runs are also dropped from the derived index. If some directories could not be removed, the failures are reported on stderr and the command exits 1.

## `ziggy config show`

Effective merged configuration with per-field source and project action.

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `--json` | flag | off | Emit fields plus provenance as JSON |

```bash
ziggy config show
ziggy config show --json | jq '.fields[] | select(.source == "project")'
```

Every effective leaf field is listed with `field`, `value`, `source` (`default`, `user`, or `project`), and `project-action` (`none`, `tightened`, `applied`, …), followed by the config fingerprint that gets embedded in each `RunResult`. The JSON form is `{fingerprint, warnings, fields}`.

## `ziggy config validate`

Validate the merged configuration. No options.

```bash
ziggy config validate
```

Prints `ok` and exits 0, or reports a path-precise `ConfigError` on stderr and exits 2 — for example a user-only key present in project scope:

```text
error [ConfigError]: project config .../.ziggy/config.toml: server.max_active_runs is forbidden in project scope
```

See [Configuration](configuration.md).

## `ziggy schemas dump`

Write the versioned JSON Schema artifacts for `result.json` and `events.jsonl`.

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `--out` | directory path | current directory | Directory to write the schema files into (created if missing) |

```bash
ziggy schemas dump --out ./schemas
```

Writes `result.v1.json` and `events.v1.json` and prints each path written. These are the exact artifacts shipped as wheel package data, so regenerating them must byte-match the committed files — which makes this command usable as a drift check in CI.

See [Schemas](schemas.md).

## `ziggy doctor`

Environment diagnostics with per-check pass/fail and fix hints.

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `--json` | flag | off | Emit check results as JSON |
| `--agent` | string | unset | Probe a single registered agent |
| `--all` | flag | off | Probe every registered agent, including the vendor-CLI builtins and custom agents |

```bash
ziggy doctor
ziggy doctor --all
ziggy doctor --agent claude
ziggy doctor --agent opencode
ziggy doctor --json | jq '.checks[] | select(.status == "fail")'
```

The default agent scope is `claude` and `codex` — the builtins whose pinned adapters the install docs require. The vendor-CLI builtins (`opencode`, `devin`) are optional installs, so probing them by default would fail the whole run (exit 1) on machines that never wanted them; reach them with `--agent opencode`, `--agent devin`, or `--all`. `--all` widens the scope to every registered agent, and `--agent NAME` narrows it to one. **`--agent` takes precedence over `--all`** when both are given. An unknown `--agent` name is a usage error (`ConfigError`, exit 2) rather than a check failure.

Checks run in this order — global, then one block per selected agent, then the remaining globals:

| Check | What it establishes |
| --- | --- |
| `config-load` | User and project config parse and merge cleanly |
| `config-forbidden-project-keys` | Project scope contains no user-only settings |
| `store-writable` | The run store root accepts writes |
| `index-integrity` | SQLite `PRAGMA integrity_check` on the derived index |
| `agent-command-resolvable:<agent>` | The registered command exists and is executable |
| `api-key-env-set:<agent>` | The named `api_key_env` variable is present |
| `acp-handshake:<agent>` | A live launch, `initialize` round-trip, and clean shutdown |
| `capability-summary:<agent>` | Capabilities reported by that handshake |
| `direct-tools-advisory:<agent>` | Whether the agent is assumed to run direct, non-ACP local tools |
| `orchestrator-planning-eligibility` | The configured planner may plan |
| `trusted-workflow-hashes` | Each configured `trusted_workflows` entry still matches its sha256 |
| `server-readiness` | Lease directory writable; route count and `max_active_runs` |

Statuses are `pass`, `fail`, `warn`, and `skip`. **Only `fail` affects the exit code** — a `warn` still exits 0. When configuration itself cannot load, every config-dependent check is reported `skip` rather than guessed at.

Human output prints one line per check, with a `hint:` line for `fail` and `warn`, ending in `doctor: ok` or `doctor: problems found`. JSON output is `{"ok": bool, "checks": [{name, status, detail, hint}]}`.

!!! note "`direct-tools-advisory` is expected to warn"
    Every builtin is assumed to have direct filesystem and shell tools, so this check warns that ACP mediation for them is **advisory** — Ziggy observes and records the ACP client-bound surface, and an agent subprocess is a normal OS process that can act outside it. The hint points to running the agent under a separately verified OS sandbox if you need hard enforcement. See [Trust and policy](trust-and-policy.md).

Two guarantees hold regardless of scope: `api_key_env` is checked for *presence* only — the value is never read into a message or printed — and the handshake probe never downloads anything (builtin commands stay behind `npx --no-install`, and resolvability is probed with `which` alone). The probe denies every mediated request it might receive and allows 20 seconds for one `initialize` round-trip.

## `ziggy serve`

Serve Ziggy itself as an ACP agent over JSON-RPC on stdio. No options.

```bash
ziggy serve
```

This is meant to be launched by an ACP client, not typed interactively. **stdout carries ACP JSON-RPC frames and nothing else**; all logging and diagnostics go to stderr at `INFO`. Client EOF or disconnect — and `SIGTERM`/`SIGINT` — cancels every active run, persists what it can, releases workspace leases, and exits after bounded teardown. Concurrency is bounded by `server.max_active_runs`, which is logged at startup.

See [ACP server](../guides/acp-server.md).

---

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | Execution or required-persistence failure |
| `2` | Usage, configuration, or trust-policy error |
| `130` | User cancellation |

For run-producing commands the code is derived from the terminal `RunResult`, in this order:

1. Status `cancelled` → **130**. Cancellation wins over any other errors present on the result.
2. Otherwise, the first run-level typed error's mapped code (unrecognized codes fall back to 1).
3. Otherwise, **0** for status `success`, **1** for any other terminal status (`failed`, `partial`, `abandoned`).

That second rule is why an otherwise successful run whose manifest could not be written still exits 1: the `PersistenceError` on the result maps to 1 even though the status is `success`.

Typed errors map to exit codes as follows:

| Exit | Error codes |
| --- | --- |
| `2` | `ValidationError`, `ConfigError`, `TrustPolicyError` (cross-provider egress that was not acknowledged serializes under this code) |
| `130` | `CancelledError` |
| `1` | `AgentLaunchError`, `ProtocolError`, `CapabilityError`, `PermissionDeniedError`, `StepTimeoutError`, `ResourceLimitError`, `OrchestratorPlanInvalid`, `ServerBusyError`, `WorkspaceBusyError`, `PersistenceError`, `AbandonedError` |

Non-run commands follow the same taxonomy, with a few specifics worth knowing:

- Typer usage errors — a missing argument, an unknown command, a bad `--since` value — exit **2**.
- `ziggy doctor` exits **1** when any check fails (`warn` does not count).
- `ziggy runs show` on a missing manifest exits **1** (`PersistenceError`).
- `ziggy runs prune` without `--yes`, when there are candidates, exits **2**; a partial deletion failure exits **1**.

## Environment variables

| Variable | Default | Effect |
| --- | --- | --- |
| `ZIGGY_HOME` | `~/.ziggy` | Root for user config, the run store, logs, workspace leases, and user-scope workflows |
| `NO_COLOR` | unset | Any non-empty value forces plain output |
| `ZIGGY_<SECTION>__<KEY>` | unset | User-scope override for a single config field |

`ZIGGY_HOME` governs four locations at once, which makes it the cleanest way to run against an isolated store — in tests, in CI, or when you want a scratch environment:

```text
$ZIGGY_HOME/config.toml     # user-scope (trusted) configuration
$ZIGGY_HOME/runs/           # run directories + the derived index.db
$ZIGGY_HOME/logs/           # daily-rotated metadata logs
$ZIGGY_HOME/workflows/      # user-scope workflow definitions
```

```bash
ZIGGY_HOME=/tmp/ziggy-scratch ziggy run claude "hello" --no-save
```

### Config overrides via environment

`ZIGGY_<SECTION>__<KEY>` — note the **double** underscore separating section from key — sets one configuration leaf. These are **user-scope** overrides applied over the user config file, so they carry full trusted-user authority; project scope cannot reach them.

```bash
ZIGGY_RESULTS__RETENTION_DAYS=7 ziggy config show
ZIGGY_ENGINE__DEFAULT_STEP_TIMEOUT_SECONDS=120 ziggy run claude "long task"
ZIGGY_SERVER__MAX_ACTIVE_RUNS=2 ziggy serve
```

Only scalar leaves are settable this way: values are coerced to the declared field type, and lists or tables (agents, permission profiles, redaction patterns, acknowledged provider sets) raise a `ConfigError`. So do unknown sections and unknown keys — the form fails loudly rather than silently ignoring a typo. Coercion errors never echo the raw value, since it may be secret-shaped.

`ZIGGY_HOME` itself contains no `__` and so is never mistaken for a config override.

See [Configuration](configuration.md) for the full field list and merge rules.

## Output modes

### The `--json` contract

Under `--json`, **stdout carries only the machine-readable document**. Progress lines, the end-of-run summary, config warnings, and errors all move to stderr. This holds for `run`, `orchestrate`, and `workflow run`, whose stdout is the final `RunResult` as 2-space-indented JSON, and equally for `runs list`, `runs show`, `workflow list`, `config show`, and `doctor`.

That separation is what makes piping safe:

```bash
ziggy run claude "audit the policy module" --json | jq -r '.steps.main.outputs.text'
ziggy runs list --failed --json | jq -r '.[].run_id' | while read -r id; do
  ziggy runs show "$id" --json | jq '{id: .run_id, errors: .errors}'
done
```

When a command fails before producing a document, stdout stays empty and the error appears on stderr in a stable form:

```text
error [<ErrorCode>]: <message>
```

Non-fatal configuration warnings use `warning: <message>`, also on stderr.

### Plain mode

Plain mode emits line-oriented output with no ANSI escapes and no live status line. It activates through **three** routes, any one of which is sufficient:

1. `--plain` is passed.
2. `NO_COLOR` is set to a non-empty value.
3. The target stream is not a TTY.

!!! note "Piping selects plain mode for you"
    Because of route 3, redirecting or piping output already yields clean, escape-free text — `ziggy run ... > run.log` and `ziggy agents list | grep claude` need no flag. `--plain` is for the case where you want plain output *on a real terminal*.

Rich mode (an interactive TTY with none of the above) adds a transient status line and a formatted end-of-run summary table. Plain mode renders the same summary as labelled lines:

```text
--- run summary ---
status: success
duration: 4182 ms
files changed: 3
permissions denied: 0
result: /Users/you/.ziggy/runs/01JZ8QK3M4N5P6R7S8T9V0W1X2/result.json
```

Note which stream the renderer targets: without `--json`, progress goes to **stdout**; with `--json`, the renderer is pointed at **stderr** so stdout stays machine-readable. The plain-mode decision is made against whichever stream is in use.

---

## See also

- [Getting started](../getting-started.md) — install, first run, and verifying agent handshakes
- [Running agents](../guides/running-agents.md) — what a direct run does end to end
- [Workflows](../guides/workflows.md) — authoring constrained multi-step workflows
- [Orchestration](../guides/orchestration.md) — planner configuration and plan validation
- [Runs and audit](../guides/runs-and-audit.md) — the run store, manifests, and retention
- [ACP server](../guides/acp-server.md) — running Ziggy as an agent for another client
- [Configuration](configuration.md) — every field, scope, and merge rule
- [Trust and policy](trust-and-policy.md) — what mediation observes, and what it cannot
- [Schemas](schemas.md) — `result.json` and `events.jsonl` structure
