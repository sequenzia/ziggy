"""Mock ACP agent fixtures for wire-conformance and integration tests.

``raw_agent.py`` is the primary fixture: a hand-rolled newline-delimited
JSON-RPC 2.0 agent with zero dependency on the production ``acp`` SDK, so
tests driving it exercise the real wire contract rather than the SDK's own
framing. ``sdk_agent.py`` is a secondary SDK-backed fixture emulating a
third-party agent. ``scenarios.py`` is the single source of scenario names,
seeded fake secrets, and expected texts shared between fixtures and tests.
"""

from __future__ import annotations

from pathlib import Path

MOCKS_DIR = Path(__file__).resolve().parent
RAW_AGENT_PATH = MOCKS_DIR / "raw_agent.py"
SDK_AGENT_PATH = MOCKS_DIR / "sdk_agent.py"
