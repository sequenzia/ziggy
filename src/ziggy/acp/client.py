"""AgentProcessClient: spawn + drive one ACP agent subprocess (REQ-001, §7.4).

Ziggy spawns the subprocess itself (``start_new_session=True`` so the agent
leads its own process group) and hands the pipes to the SDK's
``ClientSideConnection``; teardown of the whole process group is Ziggy's job
(docs/phase0/process-lifecycle.md).
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from typing import Any

from acp import PROTOCOL_VERSION, RequestError, text_block
from acp.client.connection import ClientSideConnection
from acp.connection import StreamEvent
from acp.core import DEFAULT_STDIO_BUFFER_LIMIT_BYTES
from acp.schema import (
    ClientCapabilities,
    CreateTerminalResponse,
    EnvVariable,
    FileSystemCapabilities,
    Implementation,
    KillTerminalResponse,
    PermissionOption,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    RequestPermissionResponse,
    TerminalExitStatus,
    TerminalOutputResponse,
    ToolCallUpdate,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)

from ziggy import __version__
from ziggy.acp.convert import (
    to_agent_event,
    to_handshake_info,
    to_permission_request,
    to_permission_response,
)
from ziggy.acp.types import (
    FsReadRequestN,
    FsWriteRequestN,
    HandshakeInfo,
    MediationHooks,
    PolicyDenied,
    StopInfo,
    TerminalReply,
    TerminalRequestN,
    UnsupportedByPolicy,
)
from ziggy.errors import AgentLaunchError, ProtocolError

RawFrameCallback = Callable[[str, dict[str, Any]], None]

_LAUNCH_TOKEN = object()
#: bounded wait after group SIGTERM before escalating to SIGKILL (lifecycle step 3)
_TERM_WAIT_SECONDS = 5.0
_STDERR_RING_BYTES = 64 * 1024


def _denied(exc: PolicyDenied) -> RequestError:
    return RequestError.internal_error({"denied": True, "reason": exc.reason})


class _ClientImpl:
    """SDK ``Client`` protocol implementation bridging to MediationHooks.

    All hook failures are translated to JSON-RPC errors here; SDK exceptions
    never reach the engine and hook exceptions never reach the SDK router raw.
    """

    def __init__(self, hooks: MediationHooks) -> None:
        self._hooks = hooks

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        await self._hooks.on_event(to_agent_event(update))

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        reply = await self._hooks.resolve_permission(
            to_permission_request(session_id, tool_call, options)
        )
        return to_permission_response(reply)

    async def read_text_file(
        self,
        session_id: str,
        path: str,
        line: int | None = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> ReadTextFileResponse:
        try:
            content = await self._hooks.read_text_file(
                FsReadRequestN(path=path, line=line, limit=limit)
            )
        except PolicyDenied as exc:
            raise _denied(exc) from exc
        return ReadTextFileResponse(content=content)

    async def write_text_file(
        self, session_id: str, path: str, content: str, **kwargs: Any
    ) -> WriteTextFileResponse:
        try:
            await self._hooks.write_text_file(FsWriteRequestN(path=path, content=content))
        except PolicyDenied as exc:
            raise _denied(exc) from exc
        return WriteTextFileResponse()

    async def _terminal(self, method: str, op: str, payload: dict[str, Any]) -> TerminalReply:
        try:
            return await self._hooks.handle_terminal(TerminalRequestN(op=op, payload=payload))
        except UnsupportedByPolicy as exc:
            raise RequestError.method_not_found(method) from exc
        except PolicyDenied as exc:
            raise _denied(exc) from exc

    async def create_terminal(
        self,
        session_id: str,
        command: str,
        args: list[str] | None = None,
        env: list[EnvVariable] | None = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> CreateTerminalResponse:
        payload = {
            "session_id": session_id,
            "command": command,
            "args": list(args or []),
            "env": {e.name: e.value for e in env or []},
            "cwd": cwd,
            "output_byte_limit": output_byte_limit,
        }
        reply = await self._terminal("terminal/create", "create", payload)
        return CreateTerminalResponse(terminal_id=str(reply.payload["terminal_id"]))

    async def terminal_output(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> TerminalOutputResponse:
        reply = await self._terminal(
            "terminal/output", "output", {"session_id": session_id, "terminal_id": terminal_id}
        )
        exit_status = reply.payload.get("exit_status")
        return TerminalOutputResponse(
            output=str(reply.payload.get("output", "")),
            truncated=bool(reply.payload.get("truncated", False)),
            exit_status=TerminalExitStatus(**exit_status) if exit_status else None,
        )

    async def release_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> ReleaseTerminalResponse:
        await self._terminal(
            "terminal/release", "release", {"session_id": session_id, "terminal_id": terminal_id}
        )
        return ReleaseTerminalResponse()

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> WaitForTerminalExitResponse:
        reply = await self._terminal(
            "terminal/wait_for_exit",
            "wait_for_exit",
            {"session_id": session_id, "terminal_id": terminal_id},
        )
        return WaitForTerminalExitResponse(
            exit_code=reply.payload.get("exit_code"), signal=reply.payload.get("signal")
        )

    async def kill_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> KillTerminalResponse:
        await self._terminal(
            "terminal/kill", "kill", {"session_id": session_id, "terminal_id": terminal_id}
        )
        return KillTerminalResponse()

    async def create_elicitation(self, message: str, mode: Any, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("elicitation/create")

    async def complete_elicitation(self, elicitation_id: str, **kwargs: Any) -> None:
        """Unsupported notification: nothing to do (raising would only make noise)."""

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        """Unsupported notification: nothing to do."""

    def on_connect(self, conn: Any) -> None:
        """Connection reference is owned by AgentProcessClient, not the hooks."""


class _StderrRing:
    """Bounded ring of recent stderr output (launch/crash diagnostics only)."""

    def __init__(self, max_bytes: int = _STDERR_RING_BYTES) -> None:
        self._chunks: deque[bytes] = deque()
        self._max_bytes = max_bytes
        self._size = 0

    def append(self, chunk: bytes) -> None:
        self._chunks.append(chunk)
        self._size += len(chunk)
        while self._size > self._max_bytes and self._chunks:
            self._size -= len(self._chunks.popleft())

    def tail(self) -> str:
        return b"".join(self._chunks).decode("utf-8", errors="replace")


async def _drain_stderr(stream: asyncio.StreamReader, ring: _StderrRing) -> None:
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return
        ring.append(chunk)


class AgentProcessClient:
    """One agent subprocess + its ACP connection; create via :meth:`launch`."""

    def __init__(
        self,
        *,
        _token: object,
        process: asyncio.subprocess.Process,
        connection: ClientSideConnection,
        stderr_ring: _StderrRing,
        stderr_task: asyncio.Task[None],
    ) -> None:
        if _token is not _LAUNCH_TOKEN:
            raise TypeError("AgentProcessClient must be created via AgentProcessClient.launch()")
        self._process = process
        self._conn = connection
        self._stderr_ring = stderr_ring
        self._stderr_task = stderr_task
        #: recorded at spawn: start_new_session=True makes the child its group
        #: leader, so pid == pgid and stays valid even after the process dies
        self._pgid = process.pid
        self._handshake: HandshakeInfo | None = None
        self._conn_closed = False

    @classmethod
    async def launch(
        cls,
        *,
        command: str,
        args: Sequence[str],
        env: Mapping[str, str],
        cwd: str,
        hooks: MediationHooks,
        raw_frame_cb: RawFrameCallback | None = None,
        stream_limit: int | None = None,
    ) -> AgentProcessClient:
        """Spawn the agent subprocess and wire up the ACP connection.

        ``env`` is passed verbatim — the caller composes the full explicit
        environment; nothing is inherited implicitly here.
        """
        try:
            process = await asyncio.create_subprocess_exec(
                command,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(env),
                cwd=cwd,
                start_new_session=True,
                limit=stream_limit or DEFAULT_STDIO_BUFFER_LIMIT_BYTES,
            )
        except FileNotFoundError as exc:
            raise AgentLaunchError(
                f"Failed to launch agent: command not found: {command!r}. "
                "Install the agent adapter and ensure it is on PATH, or run `ziggy doctor`.",
                details={"command": command, "reason": "command_not_found"},
            ) from exc
        except PermissionError as exc:
            raise AgentLaunchError(
                f"Failed to launch agent: {command!r} is not executable. "
                "Check the install (permissions/format) or run `ziggy doctor`.",
                details={"command": command, "reason": "not_executable"},
            ) from exc

        stderr_ring = _StderrRing()
        stderr_stream = process.stderr
        if stderr_stream is None:  # pragma: no cover - PIPE guarantees a reader
            raise AgentLaunchError(
                "Failed to launch agent: stderr pipe unavailable",
                details={"command": command, "reason": "no_stderr_pipe"},
            )
        stderr_task = asyncio.create_task(_drain_stderr(stderr_stream, stderr_ring))

        connection_kwargs: dict[str, Any] = {}
        if raw_frame_cb is not None:

            def observe(event: StreamEvent) -> None:
                raw_frame_cb(event.direction.value, event.message)

            connection_kwargs["observers"] = [observe]
        connection = ClientSideConnection(
            _ClientImpl(hooks), process.stdin, process.stdout, **connection_kwargs
        )
        return cls(
            _token=_LAUNCH_TOKEN,
            process=process,
            connection=connection,
            stderr_ring=stderr_ring,
            stderr_task=stderr_task,
        )

    @property
    def handshake(self) -> HandshakeInfo:
        if self._handshake is None:
            raise RuntimeError("handshake not recorded; call initialize() first")
        return self._handshake

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def pgid(self) -> int:
        return self._pgid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def stderr_tail(self) -> str:
        """Recent (bounded) agent stderr, for diagnostics only."""
        return self._stderr_ring.tail()

    async def initialize(self) -> HandshakeInfo:
        try:
            resp = await self._conn.initialize(
                protocol_version=PROTOCOL_VERSION,
                client_capabilities=ClientCapabilities(
                    fs=FileSystemCapabilities(read_text_file=True, write_text_file=True),
                    terminal=True,
                ),
                client_info=Implementation(name="ziggy", version=__version__),
            )
        except RequestError as exc:
            raise ProtocolError(f"initialize failed: {exc}", details={"code": exc.code}) from exc
        except (ConnectionError, EOFError) as exc:
            raise ProtocolError(
                "agent connection lost during initialize", details={"reason": str(exc)}
            ) from exc
        if resp.protocol_version != PROTOCOL_VERSION:
            # ACP spec: the client must disconnect on an unsupported version.
            await self._close_connection()
            raise ProtocolError(
                "agent protocol version mismatch",
                details={
                    "client_protocol_version": PROTOCOL_VERSION,
                    "agent_protocol_version": resp.protocol_version,
                },
            )
        self._handshake = to_handshake_info(resp)
        return self._handshake

    async def new_session(self, cwd: str) -> str:
        try:
            resp = await self._conn.new_session(cwd=cwd, mcp_servers=[])
        except RequestError as exc:
            raise ProtocolError(f"session/new failed: {exc}", details={"code": exc.code}) from exc
        except (ConnectionError, EOFError) as exc:
            raise ProtocolError(
                "agent connection lost during session/new", details={"reason": str(exc)}
            ) from exc
        return resp.session_id

    async def prompt(self, session_id: str, text: str) -> StopInfo:
        try:
            resp = await self._conn.prompt(session_id=session_id, prompt=[text_block(text)])
        except RequestError as exc:
            raise ProtocolError(
                f"session/prompt failed: {exc}", details={"code": exc.code}
            ) from exc
        except (ConnectionError, EOFError) as exc:
            raise ProtocolError("agent connection lost", details={"reason": str(exc)}) from exc
        return StopInfo(stop_reason=resp.stop_reason)

    async def cancel(self, session_id: str) -> None:
        """Fire the ``session/cancel`` notification; a dead pipe is not an error
        here (teardown handles the process)."""
        with suppress(ConnectionError, EOFError):
            await self._conn.cancel(session_id=session_id)

    async def shutdown(self, grace_seconds: float) -> int | None:
        """Teardown ladder steps 2-5; the caller sends ``session/cancel`` (step 1).

        Grace-wait for exit, then group SIGTERM with a bounded wait, then group
        SIGKILL and reap, then close streams. Idempotent: every rung is a no-op
        once the process/group is gone.
        """
        process = self._process
        if process.returncode is None:
            with suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=max(grace_seconds, 0.0))
        if process.returncode is None:
            self._signal_group(signal.SIGTERM)
            with suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=_TERM_WAIT_SECONDS)
        if process.returncode is None:
            self._signal_group(signal.SIGKILL)
            await process.wait()
        await self._close_connection()
        if not self._stderr_task.done():
            self._stderr_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._stderr_task
        return process.returncode

    def _signal_group(self, sig: int) -> None:
        # ProcessLookupError == group already gone, which is the goal state.
        with suppress(ProcessLookupError):
            os.killpg(self._pgid, sig)

    async def _close_connection(self) -> None:
        if self._conn_closed:
            return
        self._conn_closed = True
        with suppress(ConnectionError, OSError):
            await self._conn.close()
