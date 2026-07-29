# Phase 0 — SDK Pin Decision

**Decision**: Pin `agent-client-protocol == 0.11.1` (exact) in `pyproject.toml`.

| Fact | Value |
|------|-------|
| Distribution | `agent-client-protocol` (PyPI) |
| Pinned version | **0.11.1** (latest at decision time, 2026-07-28) |
| Import name | `acp` (NOT `agent_client_protocol`) |
| Upstream schema ref | `refs/tags/schema-v1.16.0` (from generated `acp/meta.py`) |
| Wire protocol version | `acp.PROTOCOL_VERSION == 1` |
| Python support | `>=3.10, <3.15` (Ziggy requires `>=3.12,<3.15`) |
| Runtime deps | pydantic v2 (only) |

## Verification performed (mock-first; live adapter probes deferred)

- Installed 0.11.1 into the project venv and introspected every runtime module
  (`interfaces`, `connection`, `client/`, `agent/`, `stdio`, `transports`, `meta`,
  `exceptions`, `schema`, `contrib/`, `task/`).
- Confirmed every ACP v1 method Ziggy requires is modeled on both sides:
  - Client-side driving of agents: `initialize`, `session/new`, `session/prompt`,
    `session/cancel` (notification), plus inbound `session/update`,
    `session/request_permission`, `fs/read_text_file`, `fs/write_text_file`, `terminal/*`.
  - Agent-side serving (`ziggy serve`): `initialize`, `session/new`,
    `session/set_config_option`, `session/prompt`, `session/cancel`, outbound
    `session/update` and `session/request_permission`.
- Confirmed newline-delimited JSON-RPC 2.0 framing in `acp.connection.Connection`,
  with `StreamObserver` hooks receiving every raw frame in both directions
  (used by Ziggy for `debug`-capture frame excerpts).
- Confirmed subprocess spawning (`spawn_agent_process` / `spawn_stdio_transport`)
  passes a **trimmed default environment** (`HOME, LOGNAME, PATH, SHELL, TERM, USER`
  on POSIX) plus explicit overrides — aligned with REQ-007's minimal-env contract.
  Ziggy still builds its own explicit env dict and passes it; it does not rely on
  the SDK default silently.
- Malformed JSON on a frame is logged and skipped by the SDK receive loop (it does
  not raise). Ziggy detects protocol breakdown via its own observer layer and
  response futures (`ConnectionError` on disconnect), and surfaces `ProtocolError`.

## Notable API facts (implementers: see sdk-api-reference.md)

- The SDK ships some ACP v2-draft / unstable surfaces (elicitation, providers,
  nes, `document/*`, `session/fork|resume|close|list`). Ziggy targets the v1
  set only and must not advertise or depend on unstable surfaces
  (`use_unstable_protocol` stays False).
- `RequestPermissionResponse.outcome` is `AllowedOutcome(outcome='selected', option_id=...)`
  or `DeniedOutcome(outcome='cancelled')`. Denying by policy = select a
  `reject_*`-kind option when one is offered, else return `DeniedOutcome`.
- `PromptResponse.stop_reason ∈ {end_turn, max_tokens, max_turn_requests, refusal, cancelled}`.
- All wire models use camelCase aliases; always serialize `by_alias=True` and
  populate via field names. `_meta` ↔ `field_meta`.

## Upgrade rule

Upgrades are deliberate PRs that re-run this introspection, the wire-conformance
suite, and (when accounts are available) the `-m live` contract suite. CI must
never resolve `latest`.
