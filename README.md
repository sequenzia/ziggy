# Ziggy

Local execution, orchestration, and audit harness for AI coding agents that
speak the [Agent Client Protocol (ACP)](https://agentclientprotocol.com).

Ziggy gives Claude, Codex, OpenCode, Devin, and trusted custom agents one
headless command surface, emits a coherent schema-versioned `RunResult` for every invocation,
runs constrained agent-only YAML workflows serially, exposes itself as an ACP
agent for editors like Zed, and can plan bounded agent-only execution graphs
from a goal. It is an **audit and trust-boundary layer, not an OS sandbox** —
ACP mediation is observable governance, and Ziggy says so explicitly wherever a
subprocess can act outside that mediation.

See `specs/ziggy-mvp-SPEC.md` for the full product/technical spec.

## Install

```bash
uv tool install git+<repo-url>@v0.1.0        # once released
# then install the pinned agent adapters explicitly (Ziggy never auto-downloads):
npm install -g @agentclientprotocol/claude-agent-acp@0.64.0 @agentclientprotocol/codex-acp@1.1.7
# opencode and devin are built in too, but speak ACP from their own CLI —
# install either one only if you use it (nothing is ever auto-downloaded):
npm install -g opencode-ai@1.18.9
brew install --cask devin-cli   # Linux: curl -fsSL https://cli.devin.ai/install.sh | bash
```

Requires Python 3.12+, macOS or Linux. Development: `uv sync`, then
`uv run pytest` and `uv run ruff check src/ tests/`.

## Commands

```bash
ziggy run <agent> "<prompt>"              # one-shot headless run
ziggy workflow run <name|path> --var k=v  # constrained YAML workflow
ziggy workflow list
ziggy orchestrate "<goal>" [--plan-only]  # planned bounded agent-only graph
ziggy serve                               # run as an ACP agent over stdio (Zed)
ziggy agents list
ziggy runs list [--failed] / runs show <id> / runs reindex / runs prune
ziggy config show / config validate
ziggy doctor [--agent NAME] [--all]
ziggy schemas dump [--out DIR]            # emit result.v1 / events.v1 JSON Schema
```

Global flags: `--json` (machine-readable RunResult on stdout, all else on
stderr), `--no-save`, `--capture metadata|standard|debug`, `--plain`,
`--acknowledge-egress p1,p2`. Exit codes: 0 success · 1 execution/persistence
failure · 2 usage/config/trust error · 130 cancellation.

## Trust model (read this)

- **Trusted user scope** (`~/.ziggy/config.toml`, env) defines executable
  agents, credentials (by env-var name only), resource ceilings, storage, and
  the maximum ACP mediation policy.
- **Project scope** (`./.ziggy/config.toml`, workflow YAML) may select trusted
  agents and *tighten* limits/policy. It can never register commands, inherit
  environment, name credentials, expand paths, raise ceilings, or weaken policy.
  Attempts fail closed with path-precise errors.
- **ACP mediation is advisory**, not enforcement: agents with direct local tools
  can act outside it. Every permission/access decision records an
  `enforcement_scope` (`acp_mediated` / `agent_reported` / `os_enforced`), and
  `os_enforced` is reserved for a future verified sandbox — never claimed in v0.1.
- Secrets are referenced by env-var name, redacted from all persisted artifacts
  (defense in depth, not a proof), and never stored literally.

## Documentation

| Doc | What |
|-----|------|
| `specs/ziggy-mvp-SPEC.md` | Product & technical specification (REQ-001..016) |
| `docs/design/ARCHITECTURE.md` | Module layout, contracts, conventions |
| `docs/design/phase*-contracts.md` | Per-phase implementation contracts |
| `docs/phase0/` | SDK pin decision, API reference, capability matrix, trust boundary, process lifecycle |
| `docs/GATES.md` | Checkpoint-gate record + adversarial review findings & disposition |
| `docs/RELEASE-CHECKLIST.md` | Deferred live/human items required before tagging v0.1.0 |

## Status

All feature phases (0–5) implemented; **1181 tests passing**, ruff clean. Live
built-in contract runs, the Zed smoke test, clean-machine onboarding timing,
and the human-labeled orchestrator-quality trial are deferred to the release
checklist (they need real accounts, a real editor, and human labelers).
