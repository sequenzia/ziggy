"""``ziggy serve`` application (REQ-012, spec §5.7/§7.4, phase45-contracts
"### server/app.py").

:class:`ZiggyServer` implements the native ``ZiggyAgentProtocol`` surface —
SDK-free; the :mod:`ziggy.acp.server` wrapper owns every SDK model — and
routes ``session/prompt`` turns into the ordinary engine paths
(``prepare_run``/``execute_run`` and ``prepare_workflow``/``execute_workflow``),
so server-mode runs persist the exact same RunResults as CLI runs.

Key behaviors:

- **Sessions**: ``session/new`` canonicalizes the client cwd (must be an
  existing directory), loads config freshly from that workspace (untrusted
  project scope engaged per session), and builds the per-session route
  catalog: ``orchestrator`` (default) + ``agent:<name>`` per registry agent
  (user-scope, computed at construction) + ``workflow:<name>`` per workflow
  discovered against the session cwd.
- **Admission**: one process-wide active-run ceiling
  (``server.max_active_runs``, default 1); excess prompts fail with a typed
  :class:`~ziggy.errors.ServerBusyError` — no queueing. The cross-process
  workspace lease still applies per run inside the engine.
- **Re-emission**: the engine's synchronous ``render_cb`` feeds an asyncio
  queue drained by a per-run task that re-emits events to the connecting
  client as ``session/update`` (message/thought chunks, tool calls, plan and
  usage pass through; step transitions, egress notices, truncation, errors,
  and permission decisions become one-line :class:`ServerNotice` chunks).
- **Permission bridge**: the run's hooks get a ``decide_permission`` override
  — policy ceiling FIRST (a policy deny is never forwarded), a policy allow
  is forwarded to the client with downstream agent/step context; a client
  that cannot receive permission requests flips the session to guarded local
  mediation with one visible fallback notice. Every decision is recorded with
  ``client_response`` (the final decision is always the policy intersection).
- **Stop mapping** (documented honest mapping): run ``success``/``partial``
  → ``end_turn``; ``cancelled`` → ``cancelled``; ``failed`` → a final
  error-summary notice then ``end_turn`` — ACP v1 has no error stop reason
  and the JSON-RPC error channel is reserved for transport/validation
  failures, not completed-but-failed runs.
- **Shutdown**: :meth:`ZiggyServer.aclose` (client EOF / SIGTERM) sets every
  active run's cancel event and awaits bounded completion via each run's own
  teardown ladder; leases are released by the runners.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from ziggy.acp import (
    ClientPermissionUnsupported,
    MessageChunkEvent,
    PermissionOptionN,
    PermissionReply,
    PermissionRequestN,
    PlanEvent,
    RouteState,
    ServerHandshake,
    ServerNotice,
    ServerStopInfo,
    ServerUpdate,
    SessionOpened,
    ToolCallEvent,
    UsageEvent,
)
from ziggy.agents import AgentRegistry
from ziggy.config import ResolvedConfig, load_config
from ziggy.engine import MAIN_STEP_ID, RunOverrides, execute_run, prepare_run
from ziggy.engine.prepare import prepare_workflow
from ziggy.errors import (
    CapabilityError,
    ProtocolError,
    ServerBusyError,
    TypedError,
    ValidationError,
)
from ziggy.ids import utc_now_iso
from ziggy.models.common import EnforcementScope, PermissionDecisionKind, RunStatus
from ziggy.models.events import EventEnvelope
from ziggy.models.result import RunResult
from ziggy.policy import Decision, MediationPolicy
from ziggy.workflows import DiscoveredWorkflow, discover
from ziggy.workflows.runner import execute_workflow

_log = logging.getLogger(__name__)

#: The default (and Phase-5 entry) route of every new session.
DEFAULT_ROUTE = "orchestrator"
_AGENT_ROUTE_PREFIX = "agent:"
_WORKFLOW_ROUTE_PREFIX = "workflow:"

#: Suffix recorded on ``policy_source`` when a decision was resolved by
#: guarded local mediation because the client cannot receive permission
#: requests (phase45 permission-bridge contract).
GUARDED_FALLBACK_SUFFIX = "(guarded-fallback)"

#: Rule ids for server-side (non-policy) permission outcomes. Recorded so a
#: bridge failure is never attributed to a policy rule that allowed it.
RULE_NO_ACTIVE_STEP = "server-no-active-step-deny"
RULE_FORWARD_FAILED = "server-forward-failed-deny"

#: Option preference orders: never volunteer a standing "always" answer.
_ALLOW_KIND_ORDER = ("allow_once", "allow_always")
_REJECT_KIND_ORDER = ("reject_once", "reject_always")


class ServerConnectionLike(Protocol):
    """Structural view of :class:`ziggy.acp.ServerConnection` (duck-typed so
    the app — and its tests — never depend on the SDK-bound class)."""

    async def emit_update(
        self, session_id: str, ev: ServerUpdate, *, run_id: str, step_id: str | None
    ) -> None: ...

    async def forward_permission(
        self, session_id: str, req: PermissionRequestN, context_label: str
    ) -> PermissionReply: ...


def _ziggy_version() -> str:
    try:
        return importlib_metadata.version("ziggy")
    except importlib_metadata.PackageNotFoundError:  # editable/unbuilt tree
        return "0.0.0"


def _request_summary(tool_call: dict[str, Any]) -> str:
    title = tool_call.get("title")
    if isinstance(title, str) and title:
        return title
    tool_call_id = tool_call.get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id:
        return tool_call_id
    return "permission request"


def _select_option(
    options: Sequence[PermissionOptionN], kind_order: Sequence[str]
) -> PermissionReply:
    """First option matching the preference order; else the cancelled outcome
    (which the wire treats as denied — failing toward denial)."""
    for kind in kind_order:
        for option in options:
            if option.kind == kind:
                return PermissionReply(kind="selected", option_id=option.option_id)
    return PermissionReply(kind="cancelled")


def _option_kind(options: Sequence[PermissionOptionN], option_id: str | None) -> str | None:
    for option in options:
        if option.option_id == option_id:
            return option.kind
    return None


def _update_for(envelope: EventEnvelope) -> ServerUpdate | None:
    """Map one recorded event to its re-emitted ``session/update`` shape.

    Passthrough: message/thought chunks, tool calls, plan, usage. One-line
    ServerNotice: step transitions, egress notices, truncation, errors, and
    permission decisions (``[policy] <rule_id>: <decision>``). Everything else
    is server-internal and not re-emitted. Payloads may be metadata-only after
    truncation, so every field access is defensive.
    """
    event_type = envelope.event_type
    payload = envelope.payload
    if event_type in ("message_chunk", "thought_chunk"):
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            return None
        role = payload.get("role")
        return MessageChunkEvent(
            role=role if role in ("agent", "user") else "agent",
            text=text,
            thought=event_type == "thought_chunk",
        )
    if event_type in ("tool_call", "tool_call_update"):
        tool_call_id = payload.get("tool_call_id")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return None
        raw_locations = payload.get("locations")
        locations = (
            [loc for loc in raw_locations if isinstance(loc, str)]
            if isinstance(raw_locations, list)
            else []
        )
        title = payload.get("title")
        kind = payload.get("kind")
        status = payload.get("status")
        return ToolCallEvent(
            tool_call_id=tool_call_id,
            phase="start" if event_type == "tool_call" else "update",
            title=title if isinstance(title, str) else None,
            kind=kind if isinstance(kind, str) else None,
            status=status if isinstance(status, str) else None,
            locations=locations,
            has_content=bool(payload.get("has_content")),
            raw={},
        )
    if event_type == "plan":
        raw_entries = payload.get("entries")
        entries = (
            [entry for entry in raw_entries if isinstance(entry, dict)]
            if isinstance(raw_entries, list)
            else []
        )
        return PlanEvent(entries=entries)
    if event_type == "usage":
        used = payload.get("used")
        size = payload.get("size")
        cost = payload.get("cost")
        currency = payload.get("currency")
        cost_ok = isinstance(cost, (int, float)) and not isinstance(cost, bool)
        return UsageEvent(
            used=used if isinstance(used, int) and not isinstance(used, bool) else None,
            size=size if isinstance(size, int) and not isinstance(size, bool) else None,
            cost=float(cost) if cost_ok else None,
            currency=currency if isinstance(currency, str) else None,
            raw={},
        )
    if event_type == "permission_decided":
        return ServerNotice(
            text=f"[policy] {payload.get('rule_id', '?')}: {payload.get('decision', '?')}"
        )
    if event_type == "step_started":
        agent = payload.get("agent")
        suffix = f" (agent: {agent})" if isinstance(agent, str) and agent else ""
        return ServerNotice(text=f"[step {envelope.step_id}] started{suffix}")
    if event_type == "step_finished":
        return ServerNotice(
            text=f"[step {envelope.step_id}] finished: {payload.get('status', '?')}"
        )
    if event_type == "egress_notice":
        raw_providers = payload.get("provider_set")
        providers = (
            ", ".join(str(p) for p in raw_providers) if isinstance(raw_providers, list) else "?"
        )
        return ServerNotice(
            text=(
                f"[egress] providers: {providers} "
                f"(acknowledged_by: {payload.get('acknowledged_by')})"
            )
        )
    if event_type == "truncation":
        return ServerNotice(
            text=f"[truncation] step {envelope.step_id}: event stream truncated (capture degraded)"
        )
    if event_type == "error":
        return ServerNotice(
            text=f"[error] {payload.get('code', 'ZiggyError')}: {payload.get('message', '')}"
        )
    return None


@dataclass(slots=True)
class SessionState:
    """One ACP session: route surface + per-session (untrusted-project) config."""

    session_id: str
    cwd: Path
    resolved: ResolvedConfig
    route: str
    route_options: list[str]
    workflows: dict[str, DiscoveredWorkflow] = field(default_factory=dict)
    #: Set once the client answered ``session/request_permission`` with
    #: method_not_found; this and all later requests in the session resolve
    #: via guarded local mediation.
    no_forwarding: bool = False
    #: The one visible fallback notice has been emitted for this session.
    fallback_noticed: bool = False


@dataclass(slots=True)
class _ActiveRun:
    """Registry entry for one admitted run."""

    cancel_event: asyncio.Event
    task: asyncio.Task[RunResult] | None = None


class _RunBridge:
    """Per-run glue between the engine's synchronous callbacks and the client.

    - ``render_cb`` (sync, called inline by the RunRecorder) tracks the active
      step and enqueues envelopes; a drain task re-emits them via
      ``ServerConnection.emit_update`` with run/step correlation.
    - ``decide_permission`` implements the permission bridge (policy ceiling
      first, then client forwarding, then guarded fallback) and returns the
      ``(reply, decision_payload)`` pair the engine hooks record.
    """

    def __init__(
        self,
        *,
        session: SessionState,
        connection: ServerConnectionLike | None,
        step_policies: dict[str, MediationPolicy],
        step_agents: dict[str, str],
        target: str,
    ) -> None:
        self._session = session
        self._connection = connection
        self._step_policies = step_policies
        self._step_agents = step_agents
        self._target = target
        self._queue: asyncio.Queue[EventEnvelope | None] = asyncio.Queue()
        self._drain_task: asyncio.Task[None] | None = None
        self._emit_failed = False
        self.current_step: str | None = None
        self.run_id: str | None = None

    # ------------------------------------------------------------- streaming

    def render_cb(self, envelope: EventEnvelope) -> None:
        """Synchronous RunRecorder fan-out: track step context, enqueue."""
        if self.run_id is None:
            self.run_id = envelope.run_id
        if envelope.event_type == "step_started":
            self.current_step = envelope.step_id
            agent = envelope.payload.get("agent")
            if isinstance(agent, str) and agent and envelope.step_id is not None:
                self._step_agents.setdefault(envelope.step_id, agent)
        if self._connection is not None:
            self._queue.put_nowait(envelope)

    def start(self) -> None:
        if self._connection is not None:
            self._drain_task = asyncio.create_task(self._drain())

    async def aclose(self) -> None:
        """Flush and stop the drain task (all queued updates re-emitted)."""
        if self._drain_task is None:
            return
        self._queue.put_nowait(None)
        await self._drain_task
        self._drain_task = None

    async def _drain(self) -> None:
        assert self._connection is not None
        while True:
            envelope = await self._queue.get()
            if envelope is None:
                return
            if self._emit_failed:
                continue
            update = _update_for(envelope)
            if update is None:
                continue
            try:
                await self._connection.emit_update(
                    self._session.session_id,
                    update,
                    run_id=envelope.run_id,
                    step_id=envelope.step_id,
                )
            except Exception:
                self._emit_failed = True
                _log.warning(
                    "session/update re-emission failed; suppressing further updates for this run",
                    exc_info=True,
                )

    async def emit_notice(self, text: str) -> None:
        """Best-effort direct ServerNotice (fallback advertisement, final
        error summary); never disturbs the run."""
        if self._connection is None or self._emit_failed:
            return
        try:
            await self._connection.emit_update(
                self._session.session_id,
                ServerNotice(text=text),
                run_id=self.run_id or "pending",
                step_id=self.current_step,
            )
        except Exception:
            self._emit_failed = True
            _log.warning("server notice emission failed", exc_info=True)

    # ----------------------------------------------------------- permissions

    def _payload(
        self,
        req: PermissionRequestN,
        *,
        kind: PermissionDecisionKind,
        rule_id: str,
        policy_name: str,
        policy_source: str,
        client_response: str | None,
        fallback: bool = False,
    ) -> dict[str, Any]:
        return {
            "request_summary": _request_summary(req.tool_call),
            "options_offered": [o.kind for o in req.options],
            "decision": kind.value,
            "rule_id": rule_id,
            "policy_name": policy_name,
            "policy_source": policy_source + (GUARDED_FALLBACK_SUFFIX if fallback else ""),
            "enforcement_scope": EnforcementScope.ACP_MEDIATED.value,
            "ts": utc_now_iso(),
            "client_response": client_response,
        }

    def _decision_payload(
        self,
        req: PermissionRequestN,
        decision: Decision,
        *,
        kind: PermissionDecisionKind,
        client_response: str | None,
        fallback: bool = False,
    ) -> dict[str, Any]:
        return self._payload(
            req,
            kind=kind,
            rule_id=decision.rule_id,
            policy_name=decision.policy_name,
            policy_source=decision.policy_source,
            client_response=client_response,
            fallback=fallback,
        )

    async def decide_permission(
        self, req: PermissionRequestN
    ) -> tuple[PermissionReply, dict[str, Any]]:
        """The bridge decision: policy ceiling first, then client forwarding.

        A policy deny is decided locally and NEVER forwarded
        (``client_response=None``). A policy allow is forwarded with the
        downstream ``agent:<name> step:<id>`` context; the client's answer is
        recorded (``approved``/``denied``) and intersected — the client can
        only narrow, never widen, the policy decision. A client without the
        permission surface flips the session to guarded local mediation
        (``policy_source`` suffixed ``(guarded-fallback)``, one visible
        notice); a broken forward fails toward denial.
        """
        step_id = self.current_step
        policy = self._step_policies.get(step_id) if step_id is not None else None
        if policy is None:
            # No active step to attribute the request to: fail toward denial.
            return (
                _select_option(req.options, _REJECT_KIND_ORDER),
                self._payload(
                    req,
                    kind=PermissionDecisionKind.DENIED,
                    rule_id=RULE_NO_ACTIVE_STEP,
                    policy_name="guarded",
                    policy_source="default",
                    client_response=None,
                ),
            )
        decision = policy.decide_permission(req)
        if not decision.allowed:
            # Policy ceiling denies: local deny, the client is never asked.
            return (
                _select_option(req.options, _REJECT_KIND_ORDER),
                self._decision_payload(
                    req,
                    decision,
                    kind=PermissionDecisionKind.DENIED,
                    client_response=None,
                ),
            )
        if self._connection is not None and not self._session.no_forwarding:
            agent = self._step_agents.get(step_id, self._target)
            label = f"agent:{agent} step:{step_id}"
            try:
                client_reply = await self._connection.forward_permission(
                    self._session.session_id, req, label
                )
            except ClientPermissionUnsupported:
                self._session.no_forwarding = True
                if not self._session.fallback_noticed:
                    self._session.fallback_noticed = True
                    await self.emit_notice(
                        "[policy] the connected client does not support "
                        "session/request_permission; this and subsequent permission "
                        "requests are resolved by guarded local mediation"
                    )
            except ProtocolError:
                # Forwarding broke mid-request (client gone / bad answer):
                # fail toward denial without engaging the session fallback.
                return (
                    _select_option(req.options, _REJECT_KIND_ORDER),
                    self._payload(
                        req,
                        kind=PermissionDecisionKind.DENIED,
                        rule_id=RULE_FORWARD_FAILED,
                        policy_name=decision.policy_name,
                        policy_source="default",
                        client_response=None,
                    ),
                )
            else:
                return self._intersect(req, decision, client_reply)
        # Guarded local fallback: the policy ceiling already allowed.
        return (
            _select_option(req.options, _ALLOW_KIND_ORDER),
            self._decision_payload(
                req,
                decision,
                kind=PermissionDecisionKind.APPROVED,
                client_response=None,
                fallback=True,
            ),
        )

    def _intersect(
        self, req: PermissionRequestN, decision: Decision, client_reply: PermissionReply
    ) -> tuple[PermissionReply, dict[str, Any]]:
        """Client answer x policy allow -> final decision (+ recorded payload)."""
        if client_reply.kind == "selected":
            kind = _option_kind(req.options, client_reply.option_id)
            if kind is not None and kind.startswith("allow"):
                return (
                    client_reply,
                    self._decision_payload(
                        req,
                        decision,
                        kind=PermissionDecisionKind.APPROVED,
                        client_response="approved",
                    ),
                )
            # A selected reject option — or an option id Ziggy never offered
            # (replaced by a local reject selection) — is a client denial.
            reply = (
                client_reply
                if kind is not None
                else _select_option(req.options, _REJECT_KIND_ORDER)
            )
            return (
                reply,
                self._decision_payload(
                    req,
                    decision,
                    kind=PermissionDecisionKind.DENIED,
                    client_response="denied",
                ),
            )
        # Cancelled outcome: the client declined to grant.
        return (
            client_reply,
            self._decision_payload(
                req,
                decision,
                kind=PermissionDecisionKind.DENIED,
                client_response="denied",
            ),
        )


class ZiggyServer:
    """The native-typed ``ziggy serve`` application (implements
    ``ZiggyAgentProtocol``; see the module docstring for the behavior map).

    ``workspace_override`` pins every session to one workspace regardless of
    the client-supplied cwd (still canonicalized and validated).
    ``store_root`` overrides run persistence (tests); ``None`` keeps the
    config/``$ZIGGY_HOME`` resolution.
    """

    def __init__(
        self,
        resolved: ResolvedConfig,
        *,
        workspace_override: Path | None = None,
        store_root: Path | None = None,
    ) -> None:
        self._resolved = resolved
        self._workspace_override = workspace_override
        self._store_root = store_root
        registry = AgentRegistry.from_config(resolved)
        #: Construction-time catalog: agents are USER_ONLY config, identical
        #: for every session; workflow routes are discovered per session.
        self._agent_routes = [f"{_AGENT_ROUTE_PREFIX}{name}" for name in registry.names()]
        self._max_active_runs = int(resolved.config.server.max_active_runs)
        self._sessions: dict[str, SessionState] = {}
        self._active: dict[str, _ActiveRun] = {}
        self._admission = asyncio.Lock()
        self._connection: ServerConnectionLike | None = None
        self._client_capabilities: dict[str, Any] = {}
        self._client_info: dict[str, Any] | None = None

    # -------------------------------------------------------------- protocol

    def on_connect(self, conn: ServerConnectionLike) -> None:
        """Wrapper hook: bind the outbound surface for this client."""
        self._connection = conn

    async def initialize(
        self,
        client_protocol_version: int,
        client_capabilities: dict[str, Any],
        client_info: dict[str, Any] | None,
    ) -> ServerHandshake:
        self._client_capabilities = dict(client_capabilities)
        self._client_info = client_info
        _log.info(
            "client connected: name=%s version=%s protocol=%s",
            (client_info or {}).get("name", "unknown"),
            (client_info or {}).get("version", "unknown"),
            client_protocol_version,
        )
        return ServerHandshake(agent_name="ziggy", agent_version=_ziggy_version())

    async def new_session(self, cwd: str) -> SessionOpened:
        raw = self._workspace_override if self._workspace_override is not None else Path(cwd)
        canonical = Path(raw).expanduser().resolve()
        if not canonical.is_dir():
            raise ValidationError(
                f"session cwd {cwd!r} must resolve to an existing directory",
                details={"cwd": cwd},
            )
        # Fresh load engages this workspace's untrusted project scope for the
        # session; a hostile project config fails session creation (exit-free:
        # surfaces as a typed JSON-RPC error to the client).
        resolved = load_config(canonical)
        for warning in resolved.warnings:
            _log.warning("config: %s", warning)
        workflows = discover(canonical)
        route_options = [
            DEFAULT_ROUTE,
            *self._agent_routes,
            *sorted(f"{_WORKFLOW_ROUTE_PREFIX}{name}" for name in workflows),
        ]
        session_id = f"ziggy-{uuid4().hex}"
        state = SessionState(
            session_id=session_id,
            cwd=canonical,
            resolved=resolved,
            route=DEFAULT_ROUTE,
            route_options=route_options,
            workflows=dict(workflows),
        )
        self._sessions[session_id] = state
        _log.info(
            "session opened: id=%s routes=%d workflows=%d",
            session_id,
            len(route_options),
            len(workflows),
        )
        return SessionOpened(
            session_id=session_id, route=state.route, route_options=list(route_options)
        )

    async def set_config_option(self, session_id: str, config_id: str, value: str) -> RouteState:
        """Switch the session route. ``ValueError`` (unknown id/value/session)
        surfaces as JSON-RPC invalid_params via the wrapper. Routes can never
        touch policy/ceilings — they only pick a catalog entry."""
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError(f"unknown session {session_id!r}")
        if config_id != "route":
            raise ValueError(f"unknown config option {config_id!r}")
        if value not in session.route_options:
            raise ValueError(f"unknown route value {value!r}")
        session.route = value
        return RouteState(route=session.route, route_options=list(session.route_options))

    async def prompt(self, session_id: str, text: str) -> ServerStopInfo:
        session = self._sessions.get(session_id)
        if session is None:
            raise ValidationError(
                f"unknown session {session_id!r}", details={"session_id": session_id}
            )
        route = session.route
        if route == DEFAULT_ROUTE:
            raise CapabilityError("orchestrator lands in Phase 5", details={"route": route})
        handle = await self._admit(session_id)
        bridge: _RunBridge | None = None
        try:
            if route.startswith(_AGENT_ROUTE_PREFIX):
                agent_name = route.removeprefix(_AGENT_ROUTE_PREFIX)
                runner, bridge = self._agent_run(session, agent_name, text, handle)
            elif route.startswith(_WORKFLOW_ROUTE_PREFIX):
                workflow_name = route.removeprefix(_WORKFLOW_ROUTE_PREFIX)
                runner, bridge = self._workflow_run(session, workflow_name, handle)
            else:  # unreachable: routes come from the validated catalog
                raise ValidationError(f"unknown route {route!r}", details={"route": route})
            handle.task = asyncio.create_task(runner)
            # Shield: if THIS handler is torn down (client EOF mid-request)
            # the engine task keeps running and aclose() owns its teardown.
            result = await asyncio.shield(handle.task)
        except BaseException:
            if bridge is not None and (handle.task is None or handle.task.done()):
                await bridge.aclose()
            raise
        finally:
            if handle.task is None or handle.task.done():
                await self._release(session_id)
        return await self._stop_info(bridge, result)

    async def cancel(self, session_id: str) -> None:
        """Propagate ``session/cancel``: set the active run's cancel event
        (normal teardown ladder). Idempotent; unknown session is a no-op."""
        handle = self._active.get(session_id)
        if handle is not None:
            handle.cancel_event.set()

    async def authenticate(self, method_id: str) -> None:
        raise NotImplementedError("ziggy serve does not implement authentication")

    # ------------------------------------------------------------- lifecycle

    async def aclose(self) -> None:
        """Disconnect/shutdown teardown: cancel every active run and await
        bounded completion (each run's own ladder releases its lease)."""
        async with self._admission:
            handles = dict(self._active)
        if handles:
            _log.info("shutdown: cancelling %d active run(s)", len(handles))
        for handle in handles.values():
            handle.cancel_event.set()
        for handle in handles.values():
            if handle.task is not None:
                with suppress(Exception):
                    await handle.task
        async with self._admission:
            for session_id in handles:
                self._active.pop(session_id, None)

    # --------------------------------------------------------------- helpers

    async def _admit(self, session_id: str) -> _ActiveRun:
        """Process-wide admission: typed busy above the ceiling, no queueing."""
        async with self._admission:
            if len(self._active) >= self._max_active_runs or session_id in self._active:
                raise ServerBusyError(
                    f"server busy: {len(self._active)} active run(s); "
                    f"server.max_active_runs is {self._max_active_runs}",
                    details={
                        "max_active_runs": self._max_active_runs,
                        "active_runs": len(self._active),
                    },
                )
            handle = _ActiveRun(cancel_event=asyncio.Event())
            self._active[session_id] = handle
            return handle

    async def _release(self, session_id: str) -> None:
        async with self._admission:
            self._active.pop(session_id, None)

    def _agent_run(
        self, session: SessionState, agent_name: str, text: str, handle: _ActiveRun
    ) -> tuple[Coroutine[Any, Any, RunResult], _RunBridge]:
        """Prepare a direct-agent run; returns (engine coroutine, bridge)."""
        prepared = prepare_run(
            session.resolved,
            agent_name=agent_name,
            prompt=text,
            workspace=session.cwd,
            overrides=RunOverrides(),
        )
        if self._store_root is not None:
            prepared.spec.store_root = self._store_root
        bridge = _RunBridge(
            session=session,
            connection=self._connection,
            step_policies={MAIN_STEP_ID: prepared.policy},
            step_agents={MAIN_STEP_ID: agent_name},
            target=agent_name,
        )

        async def run() -> RunResult:
            bridge.start()
            try:
                return await execute_run(
                    prepared.spec,
                    render_cb=bridge.render_cb,
                    cancel_event=handle.cancel_event,
                    decide_permission=bridge.decide_permission,
                )
            finally:
                await bridge.aclose()
                prepared.logger.close()

        return run(), bridge

    def _workflow_run(
        self, session: SessionState, name: str, handle: _ActiveRun
    ) -> tuple[Coroutine[Any, Any, RunResult], _RunBridge]:
        """Prepare a workflow run (no prompt variables in v0.1)."""
        workflow = session.workflows.get(name)
        if workflow is None:
            raise ValidationError(
                f"workflow {name!r} is not discoverable in this session",
                details={"workflow": name},
            )
        required = sorted(
            var_name for var_name, decl in workflow.definition.variables.items() if decl.required
        )
        if required:
            raise ValidationError(
                f"workflow {name!r} declares required variables "
                f"({', '.join(required)}); prompts cannot supply workflow variables "
                "in v0.1 — variables must have defaults for server routes",
                details={"workflow": name, "required_variables": required},
            )
        prepared = prepare_workflow(
            session.resolved,
            name_or_path=name,
            cli_vars={},
            workspace=session.cwd,
            overrides=RunOverrides(),
        )
        if self._store_root is not None:
            prepared.store_root = self._store_root
        bridge = _RunBridge(
            session=session,
            connection=self._connection,
            step_policies={sid: prep.policy for sid, prep in prepared.steps.items()},
            step_agents={sid: prep.agent_config.name for sid, prep in prepared.steps.items()},
            target=name,
        )

        async def run() -> RunResult:
            bridge.start()
            try:
                return await execute_workflow(
                    prepared,
                    render_cb=bridge.render_cb,
                    cancel_event=handle.cancel_event,
                    decide_permission=bridge.decide_permission,
                )
            finally:
                await bridge.aclose()
                if prepared.logger is not None:
                    prepared.logger.close()

        return run(), bridge

    async def _stop_info(self, bridge: _RunBridge | None, result: RunResult) -> ServerStopInfo:
        """Documented honest mapping: success|partial -> end_turn; cancelled ->
        cancelled; failed -> final error-summary notice, then end_turn."""
        if result.status is RunStatus.CANCELLED:
            return ServerStopInfo(stop_reason="cancelled")
        if result.status in (RunStatus.FAILED, RunStatus.ABANDONED):
            error = self._first_error(result)
            text = (
                f"[error] run {result.run_id} failed: {error.code}: {error.message}"
                if error is not None
                else f"[error] run {result.run_id} failed"
            )
            if bridge is not None:
                await bridge.emit_notice(text)
            return ServerStopInfo(stop_reason="end_turn")
        return ServerStopInfo(stop_reason="end_turn")

    @staticmethod
    def _first_error(result: RunResult) -> TypedError | None:
        if result.errors:
            return result.errors[0]
        for step in result.steps.values():
            if step.errors:
                return step.errors[0]
        return None
