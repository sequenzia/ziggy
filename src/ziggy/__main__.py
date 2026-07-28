"""``python -m ziggy`` — the typer CLI entry point (same app as ``ziggy``).

The Phase-1 ``run-mock`` stand-in is gone: register a mock agent in user
config (``[agents.<name>] command/args``) and use ``ziggy run`` instead.
"""

from __future__ import annotations

from ziggy.cli.main import app

if __name__ == "__main__":
    app()
