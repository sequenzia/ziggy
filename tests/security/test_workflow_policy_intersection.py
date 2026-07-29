"""Serve-time policy re-assertion for mediated filesystem I/O (FIX #11).

``PolicyHooks`` decides a mediated read/write against the agent-supplied path,
then re-canonicalizes at serve time. A symlink or directory component swapped
between the decision and the open must not let the served path drift outside
the predicates the decision proved. These tests pin that the serve path:

1. RE-ASSERTS the decision predicates (containment, sensitive-glob/project
   deny, and — for writes — step-dir membership) on the freshly resolved path,
   denying and never opening a path that flipped out from under the decision;
2. opens the final component with ``O_NOFOLLOW`` so a last-hop symlink swap is
   refused rather than followed.

Both are observable-governance narrowings of the inherent decide/serve window,
not an OS sandbox — but they close the specific swaps a hostile agent can aim.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ziggy.acp import FsReadRequestN, FsWriteRequestN, PolicyDenied
from ziggy.engine.hooks import PolicyHooks
from ziggy.events import RunRecorder
from ziggy.models.common import CaptureProfile
from ziggy.models.events import EventEnvelope
from ziggy.policy import MediationPolicy
from ziggy.redact import Redactor


def _hooks(workspace: Path) -> tuple[PolicyHooks, list[EventEnvelope]]:
    events: list[EventEnvelope] = []
    recorder = RunRecorder(
        run_id="01J000000000000000000000AA",
        store_writer=None,
        redactor=Redactor(),
        capture_profile=CaptureProfile.STANDARD,
        render_cb=events.append,
    )
    policy = MediationPolicy.guarded(workspace=workspace, step_dir=workspace)
    return PolicyHooks(recorder=recorder, policy=policy), events


def _fs_write_decisions(events: list[EventEnvelope]) -> list[EventEnvelope]:
    return [e for e in events if e.event_type == "fs_write"]


async def test_write_denied_when_resolution_flips_to_sensitive_on_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """decide sees an in-step-dir path; serve re-resolves to a deny-globbed one."""
    workspace = Path(os.path.realpath(tmp_path))
    good = workspace / "ok.txt"  # in step-dir, not sensitive -> decide allows
    bad = workspace / ".env"  # matches the **/.env sensitive glob -> serve denies

    calls = {"n": 0}

    def fake_resolve(base: Path | str, candidate: Path | str) -> Path:
        calls["n"] += 1
        return good if calls["n"] == 1 else bad

    # decide_fs_write (mediation) and the serve re-resolve (hooks) share this
    # stateful stand-in: first call is the decision, second is the serve path.
    monkeypatch.setattr("ziggy.policy.mediation.resolve_contained", fake_resolve)
    monkeypatch.setattr("ziggy.engine.hooks.resolve_contained", fake_resolve)

    hooks, events = _hooks(workspace)
    with pytest.raises(PolicyDenied):
        await hooks.write_text_file(FsWriteRequestN(path="ok.txt", content="payload"))

    # The decision resolved once, serve resolved once, and the flipped path was
    # NEVER opened — neither the good nor the sensitive target exists.
    assert calls["n"] == 2
    assert not bad.exists()
    assert not good.exists()
    # The refusal is recorded as a denied fs_write attributed to the re-check,
    # never as an applied FileChange (no ``path`` key on a denial).
    [decided] = _fs_write_decisions(events)
    assert decided.payload["decision"] == "denied"
    assert decided.payload["rule_id"] == "sensitive-path-deny"
    assert "path" not in decided.payload


async def test_write_denied_when_resolution_flips_outside_step_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write whose served path escapes the step working directory is refused."""
    workspace = Path(os.path.realpath(tmp_path))
    step_dir = workspace / "step"
    step_dir.mkdir()
    good = step_dir / "ok.txt"
    outside = workspace / "elsewhere.txt"  # inside workspace, outside step_dir

    calls = {"n": 0}

    def fake_resolve(base: Path | str, candidate: Path | str) -> Path:
        calls["n"] += 1
        return good if calls["n"] == 1 else outside

    monkeypatch.setattr("ziggy.policy.mediation.resolve_contained", fake_resolve)
    monkeypatch.setattr("ziggy.engine.hooks.resolve_contained", fake_resolve)

    recorder = RunRecorder(
        run_id="01J000000000000000000000AA",
        store_writer=None,
        redactor=Redactor(),
        capture_profile=CaptureProfile.STANDARD,
    )
    policy = MediationPolicy.guarded(workspace=workspace, step_dir=step_dir)
    hooks = PolicyHooks(recorder=recorder, policy=policy)

    with pytest.raises(PolicyDenied):
        await hooks.write_text_file(FsWriteRequestN(path="ok.txt", content="payload"))
    assert not outside.exists()
    assert not good.exists()


async def test_write_final_component_symlink_is_refused_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A final-component symlink swapped in at serve time is O_NOFOLLOW-refused."""
    workspace = Path(os.path.realpath(tmp_path))
    target = workspace / "target.txt"
    target.write_text("ORIGINAL", encoding="utf-8")
    evil = workspace / "evil"  # a symlink whose final component must not be followed
    evil.symlink_to(target)

    # Both decide and serve resolve to the symlink path itself (not its target),
    # so the re-check predicates pass and only O_NOFOLLOW can stop the write.
    monkeypatch.setattr("ziggy.policy.mediation.resolve_contained", lambda base, cand: evil)
    monkeypatch.setattr("ziggy.engine.hooks.resolve_contained", lambda base, cand: evil)

    hooks, events = _hooks(workspace)
    with pytest.raises(PolicyDenied):
        await hooks.write_text_file(FsWriteRequestN(path="evil", content="HIJACKED"))

    # The symlink target was never written through: O_NOFOLLOW refused the open.
    assert target.read_text(encoding="utf-8") == "ORIGINAL"
    [decided] = _fs_write_decisions(events)
    assert decided.payload["decision"] == "denied"
    assert decided.payload["rule_id"] == "mediated-io-error"


async def test_read_final_component_symlink_is_refused_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read served through a final-component symlink is O_NOFOLLOW-refused."""
    workspace = Path(os.path.realpath(tmp_path))
    secret = workspace / "secret.txt"
    secret.write_text("TOPSECRET", encoding="utf-8")
    evil = workspace / "peek"
    evil.symlink_to(secret)

    monkeypatch.setattr("ziggy.policy.mediation.resolve_contained", lambda base, cand: evil)
    monkeypatch.setattr("ziggy.engine.hooks.resolve_contained", lambda base, cand: evil)

    recorder = RunRecorder(
        run_id="01J000000000000000000000AA",
        store_writer=None,
        redactor=Redactor(),
        capture_profile=CaptureProfile.STANDARD,
    )
    policy = MediationPolicy.guarded(workspace=workspace, step_dir=workspace)
    hooks = PolicyHooks(recorder=recorder, policy=policy)

    with pytest.raises(PolicyDenied):
        await hooks.read_text_file(FsReadRequestN(path="peek"))
