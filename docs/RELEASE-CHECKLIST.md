# Ziggy v0.1 Release Checklist — Deferred Human/Live Items

The MVP was implemented mock-first by explicit decision (2026-07-28): all
engine, policy, workflow, server, and orchestrator behavior is verified against
raw-wire and SDK-backed mock agents. The items below require live accounts,
real adapters, humans, or scheduling, and MUST be completed before tagging
v0.1.0 (spec §9.7 release gate + §3.2 metrics).

## 1. Live built-in contract qualification (spec §3.2, §10.1 'Contract (live)')

- [ ] Install reviewed pins on a dev machine: `npm install -g claude-agent-acp@0.63.0 codex-acp@1.1.7`; record package integrity hashes.
- [ ] `ziggy doctor` per agent: handshake OK, capabilities captured into `docs/phase0/capability-matrix.md` (replace every UNVERIFIED row).
- [ ] Direct-tool probes per the capability-matrix checklist (fs/terminal mediation vs direct, cancellation latency, file-change visibility, minimal env).
- [ ] Author the `-m live` contract suite runs: fixed 20-run smoke set per built-in; target ≥95% per agent; failures classified, never excluded.
- [ ] Re-run the live suite on any SDK/adapter version bump (standing rule).

## 2. Zed interoperability smoke (spec §3.2, §9.5)

- [ ] Register `ziggy serve` in Zed's custom-agent configuration (document exact settings JSON in README).
- [ ] Scenarios: direct-agent run, named-workflow run, orchestrated run; streamed progress visible; permission forwarding prompts appear in Zed and answers are honored; cancellation from Zed tears down agents; RunResults identical to CLI runs.
- [ ] Record which ACP client capabilities current Zed advertises (Open Question 7) and whether permission forwarding used the client path or guarded fallback.

## 3. Clean-machine onboarding metric (spec §3.2)

- [ ] ≥5 timed clean-machine trials of the onboarding checklist (install uv →
  `uv tool install git+<repo>@v0.1.0` → install pinned adapters → auth →
  first successful `ziggy run`); median ≤ 15 minutes.

## 4. Orchestrator quality trial (spec §3.2, §9.6, Open Question 8)

- [ ] Fix the prompt set (all three plan types + adversarial descriptions) and blind-label rubric.
- [ ] Structural validity ≥95% after ≤1 repair (live planner); routing acceptability ≥80%; useful outcome ≥70%; validity failures stay in the denominator.

## 5. Pilot survey (spec §3.2, v0.1 + 1 month)

- [ ] ≥5 users (or all teammates) run the fixed one-shot + workflow tasks; ≥70% prefer Ziggy over direct invocation.

## 6. Security review sign-off (spec §9.7)

- [ ] Human review of: project trust merge, permission bridging, planning isolation, plan validation, egress records, workspace leases, subprocess teardown — using `docs/GATES.md` evidence links as the starting point.
- [ ] Confirm seeded-secret corpus and hostile suites run in CI on every push.

## 7. Release mechanics

- [ ] CI pipeline: ruff + pytest (not-live) on every push; `-m live` job manual/nightly with accounts.
- [ ] Verify built-in pins against the ACP registry JSON in CI (metadata check only, never a runtime trust root).
- [ ] Tag `v0.1.0`; verify `uv tool install git+<repo>@v0.1.0` on a clean machine.
- [ ] Publish §3.2 metric results with the release notes.
