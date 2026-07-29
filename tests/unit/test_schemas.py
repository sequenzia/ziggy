"""Shipped JSON Schema artifacts (REQ-005, FIX #27).

Ziggy ships a versioned JSON Schema for the two durable audit documents
(``result.json`` and each ``events.jsonl`` line). These tests pin two
guarantees: the committed ``.v1.json`` files are byte-identical to a fresh
regeneration (drift fails here, not in some downstream consumer), and a golden
``result.json`` conforms to the shipped schema.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from typer.testing import CliRunner

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ziggy.cli.main import app  # noqa: E402
from ziggy.ids import new_run_id, utc_now  # noqa: E402
from ziggy.models.common import RunKind, RunStatus, StepStatus  # noqa: E402
from ziggy.models.events import EventEnvelope  # noqa: E402
from ziggy.models.result import RunResult, StepResult  # noqa: E402
from ziggy.schemas import SCHEMA_DIR, SCHEMA_FILES, generate_schemas, schema_text  # noqa: E402

runner = CliRunner()


def _golden_result() -> RunResult:
    ts = utc_now().isoformat().replace("+00:00", "Z")
    return RunResult(
        run_id=new_run_id(),
        kind=RunKind.AGENT,
        target="mock-hello",
        status=RunStatus.SUCCESS,
        started_at=ts,
        ended_at=ts,
        duration_ms=7,
        workspace="/tmp/ws",
        steps={
            "main": StepResult(
                step_id="main",
                agent="mock-hello",
                status=StepStatus.SUCCESS,
                outputs={"text": "hello"},
            )
        },
    )


class TestSchemaArtifactsAreCommittedAndCurrent:
    def test_committed_files_exist(self) -> None:
        for name in SCHEMA_FILES:
            assert (SCHEMA_DIR / name).is_file(), f"missing shipped schema {name}"

    def test_regeneration_byte_matches_committed(self) -> None:
        """Regenerating the schema files must byte-match the committed artifacts
        (the source of truth is the pydantic model; drift is a bug)."""
        for name, text in generate_schemas().items():
            committed = (SCHEMA_DIR / name).read_text(encoding="utf-8")
            assert committed == text, f"{name} is stale; run `ziggy schemas dump`"

    def test_schemas_are_valid_json_schema_objects(self) -> None:
        for name in SCHEMA_FILES:
            schema = json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))
            assert schema["type"] == "object"
            assert isinstance(schema.get("properties"), dict)
            assert isinstance(schema.get("required"), list)


class TestGoldenDocumentsValidate:
    def test_golden_result_conforms_to_shipped_schema(self) -> None:
        """A golden result.json satisfies the shipped schema: it round-trips
        through the model the schema is generated from, and every property the
        shipped schema marks ``required`` is present."""
        golden = _golden_result().model_dump(mode="json")
        # Round-trips through the exact model the schema mirrors.
        RunResult.model_validate(golden)
        schema = json.loads((SCHEMA_DIR / "result.v1.json").read_text(encoding="utf-8"))
        for field in schema["required"]:
            assert field in golden, f"golden result.json missing required field {field!r}"
        assert schema["additionalProperties"] is False
        assert set(golden) <= set(schema["properties"]), "golden has fields absent from the schema"

    def test_golden_event_conforms_to_shipped_schema(self) -> None:
        envelope = EventEnvelope(
            seq=1,
            ts=utc_now().isoformat().replace("+00:00", "Z"),
            monotonic_offset_ms=0,
            run_id=new_run_id(),
            event_type="step_started",
        ).model_dump(mode="json")
        EventEnvelope.model_validate(envelope)
        schema = json.loads((SCHEMA_DIR / "events.v1.json").read_text(encoding="utf-8"))
        for field in schema["required"]:
            assert field in envelope, f"golden event missing required field {field!r}"
        assert set(envelope) <= set(schema["properties"])


class TestSchemasDumpCli:
    def test_dump_writes_byte_matching_files(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["schemas", "dump", "--out", str(tmp_path)])
        assert result.exit_code == 0, result.stderr
        for name, model in SCHEMA_FILES.items():
            written = (tmp_path / name).read_text(encoding="utf-8")
            assert written == schema_text(model)
            # Identical to what ships in the package.
            assert written == (SCHEMA_DIR / name).read_text(encoding="utf-8")
