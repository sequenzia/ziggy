"""Pure conversion tests for ziggy.acp (no subprocess) + the no-SDK-leak gate."""

from __future__ import annotations

import ast
from pathlib import Path

from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    AgentPlanContentUpdate,
    AgentPlanRemovedUpdate,
    AgentPlanUpdate,
    AgentThoughtChunk,
    AllowedOutcome,
    AvailableCommand,
    AvailableCommandsUpdate,
    ConfigOptionUpdate,
    ContentToolCallContent,
    Cost,
    CurrentModeUpdate,
    DeniedOutcome,
    EnvVarAuthMethod,
    ImageContentBlock,
    Implementation,
    InitializeResponse,
    PermissionOption,
    PlanEntry,
    PlanUpdateItems,
    PlanUpdateMarkdown,
    SessionInfoUpdate,
    SessionNotification,
    TextContentBlock,
    ToolCallLocation,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UsageUpdate,
    UserMessageChunk,
)

from ziggy.acp.convert import (
    content_block_text,
    to_agent_event,
    to_handshake_info,
    to_permission_request,
    to_permission_response,
)
from ziggy.acp.types import (
    MessageChunkEvent,
    ModeEvent,
    PermissionReply,
    PlanEvent,
    ToolCallEvent,
    UnknownUpdateEvent,
    UsageEvent,
)

SRC_ZIGGY = Path(__file__).resolve().parents[2] / "src" / "ziggy"


def _text(text: str) -> TextContentBlock:
    return TextContentBlock(type="text", text=text)


class TestNoSdkLeak:
    def test_acp_sdk_imported_only_inside_ziggy_acp(self) -> None:
        """REQ-001 gate: `import acp` / `from acp ...` never appears outside src/ziggy/acp/."""
        acp_pkg = SRC_ZIGGY / "acp"
        offenders: list[str] = []
        for path in sorted(SRC_ZIGGY.rglob("*.py")):
            if acp_pkg in path.parents:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "acp" or alias.name.startswith("acp."):
                            offenders.append(f"{path}:{node.lineno}")
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    module = node.module or ""
                    if module == "acp" or module.startswith("acp."):
                        offenders.append(f"{path}:{node.lineno}")
        assert not offenders, f"SDK import leaked outside ziggy.acp: {offenders}"

    def test_ziggy_acp_exports_no_sdk_names(self) -> None:
        import acp as sdk

        import ziggy.acp as adapter

        sdk_names = set(dir(sdk))
        leaked = [name for name in adapter.__all__ if name in sdk_names]
        assert not leaked, f"ziggy.acp re-exports SDK names: {leaked}"


class TestMessageChunks:
    def test_user_message_chunk(self) -> None:
        update = UserMessageChunk(session_update="user_message_chunk", content=_text("hi"))
        ev = to_agent_event(update)
        assert ev == MessageChunkEvent(role="user", text="hi", thought=False)

    def test_agent_message_chunk(self) -> None:
        update = AgentMessageChunk(session_update="agent_message_chunk", content=_text("hello"))
        ev = to_agent_event(update)
        assert ev == MessageChunkEvent(role="agent", text="hello", thought=False)

    def test_agent_thought_chunk(self) -> None:
        update = AgentThoughtChunk(session_update="agent_thought_chunk", content=_text("hmm"))
        ev = to_agent_event(update)
        assert ev == MessageChunkEvent(role="agent", text="hmm", thought=True)

    def test_non_text_content_becomes_marker_not_crash(self) -> None:
        image = ImageContentBlock(type="image", data="aGk=", mime_type="image/png")
        update = AgentMessageChunk(session_update="agent_message_chunk", content=image)
        ev = to_agent_event(update)
        assert isinstance(ev, MessageChunkEvent)
        assert ev.text == "[image]"

    def test_content_block_text_helper(self) -> None:
        assert content_block_text(_text("x")) == "x"
        assert content_block_text(object()) == "[object]"


class TestToolCalls:
    def test_tool_call_start(self) -> None:
        update = ToolCallStart(
            session_update="tool_call",
            tool_call_id="tc-1",
            title="Read file",
            kind="read",
            status="in_progress",
            locations=[ToolCallLocation(path="/tmp/a.py", line=3)],
            content=[
                ContentToolCallContent(type="content", content=_text("data")),
            ],
        )
        ev = to_agent_event(update)
        assert isinstance(ev, ToolCallEvent)
        assert ev.phase == "start"
        assert ev.tool_call_id == "tc-1"
        assert ev.title == "Read file"
        assert ev.kind == "read"
        assert ev.status == "in_progress"
        assert ev.locations == ["/tmp/a.py"]
        assert ev.has_content is True
        assert ev.raw["tool_call_id"] == "tc-1"

    def test_tool_call_update_minimal(self) -> None:
        update = ToolCallProgress(session_update="tool_call_update", tool_call_id="tc-1")
        ev = to_agent_event(update)
        assert isinstance(ev, ToolCallEvent)
        assert ev.phase == "update"
        assert ev.title is None
        assert ev.kind is None
        assert ev.status is None
        assert ev.locations == []
        assert ev.has_content is False


class TestPlans:
    def test_plan(self) -> None:
        update = AgentPlanUpdate(
            session_update="plan",
            entries=[PlanEntry(content="step one", priority="high", status="pending")],
        )
        ev = to_agent_event(update)
        assert ev == PlanEvent(
            entries=[{"content": "step one", "priority": "high", "status": "pending"}]
        )

    def test_plan_content_update_items_normalizes_to_plan_event(self) -> None:
        update = AgentPlanContentUpdate(
            session_update="plan_update",
            plan=PlanUpdateItems(
                type="items",
                id="p1",
                entries=[PlanEntry(content="do it", priority="low", status="completed")],
            ),
        )
        ev = to_agent_event(update)
        assert ev == PlanEvent(
            entries=[{"content": "do it", "priority": "low", "status": "completed"}]
        )

    def test_plan_content_update_markdown_preserved_as_unknown(self) -> None:
        update = AgentPlanContentUpdate(
            session_update="plan_update",
            plan=PlanUpdateMarkdown(type="markdown", id="p1", content="# the plan"),
        )
        ev = to_agent_event(update)
        assert isinstance(ev, UnknownUpdateEvent)
        assert ev.update_type == "plan_update"
        assert ev.payload["plan"]["content"] == "# the plan"

    def test_plan_removed_becomes_empty_plan(self) -> None:
        update = AgentPlanRemovedUpdate(session_update="plan_removed", id="p1")
        ev = to_agent_event(update)
        assert ev == PlanEvent(entries=[])


class TestUsageAndModes:
    def test_usage_update_with_cost(self) -> None:
        update = UsageUpdate(
            session_update="usage_update",
            used=1200,
            size=200_000,
            cost=Cost(amount=0.42, currency="USD"),
        )
        ev = to_agent_event(update)
        assert isinstance(ev, UsageEvent)
        assert (ev.used, ev.size, ev.cost, ev.currency) == (1200, 200_000, 0.42, "USD")
        assert ev.raw["used"] == 1200

    def test_usage_update_without_cost(self) -> None:
        update = UsageUpdate(session_update="usage_update", used=5, size=10)
        ev = to_agent_event(update)
        assert isinstance(ev, UsageEvent)
        assert (ev.cost, ev.currency) == (None, None)

    def test_current_mode_update(self) -> None:
        update = CurrentModeUpdate(session_update="current_mode_update", current_mode_id="code")
        ev = to_agent_event(update)
        assert isinstance(ev, ModeEvent)
        assert ev.kind == "current_mode"
        assert ev.payload["current_mode_id"] == "code"

    def test_config_option_update(self) -> None:
        update = ConfigOptionUpdate.model_validate(
            {
                "sessionUpdate": "config_option_update",
                "configOptions": [
                    {"type": "boolean", "id": "opt", "name": "Opt", "currentValue": True}
                ],
            }
        )
        ev = to_agent_event(update)
        assert isinstance(ev, ModeEvent)
        assert ev.kind == "config_option"
        assert ev.payload["config_options"][0]["id"] == "opt"

    def test_session_info_update(self) -> None:
        update = SessionInfoUpdate(session_update="session_info_update", title="Refactor")
        ev = to_agent_event(update)
        assert isinstance(ev, ModeEvent)
        assert ev.kind == "session_info"
        assert ev.payload["title"] == "Refactor"

    def test_available_commands_update(self) -> None:
        update = AvailableCommandsUpdate(
            session_update="available_commands_update",
            available_commands=[AvailableCommand(name="create_plan", description="plan things")],
        )
        ev = to_agent_event(update)
        assert isinstance(ev, ModeEvent)
        assert ev.kind == "available_commands"
        assert ev.payload["available_commands"][0]["name"] == "create_plan"


class TestUnknownUpdates:
    def test_unmodeled_object_becomes_unknown_event(self) -> None:
        class FutureUpdate:
            session_update = "sparkle_update"

        ev = to_agent_event(FutureUpdate())
        assert isinstance(ev, UnknownUpdateEvent)
        assert ev.update_type == "sparkle_update"
        assert "repr" in ev.payload

    def test_unmodeled_object_without_discriminator(self) -> None:
        ev = to_agent_event(object())
        assert isinstance(ev, UnknownUpdateEvent)
        assert ev.update_type == "object"


class TestWireParsing:
    def test_session_notification_wire_frame_round_trips(self) -> None:
        """CamelCase wire frame -> SDK discriminated union -> native event."""
        notification = SessionNotification.model_validate(
            {
                "sessionId": "sess-1",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "streamed"},
                },
            }
        )
        ev = to_agent_event(notification.update)
        assert ev == MessageChunkEvent(role="agent", text="streamed", thought=False)


class TestPermissions:
    def _options(self) -> list[PermissionOption]:
        return [
            PermissionOption(option_id="allow", name="Allow once", kind="allow_once"),
            PermissionOption(option_id="reject", name="Reject always", kind="reject_always"),
        ]

    def test_request_conversion(self) -> None:
        tool_call = ToolCallUpdate(tool_call_id="tc-9", title="Run tests", status="pending")
        req = to_permission_request("sess-1", tool_call, self._options())
        assert req.session_id == "sess-1"
        assert req.tool_call["tool_call_id"] == "tc-9"
        assert [o.option_id for o in req.options] == ["allow", "reject"]
        assert req.options[1].kind == "reject_always"

    def test_selected_reply_maps_to_allowed_outcome(self) -> None:
        resp = to_permission_response(PermissionReply(kind="selected", option_id="allow"))
        assert isinstance(resp.outcome, AllowedOutcome)
        assert resp.outcome.outcome == "selected"
        assert resp.outcome.option_id == "allow"

    def test_cancelled_reply_maps_to_denied_outcome(self) -> None:
        resp = to_permission_response(PermissionReply(kind="cancelled"))
        assert isinstance(resp.outcome, DeniedOutcome)
        assert resp.outcome.outcome == "cancelled"

    def test_selected_without_option_id_fails_toward_denial(self) -> None:
        resp = to_permission_response(PermissionReply(kind="selected", option_id=None))
        assert isinstance(resp.outcome, DeniedOutcome)


class TestHandshake:
    def test_full_handshake(self) -> None:
        resp = InitializeResponse(
            protocol_version=1,
            agent_capabilities=AgentCapabilities(load_session=True),
            auth_methods=[
                EnvVarAuthMethod.model_validate(
                    {
                        "type": "env_var",
                        "id": "api-key",
                        "name": "API key",
                        "vars": [],
                    }
                )
            ],
            agent_info=Implementation(name="claude-agent-acp", title="Claude", version="1.2.3"),
        )
        info = to_handshake_info(resp)
        assert info.protocol_version == 1
        assert info.agent_name == "claude-agent-acp"
        assert info.agent_title == "Claude"
        assert info.agent_version == "1.2.3"
        assert isinstance(info.capabilities, dict)
        assert info.capabilities["load_session"] is True
        assert info.auth_methods[0]["id"] == "api-key"

    def test_handshake_without_agent_info(self) -> None:
        resp = InitializeResponse(protocol_version=1)
        info = to_handshake_info(resp)
        assert info.agent_name == "unknown"
        assert info.agent_version is None
        assert info.agent_title is None
        assert info.auth_methods == []
        assert isinstance(info.capabilities, dict)

    def test_handshake_reports_agent_version_verbatim(self) -> None:
        resp = InitializeResponse(protocol_version=2)
        info = to_handshake_info(resp)
        assert info.protocol_version == 2
