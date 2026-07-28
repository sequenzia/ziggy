"""Loopback integration for ``ziggy serve`` (REQ-012; spec §9.5 Phase-4 gate,
§10.2 "ACP permission bridge"; phase45-contracts "### Tests: loopback fixture").

A real ``python -m ziggy serve`` subprocess is driven over stdio by the
SDK-free raw NDJSON client in ``tests/mocks/raw_client.py``, with the raw mock
agent registered as trusted agents in a tmp ``ZIGGY_HOME``. Every scenario the
Phase-4 checkpoint gate names runs against the real wire:

- direct-agent and named-workflow routes stream correlated updates and persist
  ordinary RunResults;
- a client approval can never exceed the trusted policy ceiling (the client is
  not even asked when policy denies);
- unsupported client permission forwarding degrades to a visible guarded
  fallback;
- cancellation tears down the complete downstream process tree;
- concurrent prompts get a typed busy error without corrupting state;
- client disconnect (stdin EOF) cancels, persists, releases the lease, and
  exits the server;
- server stdout carries ONLY JSON-RPC frames.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.mocks import RAW_AGENT_PATH, scenarios  # noqa: E402
from tests.mocks.raw_client import (  # noqa: E402
    ServeClient,
    approve_permission,
    deny_permission,
    initialize,
    new_session,
    open_routed_session,
    prompt_params,
    set_route,
    spawn_server,
    unsupported_permission,
)

pytestmark = pytest.mark.slow

PROMPT_TIMEOUT = 60.0
HELLO_CHUNKS = list(scenarios.HELLO_CHUNKS)


# ------------------------------------------------------------------ fixtures


def _agent_toml(name: str, scenario: str) -> str:
    return (
        f"[agents.{name}]\n"
        f"command = {json.dumps(sys.executable)}\n"
        f"args = [{json.dumps(str(RAW_AGENT_PATH))}, {json.dumps(scenario)}]\n"
        'provider = "mock"\n'
    )


CONFIG_TOML = (
    "schema_version = 1\n"
    "[engine]\n"
    "default_step_timeout_seconds = 30\n"
    "cancel_grace_seconds = 1.0\n"
    + _agent_toml("mock-hello", scenarios.HELLO)
    + _agent_toml("mock-slow", scenarios.SLOW_STREAM)
    + _agent_toml("mock-perm-read", scenarios.PERMISSION_READ)
    + _agent_toml("mock-perm-outside", scenarios.PERMISSION_OUTSIDE)
    + _agent_toml("mock-stubborn", scenarios.IGNORE_CANCEL)
)

PAIR_WF = """\
version: 1
name: pair
steps:
  first:
    agent: mock-hello
    prompt: say hello
  second:
    agent: mock-hello
    prompt: say hello again
    depends_on: [first]
"""

SLOW_WF = """\
version: 1
name: slow
steps:
  crawl:
    agent: mock-slow
    prompt: go slowly
"""


@pytest.fixture
def env(tmp_path: Path) -> tuple[Path, Path]:
    """Tmp ZIGGY_HOME (config + store) and a workspace with workflows."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(CONFIG_TOML, encoding="utf-8")
    workspace = tmp_path / "ws"
    wf_dir = workspace / ".ziggy" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "pair.yaml").write_text(PAIR_WF, encoding="utf-8")
    (wf_dir / "slow.yaml").write_text(SLOW_WF, encoding="utf-8")
    return home, workspace


def read_result(home: Path, run_id: str) -> dict[str, Any]:
    path = home / "runs" / run_id / "result.json"
    assert path.is_file(), f"result.json missing for run {run_id}"
    return json.loads(path.read_text(encoding="utf-8"))


def only_run_id(client: ServeClient, session_id: str) -> str:
    run_ids = client.run_ids(session_id)
    assert len(run_ids) == 1, f"expected one correlated run, saw {run_ids}"
    return run_ids[0]


def route_option(new_session_result: dict[str, Any]) -> dict[str, Any]:
    options = new_session_result.get("configOptions") or []
    routes = [o for o in options if o.get("id") == "route"]
    assert len(routes) == 1, f"expected exactly one route config option: {options}"
    return routes[0]


# ---------------------------------------------------------------- handshake


class TestHandshake:
    def test_raw_client_is_sdk_free(self) -> None:
        """The loopback client's independence from the acp SDK is the point."""
        from tests.mocks import raw_client

        pattern = re.compile(r"^\s*(import acp\b|from acp\b)", re.MULTILINE)
        source = Path(raw_client.__file__).read_text(encoding="utf-8")
        assert not pattern.search(source)

    async def test_initialize_session_new_and_stdout_purity(self, env: tuple[Path, Path]) -> None:
        home, workspace = env
        async with spawn_server(home, workspace) as client:
            init = await initialize(client)
            assert init["protocolVersion"] == 1
            assert init["agentInfo"]["name"] == "ziggy"
            assert init["agentInfo"]["version"]
            assert init["agentCapabilities"]["loadSession"] is False

            session_id, opened = await new_session(client, workspace)
            option = route_option(opened)
            assert option["type"] == "select"
            assert option["currentValue"] == "orchestrator"
            values = [o["value"] for o in option["options"]]
            assert "agent:mock-hello" in values
            assert "workflow:pair" in values

            switched = await set_route(client, session_id, "agent:mock-hello")
            assert route_option(switched)["currentValue"] == "agent:mock-hello"

            # Unknown route value -> JSON-RPC invalid_params, not a crash.
            frame = await client.request_raw(
                "session/set_config_option",
                {"sessionId": session_id, "configId": "route", "value": "agent:evil"},
            )
            assert frame["error"]["code"] == -32602

            client.assert_stdout_jsonrpc()


# ------------------------------------------------------------------- routes


class TestRoutes:
    async def test_direct_agent_route_streams_and_persists(self, env: tuple[Path, Path]) -> None:
        home, workspace = env
        async with spawn_server(home, workspace) as client:
            await initialize(client)
            session_id = await open_routed_session(client, workspace, "agent:mock-hello")
            result = await client.request(
                "session/prompt", prompt_params(session_id), timeout=PROMPT_TIMEOUT
            )
            assert result["stopReason"] == "end_turn"

            # Streamed chunks arrive verbatim, with stable run/step correlation.
            assert client.agent_texts(session_id) == HELLO_CHUNKS
            run_id = only_run_id(client, session_id)
            for meta in client.metas(session_id):
                assert meta["run_id"] == run_id
                assert meta["step_id"] == "main"
            texts = client.chunk_texts(session_id)
            assert "[step main] started (agent: mock-hello)" in texts
            assert "[step main] finished: success" in texts

            # Identical RunResult contract as CLI runs (kind=agent).
            data = read_result(home, run_id)
            assert data["kind"] == "agent"
            assert data["target"] == "mock-hello"
            assert data["status"] == "success"
            assert data["persisted"] is True
            assert data["policy"]["policy_name"] == "guarded"
            assert data["steps"]["main"]["status"] == "success"
            assert data["steps"]["main"]["outputs"]["text"] == "".join(HELLO_CHUNKS)
            client.assert_stdout_jsonrpc()

    async def test_workflow_route_streams_both_steps_and_persists(
        self, env: tuple[Path, Path]
    ) -> None:
        home, workspace = env
        async with spawn_server(home, workspace) as client:
            await initialize(client)
            session_id = await open_routed_session(client, workspace, "workflow:pair")
            result = await client.request(
                "session/prompt",
                prompt_params(session_id, "prompt text is ignored for workflows"),
                timeout=PROMPT_TIMEOUT,
            )
            assert result["stopReason"] == "end_turn"

            run_id = only_run_id(client, session_id)
            step_ids = {meta["step_id"] for meta in client.metas(session_id)}
            assert {"first", "second"} <= step_ids
            # Both steps streamed their chunks (serial, distinct step ids).
            assert client.agent_texts(session_id) == HELLO_CHUNKS + HELLO_CHUNKS

            data = read_result(home, run_id)
            assert data["kind"] == "workflow"
            assert data["target"] == "pair"
            assert data["status"] == "success"
            assert data["steps"]["first"]["status"] == "success"
            assert data["steps"]["second"]["status"] == "success"
            client.assert_stdout_jsonrpc()


# -------------------------------------------------------- permission bridge


class TestPermissionBridge:
    async def test_forwarded_request_client_approves_then_denies(
        self, env: tuple[Path, Path]
    ) -> None:
        home, workspace = env
        async with spawn_server(home, workspace) as client:
            await initialize(client)

            # Session A: the client approves the forwarded request.
            client.permission_script = approve_permission
            sess_a = await open_routed_session(client, workspace, "agent:mock-perm-read")
            result = await client.request(
                "session/prompt", prompt_params(sess_a), timeout=PROMPT_TIMEOUT
            )
            assert result["stopReason"] == "end_turn"
            assert len(client.permission_requests) == 1
            forwarded = client.permission_requests[0]
            assert forwarded["sessionId"] == sess_a
            assert forwarded["toolCall"]["title"] == (
                f"[agent:mock-perm-read step:main] {scenarios.PERMISSION_READ_TOOL_TITLE}"
            )
            assert scenarios.PERMISSION_READ_APPROVED_TEXT in client.agent_texts(sess_a)
            decisions = read_result(home, only_run_id(client, sess_a))["steps"]["main"][
                "permission_decisions"
            ]
            assert len(decisions) == 1
            assert decisions[0]["decision"] == "approved"
            assert decisions[0]["client_response"] == "approved"
            assert decisions[0]["rule_id"] == "read-in-workspace-allow"

            # Session B: the client denies -> effective decision is denied.
            client.permission_script = deny_permission
            sess_b = await open_routed_session(client, workspace, "agent:mock-perm-read")
            result = await client.request(
                "session/prompt", prompt_params(sess_b), timeout=PROMPT_TIMEOUT
            )
            assert result["stopReason"] == "end_turn"
            assert len(client.permission_requests) == 2
            assert client.permission_requests[1]["sessionId"] == sess_b
            assert scenarios.PERMISSION_READ_DENIED_TEXT in client.agent_texts(sess_b)
            decisions = read_result(home, only_run_id(client, sess_b))["steps"]["main"][
                "permission_decisions"
            ]
            assert len(decisions) == 1
            assert decisions[0]["decision"] == "denied"
            assert decisions[0]["client_response"] == "denied"
            client.assert_stdout_jsonrpc()

    async def test_client_approval_cannot_exceed_policy_ceiling(
        self, env: tuple[Path, Path]
    ) -> None:
        """Phase-4 gate: the policy ceiling denies an edit of /etc/hosts; the
        eagerly-approving client is NEVER asked (policy-deny short-circuit)."""
        home, workspace = env
        async with spawn_server(home, workspace) as client:
            await initialize(client)
            client.permission_script = approve_permission  # scripted to approve
            session_id = await open_routed_session(client, workspace, "agent:mock-perm-outside")
            result = await client.request(
                "session/prompt", prompt_params(session_id), timeout=PROMPT_TIMEOUT
            )
            assert result["stopReason"] == "end_turn"

            # No session/request_permission frame ever reached the client.
            assert client.permission_requests == []
            assert scenarios.PERMISSION_OUTSIDE_DENIED_TEXT in client.agent_texts(session_id)
            assert "[policy] outside-workspace-deny: denied" in client.chunk_texts(session_id)

            decisions = read_result(home, only_run_id(client, session_id))["steps"]["main"][
                "permission_decisions"
            ]
            assert len(decisions) == 1
            assert decisions[0]["decision"] == "denied"
            assert decisions[0]["rule_id"] == "outside-workspace-deny"
            assert decisions[0]["client_response"] is None
            client.assert_stdout_jsonrpc()

    async def test_unsupported_forwarding_uses_visible_guarded_fallback(
        self, env: tuple[Path, Path]
    ) -> None:
        """Phase-4 gate: a client answering -32601 flips the session to guarded
        local mediation with ONE visible notice; later requests in the same
        session are not forwarded at all."""
        home, workspace = env
        async with spawn_server(home, workspace) as client:
            await initialize(client)
            client.permission_script = unsupported_permission
            session_id = await open_routed_session(client, workspace, "agent:mock-perm-read")

            result = await client.request(
                "session/prompt", prompt_params(session_id), timeout=PROMPT_TIMEOUT
            )
            assert result["stopReason"] == "end_turn"
            assert len(client.permission_requests) == 1  # the failed forward
            fallback_notices = [
                t for t in client.chunk_texts(session_id) if "guarded local mediation" in t
            ]
            assert len(fallback_notices) == 1  # visible fallback advertisement
            assert scenarios.PERMISSION_READ_APPROVED_TEXT in client.agent_texts(session_id)

            # Second prompt, same session: resolved locally, no new forward,
            # no second notice.
            result = await client.request(
                "session/prompt", prompt_params(session_id), timeout=PROMPT_TIMEOUT
            )
            assert result["stopReason"] == "end_turn"
            assert len(client.permission_requests) == 1
            fallback_notices = [
                t for t in client.chunk_texts(session_id) if "guarded local mediation" in t
            ]
            assert len(fallback_notices) == 1

            run_ids = client.run_ids(session_id)
            assert len(run_ids) == 2
            for run_id in run_ids:
                decisions = read_result(home, run_id)["steps"]["main"]["permission_decisions"]
                assert len(decisions) == 1
                assert decisions[0]["decision"] == "approved"
                assert decisions[0]["client_response"] is None
                assert decisions[0]["policy_source"].endswith("(guarded-fallback)")
            client.assert_stdout_jsonrpc()


# ------------------------------------------------- busy / cancel / teardown


class TestBusyAndLifecycle:
    async def test_concurrent_prompt_gets_typed_busy_then_recovers(
        self, env: tuple[Path, Path]
    ) -> None:
        """Phase-4 gate: excess concurrent prompts get the typed
        ServerBusyError shape (no queueing) and the server recovers cleanly."""
        home, workspace = env
        async with spawn_server(home, workspace) as client:
            await initialize(client)
            sess_slow = await open_routed_session(client, workspace, "agent:mock-slow")
            sess_hello = await open_routed_session(client, workspace, "agent:mock-hello")

            prompt_slow = asyncio.create_task(
                client.request("session/prompt", prompt_params(sess_slow), timeout=PROMPT_TIMEOUT)
            )
            await client.wait_for_update(
                lambda p: (
                    p.get("sessionId") == sess_slow
                    and scenarios.SLOW_STREAM_TICK_PREFIX
                    in str((p.get("update") or {}).get("content", {}).get("text", ""))
                )
            )

            frame = await client.request_raw("session/prompt", prompt_params(sess_hello))
            error = frame["error"]
            assert error["code"] == -32603
            assert error["data"]["code"] == "ServerBusyError"
            assert error["data"]["details"]["max_active_runs"] == 1

            client.notify("session/cancel", {"sessionId": sess_slow})
            result = await prompt_slow
            assert result["stopReason"] == "cancelled"

            # The slot is free again: the next prompt succeeds end-to-end.
            result = await client.request(
                "session/prompt", prompt_params(sess_hello), timeout=PROMPT_TIMEOUT
            )
            assert result["stopReason"] == "end_turn"
            assert client.agent_texts(sess_hello) == HELLO_CHUNKS

            # Both runs persisted without corruption.
            cancelled = read_result(home, only_run_id(client, sess_slow))
            assert cancelled["status"] == "cancelled"
            assert cancelled["kind"] == "agent"
            success = read_result(home, only_run_id(client, sess_hello))
            assert success["status"] == "success"
            client.assert_stdout_jsonrpc()

    async def test_cancel_mid_run_tears_down_downstream_tree(self, env: tuple[Path, Path]) -> None:
        """Phase-4 gate: session/cancel against an agent that ignores ACP
        cancellation still reaps the agent's own child process."""
        home, workspace = env
        async with spawn_server(home, workspace) as client:
            await initialize(client)
            session_id = await open_routed_session(client, workspace, "agent:mock-stubborn")
            prompt_task = asyncio.create_task(
                client.request("session/prompt", prompt_params(session_id), timeout=PROMPT_TIMEOUT)
            )
            announced = await client.wait_for_update(
                lambda p: (
                    p.get("sessionId") == session_id
                    and str((p.get("update") or {}).get("content", {}).get("text", "")).startswith(
                        scenarios.CHILD_PID_PREFIX
                    )
                )
            )
            text = announced["update"]["content"]["text"]
            child_pid = int(text[len(scenarios.CHILD_PID_PREFIX) :])

            client.notify("session/cancel", {"sessionId": session_id})
            result = await prompt_task
            assert result["stopReason"] == "cancelled"

            deadline = time.monotonic() + 10.0
            while True:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                assert time.monotonic() < deadline, (
                    f"downstream child {child_pid} survived cancellation teardown"
                )
                await asyncio.sleep(0.05)

            data = read_result(home, only_run_id(client, session_id))
            assert data["status"] == "cancelled"
            client.assert_stdout_jsonrpc()

    async def test_disconnect_mid_run_cancels_persists_and_exits(
        self, env: tuple[Path, Path]
    ) -> None:
        """Client stdio EOF mid-run: bounded teardown, run persisted as
        cancelled, workspace lease released, server process gone."""
        home, workspace = env
        async with spawn_server(home, workspace) as client:
            await initialize(client)
            session_id = await open_routed_session(client, workspace, "workflow:slow")
            prompt_task = asyncio.create_task(
                client.request_raw(
                    "session/prompt", prompt_params(session_id), timeout=PROMPT_TIMEOUT
                )
            )
            await client.wait_for_update(
                lambda p: (
                    p.get("sessionId") == session_id
                    and scenarios.SLOW_STREAM_TICK_PREFIX
                    in str((p.get("update") or {}).get("content", {}).get("text", ""))
                )
            )
            run_id = only_run_id(client, session_id)

            client.close_stdin()
            returncode = await client.wait_exit(timeout=20.0)
            assert returncode == 0, client.stderr_text()

            # The prompt never resolved: its response died with the connection.
            with pytest.raises((EOFError, asyncio.CancelledError, TimeoutError)):
                await prompt_task

            # Bounded post-exit settle: result.json is written before the
            # engine task completes, but poll defensively anyway.
            deadline = time.monotonic() + 10.0
            while True:
                path = home / "runs" / run_id / "result.json"
                if path.is_file():
                    break
                assert time.monotonic() < deadline, "run never persisted after disconnect"
                await asyncio.sleep(0.05)
            data = read_result(home, run_id)
            assert data["status"] == "cancelled"
            assert data["kind"] == "workflow"
            assert data["steps"]["crawl"]["status"] == "cancelled"

            # Lease released (the workflow run held it while active).
            leases = list((home / "leases").glob("*.json")) if (home / "leases").is_dir() else []
            assert leases == []
            client.assert_stdout_jsonrpc()
