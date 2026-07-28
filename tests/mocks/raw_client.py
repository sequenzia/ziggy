"""Raw NDJSON loopback client for driving a real ``ziggy serve`` subprocess.

Deliberately SDK-free, like ``raw_agent.py``: the serving side of Ziggy is
validated against the ACP wire contract itself with a hand-rolled
newline-delimited JSON-RPC 2.0 client (phase45-contracts "### Tests: loopback
fixture"; spec §10.2 "ACP permission bridge"). This extends the client
patterns of ``tests/unit/test_mocks.py`` to the reverse direction: here the
hand-rolled side is the CLIENT and the spawned subprocess is Ziggy.

Capabilities on top of the test_mocks client:

- spawns ``python -m ziggy serve`` with a tmp ``ZIGGY_HOME`` and a workspace
  cwd, draining stderr separately (stdout must carry ONLY JSON-RPC frames);
- records every parsed stdout frame (for the stdout-purity assertion), every
  ``session/update`` notification, and every server-originated
  ``session/request_permission`` request;
- answers permission requests via a per-test ``permission_script``
  (:func:`approve_permission` / :func:`deny_permission` /
  :func:`unsupported_permission`), or queues them for manual handling;
- supports mid-run stdin close (client-disconnect scenarios) and bounded
  process-exit waits.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

#: Generous request deadline: covers interpreter start + config load on the
#: first request and full mock-agent turns afterwards.
REQUEST_TIMEOUT = 30.0

#: A scripted answer for a server-originated request: ``("result", payload)``
#: responds with ``payload``; ``("error", (code, message))`` responds with a
#: JSON-RPC error. ``None`` (from a script) queues the request for manual
#: handling instead.
PermissionAnswer = tuple[str, Any]
PermissionScript = Callable[[dict[str, Any]], PermissionAnswer | None]


def _option_id_of_kind(params: dict[str, Any], kind: str) -> str | None:
    for option in params.get("options") or []:
        if isinstance(option, dict) and option.get("kind") == kind:
            option_id = option.get("optionId")
            if isinstance(option_id, str):
                return option_id
    return None


def _selected(option_id: str | None) -> PermissionAnswer:
    if option_id is None:  # no matching option offered: the cancelled outcome
        return ("result", {"outcome": {"outcome": "cancelled"}})
    return ("result", {"outcome": {"outcome": "selected", "optionId": option_id}})


def approve_permission(params: dict[str, Any]) -> PermissionAnswer:
    """Select the first ``allow_once`` option (an eagerly approving client)."""
    return _selected(_option_id_of_kind(params, "allow_once"))


def deny_permission(params: dict[str, Any]) -> PermissionAnswer:
    """Select the first ``reject_once`` option (a denying client)."""
    return _selected(_option_id_of_kind(params, "reject_once"))


def unsupported_permission(params: dict[str, Any]) -> PermissionAnswer:
    """Answer method_not_found: a client without the permission surface."""
    return ("error", (-32601, "Method not found"))


class ServeClient:
    """Hand-rolled newline-delimited JSON-RPC 2.0 client bound to one
    ``ziggy serve`` process."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        #: Every parsed stdout frame, in arrival order (stdout-purity checks).
        self.frames: list[dict[str, Any]] = []
        #: stdout lines that failed to parse as a JSON object.
        self.garbage_lines: list[bytes] = []
        #: ``session/update`` notification params, in arrival order.
        self.updates: list[dict[str, Any]] = []
        #: params of every ``session/request_permission`` the server sent us.
        self.permission_requests: list[dict[str, Any]] = []
        #: server-originated requests no script answered (manual handling).
        self.server_requests: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        #: scripted answer for incoming ``session/request_permission``.
        self.permission_script: PermissionScript | None = None
        self.stderr_lines: list[str] = []
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 1
        self.reader_task = asyncio.create_task(self._read_loop())
        self.stderr_task = asyncio.create_task(self._stderr_loop())

    # ------------------------------------------------------------ read loops

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
                continue
            self.frames.append(frame)
            if "method" in frame:
                if frame.get("id") is not None:
                    self._on_server_request(frame)
                elif frame["method"] == "session/update":
                    self.updates.append(frame.get("params") or {})
            else:
                future = self._pending.pop(frame.get("id"), None)
                if future is not None and not future.done():
                    future.set_result(frame)
        # EOF: fail anything still awaiting a response (disconnect scenarios).
        for future in self._pending.values():
            if not future.done():
                future.set_exception(EOFError("server stdout reached EOF"))
        self._pending.clear()

    def _on_server_request(self, frame: dict[str, Any]) -> None:
        params = frame.get("params") or {}
        answer: PermissionAnswer | None = None
        if frame["method"] == "session/request_permission":
            self.permission_requests.append(params)
            if self.permission_script is not None:
                answer = self.permission_script(params)
        if answer is None:
            self.server_requests.put_nowait(frame)
        elif answer[0] == "result":
            self.respond(frame["id"], answer[1])
        else:
            code, message = answer[1]
            self.respond_error(frame["id"], code, message)

    async def _stderr_loop(self) -> None:
        assert self.process.stderr is not None
        while True:
            line = await self.process.stderr.readline()
            if not line:
                break
            self.stderr_lines.append(line.decode("utf-8", errors="replace").rstrip())

    # ------------------------------------------------------------- transport

    def _write(self, frame: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(frame).encode("utf-8") + b"\n")

    async def request_raw(
        self, method: str, params: dict[str, Any], timeout: float = REQUEST_TIMEOUT
    ) -> dict[str, Any]:
        """Send a request and return the full response frame (errors included)."""
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return await asyncio.wait_for(future, timeout)

    async def request(
        self, method: str, params: dict[str, Any], timeout: float = REQUEST_TIMEOUT
    ) -> dict[str, Any]:
        """Send a request; assert success and return the ``result`` payload."""
        frame = await self.request_raw(method, params, timeout)
        assert "error" not in frame, (
            f"unexpected JSON-RPC error for {method}: {frame}\nserver stderr:\n{self.stderr_text()}"
        )
        return frame["result"]

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def respond(self, request_id: Any, result: Any) -> None:
        self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def respond_error(self, request_id: Any, code: int, message: str) -> None:
        error = {"code": code, "message": message}
        self._write({"jsonrpc": "2.0", "id": request_id, "error": error})

    def close_stdin(self) -> None:
        """Simulate a client disconnect (server must tear down and exit)."""
        if self.process.stdin is not None:
            with suppress(OSError, RuntimeError):
                self.process.stdin.close()

    async def wait_exit(self, timeout: float) -> int:
        return await asyncio.wait_for(self.process.wait(), timeout)

    async def aclose(self) -> None:
        for task in (self.reader_task, self.stderr_task):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    # --------------------------------------------------------------- helpers

    def stderr_text(self) -> str:
        return "\n".join(self.stderr_lines)

    async def wait_for_update(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        timeout: float = REQUEST_TIMEOUT,
    ) -> dict[str, Any]:
        """Poll until one recorded ``session/update`` params matches."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            for params in self.updates:
                if predicate(params):
                    return params
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(
                    f"no matching session/update within {timeout}s; "
                    f"saw {len(self.updates)} update(s); stderr:\n{self.stderr_text()}"
                )
            await asyncio.sleep(0.02)

    def chunk_texts(self, session_id: str | None = None) -> list[str]:
        """Texts of ``agent_message_chunk`` updates (optionally per session)."""
        texts: list[str] = []
        for params in self.updates:
            if session_id is not None and params.get("sessionId") != session_id:
                continue
            update = params.get("update") or {}
            if update.get("sessionUpdate") != "agent_message_chunk":
                continue
            content = update.get("content") or {}
            if content.get("type") == "text":
                texts.append(str(content.get("text", "")))
        return texts

    def agent_texts(self, session_id: str | None = None) -> list[str]:
        """Chunk texts minus Ziggy's own ``[...]``-prefixed notice lines."""
        return [t for t in self.chunk_texts(session_id) if not t.startswith("[")]

    def metas(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """The ``_meta.ziggy`` correlation payloads of recorded updates."""
        metas: list[dict[str, Any]] = []
        for params in self.updates:
            if session_id is not None and params.get("sessionId") != session_id:
                continue
            meta = params.get("_meta") or {}
            ziggy = meta.get("ziggy")
            if isinstance(ziggy, dict):
                metas.append(ziggy)
        return metas

    def run_ids(self, session_id: str | None = None) -> list[str]:
        """Distinct ``_meta.ziggy.run_id`` values, in first-seen order."""
        seen: dict[str, None] = {}
        for meta in self.metas(session_id):
            run_id = meta.get("run_id")
            if isinstance(run_id, str) and run_id not in seen:
                seen[run_id] = None
        return list(seen)

    def assert_stdout_jsonrpc(self) -> None:
        """Every server stdout line so far parsed as a JSON-RPC 2.0 frame."""
        assert self.garbage_lines == [], (
            f"non-JSON stdout lines from ziggy serve: {self.garbage_lines!r}"
        )
        assert self.frames, "expected at least one JSON-RPC frame on stdout"
        for frame in self.frames:
            assert frame.get("jsonrpc") == "2.0", f"not a JSON-RPC 2.0 frame: {frame!r}"
            assert "method" in frame or "id" in frame, f"malformed frame: {frame!r}"


@asynccontextmanager
async def spawn_server(home: Path, workspace: Path) -> AsyncIterator[ServeClient]:
    """Spawn ``python -m ziggy serve`` (cwd=workspace, ZIGGY_HOME=home)."""
    env = dict(os.environ)
    env["ZIGGY_HOME"] = str(home)
    env["NO_COLOR"] = "1"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "ziggy",
        "serve",
        cwd=workspace,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    client = ServeClient(process)
    try:
        yield client
    finally:
        client.close_stdin()
        try:
            await asyncio.wait_for(process.wait(), timeout=20.0)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=5.0)
        await client.aclose()


async def initialize(client: ServeClient) -> dict[str, Any]:
    """The v1 handshake; returns the initialize result."""
    return await client.request(
        "initialize",
        {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
            "clientInfo": {"name": "ziggy-loopback-client", "version": "0"},
        },
    )


async def new_session(client: ServeClient, cwd: Path | str) -> tuple[str, dict[str, Any]]:
    """Open a session; returns ``(session_id, full session/new result)``."""
    result = await client.request("session/new", {"cwd": str(cwd), "mcpServers": []})
    session_id = result["sessionId"]
    assert isinstance(session_id, str) and session_id
    return session_id, result


async def set_route(client: ServeClient, session_id: str, route: str) -> dict[str, Any]:
    """Switch the session route via ``session/set_config_option``."""
    return await client.request(
        "session/set_config_option",
        {"sessionId": session_id, "configId": "route", "value": route},
    )


async def open_routed_session(client: ServeClient, cwd: Path | str, route: str) -> str:
    session_id, _ = await new_session(client, cwd)
    await set_route(client, session_id, route)
    return session_id


def prompt_params(session_id: str, text: str = "go") -> dict[str, Any]:
    return {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]}
