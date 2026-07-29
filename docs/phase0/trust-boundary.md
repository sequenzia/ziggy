# Phase 0 — Trust Boundary Report

What Ziggy can actually mediate versus what agent subprocesses can do directly.
This vocabulary is normative for code, docs, CLI output, and RunResults.

## The boundary

Ziggy mediates exactly the ACP **client-bound** surface: `session/request_permission`,
`fs/read_text_file`, `fs/write_text_file`, `terminal/*`. An agent subprocess is a
normal OS process; nothing prevents it from opening files, spawning shells, or
making network calls directly. Therefore:

- ACP mediation is **observable governance**, not containment.
- `enforcement_scope` on every recorded decision: `acp_mediated` (Ziggy resolved
  an ACP request), `agent_reported` (agent claims it did/didn't do something),
  `os_enforced` (reserved; only a separately verified sandbox provider may emit it —
  none exists in v0.1).
- Because live probes are deferred, both built-ins are assumed to have direct
  filesystem/shell tools (`capability-matrix.md`); doctor reports mediation as
  `advisory` for them.

## What Ziggy does enforce (its own process, not the agent's)

| Control | Mechanism | Honest limit |
|---------|-----------|--------------|
| Which commands run | Trusted user config only; project scope cannot name commands | User config itself is trusted by definition |
| Child environment | Explicit minimal baseline + `inherit_env` + one `api_key_env` | Agent may read `~/.claude`-style state via HOME |
| Mediated FS/terminal requests | Guarded policy, canonical-path checks, fail-closed | Only requests routed through ACP |
| Prompt/step/time/byte ceilings | Engine counters and timeouts | Cannot bound the agent's own internal work |
| Workspace lease | Cross-process lock outside the repo | Cooperative among Ziggy processes only |
| Redaction | Bounded streaming redactor before persist/emit | Defense in depth, not a guarantee |
| Egress records | Provider identity + upstream-output lineage + acknowledgement | Records/acknowledges; cannot un-send data |

## Terminology rules (enforced in review)

1. Never the words "sandbox", "isolated", or "contained" for ACP mediation.
   Allowed: "mediated", "advisory", "observed".
2. Planning isolation profile = "reduced exposure", not "sandbox".
3. Every user-facing permission summary carries its `enforcement_scope`.
4. Capture completeness claims come from provenance rules, never from optimism:
   an artifact class is `complete` only when the capture mechanism proves it.
