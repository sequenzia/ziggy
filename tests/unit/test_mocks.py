"""Smoke tests proving the mock agent fixtures speak correct ACP wire frames.

The driving client here is a minimal hand-rolled newline-delimited JSON-RPC
client — deliberately SDK-free, like ``raw_agent.py`` itself — so these
tests validate the fixtures against the wire contract without trusting the
production ``acp`` SDK stack (spec §10.1).
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.mocks import RAW_AGENT_PATH, SDK_AGENT_PATH, scenarios  # noqa: E402

STEP_TIMEOUT = 10.0


class RawJsonRpcClient:
    """Minimal SDK-free newline-delimited JSON-RPC 2.0 driver for the mocks."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.updates: list[dict[str, Any]] = []
        self.agent_requests: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.garbage_lines: list[bytes] = []
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 1
        self.reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self.process.stdout is not None
        while True:
            line = await self.process.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                frame = json.loads(text)
            except json.JSONDecodeError:
                self.garbage_lines.append(line)
                continue
            if not isinstance(frame, dict):
                self.garbage_lines.append(line)
            elif "method" in frame:
                if frame.get("id") is not None:
                    await self.agent_requests.put(frame)
                elif frame["method"] == "session/update":
                    self.updates.append(frame["params"])
            else:
                future = self._pending.pop(frame.get("id"), None)
                if future is not None and not future.done():
                    future.set_result(frame)

    def _write(self, frame: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(frame).encode("utf-8") + b"\n")

    async def request(
        self, method: str, params: dict[str, Any], timeout: float = STEP_TIMEOUT
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        frame = await asyncio.wait_for(future, timeout)
        assert "error" not in frame, f"unexpected JSON-RPC error: {frame}"
        return frame["result"]

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def respond(self, request_id: Any, result: Any) -> None:
        self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def respond_error(self, request_id: Any, code: int, message: str) -> None:
        error = {"code": code, "message": message}
        self._write({"jsonrpc": "2.0", "id": request_id, "error": error})

    async def next_agent_request(self, timeout: float = STEP_TIMEOUT) -> dict[str, Any]:
        return await asyncio.wait_for(self.agent_requests.get(), timeout)

    def chunk_texts(self) -> list[str]:
        return [
            params["update"]["content"]["text"]
            for params in self.updates
            if params["update"].get("sessionUpdate") == "agent_message_chunk"
        ]


@asynccontextmanager
async def spawn_agent(agent_path: Path, scenario: str) -> AsyncIterator[RawJsonRpcClient]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(agent_path),
        scenario,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    client = RawJsonRpcClient(process)
    try:
        yield client
    finally:
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            # Bounded: process.wait() can hang past SIGKILL if a scenario's
            # grandchild (ignore_cancel's sleep) inherited the stdout pipe.
            with suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=5.0)
        client.reader_task.cancel()
        with suppress(asyncio.CancelledError):
            await client.reader_task


async def handshake(
    client: RawJsonRpcClient, cwd: str, *, expected_name: str = scenarios.RAW_AGENT_NAME
) -> str:
    init = await client.request(
        "initialize",
        {
            "protocolVersion": scenarios.PROTOCOL_VERSION,
            "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}},
            "clientInfo": {"name": "ziggy-test-client", "version": "0"},
        },
    )
    assert init["protocolVersion"] == scenarios.PROTOCOL_VERSION
    assert init["agentInfo"]["name"] == expected_name
    session = await client.request("session/new", {"cwd": cwd, "mcpServers": []})
    session_id = session["sessionId"]
    assert isinstance(session_id, str) and session_id
    return session_id


def prompt_params(session_id: str) -> dict[str, Any]:
    return {"sessionId": session_id, "prompt": [{"type": "text", "text": "go"}]}


def test_raw_fixture_files_are_sdk_free() -> None:
    """The raw fixture's independence from the acp SDK is the point (§10.1)."""
    pattern = re.compile(r"^\s*(import acp\b|from acp\b)", re.MULTILINE)
    for path in (RAW_AGENT_PATH, scenarios.__file__):
        assert not pattern.search(Path(path).read_text(encoding="utf-8")), path


@pytest.mark.slow
async def test_hello_streams_expected_chunks(tmp_path: Path) -> None:
    async with spawn_agent(RAW_AGENT_PATH, scenarios.HELLO) as client:
        session_id = await handshake(client, str(tmp_path))
        result = await client.request("session/prompt", prompt_params(session_id))
        assert result["stopReason"] == "end_turn"
        assert tuple(client.chunk_texts()) == scenarios.HELLO_CHUNKS
        for params in client.updates:
            assert params["sessionId"] == session_id
            assert params["update"]["sessionUpdate"] == "agent_message_chunk"


@pytest.mark.slow
async def test_unknown_method_is_answered_with_32601(tmp_path: Path) -> None:
    async with spawn_agent(RAW_AGENT_PATH, scenarios.HELLO) as client:
        await handshake(client, str(tmp_path))
        with pytest.raises(AssertionError, match="-32601"):
            await client.request("session/load", {"sessionId": "nope"})


@pytest.mark.slow
async def test_malformed_line_between_valid_frames(tmp_path: Path) -> None:
    async with spawn_agent(RAW_AGENT_PATH, scenarios.MALFORMED) as client:
        session_id = await handshake(client, str(tmp_path))
        result = await client.request("session/prompt", prompt_params(session_id))
        assert result["stopReason"] == "end_turn"
        assert client.chunk_texts() == [scenarios.MALFORMED_FIRST_TEXT]
        assert len(client.garbage_lines) == 1
        garbage = client.garbage_lines[0].decode("utf-8").strip()
        assert garbage == scenarios.MALFORMED_GARBAGE_LINE


@pytest.mark.slow
async def test_permission_approve_path(tmp_path: Path) -> None:
    async with spawn_agent(RAW_AGENT_PATH, scenarios.PERMISSION) as client:
        session_id = await handshake(client, str(tmp_path))
        prompt_task = asyncio.create_task(
            client.request("session/prompt", prompt_params(session_id))
        )
        request = await client.next_agent_request()
        assert request["method"] == "session/request_permission"
        assert request["id"] >= scenarios.AGENT_REQUEST_ID_START
        params = request["params"]
        assert params["sessionId"] == session_id
        assert params["toolCall"]["toolCallId"] == scenarios.PERMISSION_TOOL_CALL_ID
        offered = {option["optionId"]: option["kind"] for option in params["options"]}
        assert offered == {
            scenarios.PERMISSION_ALLOW_OPTION_ID: "allow_once",
            scenarios.PERMISSION_REJECT_OPTION_ID: "reject_once",
        }
        client.respond(
            request["id"],
            {
                "outcome": {
                    "outcome": "selected",
                    "optionId": scenarios.PERMISSION_ALLOW_OPTION_ID,
                }
            },
        )
        result = await prompt_task
        assert result["stopReason"] == "end_turn"
        assert client.chunk_texts() == [scenarios.PERMISSION_APPROVED_TEXT]


@pytest.mark.slow
async def test_fs_ops_round_trip(tmp_path: Path) -> None:
    file_content = "hello file\n"
    (tmp_path / scenarios.FS_READ_NAME).write_text(file_content, encoding="utf-8")
    async with spawn_agent(RAW_AGENT_PATH, scenarios.FS_OPS) as client:
        session_id = await handshake(client, str(tmp_path))
        prompt_task = asyncio.create_task(
            client.request("session/prompt", prompt_params(session_id))
        )

        read_request = await client.next_agent_request()
        assert read_request["method"] == "fs/read_text_file"
        assert read_request["id"] >= scenarios.AGENT_REQUEST_ID_START
        assert read_request["params"]["sessionId"] == session_id
        assert read_request["params"]["path"] == str(tmp_path / scenarios.FS_READ_NAME)
        client.respond(read_request["id"], {"content": file_content})

        write_request = await client.next_agent_request()
        assert write_request["method"] == "fs/write_text_file"
        assert write_request["id"] > read_request["id"]
        assert write_request["params"]["path"] == str(tmp_path / scenarios.FS_WRITE_NAME)
        assert write_request["params"]["content"] == scenarios.FS_WRITE_CONTENT
        client.respond(write_request["id"], {})

        result = await prompt_task
        assert result["stopReason"] == "end_turn"
        assert client.chunk_texts() == [scenarios.FS_READ_PREFIX + file_content]


@pytest.mark.slow
async def test_sdk_agent_hello_speaks_same_wire(tmp_path: Path) -> None:
    """The SDK-backed fixture answers the same SDK-free client correctly."""
    async with spawn_agent(SDK_AGENT_PATH, scenarios.HELLO) as client:
        session_id = await handshake(client, str(tmp_path), expected_name=scenarios.SDK_AGENT_NAME)
        result = await client.request("session/prompt", prompt_params(session_id))
        assert result["stopReason"] == "end_turn"
        assert tuple(client.chunk_texts()) == scenarios.HELLO_CHUNKS
