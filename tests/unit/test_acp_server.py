"""Unit tests for the agent-side ACP boundary (ziggy/acp/server.py).

Pure logic — no stdio, no subprocess: ``_SdkAgent`` is driven directly with a
stub ``ZiggyAgentProtocol`` implementation and a fake connection that captures
outbound frames serialized exactly the way the SDK puts them on the wire
(outbound requests/notifications: ``exclude_defaults``; handler responses:
``exclude_unset`` — both ``by_alias`` + ``exclude_none``).
"""

from __future__ import annotations

import ast
import logging
import typing
from pathlib import Path
from typing import Any

import pytest
from acp.exceptions import RequestError
from acp.schema import (
    AllowedOutcome,
    ClientCapabilities,
    DeniedOutcome,
    FileSystemCapabilities,
    ImageContentBlock,
    Implementation,
    RequestPermissionRequest,
    RequestPermissionResponse,
    SessionNotification,
    TextContentBlock,
)

from ziggy.acp.server import ServerConnection, ZiggyAgentProtocol, _SdkAgent
from ziggy.acp.types import (
    ClientPermissionUnsupported,
    MessageChunkEvent,
    ModeEvent,
    PermissionOptionN,
    PermissionRequestN,
    PlanEvent,
    RouteState,
    ServerHandshake,
    ServerNotice,
    ServerStopInfo,
    SessionOpened,
    ToolCallEvent,
    UnknownUpdateEvent,
    UsageEvent,
)
from ziggy.errors import ProtocolError, ServerBusyError

SRC_ZIGGY = Path(__file__).resolve().parents[2] / "src" / "ziggy"

ROUTE_OPTIONS = ["orchestrator", "agent:mock", "workflow:hello"]


def _wire(model: Any) -> dict[str, Any]:
    """Serialize an outbound request/notification exactly like the SDK's
    ``serialize_params`` (used for session/update, session/request_permission)."""
    return model.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_defaults=True)


def _response_wire(model: Any) -> dict[str, Any]:
    """Serialize a handler result exactly like the SDK's response path
    (``Connection._run_request``: by_alias + exclude_none + exclude_unset)."""
    return model.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_unset=True)


def _text(text: str) -> TextContentBlock:
    return TextContentBlock(type="text", text=text)


class FakeAgentSideConnection:
    """Captures outbound client-bound traffic as wire-shaped frames."""

    def __init__(self) -> None:
        self.update_frames: list[dict[str, Any]] = []
        self.permission_frames: list[dict[str, Any]] = []
        self.permission_result: RequestPermissionResponse | Exception = RequestPermissionResponse(
            outcome=DeniedOutcome(outcome="cancelled")
        )

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        notification = SessionNotification(
            session_id=session_id, update=update, field_meta=kwargs or None
        )
        self.update_frames.append(_wire(notification))

    async def request_permission(
        self, session_id: str, tool_call: Any, options: list[Any], **kwargs: Any
    ) -> RequestPermissionResponse:
        request = RequestPermissionRequest(
            session_id=session_id, tool_call=tool_call, options=options, field_meta=kwargs or None
        )
        self.permission_frames.append(_wire(request))
        if isinstance(self.permission_result, Exception):
            raise self.permission_result
        return self.permission_result


class StubZiggyAgent:
    """Minimal native-typed server app used to drive _SdkAgent."""

    def __init__(self) -> None:
        self.route = "orchestrator"
        self.route_options = list(ROUTE_OPTIONS)
        self.stop_reason = "end_turn"
        self.initialize_calls: list[tuple[int, dict[str, Any], dict[str, Any] | None]] = []
        self.new_session_cwds: list[str] = []
        self.prompts: list[tuple[str, str]] = []
        self.cancelled: list[str] = []
        self.authenticated: list[str] = []
        self.connection: ServerConnection | None = None
        self.prompt_error: Exception | None = None

    def on_connect(self, conn: ServerConnection) -> None:
        self.connection = conn

    async def initialize(
        self,
        client_protocol_version: int,
        client_capabilities: dict[str, Any],
        client_info: dict[str, Any] | None,
    ) -> ServerHandshake:
        self.initialize_calls.append((client_protocol_version, client_capabilities, client_info))
        return ServerHandshake(agent_name="ziggy", agent_version="0.1.0-test")

    async def new_session(self, cwd: str) -> SessionOpened:
        self.new_session_cwds.append(cwd)
        return SessionOpened(
            session_id="sess-1", route=self.route, route_options=self.route_options
        )

    async def set_config_option(self, session_id: str, config_id: str, value: str) -> RouteState:
        if value not in self.route_options:
            raise ValueError(f"unknown route {value!r}")
        self.route = value
        return RouteState(route=value, route_options=self.route_options)

    async def prompt(self, session_id: str, text: str) -> ServerStopInfo:
        if self.prompt_error is not None:
            raise self.prompt_error
        self.prompts.append((session_id, text))
        return ServerStopInfo(stop_reason=self.stop_reason)

    async def cancel(self, session_id: str) -> None:
        self.cancelled.append(session_id)

    async def authenticate(self, method_id: str) -> None:
        self.authenticated.append(method_id)
        raise NotImplementedError("authentication not supported")


@pytest.fixture
def stub() -> StubZiggyAgent:
    return StubZiggyAgent()


@pytest.fixture
def fake_conn() -> FakeAgentSideConnection:
    return FakeAgentSideConnection()


@pytest.fixture
def agent(stub: StubZiggyAgent, fake_conn: FakeAgentSideConnection) -> _SdkAgent:
    sdk_agent = _SdkAgent(stub)
    sdk_agent.on_connect(fake_conn)  # type: ignore[arg-type]  # duck-typed fake
    return sdk_agent


class TestSdkContainment:
    def test_sdk_import_allowance_is_only_ziggy_acp(self) -> None:
        """REQ-001: the ONLY path allowed to import the SDK is src/ziggy/acp/;
        every other package — including the (next-stage) server/ package — is
        scanned and must stay SDK-free."""
        allowed = SRC_ZIGGY / "acp"
        offenders: list[str] = []
        scanned: list[Path] = []
        for path in sorted(SRC_ZIGGY.rglob("*.py")):
            if allowed in path.parents:
                continue
            scanned.append(path)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    offenders.extend(
                        f"{path}:{node.lineno}"
                        for alias in node.names
                        if alias.name == "acp" or alias.name.startswith("acp.")
                    )
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    module = node.module or ""
                    if module == "acp" or module.startswith("acp."):
                        offenders.append(f"{path}:{node.lineno}")
        assert not offenders, f"SDK import leaked outside ziggy.acp: {offenders}"
        server_pkg = SRC_ZIGGY / "server"
        if server_pkg.exists():
            server_files = set(server_pkg.rglob("*.py"))
            assert server_files <= set(scanned), "server/ package must be scanned for SDK leaks"

    def test_ziggy_agent_protocol_signatures_are_native_only(self) -> None:
        """No SDK type may appear in any ZiggyAgentProtocol method signature."""

        def leaves(hint: Any) -> typing.Iterator[Any]:
            args = typing.get_args(hint)
            if not args:
                yield hint
            for arg in args:
                yield from leaves(arg)

        method_names = [
            "initialize",
            "new_session",
            "set_config_option",
            "prompt",
            "cancel",
            "authenticate",
        ]
        for name in method_names:
            hints = typing.get_type_hints(getattr(ZiggyAgentProtocol, name))
            assert hints, f"{name} must be fully annotated"
            for hint in hints.values():
                for leaf in leaves(hint):
                    module = getattr(leaf, "__module__", "")
                    assert not (module == "acp" or module.startswith("acp.")), (
                        f"SDK type {leaf!r} leaked into ZiggyAgentProtocol.{name}"
                    )


class TestInitialize:
    async def test_response_wire_shape(self, agent: _SdkAgent) -> None:
        resp = await agent.initialize(
            protocol_version=1,
            client_capabilities=ClientCapabilities(
                fs=FileSystemCapabilities(read_text_file=True, write_text_file=True),
                terminal=True,
            ),
            client_info=Implementation(name="zed", version="1.0.0"),
        )
        wire = _response_wire(resp)
        assert wire["protocolVersion"] == 1
        assert wire["agentInfo"] == {"name": "ziggy", "version": "0.1.0-test"}
        assert wire["agentCapabilities"]["loadSession"] is False
        assert wire["agentCapabilities"]["promptCapabilities"] == {
            "image": False,
            "audio": False,
            "embeddedContext": False,
        }

    async def test_delegates_native_dicts_not_sdk_models(
        self, agent: _SdkAgent, stub: StubZiggyAgent
    ) -> None:
        await agent.initialize(
            protocol_version=1,
            client_capabilities=ClientCapabilities(
                fs=FileSystemCapabilities(read_text_file=True, write_text_file=False),
                terminal=True,
            ),
            client_info=Implementation(name="zed", version="1.0.0"),
        )
        version, capabilities, info = stub.initialize_calls[0]
        assert version == 1
        assert isinstance(capabilities, dict)
        assert capabilities["fs"] == {"read_text_file": True, "write_text_file": False}
        assert capabilities["terminal"] is True
        assert info == {"name": "zed", "version": "1.0.0"}

    async def test_version_mismatch_still_answers_our_version_1(
        self, agent: _SdkAgent, stub: StubZiggyAgent
    ) -> None:
        resp = await agent.initialize(protocol_version=99)
        assert _response_wire(resp)["protocolVersion"] == 1
        version, capabilities, info = stub.initialize_calls[0]
        assert version == 99
        assert capabilities == {}
        assert info is None


class TestNewSession:
    async def test_config_options_wire_shape(self, agent: _SdkAgent, stub: StubZiggyAgent) -> None:
        resp = await agent.new_session(cwd="/tmp/workspace")
        assert stub.new_session_cwds == ["/tmp/workspace"]
        wire = _response_wire(resp)
        assert wire["sessionId"] == "sess-1"
        assert wire["configOptions"] == [
            {
                "type": "select",
                "id": "route",
                "name": "Route",
                "currentValue": "orchestrator",
                "options": [{"value": option, "name": option} for option in ROUTE_OPTIONS],
            }
        ]


class TestSetConfigOption:
    async def test_route_switch(self, agent: _SdkAgent, stub: StubZiggyAgent) -> None:
        resp = await agent.set_config_option(
            config_id="route", session_id="sess-1", value="agent:mock"
        )
        assert stub.route == "agent:mock"
        wire = _response_wire(resp)
        assert wire["configOptions"][0]["currentValue"] == "agent:mock"
        assert wire["configOptions"][0]["id"] == "route"

    async def test_unknown_config_id_is_invalid_params(self, agent: _SdkAgent) -> None:
        with pytest.raises(RequestError) as exc_info:
            await agent.set_config_option(config_id="model", session_id="sess-1", value="x")
        assert exc_info.value.code == -32602
        assert exc_info.value.data["configId"] == "model"

    async def test_unknown_route_value_is_invalid_params(self, agent: _SdkAgent) -> None:
        with pytest.raises(RequestError) as exc_info:
            await agent.set_config_option(
                config_id="route", session_id="sess-1", value="agent:evil"
            )
        assert exc_info.value.code == -32602
        assert exc_info.value.data["value"] == "agent:evil"

    async def test_boolean_value_is_invalid_params(self, agent: _SdkAgent) -> None:
        with pytest.raises(RequestError) as exc_info:
            await agent.set_config_option(config_id="route", session_id="sess-1", value=True)
        assert exc_info.value.code == -32602


class TestPrompt:
    async def test_text_blocks_joined_with_newline(
        self, agent: _SdkAgent, stub: StubZiggyAgent
    ) -> None:
        resp = await agent.prompt(
            session_id="sess-1", prompt=[_text("first line"), _text("second line")]
        )
        assert stub.prompts == [("sess-1", "first line\nsecond line")]
        assert resp.stop_reason == "end_turn"
        assert _response_wire(resp) == {"stopReason": "end_turn"}

    async def test_non_text_blocks_ignored_with_counted_note(
        self, agent: _SdkAgent, stub: StubZiggyAgent, caplog: pytest.LogCaptureFixture
    ) -> None:
        image = ImageContentBlock(type="image", data="aGk=", mime_type="image/png")
        with caplog.at_level(logging.WARNING, logger="ziggy.acp.server"):
            await agent.prompt(session_id="sess-1", prompt=[_text("keep"), image, _text("this")])
        assert stub.prompts == [("sess-1", "keep\nthis")]
        assert any("1 non-text content block" in record.getMessage() for record in caplog.records)

    async def test_stop_reason_mapping(self, agent: _SdkAgent, stub: StubZiggyAgent) -> None:
        stub.stop_reason = "cancelled"
        resp = await agent.prompt(session_id="sess-1", prompt=[_text("go")])
        assert _response_wire(resp) == {"stopReason": "cancelled"}

    async def test_native_typed_error_becomes_typed_internal_error(
        self, agent: _SdkAgent, stub: StubZiggyAgent
    ) -> None:
        stub.prompt_error = ServerBusyError("run already active", details={"max_active_runs": 1})
        with pytest.raises(RequestError) as exc_info:
            await agent.prompt(session_id="sess-1", prompt=[_text("go")])
        assert exc_info.value.code == -32603
        assert exc_info.value.data == {
            "code": "ServerBusyError",
            "message": "run already active",
            "details": {"max_active_runs": 1},
        }


class TestCancelAndAuthenticate:
    async def test_cancel_passthrough(self, agent: _SdkAgent, stub: StubZiggyAgent) -> None:
        assert await agent.cancel(session_id="sess-1") is None
        assert stub.cancelled == ["sess-1"]

    async def test_authenticate_default_not_implemented_maps_to_method_not_found(
        self, agent: _SdkAgent, stub: StubZiggyAgent
    ) -> None:
        with pytest.raises(RequestError) as exc_info:
            await agent.authenticate(method_id="api-key")
        assert exc_info.value.code == -32601
        assert stub.authenticated == ["api-key"]

    async def test_authenticate_delegates_success(self, stub: StubZiggyAgent) -> None:
        async def ok_authenticate(method_id: str) -> None:
            stub.authenticated.append(method_id)

        stub.authenticate = ok_authenticate  # type: ignore[method-assign]
        sdk_agent = _SdkAgent(stub)
        assert await sdk_agent.authenticate(method_id="api-key") is None
        assert stub.authenticated == ["api-key"]


class TestUnsupportedMethods:
    @pytest.mark.parametrize(
        ("method_name", "wire_name"),
        [
            ("load_session", "session/load"),
            ("list_sessions", "session/list"),
            ("set_session_mode", "session/set_mode"),
            ("fork_session", "session/fork"),
            ("resume_session", "session/resume"),
            ("close_session", "session/close"),
        ],
    )
    async def test_raises_method_not_found(
        self, agent: _SdkAgent, method_name: str, wire_name: str
    ) -> None:
        with pytest.raises(RequestError) as exc_info:
            await getattr(agent, method_name)(session_id="sess-1", cwd="/tmp")
        assert exc_info.value.code == -32601
        assert exc_info.value.data == {"method": wire_name}

    async def test_ext_method_and_notification(self, agent: _SdkAgent) -> None:
        with pytest.raises(RequestError) as exc_info:
            await agent.ext_method("vendor/thing", {"x": 1})
        assert exc_info.value.code == -32601
        assert exc_info.value.data == {"method": "vendor/thing"}
        with pytest.raises(RequestError) as exc_info:
            await agent.ext_notification("vendor/notify", {})
        assert exc_info.value.code == -32601


class TestOnConnect:
    def test_binds_server_connection_to_the_app(
        self, agent: _SdkAgent, stub: StubZiggyAgent
    ) -> None:
        assert isinstance(agent.connection, ServerConnection)
        assert stub.connection is agent.connection


class TestEmitUpdate:
    @pytest.fixture
    def conn(self, fake_conn: FakeAgentSideConnection) -> ServerConnection:
        return ServerConnection(fake_conn)  # type: ignore[arg-type]

    async def test_message_chunk(
        self, conn: ServerConnection, fake_conn: FakeAgentSideConnection
    ) -> None:
        await conn.emit_update(
            "sess-1", MessageChunkEvent(role="agent", text="hi"), run_id="r-1", step_id="s-1"
        )
        assert fake_conn.update_frames == [
            {
                "sessionId": "sess-1",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "hi"},
                },
                "_meta": {"ziggy": {"run_id": "r-1", "step_id": "s-1"}},
            }
        ]

    async def test_thought_chunk(
        self, conn: ServerConnection, fake_conn: FakeAgentSideConnection
    ) -> None:
        await conn.emit_update(
            "sess-1",
            MessageChunkEvent(role="agent", text="thinking", thought=True),
            run_id="r-1",
            step_id=None,
        )
        frame = fake_conn.update_frames[0]
        assert frame["update"]["sessionUpdate"] == "agent_thought_chunk"
        assert frame["_meta"]["ziggy"] == {"run_id": "r-1", "step_id": None}

    async def test_tool_call_start(
        self, conn: ServerConnection, fake_conn: FakeAgentSideConnection
    ) -> None:
        ev = ToolCallEvent(
            tool_call_id="tc-1",
            phase="start",
            title="Read file",
            kind="read",
            status="in_progress",
            locations=["/tmp/a.py"],
            has_content=False,
            raw={},
        )
        await conn.emit_update("sess-1", ev, run_id="r-1", step_id="s-1")
        assert fake_conn.update_frames[0]["update"] == {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-1",
            "title": "Read file",
            "kind": "read",
            "status": "in_progress",
            "locations": [{"path": "/tmp/a.py"}],
        }

    async def test_tool_call_start_without_title_falls_back_to_id(
        self, conn: ServerConnection, fake_conn: FakeAgentSideConnection
    ) -> None:
        ev = ToolCallEvent(
            tool_call_id="tc-2",
            phase="start",
            title=None,
            kind=None,
            status=None,
            locations=[],
            has_content=False,
            raw={},
        )
        await conn.emit_update("sess-1", ev, run_id="r-1", step_id="s-1")
        assert fake_conn.update_frames[0]["update"] == {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-2",
            "title": "tc-2",
        }

    async def test_tool_call_update(
        self, conn: ServerConnection, fake_conn: FakeAgentSideConnection
    ) -> None:
        ev = ToolCallEvent(
            tool_call_id="tc-1",
            phase="update",
            title=None,
            kind=None,
            status="completed",
            locations=[],
            has_content=True,
            raw={},
        )
        await conn.emit_update("sess-1", ev, run_id="r-1", step_id="s-1")
        assert fake_conn.update_frames[0]["update"] == {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-1",
            "status": "completed",
        }

    async def test_plan(self, conn: ServerConnection, fake_conn: FakeAgentSideConnection) -> None:
        ev = PlanEvent(entries=[{"content": "step one", "priority": "high", "status": "pending"}])
        await conn.emit_update("sess-1", ev, run_id="r-1", step_id="s-1")
        assert fake_conn.update_frames[0]["update"] == {
            "sessionUpdate": "plan",
            "entries": [{"content": "step one", "priority": "high", "status": "pending"}],
        }

    async def test_usage_with_used_and_size(
        self, conn: ServerConnection, fake_conn: FakeAgentSideConnection
    ) -> None:
        ev = UsageEvent(used=1200, size=200_000, cost=0.42, currency="USD", raw={})
        await conn.emit_update("sess-1", ev, run_id="r-1", step_id="s-1")
        assert fake_conn.update_frames[0]["update"] == {
            "sessionUpdate": "usage_update",
            "used": 1200,
            "size": 200000,
            "cost": {"amount": 0.42, "currency": "USD"},
        }

    async def test_usage_without_used_or_size_is_skipped(
        self, conn: ServerConnection, fake_conn: FakeAgentSideConnection
    ) -> None:
        await conn.emit_update(
            "sess-1",
            UsageEvent(used=None, size=100, cost=None, currency=None, raw={}),
            run_id="r-1",
            step_id="s-1",
        )
        await conn.emit_update(
            "sess-1",
            UsageEvent(used=5, size=None, cost=None, currency=None, raw={}),
            run_id="r-1",
            step_id="s-1",
        )
        assert fake_conn.update_frames == []

    async def test_server_notice_is_agent_message_chunk(
        self, conn: ServerConnection, fake_conn: FakeAgentSideConnection
    ) -> None:
        await conn.emit_update(
            "sess-1",
            ServerNotice(text="permission forwarding unsupported; guarded fallback active"),
            run_id="r-1",
            step_id=None,
        )
        frame = fake_conn.update_frames[0]
        assert frame["update"]["sessionUpdate"] == "agent_message_chunk"
        assert "guarded fallback" in frame["update"]["content"]["text"]
        assert frame["_meta"]["ziggy"]["run_id"] == "r-1"

    async def test_unmapped_events_are_skipped(
        self, conn: ServerConnection, fake_conn: FakeAgentSideConnection
    ) -> None:
        await conn.emit_update(
            "sess-1",
            ModeEvent(kind="current_mode", payload={"current_mode_id": "code"}),
            run_id="r-1",
            step_id="s-1",
        )
        await conn.emit_update(
            "sess-1",
            UnknownUpdateEvent(update_type="sparkle_update", payload={}),
            run_id="r-1",
            step_id="s-1",
        )
        assert fake_conn.update_frames == []


class TestForwardPermission:
    @pytest.fixture
    def conn(self, fake_conn: FakeAgentSideConnection) -> ServerConnection:
        return ServerConnection(fake_conn)  # type: ignore[arg-type]

    def _request(self, tool_call: dict[str, Any] | None = None) -> PermissionRequestN:
        return PermissionRequestN(
            session_id="down-sess",
            tool_call=tool_call
            if tool_call is not None
            else {
                "tool_call_id": "tc-9",
                "title": "Write file",
                "kind": "edit",
                "status": "pending",
            },
            options=[
                PermissionOptionN(option_id="allow", name="Allow once", kind="allow_once"),
                PermissionOptionN(option_id="deny", name="Reject once", kind="reject_once"),
            ],
        )

    async def test_forwards_with_context_label_and_maps_approval(
        self, conn: ServerConnection, fake_conn: FakeAgentSideConnection
    ) -> None:
        fake_conn.permission_result = RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id="allow")
        )
        reply = await conn.forward_permission("sess-1", self._request(), "agent:mock/step-1")
        assert reply.kind == "selected"
        assert reply.option_id == "allow"
        frame = fake_conn.permission_frames[0]
        assert frame["sessionId"] == "sess-1"
        assert frame["toolCall"] == {
            "toolCallId": "tc-9",
            "title": "[agent:mock/step-1] Write file",
            "kind": "edit",
            "status": "pending",
        }
        assert frame["options"] == [
            {"optionId": "allow", "name": "Allow once", "kind": "allow_once"},
            {"optionId": "deny", "name": "Reject once", "kind": "reject_once"},
        ]

    async def test_selected_reject_option_still_maps_to_selected(
        self, conn: ServerConnection, fake_conn: FakeAgentSideConnection
    ) -> None:
        fake_conn.permission_result = RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id="deny")
        )
        reply = await conn.forward_permission("sess-1", self._request(), "agent:mock")
        assert reply.kind == "selected"
        assert reply.option_id == "deny"

    async def test_cancelled_outcome_maps_to_cancelled(
        self, conn: ServerConnection, fake_conn: FakeAgentSideConnection
    ) -> None:
        fake_conn.permission_result = RequestPermissionResponse(
            outcome=DeniedOutcome(outcome="cancelled")
        )
        reply = await conn.forward_permission("sess-1", self._request(), "agent:mock")
        assert reply.kind == "cancelled"
        assert reply.option_id is None

    async def test_title_missing_uses_bare_context_label(
        self, conn: ServerConnection, fake_conn: FakeAgentSideConnection
    ) -> None:
        await conn.forward_permission(
            "sess-1", self._request(tool_call={"tool_call_id": "tc-0"}), "workflow:hello/lint"
        )
        assert fake_conn.permission_frames[0]["toolCall"]["title"] == "[workflow:hello/lint]"

    async def test_method_not_found_raises_client_permission_unsupported(
        self, conn: ServerConnection, fake_conn: FakeAgentSideConnection
    ) -> None:
        fake_conn.permission_result = RequestError.method_not_found("session/request_permission")
        with pytest.raises(ClientPermissionUnsupported):
            await conn.forward_permission("sess-1", self._request(), "agent:mock")

    async def test_other_request_error_raises_protocol_error(
        self, conn: ServerConnection, fake_conn: FakeAgentSideConnection
    ) -> None:
        fake_conn.permission_result = RequestError.internal_error({"boom": True})
        with pytest.raises(ProtocolError) as exc_info:
            await conn.forward_permission("sess-1", self._request(), "agent:mock")
        assert exc_info.value.details["code"] == -32603

    async def test_connection_loss_raises_protocol_error(
        self, conn: ServerConnection, fake_conn: FakeAgentSideConnection
    ) -> None:
        fake_conn.permission_result = ConnectionError("pipe closed")
        with pytest.raises(ProtocolError):
            await conn.forward_permission("sess-1", self._request(), "agent:mock")


class TestNativeSurfaceExports:
    def test_server_surface_is_re_exported_from_ziggy_acp(self) -> None:
        import ziggy.acp as adapter

        for name in (
            "ClientPermissionUnsupported",
            "RouteState",
            "ServerConnection",
            "ServerHandshake",
            "ServerNotice",
            "ServerStopInfo",
            "ServerUpdate",
            "SessionOpened",
            "ZiggyAgentProtocol",
            "serve_stdio",
        ):
            assert name in adapter.__all__
            assert hasattr(adapter, name)
