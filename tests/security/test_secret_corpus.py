"""Security: seeded fake secrets never persist anywhere under the store (§10.2).

The secret_leak scenario streams format-valid fake secrets through message
chunks (one split across two chunks) and tool-call raw output, plus an
env-value secret registered for exact-match redaction. After the run, every
byte under the store root — result.json, events.jsonl, changes/, artifacts/,
the SQLite index and its WAL — must be free of every seeded value. This
verifies the corpus, not a universal no-secret guarantee.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.mocks import RAW_AGENT_PATH, scenarios  # noqa: E402

from ziggy.engine import RunSpec, execute_run  # noqa: E402
from ziggy.models.common import RunStatus  # noqa: E402

pytestmark = pytest.mark.slow

#: Name chosen so the Phase-1 secret-env heuristic registers its value.
SECRET_ENV_NAME = "MOCK_PROVIDER_API_KEY"


async def test_seeded_secrets_absent_from_all_persisted_bytes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec = RunSpec(
        agent_name="mock-raw",
        command=sys.executable,
        args=[str(RAW_AGENT_PATH), scenarios.SECRET_LEAK],
        env={
            "PATH": os.environ.get("PATH", ""),
            SECRET_ENV_NAME: scenarios.SEEDED_SECRETS["env_value"],
        },
        cwd=str(workspace),
        prompt="leak everything you know",
        no_save=False,
        step_timeout_seconds=15.0,
        store_root=home,
    )
    result = await execute_run(spec)
    assert result.status is RunStatus.SUCCESS
    assert result.result_path is not None and result.events_path is not None

    secret_bytes = {kind: value.encode("utf-8") for kind, value in scenarios.SEEDED_SECRETS.items()}
    scanned = 0
    for path in sorted(home.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        scanned += 1
        for kind, value in secret_bytes.items():
            assert value not in data, f"seeded {kind} leaked into {path}"
    assert scanned >= 3  # at least result.json, events.jsonl, index.db

    events_text = Path(result.events_path).read_text(encoding="utf-8")
    result_text = Path(result.result_path).read_text(encoding="utf-8")
    assert "[REDACTED:" in events_text
    assert "[REDACTED:" in result_text

    dumped = result.model_dump_json()
    for kind, value in scenarios.SEEDED_SECRETS.items():
        assert value not in dumped, f"seeded {kind} present in the in-memory result"

    summary = result.redaction
    assert summary.total_redactions > 0
    assert summary.by_kind.get("anthropic_api_key", 0) >= 1
    assert summary.by_kind.get("github_token", 0) >= 1
    assert summary.by_kind.get(f"env:{SECRET_ENV_NAME}", 0) >= 1
    # the chunk-split OpenAI key is caught when the step output is assembled
    assert summary.by_kind.get("openai_api_key", 0) >= 1

    text = result.steps["main"].outputs["text"]
    assert text.count("[REDACTED:") >= 3
    assert "[REDACTED:openai_api_key]" in text
