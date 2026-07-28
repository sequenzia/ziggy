"""Agent-side ACP boundary: serve Ziggy to an ACP client over stdio (REQ-012).

``serve_stdio`` wraps the SDK's ``run_agent`` loop around an internal
``_SdkAgent`` that translates between the SDK ``Agent`` protocol and
``ZiggyAgentProtocol`` — the native-typed protocol the ``ziggy.server`` app
implements — so ``server/`` stays SDK-free (phase45-contracts
"### ziggy/acp/server.py"). SDK models never cross this boundary; this module
owns all SDK model construction and JSON-RPC error mapping.

Server logging goes through the ``logging`` module only (the CLI configures a
stderr handler); stdout belongs to the ACP wire.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from typing import Any, Protocol

from acp.agent.connection import AgentSideConnection
from acp.core import run_agent
from acp.exceptions import RequestError
from acp.meta import PROTOCOL_VERSION
from acp.schema import (
    AgentCapabilities,
    AuthenticateResponse,
    ClientCapabilities,
    Implementation,
    InitializeResponse,
    NewSessionResponse,
    PromptCapabilities,
    PromptResponse,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SetSessionConfigOptionResponse,
    TextContentBlock,
)

from ziggy.acp.convert import (
    _dump,
    from_agent_event,
    from_permission_outcome,
    from_permission_request,
)
from ziggy.acp.types import (
    ClientPermissionUnsupported,
    PermissionReply,
    PermissionRequestN,
    RouteState,
    ServerHandshake,
    ServerStopInfo,
    ServerUpdate,
    SessionOpened,
)
from ziggy.errors import ProtocolError, ZiggyError

_log = logging.getLogger(__name__)

#: The single v1 session-config surface Ziggy exposes (spec §5.7): routing.
_ROUTE_CONFIG_ID = "route"


class ZiggyAgentProtocol(Protocol):
    """Native-typed protocol the ``ziggy.server`` app implements.

    Signatures use ONLY native types (never SDK models) so the server app
    stays SDK-free. The optional ``on_connect(ServerConnection)`` hook —
    deliberately not part of this protocol — is invoked by the wrapper when
    the client connection is established, mirroring the SDK's own duck-typed
    ``on_connect`` convention.
    """

    async def initialize(
        self,
        client_protocol_version: int,
        client_capabilities: dict[str, Any],
        client_info: dict[str, Any] | None,
    ) -> ServerHandshake:
        """Record client identity/capabilities; return Ziggy's identity."""
        ...

    async def new_session(self, cwd: str) -> SessionOpened:
        """Open a session bound to the (canonicalized) client cwd."""
        ...

    async def set_config_option(self, session_id: str, config_id: str, value: str) -> RouteState:
        """Switch the session route; raise ``ValueError`` for an unknown route
        value (surfaced to the client as JSON-RPC invalid_params)."""
        ...

    async def prompt(self, session_id: str, text: str) -> ServerStopInfo:
        """Run the routed prompt to a terminal state; return the stop reason."""
        ...

    async def cancel(self, session_id: str) -> None:
        """Propagate ``session/cancel`` to the active run (notification)."""
        ...

    async def authenticate(self, method_id: str) -> None:
        """Handle ``authenticate``; the default app raises
        ``NotImplementedError`` (surfaced as JSON-RPC method_not_found)."""
        ...


def _to_request_error(exc: ZiggyError) -> RequestError:
    """Native typed error -> JSON-RPC internal_error carrying the typed shape
    (e.g. ServerBusyError / WorkspaceBusyError per phase45 server contract)."""
    return RequestError.internal_error(
        data={"code": exc.code, "message": exc.message, "details": exc.details}
    )


def _route_select(route: str, route_options: list[str]) -> SessionConfigOptionSelect:
    """The v1 session-config surface for routing (domain stays neutral)."""
    return SessionConfigOptionSelect(
        type="select",
        id=_ROUTE_CONFIG_ID,
        name="Route",
        current_value=route,
        options=[SessionConfigSelectOption(value=option, name=option) for option in route_options],
    )


class ServerConnection:
    """Native outbound surface bound to one connected ACP client.

    Handed to the server app via its ``on_connect`` hook; the app never sees
    the underlying SDK ``AgentSideConnection``.
    """

    def __init__(self, conn: AgentSideConnection) -> None:
        self._conn = conn

    async def emit_update(
        self,
        session_id: str,
        ev: ServerUpdate,
        *,
        run_id: str,
        step_id: str | None,
    ) -> None:
        """Re-emit one native event as ``session/update`` with stable
        ``_meta={'ziggy': {'run_id', 'step_id'}}`` correlation; events without
        a v1 mapping (ModeEvent/UnknownUpdateEvent, usage without used+size)
        are skipped."""
        update = from_agent_event(ev)
        if update is None:
            return
        await self._conn.session_update(
            session_id=session_id,
            update=update,
            ziggy={"run_id": run_id, "step_id": step_id},
        )

    async def forward_permission(
        self, session_id: str, req: PermissionRequestN, context_label: str
    ) -> PermissionReply:
        """Forward a downstream permission request to the connecting client.

        The tool-call title is prefixed with ``[context_label] `` (downstream
        agent/step context). A client answering method_not_found (-32601)
        raises ``ClientPermissionUnsupported`` so the server app can fall back
        to guarded local mediation; any other JSON-RPC or connection failure
        raises ``ProtocolError``.
        """
        tool_call, options = from_permission_request(req, context_label)
        try:
            resp = await self._conn.request_permission(
                session_id=session_id, tool_call=tool_call, options=options
            )
        except RequestError as exc:
            if exc.code == -32601:
                raise ClientPermissionUnsupported() from exc
            raise ProtocolError(
                f"session/request_permission failed: {exc}", details={"code": exc.code}
            ) from exc
        except (ConnectionError, EOFError) as exc:
            raise ProtocolError(
                "client connection lost during session/request_permission",
                details={"reason": str(exc)},
            ) from exc
        return from_permission_outcome(resp)


class _SdkAgent:
    """SDK ``Agent`` protocol implementation delegating to a ZiggyAgentProtocol.

    Owns SDK model construction and error mapping; unsupported ACP surfaces
    are implemented explicitly and answer method_not_found (sdk-api-reference
    gotcha 7 — missing methods would otherwise surface as internal errors).
    """

    def __init__(self, agent_impl: ZiggyAgentProtocol) -> None:
        self._impl = agent_impl
        self.connection: ServerConnection | None = None

    def on_connect(self, conn: AgentSideConnection) -> None:
        self.connection = ServerConnection(conn)
        impl_on_connect = getattr(self._impl, "on_connect", None)
        if callable(impl_on_connect):
            impl_on_connect(self.connection)

    async def _call[T](self, awaitable: Awaitable[T]) -> T:
        """Await a delegate call, translating native typed errors to JSON-RPC."""
        try:
            return await awaitable
        except ZiggyError as exc:
            raise _to_request_error(exc) from exc

    # -- supported agent-bound methods -------------------------------------

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        if protocol_version != PROTOCOL_VERSION:
            # v1 spec: answer with the latest version WE support (1); the
            # client decides whether to continue or disconnect.
            _log.info(
                "client requested ACP protocol version %s; responding with version %s",
                protocol_version,
                PROTOCOL_VERSION,
            )
        handshake = await self._call(
            self._impl.initialize(
                client_protocol_version=protocol_version,
                client_capabilities=(
                    _dump(client_capabilities) if client_capabilities is not None else {}
                ),
                client_info=_dump(client_info) if client_info is not None else None,
            )
        )
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(
                load_session=False,
                prompt_capabilities=PromptCapabilities(
                    image=False, audio=False, embedded_context=False
                ),
            ),
            agent_info=Implementation(name=handshake.agent_name, version=handshake.agent_version),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        opened = await self._call(self._impl.new_session(cwd=cwd))
        return NewSessionResponse(
            session_id=opened.session_id,
            config_options=[_route_select(opened.route, opened.route_options)],
        )

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> SetSessionConfigOptionResponse:
        if config_id != _ROUTE_CONFIG_ID:
            raise RequestError.invalid_params(
                data={"configId": config_id, "reason": "unknown config option"}
            )
        if not isinstance(value, str):
            raise RequestError.invalid_params(
                data={"configId": config_id, "reason": "route value must be a string"}
            )
        try:
            state = await self._impl.set_config_option(
                session_id=session_id, config_id=config_id, value=value
            )
        except ValueError as exc:
            # SDK-free signal from the app: unknown route value.
            raise RequestError.invalid_params(
                data={"configId": config_id, "value": value, "reason": str(exc)}
            ) from exc
        except ZiggyError as exc:
            raise _to_request_error(exc) from exc
        return SetSessionConfigOptionResponse(
            config_options=[_route_select(state.route, state.route_options)]
        )

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        texts = [block.text for block in prompt if isinstance(block, TextContentBlock)]
        ignored = len(prompt) - len(texts)
        if ignored:
            _log.warning(
                "session/prompt: ignoring %d non-text content block(s) (text-only prompting)",
                ignored,
            )
        stop = await self._call(self._impl.prompt(session_id=session_id, text="\n".join(texts)))
        return PromptResponse(stop_reason=stop.stop_reason)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        await self._call(self._impl.cancel(session_id=session_id))

    async def authenticate(self, method_id: str, **kwargs: Any) -> AuthenticateResponse | None:
        try:
            await self._impl.authenticate(method_id=method_id)
        except NotImplementedError as exc:
            raise RequestError.method_not_found("authenticate") from exc
        except ZiggyError as exc:
            raise _to_request_error(exc) from exc
        return None

    # -- unsupported surfaces: explicit method_not_found (gotcha 7) --------

    async def load_session(self, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("session/load")

    async def list_sessions(self, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("session/list")

    async def set_session_mode(self, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("session/set_mode")

    async def fork_session(self, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("session/fork")

    async def resume_session(self, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("session/resume")

    async def close_session(self, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("session/close")

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        raise RequestError.method_not_found(method)


async def serve_stdio(agent_impl: ZiggyAgentProtocol) -> None:
    """Serve ``agent_impl`` on real stdio until the client disconnects (EOF).

    Binds a ``ServerConnection`` to the app via ``on_connect`` when the client
    attaches; returns when client stdio reaches EOF, which is where the server
    app hooks its disconnect-cancels-run teardown.
    """
    await run_agent(_SdkAgent(agent_impl))
