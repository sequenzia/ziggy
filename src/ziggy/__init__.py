"""Ziggy — local execution, orchestration, and audit harness for ACP agents."""

try:
    # Generated from git tags by hatch-vcs at build/sync time; not in git.
    from ziggy._version import __version__
except ImportError:  # fresh checkout before the first `uv sync`
    __version__ = "0.0.0"

RESULT_SCHEMA_VERSION = 1
EVENTS_SCHEMA_VERSION = 1
CONFIG_SCHEMA_VERSION = 1
WORKFLOW_SCHEMA_VERSION = 1
INDEX_SCHEMA_VERSION = 1
