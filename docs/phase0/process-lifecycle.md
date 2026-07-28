# Phase 0 — Process Lifecycle Decision

Cancellation and descendant cleanup for agent subprocesses (REQ-010, §6.4).

## Decision

Ziggy spawns agent subprocesses itself (mirroring `acp.transports.spawn_stdio_transport`,
which handles only the direct child) with **`start_new_session=True`**, making the
agent the leader of a new process group/session. The yielded
`process.stdin`/`process.stdout` StreamWriter/StreamReader are handed to
`ClientSideConnection` directly — no SDK behavior is lost, and Ziggy owns teardown.

## Teardown ladder (single implementation used by CLI cancel, timeout, server cancel/disconnect)

1. **ACP cancel** — send `session/cancel` (notification), keep draining
   `session/update` events; a compliant agent resolves the in-flight
   `session/prompt` with `stop_reason='cancelled'`.
2. **Grace period** — wait `grace_seconds` (default 5s; configurable user scope).
3. **Group terminate** — `os.killpg(pgid, SIGTERM)`; wait bounded (default 5s).
4. **Group kill** — `os.killpg(pgid, SIGKILL)`; reap.
5. Close streams; record `cancelled` (user/timeout) with a typed error
   (`StepTimeoutError` on timeout path) and persist.

Notes:
- `killpg` failure with `ProcessLookupError` = group already gone (success).
- Direct-child `terminate()/kill()` remain the fallback if `start_new_session`
  or `killpg` is unavailable (should not happen on macOS/Linux; Windows is out of scope).
- Descendants that escape their process group (double-fork daemons) are beyond
  reliable teardown; documented limitation, surfaced nowhere as a guarantee.
- The same ladder runs on: Ctrl-C (SIGINT handler), step timeout, workflow
  deadline, server `session/cancel`, server client-EOF/disconnect.

## Abandoned-run recovery

A run directory without a durable terminal `result.json` (crash, SIGKILL of Ziggy
itself) is finalized as `abandoned` by the next store inspection
(`ziggy runs reindex`, `runs list` startup scan), never counted as success, and
never mutated while its writer may still be alive (lease/owner liveness check
mirrors WorkspaceLease recovery rules).
