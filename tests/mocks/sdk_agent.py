#!/usr/bin/env python3
"""SDK-backed programmable mock ACP agent (secondary fixture).

Emulates a third-party agent built on the official ``agent-client-protocol``
SDK so integration tests cover both a hand-rolled wire implementation
(``raw_agent.py``, the primary fixture) and the SDK's own framing.

Importing ``acp`` here is intentionally allowed: this file is a test fixture
standing in for an external agent process, not Ziggy production code — the
"no ``acp`` outside ``src/ziggy/acp/``" rule targets Ziggy's own modules.

Usable two ways:
- as a subprocess: ``[sys.executable, path_to_this_file, scenario_name]``
- in-process: construct ``ScenarioAgent`` and pass it to ``acp.run_agent``.

Supported scenarios (see ``scenarios.SDK_SCENARIOS``): hello, slow_stream,
permission.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from acp import PROTOCOL_VERSION, run_agent, update_agent_message_text
from acp.schema import (
    AgentCapabilities,
    Implementation,
    InitializeResponse,
    NewSessionResponse,
    PermissionOption,
    PromptResponse,
    ToolCallUpdate,
)

try:
    import scenarios  # script mode: sys.path[0] is this file's directory
except ImportError:  # imported as a package module by tests
    from tests.mocks import scenarios


class ScenarioAgent:
    """Programmable ``acp.interfaces.Agent`` driven by a scenario name."""

    def __init__(self, scenario: str) -> None:
        if scenario not in scenarios.SDK_SCENARIOS:
            supported = ", ".join(scenarios.SDK_SCENARIOS)
            raise ValueError(f"sdk_agent supports [{supported}], got {scenario!r}")
        self.scenario = scenario
        self._conn: Any = None
        self._session_counter = 0
        self._cancelled = asyncio.Event()

    def on_connect(self, conn: Any) -> None:
        self._conn = conn

    # --- ACP agent surface ------------------------------------------------

    async def initialize(self, protocol_version: int, **kwargs: Any) -> InitializeResponse:
        return InitializeResponse(
            protocol_version=min(protocol_version, PROTOCOL_VERSION),
            agent_capabilities=AgentCapabilities(load_session=False),
            auth_methods=[],
            agent_info=Implementation(
                name=scenarios.SDK_AGENT_NAME, version=scenarios.AGENT_VERSION
            ),
        )

    async def new_session(self, cwd: str, **kwargs: Any) -> NewSessionResponse:
        self._session_counter += 1
        return NewSessionResponse(session_id=f"sdk-sess-{self._session_counter}")

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        if self.scenario == scenarios.HELLO:
            stop_reason = await self._hello(session_id)
        elif self.scenario == scenarios.SLOW_STREAM:
            stop_reason = await self._slow_stream(session_id)
        else:
            stop_reason = await self._permission(session_id)
        return PromptResponse(stop_reason=stop_reason)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        self._cancelled.set()

    # --- scenarios --------------------------------------------------------

    async def _chunk(self, session_id: str, text: str) -> None:
        await self._conn.session_update(
            session_id=session_id, update=update_agent_message_text(text)
        )

    async def _hello(self, session_id: str) -> str:
        for text in scenarios.HELLO_CHUNKS:
            await self._chunk(session_id, text)
        return "end_turn"

    async def _slow_stream(self, session_id: str) -> str:
        for tick in range(scenarios.SLOW_STREAM_CHUNK_COUNT):
            if self._cancelled.is_set():
                return "cancelled"
            await self._chunk(session_id, scenarios.SLOW_STREAM_TICK_PREFIX + str(tick))
            try:
                await asyncio.wait_for(
                    self._cancelled.wait(), timeout=scenarios.SLOW_STREAM_DELAY_SECONDS
                )
            except TimeoutError:
                continue
            return "cancelled"
        return "end_turn"

    async def _permission(self, session_id: str) -> str:
        response = await self._conn.request_permission(
            session_id=session_id,
            tool_call=ToolCallUpdate(
                tool_call_id=scenarios.PERMISSION_TOOL_CALL_ID,
                title=scenarios.PERMISSION_TOOL_TITLE,
                kind="execute",
                status="pending",
            ),
            options=[
                PermissionOption(
                    option_id=scenarios.PERMISSION_ALLOW_OPTION_ID,
                    name="Allow once",
                    kind="allow_once",
                ),
                PermissionOption(
                    option_id=scenarios.PERMISSION_REJECT_OPTION_ID,
                    name="Reject once",
                    kind="reject_once",
                ),
            ],
        )
        outcome = response.outcome
        approved = (
            getattr(outcome, "outcome", None) == "selected"
            and getattr(outcome, "option_id", None) == scenarios.PERMISSION_ALLOW_OPTION_ID
        )
        if approved:
            await self._chunk(session_id, scenarios.PERMISSION_APPROVED_TEXT)
        else:
            await self._chunk(session_id, scenarios.PERMISSION_DENIED_TEXT)
        return "end_turn"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        supported = ", ".join(scenarios.SDK_SCENARIOS)
        sys.stderr.write(f"usage: sdk_agent.py <scenario>\nscenarios: {supported}\n")
        return 2
    try:
        agent = ScenarioAgent(args[0])
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    asyncio.run(run_agent(agent))
    return 0


if __name__ == "__main__":
    sys.exit(main())
