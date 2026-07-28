"""Phase-1 smoke entry point: ``python -m ziggy run-mock <scenario> <prompt>``.

Not the real CLI — Phase 2 replaces this with the typer app. It runs the
repo's raw mock agent (tests/mocks/raw_agent.py) through the direct-run
engine from the current working directory. ``print`` is acceptable here
because this module *is* the (temporary) CLI surface.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from ziggy.engine import RunSpec, execute_run
from ziggy.models.common import RunStatus

_USAGE = "usage: python -m ziggy run-mock <scenario> <prompt>"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3 or args[0] != "run-mock":
        print(_USAGE, file=sys.stderr)
        return 2
    scenario, prompt = args[1], args[2]
    raw_agent = Path.cwd() / "tests" / "mocks" / "raw_agent.py"
    if not raw_agent.is_file():
        print(
            "run-mock needs the repo mock agents (tests/mocks/raw_agent.py); "
            "run it from the ziggy repository root.",
            file=sys.stderr,
        )
        return 2
    env = {name: value for name in ("PATH", "HOME") if (value := os.environ.get(name))}
    spec = RunSpec(
        agent_name="mock-raw",
        command=sys.executable,
        args=[str(raw_agent), scenario],
        env=env,
        cwd=str(Path.cwd()),
        prompt=prompt,
    )
    result = asyncio.run(execute_run(spec))
    print(f"run {result.run_id}: {result.status.value} ({result.duration_ms} ms)")
    if result.result_path:
        print(f"result: {result.result_path}")
    if result.status is RunStatus.CANCELLED:
        return 130
    if result.status is not RunStatus.SUCCESS:
        return 1
    if any(err.code == "PersistenceError" for err in result.errors):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
