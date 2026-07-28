#!/usr/bin/env python3
"""Raw JSON-RPC 2.0 mock ACP agent — deliberately SDK-free.

Run as ``[sys.executable, path_to_this_file, scenario_name]``. Speaks
newline-delimited JSON-RPC 2.0 over real stdin/stdout with a hand-rolled
loop so wire-conformance tests validate Ziggy against the ACP wire contract
itself, independent of the production ``acp`` SDK (spec §10.1). The scenario
argument (see ``scenarios.py``) controls behavior during the prompt turn.

Wire behavior implemented:
- ``initialize`` / ``session/new`` / ``session/prompt`` requests (camelCase
  payload keys, protocol version 1, ``agentInfo`` identity).
- ``session/cancel`` accepted as a notification (also tolerated as a
  request); scenarios decide whether to honor it.
- Agent-originated requests (``session/request_permission``, ``fs/*``) use
  ids from a high range so they never collide with client request ids.
- Unknown methods are answered with JSON-RPC error ``-32601``.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from typing import Any

try:
    import scenarios  # script mode: sys.path[0] is this file's directory
except ImportError:  # imported as a package module by tests
    from tests.mocks import scenarios

JSONRPC = "2.0"


class MockAgent:
    """Scenario-driven ACP agent speaking raw newline-delimited JSON-RPC."""

    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self._next_request_id = scenarios.AGENT_REQUEST_ID_START
        self._pending: dict[Any, asyncio.Future[dict[str, Any]]] = {}
        self._session_cwd: dict[str, str] = {}
        self._session_prompt: dict[str, str] = {}
        self._session_counter = 0
        self._cancelled = asyncio.Event()
        self._prompt_tasks: set[asyncio.Task[None]] = set()
        self._handlers: dict[str, Callable[[str], Awaitable[str | None]]] = {
            scenarios.HELLO: self._scenario_hello,
            scenarios.TOOL_CALLS: self._scenario_tool_calls,
            scenarios.PERMISSION: self._scenario_permission,
            scenarios.PERMISSION_READ: self._scenario_permission_read,
            scenarios.PERMISSION_OUTSIDE: self._scenario_permission_outside,
            scenarios.FS_OPS: self._scenario_fs_ops,
            scenarios.MALFORMED: self._scenario_malformed,
            scenarios.CRASH_MID_TURN: self._scenario_crash_mid_turn,
            scenarios.IGNORE_CANCEL: self._scenario_ignore_cancel,
            scenarios.SLOW_STREAM: self._scenario_slow_stream,
            scenarios.SECRET_LEAK: self._scenario_secret_leak,
            scenarios.ENV_ECHO: self._scenario_env_echo,
            scenarios.ECHO_PROMPT: self._scenario_echo_prompt,
        }

    # --- wire helpers ----------------------------------------------------

    @staticmethod
    def _send_raw(text: str) -> None:
        """Write one already-framed line; survive the client vanishing.

        Swallowing write errors matters for ``ignore_cancel``: that scenario
        must stay alive until it is killed by signal, even if the harness
        closes the pipes first.
        """
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except OSError:
            pass

    def _send(self, frame: dict[str, Any]) -> None:
        self._send_raw(json.dumps(frame, separators=(",", ":")) + "\n")

    def _send_result(self, request_id: Any, result: Any) -> None:
        self._send({"jsonrpc": JSONRPC, "id": request_id, "result": result})

    def _send_error(self, request_id: Any, code: int, message: str) -> None:
        error = {"code": code, "message": message}
        self._send({"jsonrpc": JSONRPC, "id": request_id, "error": error})

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send an agent->client request; return the raw response frame."""
        request_id = self._next_request_id
        self._next_request_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._send({"jsonrpc": JSONRPC, "id": request_id, "method": method, "params": params})
        return await future

    def _session_update(self, session_id: str, update: dict[str, Any]) -> None:
        self._send(
            {
                "jsonrpc": JSONRPC,
                "method": "session/update",
                "params": {"sessionId": session_id, "update": update},
            }
        )

    def _chunk(self, session_id: str, text: str) -> None:
        self._session_update(
            session_id,
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}},
        )

    # --- dispatch --------------------------------------------------------

    async def dispatch(self, frame: dict[str, Any]) -> None:
        if "method" not in frame:
            self._resolve_response(frame)
            return
        method = frame["method"]
        params = frame.get("params") or {}
        request_id = frame.get("id")
        if method == "session/cancel":
            self._cancelled.set()
            if request_id is not None:
                self._send_result(request_id, None)
            return
        if request_id is None:
            return  # unknown notification: ignore
        if method == "initialize":
            self._send_result(request_id, self._initialize_result())
        elif method == "session/new":
            self._session_counter += 1
            session_id = f"mock-sess-{self._session_counter}"
            self._session_cwd[session_id] = str(params.get("cwd", ""))
            self._send_result(request_id, {"sessionId": session_id})
        elif method == "session/prompt":
            task = asyncio.create_task(self._run_prompt(request_id, params))
            self._prompt_tasks.add(task)
            task.add_done_callback(self._prompt_tasks.discard)
        else:
            self._send_error(request_id, -32601, f"Method not found: {method}")

    def _resolve_response(self, frame: dict[str, Any]) -> None:
        future = self._pending.pop(frame.get("id"), None)
        if future is not None and not future.done():
            future.set_result(frame)

    def _initialize_result(self) -> dict[str, Any]:
        return {
            "protocolVersion": scenarios.PROTOCOL_VERSION,
            "agentCapabilities": {"loadSession": False},
            "authMethods": [],
            "agentInfo": {
                "name": scenarios.RAW_AGENT_NAME,
                "title": scenarios.RAW_AGENT_TITLE,
                "version": scenarios.AGENT_VERSION,
            },
        }

    @staticmethod
    def _prompt_text(params: dict[str, Any]) -> str:
        """Concatenated text of the prompt's content blocks (wire shape)."""
        blocks = params.get("prompt")
        if not isinstance(blocks, list):
            return ""
        return "".join(
            str(block.get("text", ""))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )

    async def _run_prompt(self, request_id: Any, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId", ""))
        self._session_prompt[session_id] = self._prompt_text(params)
        stop_reason = await self._handlers[self.scenario](session_id)
        if stop_reason is not None:
            self._send_result(request_id, {"stopReason": stop_reason})

    # --- scenarios -------------------------------------------------------

    async def _scenario_hello(self, session_id: str) -> str | None:
        for text in scenarios.HELLO_CHUNKS:
            self._chunk(session_id, text)
        return "end_turn"

    async def _scenario_tool_calls(self, session_id: str) -> str | None:
        self._chunk(session_id, scenarios.TOOL_CALLS_INTRO)
        self._session_update(
            session_id,
            {
                "sessionUpdate": "tool_call",
                "toolCallId": scenarios.EXEC_TOOL_CALL_ID,
                "title": scenarios.EXEC_TOOL_TITLE,
                "kind": "execute",
                "status": "in_progress",
            },
        )
        self._session_update(
            session_id,
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": scenarios.EXEC_TOOL_CALL_ID,
                "status": "completed",
                "rawOutput": scenarios.EXEC_TOOL_RAW_OUTPUT,
            },
        )
        self._session_update(
            session_id,
            {
                "sessionUpdate": "tool_call",
                "toolCallId": scenarios.DIFF_TOOL_CALL_ID,
                "title": scenarios.DIFF_TOOL_TITLE,
                "kind": "edit",
                "status": "completed",
                "content": [
                    {
                        "type": "diff",
                        "path": scenarios.DIFF_PATH,
                        "oldText": scenarios.DIFF_OLD_TEXT,
                        "newText": scenarios.DIFF_NEW_TEXT,
                    }
                ],
                "locations": [{"path": scenarios.DIFF_PATH}],
            },
        )
        return "end_turn"

    async def _permission_round_trip(
        self,
        session_id: str,
        tool_call: dict[str, Any],
        *,
        approved_text: str,
        denied_text: str,
    ) -> str:
        """Shared permission flow: request -> chunk the outcome -> end_turn."""
        response = await self._request(
            "session/request_permission",
            {
                "sessionId": session_id,
                "toolCall": tool_call,
                "options": [
                    {
                        "optionId": scenarios.PERMISSION_ALLOW_OPTION_ID,
                        "name": "Allow once",
                        "kind": "allow_once",
                    },
                    {
                        "optionId": scenarios.PERMISSION_REJECT_OPTION_ID,
                        "name": "Reject once",
                        "kind": "reject_once",
                    },
                ],
            },
        )
        outcome = (response.get("result") or {}).get("outcome") or {}
        approved = (
            outcome.get("outcome") == "selected"
            and outcome.get("optionId") == scenarios.PERMISSION_ALLOW_OPTION_ID
        )
        self._chunk(session_id, approved_text if approved else denied_text)
        return "end_turn"

    async def _scenario_permission(self, session_id: str) -> str | None:
        return await self._permission_round_trip(
            session_id,
            {
                "toolCallId": scenarios.PERMISSION_TOOL_CALL_ID,
                "title": scenarios.PERMISSION_TOOL_TITLE,
                "kind": "execute",
                "status": "pending",
            },
            approved_text=scenarios.PERMISSION_APPROVED_TEXT,
            denied_text=scenarios.PERMISSION_DENIED_TEXT,
        )

    async def _scenario_permission_read(self, session_id: str) -> str | None:
        """Read permission for a file INSIDE the session cwd: the guarded
        ceiling allows it, so a serving Ziggy must forward it to its client."""
        cwd = self._session_cwd.get(session_id, "")
        path = os.path.join(cwd, scenarios.PERMISSION_READ_FILE_NAME)
        return await self._permission_round_trip(
            session_id,
            {
                "toolCallId": scenarios.PERMISSION_READ_TOOL_CALL_ID,
                "title": scenarios.PERMISSION_READ_TOOL_TITLE,
                "kind": "read",
                "status": "pending",
                "locations": [{"path": path}],
            },
            approved_text=scenarios.PERMISSION_READ_APPROVED_TEXT,
            denied_text=scenarios.PERMISSION_READ_DENIED_TEXT,
        )

    async def _scenario_permission_outside(self, session_id: str) -> str | None:
        """Edit permission for a path OUTSIDE any workspace: the policy ceiling
        must deny locally — the upstream client is never asked, even when it is
        scripted to approve (approval cannot exceed the trusted user ceiling)."""
        return await self._permission_round_trip(
            session_id,
            {
                "toolCallId": scenarios.PERMISSION_OUTSIDE_TOOL_CALL_ID,
                "title": scenarios.PERMISSION_OUTSIDE_TOOL_TITLE,
                "kind": "edit",
                "status": "pending",
                "locations": [{"path": scenarios.PERMISSION_OUTSIDE_PATH}],
            },
            approved_text=scenarios.PERMISSION_OUTSIDE_APPROVED_TEXT,
            denied_text=scenarios.PERMISSION_OUTSIDE_DENIED_TEXT,
        )

    async def _scenario_fs_ops(self, session_id: str) -> str | None:
        cwd = self._session_cwd.get(session_id, "")
        read_path = os.path.join(cwd, scenarios.FS_READ_NAME)
        response = await self._request(
            "fs/read_text_file", {"sessionId": session_id, "path": read_path}
        )
        if "error" in response:
            self._chunk(session_id, scenarios.FS_DENIED_TEXT)
            return "end_turn"
        content = (response.get("result") or {}).get("content", "")
        self._chunk(session_id, scenarios.FS_READ_PREFIX + str(content))
        write_path = os.path.join(cwd, scenarios.FS_WRITE_NAME)
        response = await self._request(
            "fs/write_text_file",
            {"sessionId": session_id, "path": write_path, "content": scenarios.FS_WRITE_CONTENT},
        )
        if "error" in response:
            self._chunk(session_id, scenarios.FS_DENIED_TEXT)
        return "end_turn"

    async def _scenario_malformed(self, session_id: str) -> str | None:
        self._chunk(session_id, scenarios.MALFORMED_FIRST_TEXT)
        self._send_raw(scenarios.MALFORMED_GARBAGE_LINE + "\n")
        return "end_turn"

    async def _scenario_crash_mid_turn(self, session_id: str) -> str | None:
        self._chunk(session_id, scenarios.CRASH_FIRST_TEXT)
        os._exit(scenarios.CRASH_EXIT_CODE)

    async def _scenario_ignore_cancel(self, session_id: str) -> str | None:
        """Hostile-agent shape for teardown-ladder tests.

        Spawns a child in the same process group (so ``killpg`` must reap
        it), announces the child pid as the first chunk, then streams
        forever and never resolves the prompt — ``session/cancel`` is
        deliberately ignored.
        """
        child = subprocess.Popen(["sleep", "300"])
        self._chunk(session_id, scenarios.CHILD_PID_PREFIX + str(child.pid))
        tick = 0
        while True:
            self._chunk(session_id, scenarios.IGNORE_CANCEL_TICK_PREFIX + str(tick))
            tick += 1
            await asyncio.sleep(scenarios.IGNORE_CANCEL_TICK_SECONDS)

    async def _scenario_slow_stream(self, session_id: str) -> str | None:
        for tick in range(scenarios.SLOW_STREAM_CHUNK_COUNT):
            if self._cancelled.is_set():
                return "cancelled"
            self._chunk(session_id, scenarios.SLOW_STREAM_TICK_PREFIX + str(tick))
            try:
                await asyncio.wait_for(
                    self._cancelled.wait(), timeout=scenarios.SLOW_STREAM_DELAY_SECONDS
                )
            except TimeoutError:
                continue
            return "cancelled"
        return "end_turn"

    async def _scenario_secret_leak(self, session_id: str) -> str | None:
        for text in scenarios.SECRET_LEAK_PRE_TOOL_CHUNKS:
            self._chunk(session_id, text)
        self._session_update(
            session_id,
            {
                "sessionUpdate": "tool_call",
                "toolCallId": scenarios.SECRET_LEAK_TOOL_CALL_ID,
                "title": scenarios.SECRET_LEAK_TOOL_TITLE,
                "kind": "execute",
                "status": "completed",
                "rawOutput": scenarios.SECRET_LEAK_RAW_OUTPUT,
            },
        )
        for text in scenarios.SECRET_LEAK_POST_TOOL_CHUNKS:
            self._chunk(session_id, text)
        return "end_turn"

    async def _scenario_env_echo(self, session_id: str) -> str | None:
        self._chunk(session_id, scenarios.ENV_ECHO_SEPARATOR.join(sorted(os.environ)))
        return "end_turn"

    async def _scenario_echo_prompt(self, session_id: str) -> str | None:
        """Echo the received prompt text back verbatim as message chunks.

        Lets workflow tests observe the EXACT composed prompt a step received
        (untrusted-input delimiters and any literal template tokens included).
        """
        text = self._session_prompt.get(session_id, "")
        step = scenarios.ECHO_PROMPT_CHUNK_CHARS
        for start in range(0, len(text), step):
            self._chunk(session_id, text[start : start + step])
        return "end_turn"


async def _stdin_reader() -> asyncio.StreamReader:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=1024 * 1024, loop=loop)
    protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    return reader


async def _serve(scenario: str) -> None:
    agent = MockAgent(scenario)
    reader = await _stdin_reader()
    while True:
        line = await reader.readline()
        if not line:
            break  # client closed stdin; asyncio.run cancels live prompt tasks
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            frame = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(frame, dict):
            await agent.dispatch(frame)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in scenarios.ALL_SCENARIOS:
        names = ", ".join(scenarios.ALL_SCENARIOS)
        sys.stderr.write(f"usage: raw_agent.py <scenario>\nscenarios: {names}\n")
        return 2
    asyncio.run(_serve(args[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
