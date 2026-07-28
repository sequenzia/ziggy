"""Environment diagnostics (REQ-014, phase2-contracts '## doctor checks').

``run_checks`` produces an ordered list of :class:`CheckResult` rows, each
``{name, status: pass|fail|warn|skip, detail, hint}``. Agent-scoped checks
carry the agent name as a ``:<agent>`` suffix. The default scope is the v0.1
builtins; ``--agent X`` narrows to one agent, ``--all`` widens to every
registered agent.

The live handshake check launches the *registered* command via
:class:`ziggy.acp.AgentProcessClient` (launch + ``initialize`` + clean
shutdown) and never downloads anything: builtin commands stay behind
``npx --no-install`` and command resolvability is probed with
``shutil.which`` only. ``api_key_env`` is checked for presence only — the
value is never read into a message or printed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ziggy.acp import (
    AgentProcessClient,
    FsReadRequestN,
    FsWriteRequestN,
    HandshakeInfo,
    PermissionReply,
    PermissionRequestN,
    PolicyDenied,
    TerminalReply,
    TerminalRequestN,
    UnsupportedByPolicy,
)
from ziggy.agents import INSTALL_HINTS, KNOWN_DEGRADATIONS, AgentRegistry
from ziggy.config import ResolvedConfig, ZiggyConfig, load_config
from ziggy.engine import compose_child_env
from ziggy.errors import ConfigError, PersistenceError, ZiggyError
from ziggy.models.agent import AgentConfig
from ziggy.store import RunStore

#: Ceiling for one live ``initialize`` round-trip (launch latency included).
HANDSHAKE_TIMEOUT_SECONDS = 20.0

PASS = "pass"
FAIL = "fail"
WARN = "warn"
SKIP = "skip"

#: Global checks that require a loaded configuration (skipped when it fails).
_CONFIG_DEPENDENT_CHECKS: tuple[str, ...] = (
    "store-writable",
    "index-integrity",
    "orchestrator-planning-eligibility",
    "trusted-workflow-hashes",
    "server-readiness",
)


@dataclass(slots=True)
class CheckResult:
    """One doctor check outcome."""

    name: str
    status: str  # pass | fail | warn | skip
    detail: str
    hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "hint": self.hint}


class _ProbeHooks:
    """Deny-everything MediationHooks for the handshake probe.

    No prompt turn is ever started, so none of these should fire; if a
    misbehaving agent calls them anyway, everything fails toward denial.
    """

    async def on_event(self, ev: object) -> None:
        return None

    async def resolve_permission(self, req: PermissionRequestN) -> PermissionReply:
        return PermissionReply(kind="cancelled")

    async def read_text_file(self, req: FsReadRequestN) -> str:
        raise PolicyDenied("doctor probe does not serve file reads")

    async def write_text_file(self, req: FsWriteRequestN) -> None:
        raise PolicyDenied("doctor probe does not serve file writes")

    async def handle_terminal(self, req: TerminalRequestN) -> TerminalReply:
        raise UnsupportedByPolicy("doctor probe does not serve terminals")


# ------------------------------------------------------------------ config


def _config_checks(
    workspace: Path, env_map: Mapping[str, str]
) -> tuple[ResolvedConfig | None, list[CheckResult]]:
    try:
        resolved = load_config(workspace, env=env_map)
    except ZiggyError as exc:
        try:
            load_config(None, env=env_map)  # does user scope alone load?
        except ZiggyError:
            return None, [
                CheckResult(
                    "config-load",
                    FAIL,
                    exc.message,
                    hint="fix the user-scope config (see the path in the message) and re-run",
                ),
                CheckResult("config-forbidden-project-keys", SKIP, "configuration unavailable"),
            ]
        if "forbidden in project scope" in exc.message:
            return None, [
                CheckResult("config-load", PASS, "user-scope configuration is valid"),
                CheckResult(
                    "config-forbidden-project-keys",
                    FAIL,
                    exc.message,
                    hint="remove user-only settings from <workspace>/.ziggy/config.toml",
                ),
            ]
        return None, [
            CheckResult(
                "config-load",
                FAIL,
                exc.message,
                hint="fix <workspace>/.ziggy/config.toml (user scope loads cleanly)",
            ),
            CheckResult("config-forbidden-project-keys", SKIP, "configuration unavailable"),
        ]
    return resolved, [
        CheckResult("config-load", PASS, f"fingerprint {resolved.fingerprint[:12]}…"),
        CheckResult("config-forbidden-project-keys", PASS, "no forbidden project-scope settings"),
    ]


# ------------------------------------------------------------------- store


def _probe_writable(directory: Path) -> None:
    probe = directory / ".doctor-probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()


def _store_writable(store: RunStore) -> CheckResult:
    try:
        _probe_writable(store.runs_dir)
    except (OSError, PersistenceError) as exc:
        return CheckResult(
            "store-writable",
            FAIL,
            f"cannot write under {store.root}: {exc}",
            hint=f"check ownership/permissions of {store.root}",
        )
    return CheckResult("store-writable", PASS, f"{store.root} is writable")


def _index_integrity(store: RunStore) -> CheckResult:
    db_path = store.root / "runs" / "index.db"
    if not db_path.is_file():
        return CheckResult("index-integrity", SKIP, "index not created yet (no persisted runs)")
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return CheckResult(
            "index-integrity",
            FAIL,
            f"cannot check {db_path}: {exc}",
            hint="run 'ziggy runs reindex' to rebuild the derived index",
        )
    verdict = str(row[0]) if row else "no result"
    if verdict == "ok":
        return CheckResult("index-integrity", PASS, "PRAGMA integrity_check: ok")
    return CheckResult(
        "index-integrity",
        FAIL,
        f"PRAGMA integrity_check: {verdict}",
        hint="run 'ziggy runs reindex' to rebuild the derived index",
    )


# ------------------------------------------------------------------ agents


def _agent_hint(agent: AgentConfig) -> str:
    builtin_hint = INSTALL_HINTS.get(agent.name)
    if agent.builtin and builtin_hint:
        return f"install the pinned adapter: {builtin_hint}"
    return "install the agent adapter and ensure its command is on PATH"


def _command_resolvable(agent: AgentConfig) -> CheckResult:
    name = f"agent-command-resolvable:{agent.name}"
    command = agent.command
    if os.path.isabs(command):
        resolved = command if os.path.isfile(command) and os.access(command, os.X_OK) else None
    else:
        resolved = shutil.which(command)
    if resolved is None:
        return CheckResult(
            name, FAIL, f"command {command!r} is not resolvable/executable", hint=_agent_hint(agent)
        )
    return CheckResult(name, PASS, f"{command!r} -> {resolved} (nothing downloaded)")


def _api_key_env(agent: AgentConfig, env_map: Mapping[str, str]) -> CheckResult:
    name = f"api-key-env-set:{agent.name}"
    if agent.api_key_env is None:
        return CheckResult(name, SKIP, "no api_key_env configured (agent-managed login)")
    if env_map.get(agent.api_key_env):
        return CheckResult(name, PASS, f"{agent.api_key_env} is set (value not read)")
    return CheckResult(
        name,
        FAIL,
        f"{agent.api_key_env} is not set",
        hint=f"export {agent.api_key_env} in the environment that runs ziggy",
    )


async def _probe_handshake(agent: AgentConfig, env: dict[str, str], cwd: str) -> HandshakeInfo:
    client = await AgentProcessClient.launch(
        command=agent.command, args=list(agent.args), env=env, cwd=cwd, hooks=_ProbeHooks()
    )
    try:
        return await asyncio.wait_for(client.initialize(), timeout=HANDSHAKE_TIMEOUT_SECONDS)
    finally:
        await client.shutdown(1.0)


def _acp_handshake(
    agent: AgentConfig,
    workspace: Path,
    env_map: Mapping[str, str],
    *,
    command_ok: bool,
) -> tuple[CheckResult, HandshakeInfo | None]:
    name = f"acp-handshake:{agent.name}"
    if not command_ok:
        return CheckResult(name, SKIP, "agent command is not resolvable"), None
    try:
        child_env, _secrets = compose_child_env(agent, env_map)
    except ConfigError as exc:
        return CheckResult(name, FAIL, exc.message, hint=_agent_hint(agent)), None
    try:
        handshake = asyncio.run(_probe_handshake(agent, child_env, str(workspace)))
    except TimeoutError:
        return (
            CheckResult(
                name,
                FAIL,
                f"handshake timed out after {HANDSHAKE_TIMEOUT_SECONDS:.0f}s",
                hint=_agent_hint(agent),
            ),
            None,
        )
    except ZiggyError as exc:
        return CheckResult(name, FAIL, f"{exc.code}: {exc.message}", hint=_agent_hint(agent)), None
    detail = (
        f"protocol v{handshake.protocol_version}; agent {handshake.agent_name}"
        + (f" {handshake.agent_version}" if handshake.agent_version else "")
        + "; clean shutdown"
    )
    return CheckResult(name, PASS, detail), handshake


def _capability_summary(agent: AgentConfig, handshake: HandshakeInfo | None) -> CheckResult:
    name = f"capability-summary:{agent.name}"
    if handshake is None:
        return CheckResult(name, SKIP, "no handshake result to summarize")
    if not handshake.capabilities:
        return CheckResult(name, PASS, "no capabilities reported")
    summary = ", ".join(
        f"{key}={json.dumps(value, separators=(',', ':'), sort_keys=True)}"
        for key, value in sorted(handshake.capabilities.items())
    )
    return CheckResult(name, PASS, summary)


def _direct_tools_advisory(agent: AgentConfig) -> CheckResult:
    name = f"direct-tools-advisory:{agent.name}"
    degradations = KNOWN_DEGRADATIONS.get(agent.name, [])
    extra = f"; known degradations: {', '.join(degradations)}" if degradations else ""
    if agent.direct_tools_assumed:
        return CheckResult(
            name,
            WARN,
            "agent is assumed to run direct (non-ACP) local tools; guarded mediation "
            "is advisory, not OS-enforced" + extra,
            hint="run the agent in a separately verified OS sandbox for hard enforcement",
        )
    return CheckResult(name, PASS, "no direct local tools assumed" + extra)


# ------------------------------------------------------- orchestrator/server


def _orchestrator_check(config: ZiggyConfig, registry: AgentRegistry) -> CheckResult:
    name = "orchestrator-planning-eligibility"
    planner = config.orchestrator.agent
    if planner is None:
        return CheckResult(name, SKIP, "no orchestrator agent configured")
    if config.orchestrator.allow_uncontained_planner:
        return CheckResult(
            name,
            PASS,
            f"planner '{planner}' acknowledged as uncontained by trusted user config",
        )
    if registry.get(planner).direct_tools_assumed:
        return CheckResult(
            name,
            WARN,
            f"planner '{planner}' is assumed uncontained (direct tools); planning "
            "requires the orchestrator.allow_uncontained_planner acknowledgement",
            hint="set [orchestrator] allow_uncontained_planner = true in user config",
        )
    return CheckResult(name, PASS, f"planner '{planner}' is planning-eligible by default")


def _trusted_workflows(config: ZiggyConfig) -> CheckResult:
    name = "trusted-workflow-hashes"
    entries = config.orchestrator.trusted_workflows
    if not entries:
        return CheckResult(name, SKIP, "no trusted workflows configured")
    problems: list[str] = []
    for entry in entries:
        path = Path(entry.path).expanduser()
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            problems.append(f"{entry.path}: unreadable/missing")
            continue
        if digest != entry.sha256.lower():
            problems.append(f"{entry.path}: sha256 mismatch")
    if problems:
        return CheckResult(
            name,
            FAIL,
            "; ".join(problems),
            hint="update [orchestrator] trusted_workflows path/sha256 entries",
        )
    noun = "entry" if len(entries) == 1 else "entries"
    return CheckResult(name, PASS, f"{len(entries)} {noun} verified")


def _server_readiness(config: ZiggyConfig, registry: AgentRegistry, store: RunStore) -> CheckResult:
    name = "server-readiness"
    try:
        _probe_writable(store.leases_dir)
    except (OSError, PersistenceError) as exc:
        return CheckResult(
            name,
            FAIL,
            f"workspace-lease directory is not writable: {exc}",
            hint=f"check ownership/permissions of {store.root}",
        )
    routes = len(registry.names())
    return CheckResult(
        name,
        PASS,
        f"{routes} agent routes; max_active_runs={config.server.max_active_runs}; "
        "lease directory writable; permission forwarding per current adapter fixture",
    )


# -------------------------------------------------------------- entry point


def _select_agents(registry: AgentRegistry, *, agent: str | None, all_agents: bool) -> list[str]:
    if agent is not None:
        registry.get(agent)  # ConfigError (exit 2) on unknown names
        return [agent]
    if all_agents:
        return registry.names()
    return [name for name in registry.names() if registry.is_builtin(name)]


def run_checks(
    *,
    workspace: Path,
    agent: str | None = None,
    all_agents: bool = False,
    env: Mapping[str, str] | None = None,
) -> list[CheckResult]:
    """Run the REQ-014 check list; raises ``ConfigError`` for an unknown
    ``--agent`` name (a usage error, not a check failure)."""
    env_map: Mapping[str, str] = os.environ if env is None else env
    resolved, checks = _config_checks(workspace, env_map)
    if resolved is None:
        checks.extend(
            CheckResult(name, SKIP, "configuration unavailable")
            for name in _CONFIG_DEPENDENT_CHECKS
        )
        return checks
    config = resolved.config
    registry = AgentRegistry.from_config(resolved)
    agent_names = _select_agents(registry, agent=agent, all_agents=all_agents)
    store_root = (
        Path(config.results.store_path).expanduser()
        if config.results.store_path is not None
        else None
    )
    store = RunStore(store_root)

    checks.append(_store_writable(store))
    checks.append(_index_integrity(store))
    for name in agent_names:
        agent_config = registry.get(name)
        resolvable = _command_resolvable(agent_config)
        checks.append(resolvable)
        checks.append(_api_key_env(agent_config, env_map))
        handshake_check, handshake = _acp_handshake(
            agent_config, workspace, env_map, command_ok=resolvable.status == PASS
        )
        checks.append(handshake_check)
        checks.append(_capability_summary(agent_config, handshake))
        checks.append(_direct_tools_advisory(agent_config))
    checks.append(_orchestrator_check(config, registry))
    checks.append(_trusted_workflows(config))
    checks.append(_server_readiness(config, registry, store))
    return checks
