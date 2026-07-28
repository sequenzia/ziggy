"""Ziggy CLI (REQ-003/014/015): the typer command surface.

Command semantics per spec §5.2: headless one-shot ``run``, workflow execution
and discovery under ``workflow``, run browsing under ``runs``, config
inspection under ``config``, ``agents list``, and ``doctor``.
Exit codes: 0 success; 1 execution or required-persistence failure; 2
usage/config/trust errors (typer usage errors included); 130 user
cancellation. Under ``--json`` stdout carries ONLY the machine-readable
document (final RunResult for ``run``); progress and diagnostics go to stderr.

SIGINT during ``ziggy run``: the first Ctrl-C sets the engine's cancel event
(clean teardown ladder, terminal state ``cancelled``, exit 130); a second
Ctrl-C hard-exits immediately with ``os._exit(130)``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import stat
import sys
from collections.abc import Callable, Coroutine
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from ziggy.agents import AgentRegistry
from ziggy.cli.doctor import run_checks
from ziggy.cli.render import RunRenderer, format_table, run_show_lines, use_plain
from ziggy.config import ResolvedConfig, ZiggyConfig, load_config
from ziggy.engine import RunOverrides, execute_run, prepare_run
from ziggy.engine.prepare import prepare_workflow
from ziggy.errors import ERROR_CLASSES, PersistenceError, ZiggyError
from ziggy.ids import is_run_id, utc_now
from ziggy.models.common import CaptureProfile, RunStatus
from ziggy.models.result import RunResult
from ziggy.models.workflow import WorkflowDef
from ziggy.store import IndexRow, RunIndex, RunStore
from ziggy.workflows import discover, parse_var_args
from ziggy.workflows.runner import execute_workflow

app = typer.Typer(
    name="ziggy",
    help="Local execution, orchestration, and audit harness for ACP agents.",
    no_args_is_help=True,
)
agents_app = typer.Typer(help="Registered agents.", no_args_is_help=True)
runs_app = typer.Typer(help="Browse and maintain persisted runs.", no_args_is_help=True)
config_app = typer.Typer(help="Inspect and validate configuration.", no_args_is_help=True)
workflow_app = typer.Typer(help="Run and inspect constrained workflows.", no_args_is_help=True)
app.add_typer(agents_app, name="agents")
app.add_typer(runs_app, name="runs")
app.add_typer(config_app, name="config")
app.add_typer(workflow_app, name="workflow")

_SINCE_DAYS_RE = re.compile(r"^(\d+)d$")


# ------------------------------------------------------------------ helpers


def _fail(exc: ZiggyError) -> NoReturn:
    """Report a typed error on stderr and exit with its mapped code."""
    print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
    raise typer.Exit(exc.exit_code)


def _load_config(workspace: Path) -> ResolvedConfig:
    try:
        resolved = load_config(workspace)
    except ZiggyError as exc:
        _fail(exc)
    for warning in resolved.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return resolved


def _store_for(config: ZiggyConfig) -> RunStore:
    root = (
        Path(config.results.store_path).expanduser()
        if config.results.store_path is not None
        else None
    )
    return RunStore(root)


def _index_db_path(store: RunStore) -> Path:
    """Index path without creating store directories as a side effect."""
    return store.root / "runs" / "index.db"


def exit_code_for(result: RunResult) -> int:
    """Terminal RunResult -> REQ-003 exit code.

    Cancellation wins (130); then the first run-level typed error's mapped
    exit code (e.g. a required-persistence failure on an otherwise successful
    run); then 0 for success and 1 for any other terminal status.
    """
    if result.status is RunStatus.CANCELLED:
        return 130
    for error in result.errors:
        error_class = ERROR_CLASSES.get(error.code)
        return error_class.exit_code if error_class is not None else 1
    return 0 if result.status is RunStatus.SUCCESS else 1


async def _execute_with_sigint(
    start: Callable[[asyncio.Event], Coroutine[Any, Any, RunResult]],
) -> RunResult:
    """Run one engine coroutine with SIGINT -> cancel_event; a second SIGINT
    hard-exits. ``start`` receives the cancel event and returns the run
    coroutine (``execute_run`` for direct runs, ``execute_workflow`` for
    workflows)."""
    cancel_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    sigints = 0

    def on_sigint() -> None:
        nonlocal sigints
        sigints += 1
        if sigints >= 2:
            os._exit(130)  # second Ctrl-C: skip teardown entirely
        cancel_event.set()

    installed = False
    try:
        loop.add_signal_handler(signal.SIGINT, on_sigint)
        installed = True
    except (NotImplementedError, RuntimeError, ValueError):
        pass  # unsupported platform/thread: run without cancel-on-SIGINT
    try:
        return await start(cancel_event)
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGINT)


def _parse_provider_flag(value: str | None) -> list[str] | None:
    """``--acknowledge-egress p1,p2`` -> provider list (None when not given)."""
    if value is None:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


# ---------------------------------------------------------------------- run


@app.command()
def run(
    agent: Annotated[str, typer.Argument(help="Registered agent name.")],
    prompt: Annotated[str, typer.Argument(help="One-shot prompt text.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="stdout carries only the final RunResult JSON."),
    ] = False,
    no_save: Annotated[bool, typer.Option("--no-save", help="Do not persist this run.")] = False,
    capture: Annotated[
        CaptureProfile | None,
        typer.Option("--capture", help="Capture profile override (direct user intent)."),
    ] = None,
    plain: Annotated[
        bool, typer.Option("--plain", help="Line-oriented output without ANSI escapes.")
    ] = False,
    timeout: Annotated[
        float | None,
        typer.Option(
            "--timeout",
            min=0.0,
            help="Step timeout in seconds (may only lower the configured ceiling).",
        ),
    ] = None,
    acknowledge_egress: Annotated[
        str | None,
        typer.Option(
            "--acknowledge-egress",
            help="Comma-separated provider set whose egress is acknowledged.",
        ),
    ] = None,
) -> None:
    """One-shot headless run against a named agent."""
    workspace = Path.cwd()
    resolved = _load_config(workspace)
    overrides = RunOverrides(
        no_save=no_save,
        capture=capture,
        timeout_seconds=timeout,
        acknowledge_egress=_parse_provider_flag(acknowledge_egress),
    )
    try:
        prepared = prepare_run(
            resolved,
            agent_name=agent,
            prompt=prompt,
            workspace=workspace,
            overrides=overrides,
        )
    except ZiggyError as exc:
        _fail(exc)
    progress = sys.stderr if json_output else sys.stdout
    renderer = RunRenderer(progress, plain=use_plain(progress, plain_flag=plain))
    try:
        result = asyncio.run(
            _execute_with_sigint(
                lambda cancel_event: execute_run(
                    prepared.spec, render_cb=renderer.on_event, cancel_event=cancel_event
                )
            )
        )
    finally:
        renderer.close()
        prepared.logger.close()
    renderer.finish(result)
    if json_output:
        sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    raise typer.Exit(exit_code_for(result))


# ---------------------------------------------------------------- workflow


@workflow_app.command("run")
def workflow_run(
    name_or_path: Annotated[
        str, typer.Argument(help="Discovered workflow name, or a direct YAML path.")
    ],
    var: Annotated[
        list[str] | None,
        typer.Option("--var", help="Typed workflow variable NAME=VALUE (repeatable)."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="stdout carries only the final RunResult JSON."),
    ] = False,
    no_save: Annotated[bool, typer.Option("--no-save", help="Do not persist this run.")] = False,
    capture: Annotated[
        CaptureProfile | None,
        typer.Option("--capture", help="Capture profile override (direct user intent)."),
    ] = None,
    plain: Annotated[
        bool, typer.Option("--plain", help="Line-oriented output without ANSI escapes.")
    ] = False,
    acknowledge_egress: Annotated[
        str | None,
        typer.Option(
            "--acknowledge-egress",
            help="Comma-separated provider set whose egress is acknowledged.",
        ),
    ] = None,
) -> None:
    """Execute one constrained workflow serially in deterministic order."""
    workspace = Path.cwd()
    resolved = _load_config(workspace)
    overrides = RunOverrides(
        no_save=no_save,
        capture=capture,
        acknowledge_egress=_parse_provider_flag(acknowledge_egress),
    )
    try:
        cli_vars = parse_var_args(var or [])
        prepared = prepare_workflow(
            resolved,
            name_or_path=name_or_path,
            cli_vars=cli_vars,
            workspace=workspace,
            overrides=overrides,
        )
    except ZiggyError as exc:
        _fail(exc)
    progress = sys.stderr if json_output else sys.stdout
    renderer = RunRenderer(progress, plain=use_plain(progress, plain_flag=plain))
    try:
        result = asyncio.run(
            _execute_with_sigint(
                lambda cancel_event: execute_workflow(
                    prepared, render_cb=renderer.on_event, cancel_event=cancel_event
                )
            )
        )
    finally:
        renderer.close()
        if prepared.logger is not None:
            prepared.logger.close()
    renderer.finish(result)
    if json_output:
        sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    raise typer.Exit(exit_code_for(result))


def _variables_summary(definition: WorkflowDef) -> str:
    """Compact declared-variable summary for the workflow list table."""
    if not definition.variables:
        return "-"
    parts: list[str] = []
    for name, decl in definition.variables.items():
        marker = "*" if decl.required else ""
        secret = " (secret)" if decl.secret else ""
        parts.append(f"{name}:{decl.type}{marker}{secret}")
    return ", ".join(parts)


@workflow_app.command("list")
def workflow_list(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit discovered workflows as JSON.")
    ] = False,
) -> None:
    """Discovered workflows (project scope, then user scope)."""
    try:
        found = discover(Path.cwd())
    except ZiggyError as exc:
        _fail(exc)  # duplicate names exit 2 naming both paths
    entries = sorted(found.values(), key=lambda wf: wf.name)
    if json_output:
        document = [
            {
                "name": wf.name,
                "scope": wf.source_scope,
                "path": str(wf.path),
                "description": wf.definition.description,
                "variables": {
                    name: decl.model_dump(mode="json")
                    for name, decl in wf.definition.variables.items()
                },
            }
            for wf in entries
        ]
        print(json.dumps(document, indent=2))
        return
    if not entries:
        print("no workflows found")
        return
    rows = [
        (
            wf.name,
            wf.source_scope,
            str(wf.path),
            wf.definition.description or "-",
            _variables_summary(wf.definition),
        )
        for wf in entries
    ]
    for line in format_table(("name", "scope", "path", "description", "variables"), rows):
        print(line)


# ------------------------------------------------------------------- agents


def _last_capabilities(store: RunStore, agent_name: str) -> str:
    """Capability summary from the newest persisted run of this agent, or '-'."""
    db_path = _index_db_path(store)
    if not db_path.is_file():
        return "-"
    try:
        with RunIndex(db_path) as index:
            rows = index.list_runs(kind="agent", agent_target=agent_name, limit=1)
        if not rows:
            return "-"
        data = store.read_result(rows[0].run_id)
    except PersistenceError:
        return "-"
    steps = data.get("steps")
    if not isinstance(steps, dict):
        return "-"
    for step in steps.values():
        info = step.get("agent_info") if isinstance(step, dict) else None
        capabilities = info.get("capabilities") if isinstance(info, dict) else None
        if isinstance(capabilities, dict) and capabilities:
            return ", ".join(
                f"{key}={json.dumps(value, separators=(',', ':'), sort_keys=True)}"
                for key, value in sorted(capabilities.items())
            )
    return "-"


@agents_app.command("list")
def agents_list() -> None:
    """Registered agents with the capability summary from their last handshake."""
    resolved = _load_config(Path.cwd())
    registry = AgentRegistry.from_config(resolved)
    store = _store_for(resolved.config)
    rows = [
        (
            entry.name,
            "yes" if entry.builtin else "no",
            entry.provider or "-",
            " ".join([entry.command, *entry.args]),
            "yes" if entry.orchestration_eligible else "no",
            _last_capabilities(store, entry.name),
        )
        for entry in registry.list()
    ]
    headers = ("name", "builtin", "provider", "command", "orchestration", "capabilities")
    for line in format_table(headers, rows):
        print(line)


# --------------------------------------------------------------------- runs


def _parse_since(value: str) -> str:
    """--since accepts ISO-8601 (date or datetime) or a relative '<N>d'."""
    text = value.strip()
    match = _SINCE_DAYS_RE.match(text)
    if match:
        cutoff = utc_now() - timedelta(days=int(match.group(1)))
        return cutoff.isoformat().replace("+00:00", "Z")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise typer.BadParameter(
            "expected an ISO-8601 date/datetime or '<N>d' (e.g. 7d)", param_hint="--since"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


@runs_app.command("list")
def runs_list(
    failed: Annotated[
        bool,
        typer.Option("--failed", help="Only runs that ended without completing all work."),
    ] = False,
    kind: Annotated[
        str | None, typer.Option("--kind", help="Filter by run kind (agent|workflow|...).")
    ] = None,
    agent: Annotated[
        str | None, typer.Option("--agent", help="Filter by agent/workflow target name.")
    ] = None,
    since: Annotated[
        str | None, typer.Option("--since", help="ISO-8601 date/datetime or '<N>d'.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit rows as JSON.")] = False,
) -> None:
    """List persisted runs from the derived SQLite index (newest first)."""
    resolved = _load_config(Path.cwd())
    store = _store_for(resolved.config)
    since_iso = _parse_since(since) if since is not None else None
    rows: list[IndexRow] = []
    db_path = _index_db_path(store)
    if db_path.is_file():
        try:
            with RunIndex(db_path) as index:
                rows = index.list_runs(
                    failed_only=failed, kind=kind, agent_target=agent, since_iso=since_iso
                )
        except ZiggyError as exc:
            _fail(exc)
    if json_output:
        print(json.dumps([asdict(row) for row in rows], indent=2))
        return
    if not rows:
        print("no runs recorded")
        return
    table_rows = [
        (
            row.run_id,
            row.kind,
            row.target,
            row.status,
            row.started_at,
            f"{row.duration_ms} ms" if row.duration_ms is not None else "-",
        )
        for row in rows
    ]
    headers = ("run-id", "kind", "target", "status", "started-at", "duration")
    for line in format_table(headers, table_rows):
        print(line)


@runs_app.command("show")
def runs_show(
    run_id: Annotated[str, typer.Argument(help="Run id (ULID).")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the raw result.json manifest.")
    ] = False,
) -> None:
    """Inspect one persisted RunResult manifest (REQ-015 detail view)."""
    resolved = _load_config(Path.cwd())
    store = _store_for(resolved.config)
    try:
        data = store.read_result(run_id)
    except ZiggyError as exc:
        _fail(exc)
    if json_output:
        print(json.dumps(data, indent=2))
        return
    for line in run_show_lines(data):
        print(line)


@runs_app.command("reindex")
def runs_reindex() -> None:
    """Rebuild the derived index from durable result.json manifests."""
    resolved = _load_config(Path.cwd())
    store = _store_for(resolved.config)
    try:
        with RunIndex(store.index_db_path) as index:
            recovered = index.recover_abandoned(store)
            count = index.reindex(store)
    except ZiggyError as exc:
        _fail(exc)
    if recovered:
        print(f"finalized {len(recovered)} interrupted run(s) as abandoned")
    print(f"indexed {count} run(s)")


def _prune_candidates(store: RunStore, cutoff_iso: str) -> list[str]:
    """ULID-named REAL directories under runs/ whose durable manifest ended
    before the cutoff. Symlinks are never followed (lstat check) and never
    deleted; directories without a durable ``result.json`` (in-flight or
    crashed runs) are never candidates."""
    runs_dir = store.root / "runs"
    if not runs_dir.is_dir():
        return []
    candidates: list[str] = []
    for entry in sorted(runs_dir.iterdir()):
        if not is_run_id(entry.name):
            continue
        try:
            entry_stat = entry.lstat()
        except OSError:
            continue
        if not stat.S_ISDIR(entry_stat.st_mode):
            continue  # symlink or plain file: never followed, never deleted
        try:
            data = store.read_result(entry.name)
        except PersistenceError:
            continue
        ended = data.get("ended_at") or data.get("started_at")
        if isinstance(ended, str) and ended and ended < cutoff_iso:
            candidates.append(entry.name)
    return candidates


def _drop_index_rows(store: RunStore, run_ids: list[str]) -> None:
    db_path = _index_db_path(store)
    if not db_path.is_file() or not run_ids:
        return
    try:
        with RunIndex(db_path) as index:
            for run_id in run_ids:
                index.delete(run_id)
    except PersistenceError as exc:
        print(f"warning: could not update the run index: {exc.message}", file=sys.stderr)


@runs_app.command("prune")
def runs_prune(
    older_than: Annotated[
        int | None,
        typer.Option(
            "--older-than",
            min=0,
            help="Delete runs that ended more than DAYS days ago "
            "(default: results.retention_days).",
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="List candidates without deleting anything.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", help="Actually delete (required; headless-safe default).")
    ] = False,
) -> None:
    """Explicitly delete expired run directories (the only deletion mechanism)."""
    resolved = _load_config(Path.cwd())
    store = _store_for(resolved.config)
    days = older_than if older_than is not None else resolved.config.results.retention_days
    cutoff = (utc_now() - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    candidates = _prune_candidates(store, cutoff)
    if not candidates:
        print(f"no completed runs older than {days} day(s)")
        return
    print(f"{len(candidates)} run(s) older than {days} day(s):")
    for run_id in candidates:
        print(run_id)
    if dry_run:
        print("dry run: nothing deleted")
        return
    if not yes:
        print(
            "refusing to delete without --yes (headless-safe); re-run with --yes to "
            "delete the runs listed above",
            file=sys.stderr,
        )
        raise typer.Exit(2)
    deleted: list[str] = []
    failures = 0
    for run_id in candidates:
        try:
            shutil.rmtree(store.root / "runs" / run_id)
        except OSError as exc:
            failures += 1
            print(f"error deleting {run_id}: {exc}", file=sys.stderr)
        else:
            deleted.append(run_id)
    _drop_index_rows(store, deleted)
    print(f"deleted {len(deleted)} run(s)")
    if failures:
        raise typer.Exit(1)


# ------------------------------------------------------------------- config


@config_app.command("show")
def config_show(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit fields + provenance as JSON.")
    ] = False,
) -> None:
    """Effective merged configuration with per-field source and project action."""
    resolved = _load_config(Path.cwd())
    fields = resolved.describe()
    if json_output:
        document = {
            "fingerprint": resolved.fingerprint,
            "warnings": resolved.warnings,
            "fields": fields,
        }
        print(json.dumps(document, indent=2))
        return
    rows = [
        (
            row["path"],
            json.dumps(row["value"]),
            row["source"],
            row["project_action"],
        )
        for row in fields
    ]
    for line in format_table(("field", "value", "source", "project-action"), rows):
        print(line)
    print(f"fingerprint: {resolved.fingerprint}")


@config_app.command("validate")
def config_validate() -> None:
    """Exit 0 with 'ok', or a path-precise ConfigError with exit 2."""
    _load_config(Path.cwd())
    print("ok")


# ------------------------------------------------------------------- doctor


@app.command()
def doctor(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit check results as JSON.")
    ] = False,
    agent: Annotated[
        str | None, typer.Option("--agent", help="Probe a single registered agent.")
    ] = None,
    all_agents: Annotated[
        bool, typer.Option("--all", help="Include custom agents (default: builtins).")
    ] = False,
) -> None:
    """Environment diagnostics with per-check pass/fail and fix hints (REQ-014)."""
    try:
        checks = run_checks(workspace=Path.cwd(), agent=agent, all_agents=all_agents)
    except ZiggyError as exc:
        _fail(exc)
    ok = all(check.status != "fail" for check in checks)
    if json_output:
        print(json.dumps({"ok": ok, "checks": [check.to_dict() for check in checks]}, indent=2))
    else:
        for check in checks:
            print(f"[{check.status:>4}] {check.name}: {check.detail}")
            if check.hint and check.status in {"fail", "warn"}:
                print(f"       hint: {check.hint}")
        print("doctor: ok" if ok else "doctor: problems found")
    if not ok:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
