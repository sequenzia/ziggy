"""Unit tests for the ``ziggy serve`` application (ziggy/server/app.py).

The native ``ZiggyServer`` surface is driven directly with a stub
ServerConnection — no SDK, no stdio. Route dispatch, prompt mapping, and the
permission short-circuit run through the REAL engine paths against the raw
mock agent under a tmp ``ZIGGY_HOME``; admission/cancel/shutdown wiring uses
a monkeypatched ``execute_run`` for determinism. The full subprocess loopback
(`ziggy serve` over real stdio) lives in the next stage's integration suite.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.mocks import RAW_AGENT_PATH, scenarios  # noqa: E402

import ziggy.server.app as app_module  # noqa: E402
from ziggy.acp import (  # noqa: E402
    ClientPermissionUnsupported,
    MessageChunkEvent,
    PermissionOptionN,
    PermissionReply,
    PermissionRequestN,
    PlanEvent,
    ServerNotice,
    ServerUpdate,
    ToolCallEvent,
    UsageEvent,
)
from ziggy.config import load_config  # noqa: E402
from ziggy.errors import (  # noqa: E402
    ConfigError,
    ProtocolError,
    ServerBusyError,
    ValidationError,
)
from ziggy.models.common import (  # noqa: E402
    CaptureProfile,
    RunKind,
    RunStatus,
    StepStatus,
)
from ziggy.models.events import EventEnvelope  # noqa: E402
from ziggy.models.result import RunResult, StepResult  # noqa: E402
from ziggy.policy import (  # noqa: E402
    RULE_OUTSIDE_WORKSPACE_DENY,
    RULE_READ_IN_WORKSPACE_ALLOW,
    RULE_TERMINAL_DEFAULT_DENY,
    MediationPolicy,
)
from ziggy.server import DEFAULT_ROUTE, SessionState, ZiggyServer  # noqa: E402
from ziggy.server.app import (  # noqa: E402
    GUARDED_FALLBACK_SUFFIX,
    RULE_FORWARD_FAILED,
    _RunBridge,
    _update_for,
)
from ziggy.store import RunStore  # noqa: E402

HELLO_TEXT = "".join(scenarios.HELLO_CHUNKS)


# ------------------------------------------------------------------ fixtures


def config_toml() -> str:
    def agent(name: str, scenario: str) -> str:
        return (
            f"[agents.{name}]\n"
            f"command = {json.dumps(sys.executable)}\n"
            f"args = [{json.dumps(str(RAW_AGENT_PATH))}, {json.dumps(scenario)}]\n"
            'provider = "mock"\n'
        )

    return (
        "schema_version = 1\n"
        "[engine]\n"
        "default_step_timeout_seconds = 30\n"
        + agent("mock-hello", scenarios.HELLO)
        + agent("mock-perm", scenarios.PERMISSION)
        + "[agents.mock-broken]\n"
        'command = "/nonexistent-ziggy-agent-binary"\n'
        'provider = "mock"\n'
    )


HELLO_WF = """\
version: 1
name: hello
steps:
  greet:
    agent: mock-hello
    prompt: say hello
"""

REQVARS_WF = """\
version: 1
name: reqvars
variables:
  target:
    required: true
steps:
  one:
    agent: mock-hello
    prompt: "hi {{ vars.target }}"
"""


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Tmp ZIGGY_HOME (user config + store) and a workspace with workflows."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(config_toml(), encoding="utf-8")
    monkeypatch.setenv("ZIGGY_HOME", str(home))
    workspace = tmp_path / "ws"
    wf_dir = workspace / ".ziggy" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "hello.yaml").write_text(HELLO_WF, encoding="utf-8")
    (wf_dir / "reqvars.yaml").write_text(REQVARS_WF, encoding="utf-8")
    return home, workspace


class StubConnection:
    """Native-typed stand-in for ziggy.acp.ServerConnection."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, ServerUpdate, str, str | None]] = []
        self.permission_calls: list[tuple[str, PermissionRequestN, str]] = []
        self.permission_reply: PermissionReply | Exception = PermissionReply(kind="cancelled")

    async def emit_update(
        self, session_id: str, ev: ServerUpdate, *, run_id: str, step_id: str | None
    ) -> None:
        self.updates.append((session_id, ev, run_id, step_id))

    async def forward_permission(
        self, session_id: str, req: PermissionRequestN, context_label: str
    ) -> PermissionReply:
        self.permission_calls.append((session_id, req, context_label))
        if isinstance(self.permission_reply, Exception):
            raise self.permission_reply
        return self.permission_reply

    def notices(self) -> list[str]:
        return [ev.text for (_, ev, _, _) in self.updates if isinstance(ev, ServerNotice)]

    def chunks(self) -> str:
        return "".join(
            ev.text
            for (_, ev, _, _) in self.updates
            if isinstance(ev, MessageChunkEvent) and not ev.thought
        )


@pytest.fixture
def server(env: tuple[Path, Path]) -> tuple[ZiggyServer, StubConnection, Path]:
    _, workspace = env
    resolved = load_config(workspace)
    srv = ZiggyServer(resolved)
    conn = StubConnection()
    srv.on_connect(conn)
    return srv, conn, workspace


async def open_session(srv: ZiggyServer, workspace: Path, route: str | None = None) -> str:
    opened = await srv.new_session(str(workspace))
    if route is not None:
        await srv.set_config_option(session_id=opened.session_id, config_id="route", value=route)
    return opened.session_id


def fake_result(status: RunStatus) -> RunResult:
    return RunResult(
        run_id="run-fake",
        kind=RunKind.AGENT,
        target="mock-hello",
        status=status,
        started_at="2026-07-28T00:00:00Z",
        workspace="/tmp/ws",
        capture_profile=CaptureProfile.STANDARD,
        persisted=False,
        steps={"main": StepResult(step_id="main", agent="mock-hello", status=StepStatus.SUCCESS)},
    )


# ----------------------------------------------------------------- protocol


class TestHandshakeAndSessions:
    async def test_initialize_returns_ziggy_identity(
        self, server: tuple[ZiggyServer, StubConnection, Path]
    ) -> None:
        srv, _, _ = server
        handshake = await srv.initialize(
            client_protocol_version=1,
            client_capabilities={"fs": {"read_text_file": True}},
            client_info={"name": "zed", "version": "1.0"},
        )
        assert handshake.agent_name == "ziggy"
        assert handshake.agent_version

    async def test_route_catalog_per_session(
        self, server: tuple[ZiggyServer, StubConnection, Path]
    ) -> None:
        srv, _, workspace = server
        opened = await srv.new_session(str(workspace))
        assert opened.route == DEFAULT_ROUTE == "orchestrator"
        assert opened.route_options == [
            "orchestrator",
            "agent:claude",
            "agent:codex",
            "agent:mock-broken",
            "agent:mock-hello",
            "agent:mock-perm",
            "workflow:hello",
            "workflow:reqvars",
        ]

    async def test_cwd_is_canonicalized(
        self, server: tuple[ZiggyServer, StubConnection, Path]
    ) -> None:
        srv, _, workspace = server
        dotted = workspace / "." / ".ziggy" / ".."
        opened = await srv.new_session(str(dotted))
        state = srv._sessions[opened.session_id]
        assert state.cwd == workspace.resolve()

    async def test_missing_cwd_rejected(
        self, server: tuple[ZiggyServer, StubConnection, Path]
    ) -> None:
        srv, _, workspace = server
        with pytest.raises(ValidationError):
            await srv.new_session(str(workspace / "nope"))

    async def test_file_cwd_rejected(
        self, server: tuple[ZiggyServer, StubConnection, Path]
    ) -> None:
        srv, _, workspace = server
        file_path = workspace / "plain.txt"
        file_path.write_text("x", encoding="utf-8")
        with pytest.raises(ValidationError):
            await srv.new_session(str(file_path))


class TestSetConfigOption:
    async def test_route_switch(self, server: tuple[ZiggyServer, StubConnection, Path]) -> None:
        srv, _, workspace = server
        session_id = await open_session(srv, workspace)
        state = await srv.set_config_option(
            session_id=session_id, config_id="route", value="agent:mock-hello"
        )
        assert state.route == "agent:mock-hello"
        assert "workflow:hello" in state.route_options

    async def test_unknown_route_value(
        self, server: tuple[ZiggyServer, StubConnection, Path]
    ) -> None:
        srv, _, workspace = server
        session_id = await open_session(srv, workspace)
        with pytest.raises(ValueError, match="unknown route value"):
            await srv.set_config_option(
                session_id=session_id, config_id="route", value="agent:evil"
            )

    async def test_unknown_config_id(
        self, server: tuple[ZiggyServer, StubConnection, Path]
    ) -> None:
        srv, _, workspace = server
        session_id = await open_session(srv, workspace)
        with pytest.raises(ValueError, match="unknown config option"):
            await srv.set_config_option(
                session_id=session_id, config_id="model", value="agent:mock-hello"
            )

    async def test_unknown_session(self, server: tuple[ZiggyServer, StubConnection, Path]) -> None:
        srv, _, _ = server
        with pytest.raises(ValueError, match="unknown session"):
            await srv.set_config_option(session_id="nope", config_id="route", value="orchestrator")


class TestPromptDispatch:
    async def test_orchestrator_route_without_planner_is_config_error(
        self, server: tuple[ZiggyServer, StubConnection, Path]
    ) -> None:
        """The default route now dispatches to the Phase-5 orchestrator: with
        no ``orchestrator.agent`` configured, the prepare-time gate raises a
        typed ConfigError before any subprocess and releases admission."""
        srv, _, workspace = server
        session_id = await open_session(srv, workspace)
        with pytest.raises(ConfigError, match=r"orchestrator\.agent"):
            await srv.prompt(session_id=session_id, text="do things")
        assert srv._active == {}

    async def test_unknown_session_prompt(
        self, server: tuple[ZiggyServer, StubConnection, Path]
    ) -> None:
        srv, _, _ = server
        with pytest.raises(ValidationError, match="unknown session"):
            await srv.prompt(session_id="nope", text="hi")

    async def test_workflow_with_required_variables_rejected(
        self, server: tuple[ZiggyServer, StubConnection, Path]
    ) -> None:
        srv, _, workspace = server
        session_id = await open_session(srv, workspace, route="workflow:reqvars")
        with pytest.raises(ValidationError, match="defaults") as exc_info:
            await srv.prompt(session_id=session_id, text="hi")
        assert exc_info.value.details["required_variables"] == ["target"]
        assert srv._active == {}

    async def test_authenticate_not_implemented(
        self, server: tuple[ZiggyServer, StubConnection, Path]
    ) -> None:
        srv, _, _ = server
        with pytest.raises(NotImplementedError):
            await srv.authenticate(method_id="api-key")


# ------------------------------------------------- admission / cancel / EOF


class TestAdmissionAndLifecycle:
    async def test_busy_admission_typed_error(
        self,
        server: tuple[ZiggyServer, StubConnection, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        srv, _, workspace = server
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_execute_run(spec: Any, **kwargs: Any) -> RunResult:
            started.set()
            await release.wait()
            return fake_result(RunStatus.SUCCESS)

        monkeypatch.setattr(app_module, "execute_run", fake_execute_run)
        s1 = await open_session(srv, workspace, route="agent:mock-hello")
        s2 = await open_session(srv, workspace, route="agent:mock-hello")
        task = asyncio.create_task(srv.prompt(session_id=s1, text="one"))
        await started.wait()
        with pytest.raises(ServerBusyError) as exc_info:
            await srv.prompt(session_id=s2, text="two")
        assert exc_info.value.details["max_active_runs"] == 1
        release.set()
        assert (await task).stop_reason == "end_turn"
        assert srv._active == {}

    async def test_cancel_sets_the_active_runs_cancel_event(
        self,
        server: tuple[ZiggyServer, StubConnection, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        srv, _, workspace = server
        started = asyncio.Event()

        async def fake_execute_run(
            spec: Any, *, cancel_event: asyncio.Event, **kwargs: Any
        ) -> RunResult:
            started.set()
            await cancel_event.wait()
            return fake_result(RunStatus.CANCELLED)

        monkeypatch.setattr(app_module, "execute_run", fake_execute_run)
        session_id = await open_session(srv, workspace, route="agent:mock-hello")
        task = asyncio.create_task(srv.prompt(session_id=session_id, text="go"))
        await started.wait()
        await srv.cancel(session_id="unknown-session")  # no-op
        await srv.cancel(session_id=session_id)
        await srv.cancel(session_id=session_id)  # idempotent
        assert (await task).stop_reason == "cancelled"
        assert srv._active == {}

    async def test_aclose_cancels_active_runs_and_clears_registry(
        self,
        server: tuple[ZiggyServer, StubConnection, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        srv, _, workspace = server
        started = asyncio.Event()

        async def fake_execute_run(
            spec: Any, *, cancel_event: asyncio.Event, **kwargs: Any
        ) -> RunResult:
            started.set()
            await cancel_event.wait()
            return fake_result(RunStatus.CANCELLED)

        monkeypatch.setattr(app_module, "execute_run", fake_execute_run)
        session_id = await open_session(srv, workspace, route="agent:mock-hello")
        task = asyncio.create_task(srv.prompt(session_id=session_id, text="go"))
        await started.wait()
        await srv.aclose()
        assert (await task).stop_reason == "cancelled"
        assert srv._active == {}


# -------------------------------------------- real engine paths (raw agent)


@pytest.mark.slow
class TestRealRuns:
    async def test_agent_route_streams_and_ends_turn(
        self, server: tuple[ZiggyServer, StubConnection, Path]
    ) -> None:
        srv, conn, workspace = server
        session_id = await open_session(srv, workspace, route="agent:mock-hello")
        stop = await srv.prompt(session_id=session_id, text="go")
        assert stop.stop_reason == "end_turn"
        assert conn.chunks() == HELLO_TEXT
        notices = conn.notices()
        assert "[step main] started (agent: mock-hello)" in notices
        assert "[step main] finished: success" in notices
        run_ids = {run_id for (_, _, run_id, _) in conn.updates}
        assert len(run_ids) == 1  # stable run correlation on every update
        session_ids = {sid for (sid, _, _, _) in conn.updates}
        assert session_ids == {session_id}

    async def test_failed_run_emits_error_summary_then_end_turn(
        self, server: tuple[ZiggyServer, StubConnection, Path]
    ) -> None:
        srv, conn, workspace = server
        session_id = await open_session(srv, workspace, route="agent:mock-broken")
        stop = await srv.prompt(session_id=session_id, text="go")
        assert stop.stop_reason == "end_turn"
        notices = conn.notices()
        assert notices, "expected re-emitted notices for the failed run"
        assert notices[-1].startswith("[error] run ")
        assert "AgentLaunchError" in notices[-1]
        assert srv._active == {}

    async def test_workflow_route_runs_to_end_turn(
        self, server: tuple[ZiggyServer, StubConnection, Path]
    ) -> None:
        srv, conn, workspace = server
        session_id = await open_session(srv, workspace, route="workflow:hello")
        stop = await srv.prompt(session_id=session_id, text="ignored in v0.1")
        assert stop.stop_reason == "end_turn"
        assert conn.chunks() == HELLO_TEXT
        notices = conn.notices()
        assert "[step greet] started (agent: mock-hello)" in notices
        assert "[step greet] finished: success" in notices

    async def test_policy_deny_short_circuits_without_forwarding(
        self, env: tuple[Path, Path], server: tuple[ZiggyServer, StubConnection, Path]
    ) -> None:
        """The permission scenario asks to execute with no matchable command:
        the guarded ceiling denies locally — the client is NEVER asked — and
        the persisted decision records client_response=None."""
        home, _ = env
        srv, conn, workspace = server
        session_id = await open_session(srv, workspace, route="agent:mock-perm")
        stop = await srv.prompt(session_id=session_id, text="go")
        assert stop.stop_reason == "end_turn"
        assert conn.permission_calls == []
        assert f"[policy] {RULE_TERMINAL_DEFAULT_DENY}: denied" in conn.notices()
        assert scenarios.PERMISSION_DENIED_TEXT in conn.chunks()

        run_id = next(run_id for (_, _, run_id, _) in conn.updates)
        data = RunStore(home).read_result(run_id)
        decisions = data["steps"]["main"]["permission_decisions"]
        assert len(decisions) == 1
        assert decisions[0]["decision"] == "denied"
        assert decisions[0]["rule_id"] == RULE_TERMINAL_DEFAULT_DENY
        assert decisions[0]["client_response"] is None


# ------------------------------------------------- permission bridge matrix


def perm_request(tool_call: dict[str, Any]) -> PermissionRequestN:
    return PermissionRequestN(
        session_id="down-sess",
        tool_call=tool_call,
        options=[
            PermissionOptionN(option_id="ok", name="Allow once", kind="allow_once"),
            PermissionOptionN(option_id="no", name="Reject once", kind="reject_once"),
        ],
    )


class TestPermissionBridge:
    @pytest.fixture
    def bridge_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[_RunBridge, StubConnection, SessionState, Path]:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("ZIGGY_HOME", str(home))
        workspace = tmp_path / "bw"
        workspace.mkdir()
        session = SessionState(
            session_id="sess-b",
            cwd=workspace,
            resolved=load_config(None),
            route="agent:mock-hello",
            route_options=["orchestrator", "agent:mock-hello"],
        )
        conn = StubConnection()
        policy = MediationPolicy.guarded(workspace=workspace, step_dir=workspace)
        bridge = _RunBridge(
            session=session,
            connection=conn,
            step_policies={"main": policy},
            step_agents={"main": "mock-hello"},
            target="mock-hello",
        )
        bridge.current_step = "main"
        bridge.run_id = "run-b"
        return bridge, conn, session, workspace

    def read_request(self, workspace: Path) -> PermissionRequestN:
        return perm_request(
            {
                "tool_call_id": "tc-1",
                "title": "Read a.txt",
                "kind": "read",
                "locations": [str(workspace / "a.txt")],
            }
        )

    async def test_policy_deny_never_forwards(
        self, bridge_env: tuple[_RunBridge, StubConnection, SessionState, Path]
    ) -> None:
        bridge, conn, _, _ = bridge_env
        req = perm_request({"tool_call_id": "tc-2", "kind": "read", "locations": ["/etc/passwd"]})
        reply, payload = await bridge.decide_permission(req)
        assert conn.permission_calls == []
        assert reply == PermissionReply(kind="selected", option_id="no")
        assert payload["decision"] == "denied"
        assert payload["rule_id"] == RULE_OUTSIDE_WORKSPACE_DENY
        assert payload["client_response"] is None

    async def test_policy_allow_client_approve(
        self, bridge_env: tuple[_RunBridge, StubConnection, SessionState, Path]
    ) -> None:
        bridge, conn, _, workspace = bridge_env
        conn.permission_reply = PermissionReply(kind="selected", option_id="ok")
        reply, payload = await bridge.decide_permission(self.read_request(workspace))
        assert reply == PermissionReply(kind="selected", option_id="ok")
        assert payload["decision"] == "approved"
        assert payload["client_response"] == "approved"
        assert payload["rule_id"] == RULE_READ_IN_WORKSPACE_ALLOW
        (sess_id, _, label) = conn.permission_calls[0]
        assert sess_id == "sess-b"
        assert label == "agent:mock-hello step:main"

    async def test_policy_allow_client_deny(
        self, bridge_env: tuple[_RunBridge, StubConnection, SessionState, Path]
    ) -> None:
        bridge, conn, _, workspace = bridge_env
        conn.permission_reply = PermissionReply(kind="selected", option_id="no")
        reply, payload = await bridge.decide_permission(self.read_request(workspace))
        assert reply == PermissionReply(kind="selected", option_id="no")
        assert payload["decision"] == "denied"
        assert payload["client_response"] == "denied"

    async def test_client_cancelled_outcome_is_denied(
        self, bridge_env: tuple[_RunBridge, StubConnection, SessionState, Path]
    ) -> None:
        bridge, conn, _, workspace = bridge_env
        conn.permission_reply = PermissionReply(kind="cancelled")
        reply, payload = await bridge.decide_permission(self.read_request(workspace))
        assert reply.kind == "cancelled"
        assert payload["decision"] == "denied"
        assert payload["client_response"] == "denied"

    async def test_client_unknown_option_fails_toward_denial(
        self, bridge_env: tuple[_RunBridge, StubConnection, SessionState, Path]
    ) -> None:
        bridge, conn, _, workspace = bridge_env
        conn.permission_reply = PermissionReply(kind="selected", option_id="never-offered")
        reply, payload = await bridge.decide_permission(self.read_request(workspace))
        assert reply == PermissionReply(kind="selected", option_id="no")
        assert payload["decision"] == "denied"
        assert payload["client_response"] == "denied"

    async def test_unsupported_client_falls_back_with_one_notice(
        self, bridge_env: tuple[_RunBridge, StubConnection, SessionState, Path]
    ) -> None:
        bridge, conn, session, workspace = bridge_env
        conn.permission_reply = ClientPermissionUnsupported()
        reply, payload = await bridge.decide_permission(self.read_request(workspace))
        assert session.no_forwarding is True
        assert reply == PermissionReply(kind="selected", option_id="ok")
        assert payload["decision"] == "approved"
        assert payload["client_response"] is None
        assert payload["policy_source"].endswith(GUARDED_FALLBACK_SUFFIX)
        fallback_notices = [t for t in conn.notices() if "guarded local mediation" in t]
        assert len(fallback_notices) == 1
        # Subsequent requests: resolved locally, no new forward, no new notice.
        forwarded = len(conn.permission_calls)
        reply2, payload2 = await bridge.decide_permission(self.read_request(workspace))
        assert len(conn.permission_calls) == forwarded
        assert reply2 == PermissionReply(kind="selected", option_id="ok")
        assert payload2["policy_source"].endswith(GUARDED_FALLBACK_SUFFIX)
        assert payload2["client_response"] is None
        assert len([t for t in conn.notices() if "guarded local mediation" in t]) == 1

    async def test_forward_protocol_error_fails_toward_denial(
        self, bridge_env: tuple[_RunBridge, StubConnection, SessionState, Path]
    ) -> None:
        bridge, conn, session, workspace = bridge_env
        conn.permission_reply = ProtocolError("client connection lost")
        reply, payload = await bridge.decide_permission(self.read_request(workspace))
        assert reply == PermissionReply(kind="selected", option_id="no")
        assert payload["decision"] == "denied"
        assert payload["rule_id"] == RULE_FORWARD_FAILED
        assert payload["client_response"] is None
        assert session.no_forwarding is False  # broken forward != unsupported


# --------------------------------------------------- re-emission normalizer


def envelope(
    event_type: str, payload: dict[str, Any], step_id: str | None = "main"
) -> EventEnvelope:
    return EventEnvelope(
        seq=0,
        ts="2026-07-28T00:00:00Z",
        monotonic_offset_ms=0,
        run_id="run-1",
        step_id=step_id,
        event_type=event_type,
        payload=payload,
    )


class TestUpdateNormalization:
    def test_message_and_thought_chunks(self) -> None:
        ev = _update_for(envelope("message_chunk", {"role": "agent", "text": "hi"}))
        assert ev == MessageChunkEvent(role="agent", text="hi", thought=False)
        ev = _update_for(envelope("thought_chunk", {"role": "weird", "text": "th"}))
        assert ev == MessageChunkEvent(role="agent", text="th", thought=True)

    def test_tool_call_passthrough_and_truncated_payload(self) -> None:
        ev = _update_for(
            envelope(
                "tool_call",
                {
                    "tool_call_id": "tc-1",
                    "title": "Read",
                    "kind": "read",
                    "status": "pending",
                    "locations": ["/tmp/a"],
                    "has_content": False,
                },
            )
        )
        assert isinstance(ev, ToolCallEvent)
        assert ev.phase == "start"
        assert ev.locations == ["/tmp/a"]
        # Metadata-only continuation after truncation: no tool_call_id -> skip.
        assert _update_for(envelope("tool_call", {"truncated": True})) is None
        update = _update_for(envelope("tool_call_update", {"tool_call_id": "tc-1"}))
        assert isinstance(update, ToolCallEvent)
        assert update.phase == "update"

    def test_plan_and_usage(self) -> None:
        plan = _update_for(
            envelope("plan", {"entries": [{"content": "x", "priority": "high"}, "junk"]})
        )
        assert plan == PlanEvent(entries=[{"content": "x", "priority": "high"}])
        usage = _update_for(
            envelope("usage", {"used": 5, "size": 10, "cost": 0.1, "currency": "USD"})
        )
        assert isinstance(usage, UsageEvent)
        assert (usage.used, usage.size, usage.cost, usage.currency) == (5, 10, 0.1, "USD")

    def test_notice_one_liners(self) -> None:
        cases = {
            "permission_decided": (
                {"rule_id": "rule-x", "decision": "denied"},
                "[policy] rule-x: denied",
            ),
            "step_started": ({"agent": "mock"}, "[step main] started (agent: mock)"),
            "step_finished": ({"status": "success"}, "[step main] finished: success"),
            "egress_notice": (
                {"provider_set": ["a", "b"], "acknowledged_by": "config"},
                "[egress] providers: a, b (acknowledged_by: config)",
            ),
            "truncation": (
                {},
                "[truncation] step main: event stream truncated (capture degraded)",
            ),
            "error": (
                {"code": "StepTimeoutError", "message": "boom"},
                "[error] StepTimeoutError: boom",
            ),
        }
        for event_type, (payload, expected) in cases.items():
            ev = _update_for(envelope(event_type, payload))
            assert ev == ServerNotice(text=expected), event_type

    def test_internal_events_are_not_re_emitted(self) -> None:
        for event_type in (
            "run_started",
            "config_resolved",
            "policy_resolved",
            "agent_launching",
            "agent_launched",
            "handshake",
            "session_created",
            "prompt_started",
            "permission_requested",
            "fs_read",
            "fs_write",
            "terminal_op",
            "mode",
            "unknown_update",
            "cancel_requested",
            "terminated",
            "lease_acquired",
            "lease_released",
            "run_finished",
        ):
            assert _update_for(envelope(event_type, {"anything": 1})) is None, event_type


class TestProtocolConformance:
    def test_ziggy_server_implements_the_native_protocol(self) -> None:
        for name in (
            "initialize",
            "new_session",
            "set_config_option",
            "prompt",
            "cancel",
            "authenticate",
        ):
            assert callable(getattr(ZiggyServer, name)), name
        assert callable(ZiggyServer.on_connect)
        assert callable(ZiggyServer.aclose)
