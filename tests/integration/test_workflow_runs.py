"""Integration: deterministic workflow state machine against mock agents.

Spec §10.2 "Serial workflow with failure + partial results" + REQ-010/011,
driven end to end through ``prepare_workflow`` -> ``execute_workflow`` with
the raw mock agent (``tests/mocks/raw_agent.py``) under a tmp ``ZIGGY_HOME``:

- 4-step DAG all-success: strictly serial execution in the stable
  topological/declaration order, upstream output threaded into the dependent
  prompt wrapped in the untrusted-input delimiters (proven byte-exact via the
  ``echo_prompt`` scenario).
- Failure propagation: blocked (transitive dependents) vs skipped (other
  pending) vs unchanged (already-successful), run ``partial``, <=1 attempt.
- All-fail -> ``failed``; cancellation mid-step; per-step timeout; workflow
  deadline; composed-prompt ceiling breach mid-workflow.
- Egress: headless unacknowledged crossing fails BEFORE any launch;
  flag/config acknowledgement runs and records lineage + ``egress_notice``.
- Workspace lease: a concurrent second workflow run fails busy without
  launching anything; the lease file is gone after completion.
- Secret workflow variables are exact-match-redacted everywhere persisted.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.mocks import RAW_AGENT_PATH, scenarios  # noqa: E402

from ziggy.config import load_config  # noqa: E402
from ziggy.engine.prepare import RunOverrides, prepare_workflow  # noqa: E402
from ziggy.errors import EgressNotAcknowledgedError  # noqa: E402
from ziggy.models.common import RunKind, RunStatus, StepStatus  # noqa: E402
from ziggy.models.events import EventEnvelope  # noqa: E402
from ziggy.models.result import RunResult  # noqa: E402
from ziggy.workflows.egress import ACK_BY_CONFIG, ACK_BY_FLAG  # noqa: E402
from ziggy.workflows.runner import PreparedWorkflow, execute_workflow  # noqa: E402

pytestmark = pytest.mark.slow

HELLO_TEXT = "".join(scenarios.HELLO_CHUNKS)

#: The contract's exact untrusted-input delimiters (phase-3, workflows/interpolate).
UNTRUSTED_OPEN = '<<<ziggy:untrusted-input name="{name}" source="{source}">>>'
UNTRUSTED_CLOSE = '<<<ziggy:end-untrusted-input name="{name}">>>'


def delimited(name: str, source: str, value: str) -> str:
    """The exact delimiter-wrapped block a step-output input must become."""
    return (
        UNTRUSTED_OPEN.format(name=name, source=source)
        + "\n"
        + value
        + "\n"
        + UNTRUSTED_CLOSE.format(name=name)
    )


def agent_toml(name: str, scenario: str, provider: str | None = None) -> str:
    lines = [
        f"[agents.{name}]",
        f"command = {json.dumps(sys.executable)}",
        f"args = [{json.dumps(str(RAW_AGENT_PATH))}, {json.dumps(scenario)}]",
    ]
    if provider is not None:
        lines.append(f"provider = {json.dumps(provider)}")
    return "\n".join(lines) + "\n"


def config_toml(engine: str = "default_step_timeout_seconds = 30", extra: str = "") -> str:
    """Trusted user config registering every mock agent the suite uses.

    The state-machine agents share provider ``mock`` so plain DAG workflows
    never cross providers; ``prov-plan``/``prov-fix`` carry real provider
    labels for the egress scenarios.
    """
    return (
        "schema_version = 1\n"
        f"[engine]\n{engine}\n"
        + agent_toml("mock-hello", scenarios.HELLO, provider="mock")
        + agent_toml("mock-echo", scenarios.ECHO_PROMPT, provider="mock")
        + agent_toml("mock-crash", scenarios.CRASH_MID_TURN, provider="mock")
        + agent_toml("mock-slow", scenarios.SLOW_STREAM, provider="mock")
        + agent_toml("prov-plan", scenarios.HELLO, provider="anthropic")
        + agent_toml("prov-fix", scenarios.ECHO_PROMPT, provider="openai")
        + extra
    )


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Tmp ZIGGY_HOME (store + user config) and a tmp workspace."""
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (home / "config.toml").write_text(config_toml(), encoding="utf-8")
    monkeypatch.setenv("ZIGGY_HOME", str(home))
    return home, workspace


def write_workflow(workspace: Path, name: str, text: str) -> Path:
    wf_dir = workspace / ".ziggy" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    path = wf_dir / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def prepare(
    home: Path,
    workspace: Path,
    name: str,
    *,
    cli_vars: dict[str, str] | None = None,
    overrides: RunOverrides | None = None,
) -> PreparedWorkflow:
    resolved = load_config(workspace, user_path=home / "config.toml")
    return prepare_workflow(
        resolved,
        name_or_path=name,
        cli_vars=cli_vars or {},
        workspace=workspace,
        overrides=overrides or RunOverrides(),
    )


def read_envelopes(result: RunResult) -> list[EventEnvelope]:
    assert result.events_path is not None
    lines = Path(result.events_path).read_text(encoding="utf-8").splitlines()
    return [EventEnvelope.model_validate_json(line) for line in lines]


def step_markers(envelopes: list[EventEnvelope]) -> list[tuple[str, str | None]]:
    """The (event_type, step_id) sequence of step boundaries, in emit order."""
    return [
        (e.event_type, e.step_id)
        for e in envelopes
        if e.event_type in ("step_started", "step_finished")
    ]


def lease_files(home: Path) -> list[Path]:
    leases = home / "leases"
    return sorted(leases.glob("*.json")) if leases.is_dir() else []


# --------------------------------------------------------------- all-success

DAG_WF = """\
version: 1
name: dag
steps:
  a:
    agent: mock-hello
    prompt: produce the plan
  b:
    agent: mock-echo
    inputs:
      plan: steps.a.outputs.text
    prompt: |
      apply this:
      {{ inputs.plan }}
  c:
    agent: mock-hello
    prompt: independent probe
  d:
    agent: mock-hello
    prompt: after b
    depends_on: [b]
"""


async def test_dag_all_success_serial_order_and_threading(env: tuple[Path, Path]) -> None:
    home, workspace = env
    write_workflow(workspace, "dag", DAG_WF)
    prepared = prepare(home, workspace, "dag")
    # Kahn with declaration-order tie-break: a, then b (declared before c), c, d.
    assert prepared.order == ["a", "b", "c", "d"]

    result = await execute_workflow(prepared)

    assert result.status is RunStatus.SUCCESS
    assert result.kind is RunKind.WORKFLOW
    assert result.target == "dag"
    assert set(result.steps) == {"a", "b", "c", "d"}
    for step_id in ("a", "b", "c", "d"):
        assert result.steps[step_id].status is StepStatus.SUCCESS
        assert len(result.steps[step_id].attempts) == 1

    envelopes = read_envelopes(result)
    assert envelopes[0].event_type == "run_started"
    assert envelopes[0].payload["step_order"] == ["a", "b", "c", "d"]
    assert all(e.run_id == result.run_id for e in envelopes)  # ONE run, one stream
    # Strictly serial: each step opens and closes before the next one starts.
    assert step_markers(envelopes) == [
        ("step_started", "a"),
        ("step_finished", "a"),
        ("step_started", "b"),
        ("step_finished", "b"),
        ("step_started", "c"),
        ("step_finished", "c"),
        ("step_started", "d"),
        ("step_finished", "d"),
    ]

    # Data edge a->b: the upstream output arrives inside the untrusted-input
    # delimiters; the echo agent proves the composed prompt byte-exactly.
    block = delimited("plan", "steps.a.outputs.text", HELLO_TEXT)
    assert result.steps["b"].outputs["text"] == "apply this:\n" + block + "\n"
    assert result.steps["b"].input_sources == {"plan": "steps.a.outputs.text"}
    assert result.steps["b"].inputs_resolved["plan"] == HELLO_TEXT
    assert block in result.steps["b"].inputs_resolved["prompt"]

    # Single-provider workflow: per-step lineage recorded, nothing acknowledged.
    [record] = result.egress
    assert record.step_id == "b"
    assert record.provider == "mock"
    assert record.input_sources == ["steps.a.outputs.text"]
    assert record.acknowledged_by is None

    assert result.persisted is True
    assert lease_files(home) == []  # released after completion


# ------------------------------------------------------- failure propagation

DAG_FAIL_WF = DAG_WF.replace("name: dag", "name: dag-fail").replace(
    "agent: mock-echo", "agent: mock-crash"
)


async def test_failure_blocked_vs_skipped_vs_unchanged(env: tuple[Path, Path]) -> None:
    home, workspace = env
    write_workflow(workspace, "dag-fail", DAG_FAIL_WF)
    result = await execute_workflow(prepare(home, workspace, "dag-fail"))

    steps = result.steps
    assert steps["a"].status is StepStatus.SUCCESS  # already-run: unchanged
    assert steps["b"].status is StepStatus.FAILED
    assert [e.code for e in steps["b"].errors] == ["ProtocolError"]
    assert steps["c"].status is StepStatus.SKIPPED  # pending independent
    assert steps["d"].status is StepStatus.BLOCKED  # transitive dependent
    assert result.status is RunStatus.PARTIAL
    assert result.errors == []  # the failure lives on the step, not the run

    # Every declared step is terminal with at most one attempt; no retries.
    terminal = {
        StepStatus.SUCCESS,
        StepStatus.FAILED,
        StepStatus.BLOCKED,
        StepStatus.SKIPPED,
        StepStatus.CANCELLED,
    }
    for step in steps.values():
        assert step.status in terminal
        assert len(step.attempts) <= 1
    assert len(steps["a"].attempts) == 1
    assert len(steps["b"].attempts) == 1
    assert steps["c"].attempts == [] and steps["d"].attempts == []

    envelopes = read_envelopes(result)
    started = [e.step_id for e in envelopes if e.event_type == "step_started"]
    assert started == ["a", "b"]  # scheduling stopped at the first failure


ALL_FAIL_WF = """\
version: 1
name: all-fail
steps:
  one:
    agent: mock-crash
    prompt: boom
  two:
    agent: mock-hello
    prompt: never reached
"""


async def test_no_success_aggregates_to_failed(env: tuple[Path, Path]) -> None:
    home, workspace = env
    write_workflow(workspace, "all-fail", ALL_FAIL_WF)
    result = await execute_workflow(prepare(home, workspace, "all-fail"))
    assert result.steps["one"].status is StepStatus.FAILED
    assert result.steps["two"].status is StepStatus.SKIPPED
    assert result.status is RunStatus.FAILED


# ------------------------------------------------------------- cancellation

CANCEL_WF = """\
version: 1
name: cancel-mid
steps:
  s1:
    agent: mock-hello
    prompt: warm up
  s2:
    agent: mock-slow
    prompt: stream slowly
    depends_on: [s1]
  s3:
    agent: mock-hello
    prompt: never reached
    depends_on: [s2]
"""


async def test_cancellation_mid_step_two(env: tuple[Path, Path]) -> None:
    home, workspace = env
    write_workflow(workspace, "cancel-mid", CANCEL_WF)
    prepared = prepare(home, workspace, "cancel-mid")
    cancel_event = asyncio.Event()

    def on_event(envelope: EventEnvelope) -> None:
        # Cancel only once step 2 is demonstrably mid-turn (first chunk seen).
        if envelope.event_type == "message_chunk" and envelope.step_id == "s2":
            cancel_event.set()

    result = await execute_workflow(prepared, render_cb=on_event, cancel_event=cancel_event)

    assert result.steps["s1"].status is StepStatus.SUCCESS
    assert result.steps["s2"].status is StepStatus.CANCELLED
    assert [e.code for e in result.steps["s2"].errors] == ["CancelledError"]
    assert result.steps["s3"].status is StepStatus.SKIPPED
    assert result.status is RunStatus.CANCELLED
    assert lease_files(home) == []  # lease released on the cancel teardown


# ---------------------------------------------------------- per-step timeout

STEP_TIMEOUT_WF = """\
version: 1
name: step-timeout
steps:
  slow:
    agent: mock-slow
    prompt: stream slowly
    timeout_seconds: 1
  dependent:
    agent: mock-hello
    inputs:
      text: steps.slow.outputs.text
    prompt: |
      use {{ inputs.text }}
  free:
    agent: mock-hello
    prompt: independent
"""


async def test_per_step_timeout_is_step_failure_not_deadline(env: tuple[Path, Path]) -> None:
    home, workspace = env
    write_workflow(workspace, "step-timeout", STEP_TIMEOUT_WF)
    result = await execute_workflow(prepare(home, workspace, "step-timeout"))

    slow = result.steps["slow"]
    assert slow.status is StepStatus.FAILED
    assert [e.code for e in slow.errors] == ["StepTimeoutError"]
    assert len(slow.attempts) == 1
    assert result.steps["dependent"].status is StepStatus.BLOCKED
    assert result.steps["free"].status is StepStatus.SKIPPED
    assert result.status is RunStatus.FAILED
    assert result.errors == []  # a per-step timeout is NOT the deadline path


# ---------------------------------------------------------- workflow deadline

DEADLINE_WF = """\
version: 1
name: deadline
steps:
  quick:
    agent: mock-hello
    prompt: quick first step
  stall:
    agent: mock-slow
    prompt: stream past the deadline
    depends_on: [quick]
  never:
    agent: mock-hello
    prompt: after stall
    depends_on: [stall]
"""


async def test_workflow_deadline_skips_remainder_with_run_level_timeout(
    env: tuple[Path, Path],
) -> None:
    home, workspace = env
    (home / "config.toml").write_text(
        config_toml("default_step_timeout_seconds = 30\ndefault_workflow_timeout_seconds = 3"),
        encoding="utf-8",
    )
    write_workflow(workspace, "deadline", DEADLINE_WF)
    prepared = prepare(home, workspace, "deadline")
    assert prepared.workflow_timeout_seconds == 3.0

    result = await execute_workflow(prepared)

    assert result.steps["quick"].status is StepStatus.SUCCESS
    # Normally the deadline trips MID-'stall' (its clamped timeout expires at
    # the deadline -> the step's timeout path); on a pathologically slow spawn
    # it can trip in the pre-step check instead, skipping 'stall' outright.
    stall = result.steps["stall"]
    assert stall.status in (StepStatus.FAILED, StepStatus.SKIPPED)
    if stall.status is StepStatus.FAILED:
        assert [e.code for e in stall.errors] == ["StepTimeoutError"]
    assert result.steps["never"].status is StepStatus.SKIPPED
    assert result.status is RunStatus.PARTIAL
    assert [e.code for e in result.errors] == ["StepTimeoutError"]
    assert "deadline" in result.errors[0].message


# ------------------------------------------------- composed-prompt ceiling

CEILING_WF = """\
version: 1
name: ceiling
steps:
  a:
    agent: mock-hello
    prompt: make text
  b:
    agent: mock-echo
    inputs:
      plan: steps.a.outputs.text
    prompt: 'use: {{ inputs.plan }}'
  c:
    agent: mock-hello
    prompt: after b
    depends_on: [b]
"""


async def test_composed_prompt_ceiling_breach_mid_workflow(env: tuple[Path, Path]) -> None:
    """The ceiling is only knowable once upstream output exists (REQ-010)."""
    home, workspace = env
    # 100 bytes: every RAW prompt fits, but b's COMPOSED prompt (upstream
    # output + untrusted delimiters) does not.
    (home / "config.toml").write_text(
        config_toml("default_step_timeout_seconds = 30\nmax_prompt_bytes = 100"),
        encoding="utf-8",
    )
    write_workflow(workspace, "ceiling", CEILING_WF)
    result = await execute_workflow(prepare(home, workspace, "ceiling"))

    assert result.steps["a"].status is StepStatus.SUCCESS
    b = result.steps["b"]
    assert b.status is StepStatus.FAILED
    assert [e.code for e in b.errors] == ["ResourceLimitError"]
    assert b.errors[0].details["max_prompt_bytes"] == 100
    assert b.attempts == []  # failed BEFORE launch: no attempt, no process
    assert result.steps["c"].status is StepStatus.BLOCKED
    assert result.status is RunStatus.PARTIAL

    envelopes = read_envelopes(result)
    launches = [e.step_id for e in envelopes if e.event_type == "agent_launching"]
    assert launches == ["a"]  # b was never launched
    b_errors = [e for e in envelopes if e.event_type == "error" and e.step_id == "b"]
    assert [e.payload["code"] for e in b_errors] == ["ResourceLimitError"]


# ------------------------------------------------------------------- egress

CROSS_WF = """\
version: 1
name: cross
steps:
  plan:
    agent: prov-plan
    prompt: plan the fix
  fix:
    agent: prov-fix
    inputs:
      plan: steps.plan.outputs.text
    prompt: |
      apply:
      {{ inputs.plan }}
"""


async def test_egress_headless_unacknowledged_fails_before_any_launch(
    env: tuple[Path, Path],
) -> None:
    home, workspace = env
    write_workflow(workspace, "cross", CROSS_WF)
    with pytest.raises(EgressNotAcknowledgedError) as exc_info:
        prepare(home, workspace, "cross")
    assert exc_info.value.exit_code == 2
    assert "--acknowledge-egress anthropic,openai" in exc_info.value.message
    # Fails in prepare: no run dir, no events, no lease, nothing launched.
    assert not (home / "runs").exists()
    assert lease_files(home) == []


async def test_egress_flag_acknowledged_runs_with_lineage_and_notice(
    env: tuple[Path, Path],
) -> None:
    home, workspace = env
    write_workflow(workspace, "cross", CROSS_WF)
    prepared = prepare(
        home,
        workspace,
        "cross",
        overrides=RunOverrides(acknowledge_egress=["anthropic", "openai"]),
    )
    result = await execute_workflow(prepared)

    assert result.status is RunStatus.SUCCESS
    [record] = result.egress
    assert record.step_id == "fix"
    assert record.provider == "openai"
    assert record.input_sources == ["steps.plan.outputs.text"]  # input lineage
    assert record.acknowledged_by == ACK_BY_FLAG

    envelopes = read_envelopes(result)
    [notice] = [e for e in envelopes if e.event_type == "egress_notice"]
    assert notice.payload["provider_set"] == ["anthropic", "openai"]
    assert notice.payload["acknowledged_by"] == ACK_BY_FLAG
    assert notice.payload["records"][0]["input_sources"] == ["steps.plan.outputs.text"]
    types = [e.event_type for e in envelopes]
    assert types.index("egress_notice") < types.index("agent_launching")

    # The crossing really happened: anthropic-step output reached the
    # openai step's prompt, wrapped in the untrusted delimiters.
    block = delimited("plan", "steps.plan.outputs.text", HELLO_TEXT)
    assert block in result.steps["fix"].outputs["text"]


async def test_egress_config_acknowledged_records_config(env: tuple[Path, Path]) -> None:
    home, workspace = env
    (home / "config.toml").write_text(
        config_toml(extra='[egress]\nacknowledged_provider_sets = [["openai", "anthropic"]]\n'),
        encoding="utf-8",
    )
    write_workflow(workspace, "cross", CROSS_WF)
    prepared = prepare(home, workspace, "cross")
    assert prepared.egress.acknowledged_by == ACK_BY_CONFIG
    result = await execute_workflow(prepared)
    assert result.status is RunStatus.SUCCESS
    [record] = result.egress
    assert record.acknowledged_by == ACK_BY_CONFIG


# -------------------------------------------------------------------- lease

LOCK_WF = """\
version: 1
name: locker
steps:
  hold:
    agent: mock-slow
    prompt: hold the lease
"""


async def test_lease_blocks_concurrent_workflow_and_releases(env: tuple[Path, Path]) -> None:
    home, workspace = env
    write_workflow(workspace, "locker", LOCK_WF)
    first = prepare(home, workspace, "locker")
    second = prepare(home, workspace, "locker")
    cancel_first = asyncio.Event()
    task = asyncio.create_task(execute_workflow(first, cancel_event=cancel_first))
    try:
        deadline = time.monotonic() + 15.0
        while not lease_files(home):
            assert time.monotonic() < deadline, "first run never acquired the lease"
            await asyncio.sleep(0.05)

        events: list[EventEnvelope] = []
        blocked = await execute_workflow(second, render_cb=events.append)

        assert blocked.errors
        assert blocked.errors[0].code == "WorkspaceBusyError"
        assert blocked.status is RunStatus.FAILED
        assert all(s.status is StepStatus.SKIPPED for s in blocked.steps.values())
        # Nothing launched, no step ever started for the losing run.
        assert not any(e.event_type == "agent_launching" for e in events)
        assert not any(e.event_type == "step_started" for e in events)
        # The winner still holds its lease while the loser was refused.
        [lease_file] = lease_files(home)
        holder = json.loads(lease_file.read_text(encoding="utf-8"))
        assert holder["run_id"] != blocked.run_id
    finally:
        cancel_first.set()
        winner = await task
    assert winner.status is RunStatus.CANCELLED
    assert winner.run_id != blocked.run_id
    assert lease_files(home) == []  # released after completion (file gone)


# ------------------------------------------------------------- secret vars

SECRET_WF = """\
version: 1
name: sekrit
variables:
  token:
    type: string
    required: true
    secret: true
steps:
  use:
    agent: mock-echo
    prompt: |
      credential is {{ vars.token }}
"""

SECRET_VALUE = "wf-secret-c4n4ry-0011223344"


async def test_secret_variable_redacted_everywhere_persisted(env: tuple[Path, Path]) -> None:
    home, workspace = env
    (home / "config.toml").write_text(
        config_toml(extra='[workflows.secret_variable_allowances]\ntoken = ["mock"]\n'),
        encoding="utf-8",
    )
    write_workflow(workspace, "sekrit", SECRET_WF)
    prepared = prepare(home, workspace, "sekrit", cli_vars={"token": SECRET_VALUE})
    assert ("var:token", SECRET_VALUE) in prepared.secret_values

    result = await execute_workflow(prepared)

    assert result.status is RunStatus.SUCCESS
    step = result.steps["use"]
    # The echo agent sent the secret back; it must come out redacted.
    assert "[REDACTED:var:token]" in step.outputs["text"]
    assert "[REDACTED:var:token]" in step.inputs_resolved["prompt"]
    assert SECRET_VALUE not in result.model_dump_json()

    # Exact-match scan of EVERY persisted byte under the store root:
    # events.jsonl, result.json, the SQLite index, metadata logs, leases.
    needle = SECRET_VALUE.encode("utf-8")
    scanned = 0
    for path in home.rglob("*"):
        if path.is_file():
            scanned += 1
            assert needle not in path.read_bytes(), f"secret leaked into {path}"
    assert scanned > 0
    assert result.events_path is not None
    events_text = Path(result.events_path).read_text(encoding="utf-8")
    assert "[REDACTED:var:token]" in events_text
