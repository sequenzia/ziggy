# Phase 0 — Built-in Agent Capability Matrix

**Status: live probes DEFERRED by explicit project decision (2026-07-28).**
Implementation is mock-first; every row below that requires observing a real
adapter is marked `UNVERIFIED (live probe deferred)` and is **not** load-bearing
for any feature or security claim. Code paths depending on unverified behavior
must be capability-gated at handshake time, never assumed. This file is the
checklist to complete when live probes run (see "Deferred live checklist").

## Pinned launch metadata (reviewed; from spec §12.1 + ACP registry review)

| Agent | Package | Pinned version | Launch command (default) | Auth |
|-------|---------|----------------|--------------------------|------|
| claude | `claude-agent-acp` (npm) | 0.63.0 | `npx --no-install claude-agent-acp@0.63.0` *(requires prior explicit install; Ziggy never auto-downloads)* | `ANTHROPIC_API_KEY` env or adapter-managed login via `HOME` |
| codex | `codex-acp` (npm) | 1.1.7 | `npx --no-install codex-acp@1.1.7` | ChatGPT login state or `OPENAI_API_KEY` |

`--no-install` enforces REQ-002's no-silent-download rule: if the pinned package
is absent, launch fails with an install hint (`AgentLaunchError`), it is never fetched
at run time. `ziggy doctor` verifies resolvability the same way.

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

**Consequence of UNVERIFIED direct-tool rows:** until live probes prove otherwise,
both built-ins are classified `direct_tools_assumed = true`. `ziggy doctor` and
RunResults label their mediation `advisory` (`enforcement_scope = acp_mediated`
for individual mediated decisions; never `os_enforced`). The orchestrator's
uncontained-planner gate treats both as uncontained by default (planning with
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
7. Update this matrix + per-agent `KNOWN_DEGRADATIONS` in `ziggy/agents/builtins.py`.
