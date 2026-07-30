# Phase 0 — Built-in Agent Capability Matrix

**Status: live probes DEFERRED by explicit project decision (2026-07-28).**
Implementation is mock-first; every row below that requires observing a real
adapter is marked `UNVERIFIED (live probe deferred)` and is **not** load-bearing
for any feature or security claim. Code paths depending on unverified behavior
must be capability-gated at handshake time, never assumed. This file is the
checklist to complete when live probes run (see "Deferred live checklist").

## Pinned launch metadata (reviewed; from spec §12.1 + ACP registry review)

Built-ins come in two launch shapes. The distinction is load-bearing and is
encoded in `ziggy.agents.builtins` (`VENDOR_CLI_AGENTS`, `DEFAULT_PROBED_AGENTS`).

### npm-adapter built-ins (release-gating)

| Agent | Package | Pinned version | Launch command (default) | Auth |
|-------|---------|----------------|--------------------------|------|
| claude | `claude-agent-acp` (npm) | 0.63.0 | `npx --no-install claude-agent-acp@0.63.0` *(requires prior explicit install; Ziggy never auto-downloads)* | `ANTHROPIC_API_KEY` env or adapter-managed login via `HOME` |
| codex | `codex-acp` (npm) | 1.1.7 | `npx --no-install codex-acp@1.1.7` | ChatGPT login state or `OPENAI_API_KEY` |

`--no-install` enforces REQ-002's no-silent-download rule: if the pinned package
is absent, launch fails (`AgentLaunchError`) and it is never fetched at run time.
`ziggy doctor` verifies resolvability the same way and prints the exact install
line from `INSTALL_HINTS`, which the launch error itself cannot — it sees only
the command, not the agent name.

### Vendor-CLI built-ins (registered by default; optional install)

These agents speak ACP from the vendor CLI itself, so **there is no npm adapter
to pin**. The command is resolved on `PATH` (`shutil.which`) and a missing binary
fails the launch — nothing is ever downloaded — but the launch command **cannot
pin a version**.

| Agent | Distribution | Reviewed version | Launch command (default) | Auth |
|-------|--------------|------------------|--------------------------|------|
| opencode | `opencode-ai` (npm), install script, or Homebrew | 1.18.9 *(install-hint only; not enforced at launch)* | `opencode acp` *(PATH-resolved)* | `opencode auth login` state, or the configured provider's own env vars |
| devin | `devin-cli` cask / `cli.devin.ai/install.sh` — **no pinnable version published** | n/a | `devin acp` *(PATH-resolved)* | browser login to a Devin Cloud account |

Consequences, all deliberate:

- **Version identity comes from the handshake, never from these constants.**
  `OPENCODE_REVIEWED_VERSION` feeds install hints and this table only. The real
  running version is `agentInfo.version` from `initialize`, recorded per run.
- **Egress identity is `custom:<name>`.** Neither is labelled with a vendor:
  OpenCode is provider-agnostic (it routes to whichever model provider the user
  configured) and the Devin CLI's model routing is unverified here, so labelling
  either `anthropic`/`openai` would misstate egress. The value is *declared*
  rather than left to the `custom:<name>` fallback because it is an interface:
  it lands in persisted RunResults and in users' `[egress]
  acknowledged_provider_sets`. A workflow mixing `claude` with `opencode` crosses
  providers and needs `--acknowledge-egress anthropic,custom:opencode`.
- **A bare `ziggy doctor` does not probe them.** Its default scope is
  `DEFAULT_PROBED_AGENTS` (claude, codex) — the pair whose adapters the install
  docs require. Probing an optional CLI nobody installed would fail the whole
  run (exit 1). Use `ziggy doctor --agent opencode` / `--agent devin` / `--all`.
- **The npm name is not a trust root.** `opencode-acp` on npm is an unrelated
  third-party package ("Active Context Pruning"), and `devin` on npm is an
  unrelated personal package. Neither is a vendor ACP adapter; do not pin them.

## Handshake-derived state (always trusted over cache)

Recorded per run at `initialize`: negotiated `protocolVersion`, `agentInfo`
(name/title/version), `agentCapabilities` (loadSession, promptCapabilities,
mcpCapabilities, sessionCapabilities), `authMethods`. Cached summaries shown by
`ziggy agents list` are diagnostic hints only.

## Behavior matrix (to be populated by live probes)

| Probe | claude-agent-acp 0.63.0 | codex-acp 1.1.7 |
|-------|--------------------------|------------------|
| Install + resolvable without download | UNVERIFIED (live probe deferred) | UNVERIFIED (live probe deferred) |
| `initialize` capabilities snapshot | UNVERIFIED | UNVERIFIED |
| Auth failure shape (missing key/login) | UNVERIFIED | UNVERIFIED |
| Streams `agent_message_chunk` | UNVERIFIED | UNVERIFIED |
| Emits tool_call / tool_call_update | UNVERIFIED | UNVERIFIED |
| Uses `session/request_permission` | UNVERIFIED | UNVERIFIED |
| Uses client `fs/*` (vs direct FS access) | UNVERIFIED — treat as **direct tools present** | UNVERIFIED — treat as **direct tools present** |
| Uses client `terminal/*` (vs direct shell) | UNVERIFIED — treat as **direct shell present** | UNVERIFIED — treat as **direct shell present** |
| Honors `session/cancel` promptly | UNVERIFIED | UNVERIFIED |
| Child process tree shape on cancel | UNVERIFIED | UNVERIFIED |
| File-change visibility (ACP-reported diffs vs workspace-derived) | UNVERIFIED | UNVERIFIED |
| Usage/cost updates exposed | UNVERIFIED | UNVERIFIED |
| Thought summaries exposed | UNVERIFIED | UNVERIFIED |

### Vendor-CLI built-ins (nothing here has been observed)

Neither CLI was installed when these agents were registered, so **every row is
unprobed** — including the version actually running and whether the `acp`
subcommand's ACP surface matches what the vendor docs describe. The quirks named
illustratively in spec REQ-002 (Devin degraded terminal rendering, OpenCode
missing undo/redo over ACP) are vendor-doc hearsay here, not probe results:
they stay UNVERIFIED rows and are **not** entered into `KNOWN_DEGRADATIONS`.

| Probe | opencode acp | devin acp |
|-------|--------------|-----------|
| CLI present + `acp` subcommand resolvable | UNVERIFIED (live probe deferred) | UNVERIFIED (live probe deferred) |
| Running version (`agentInfo.version`) | UNVERIFIED — no launch-time pin exists | UNVERIFIED — no launch-time pin exists |
| `initialize` capabilities snapshot | UNVERIFIED | UNVERIFIED |
| Auth failure shape (no login / no Devin Cloud account) | UNVERIFIED | UNVERIFIED |
| Streams `agent_message_chunk` | UNVERIFIED | UNVERIFIED |
| Emits tool_call / tool_call_update | UNVERIFIED | UNVERIFIED |
| Uses `session/request_permission` | UNVERIFIED | UNVERIFIED |
| Uses client `fs/*` (vs direct FS access) | UNVERIFIED — treat as **direct tools present** | UNVERIFIED — treat as **direct tools present** |
| Uses client `terminal/*` (vs direct shell) | UNVERIFIED — treat as **direct shell present** | UNVERIFIED — treat as **direct shell present** |
| Honors `session/cancel` promptly | UNVERIFIED | UNVERIFIED |
| Child process tree shape on cancel | UNVERIFIED | UNVERIFIED |
| File-change visibility | UNVERIFIED | UNVERIFIED |
| Usage/cost updates exposed | UNVERIFIED | UNVERIFIED |
| Egress destination(s) actually contacted | UNVERIFIED — `custom:opencode` until observed | UNVERIFIED — `custom:devin` until observed |
| Spec REQ-002 quirk (undo/redo over ACP · terminal rendering) | UNVERIFIED (vendor-doc claim, unprobed) | UNVERIFIED (vendor-doc claim, unprobed) |

**Consequence of UNVERIFIED direct-tool rows:** until live probes prove otherwise,
all four built-ins are classified `direct_tools_assumed = true`. `ziggy doctor` and
RunResults label their mediation `advisory` (`enforcement_scope = acp_mediated`
for individual mediated decisions; never `os_enforced`). The orchestrator's
uncontained-planner gate treats them all as uncontained by default (planning with
them requires `allow_uncontained_planner = true`). This is the conservative,
honest default the spec requires.

## Deferred live checklist (run before v0.1 release)

1. `npm install -g claude-agent-acp@0.63.0 codex-acp@1.1.7` (reviewed exact versions; record hashes).
2. `ziggy doctor` — handshake per agent; capture capabilities JSON into this file.
3. `pytest -m live` — 20-run smoke set per built-in (§3.2 target ≥95%).
4. Probe direct-tool behavior: run each adapter against a canary workspace with
   client `fs`/`terminal` capabilities disabled; observe whether edits/commands
   still occur (direct tools) or the agent degrades (mediated).
5. Cancellation probe: long prompt + `session/cancel`; record time-to-stop and
   surviving descendants.
6. File-change probe: dirty git workspace, untracked files, binary file, agent-made
   commit; classify per-source capture status (`complete|partial|derived|unavailable`).
7. Vendor-CLI built-ins (`opencode`, `devin`), which no step above covers because
   `ziggy doctor` skips them by default:
   - Install both (`npm install -g opencode-ai@1.18.9`; `brew install --cask devin-cli`
     or `curl -fsSL https://cli.devin.ai/install.sh | bash`) and record what
     `opencode --version` / `devin --version` report.
   - `ziggy doctor --agent opencode` and `--agent devin`: confirm the `acp`
     subcommand completes `initialize`, and capture `agentInfo.version` — the only
     version identity these two have.
   - Decide each one's `provider` from the observed egress destination, or leave it
     unset (`custom:<name>`) if routing is genuinely user-configurable.
   - Re-run steps 3–6 for both, then reconsider `DEFAULT_PROBED_AGENTS`: an agent
     whose contract suite passes and whose CLI the install docs require belongs in
     the default `ziggy doctor` scope.
8. Update this matrix + per-agent `KNOWN_DEGRADATIONS` in `ziggy/agents/builtins.py`.
