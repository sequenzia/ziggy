"""Shipped, versioned JSON Schema artifacts (REQ-005).

Ziggy publishes a stable JSON Schema for the two durable audit documents —
``result.json`` (a :class:`~ziggy.models.result.RunResult`) and each
``events.jsonl`` line (an :class:`~ziggy.models.events.EventEnvelope`) — so
external tooling can validate a run's evidence without importing Ziggy.

The ``.v1.json`` files in this package are GENERATED from the pydantic models
and committed. :func:`schema_text` is the single canonical serializer; the
build ships the files as wheel package data (see ``pyproject.toml``), the
``ziggy schemas dump`` CLI writes them out, and a test asserts the committed
files are byte-identical to a fresh regeneration (drift fails CI).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ziggy.models.events import EventEnvelope
from ziggy.models.result import RunResult

__all__ = [
    "SCHEMA_DIR",
    "SCHEMA_FILES",
    "generate_schemas",
    "schema_text",
    "write_schemas",
]

#: Directory holding the committed ``.v1.json`` artifacts (this package).
SCHEMA_DIR: Path = Path(__file__).resolve().parent

#: Output filename -> pydantic model whose ``model_json_schema()`` it holds.
#: The ``v1`` in each name tracks the document schema_version, not the model
#: shape; a breaking change ships a new ``.v2.json`` beside the old file.
SCHEMA_FILES: dict[str, type[RunResult] | type[EventEnvelope]] = {
    "result.v1.json": RunResult,
    "events.v1.json": EventEnvelope,
}


def schema_text(model: type[RunResult] | type[EventEnvelope]) -> str:
    """Canonical, deterministic JSON text for one model's schema.

    ``sort_keys`` + a trailing newline make the output stable across pydantic
    dict-ordering and editors, so the byte-for-byte regeneration check is
    meaningful.
    """
    schema: dict[str, Any] = model.model_json_schema()
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def generate_schemas() -> dict[str, str]:
    """Map each output filename to its freshly generated canonical text."""
    return {name: schema_text(model) for name, model in SCHEMA_FILES.items()}


def write_schemas(out_dir: Path) -> list[Path]:
    """Write every schema artifact into ``out_dir`` (created if missing)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, text in generate_schemas().items():
        path = out_dir / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written
