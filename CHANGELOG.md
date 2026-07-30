# Changelog

All notable changes to Ziggy will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Versions are derived from git tags (`vX.Y.Z`) via hatch-vcs; see
[docs/RELEASING.md](docs/RELEASING.md) for the release process.

## [Unreleased]

## [0.1.0] - 2026-07-30

### Added

- Add one-shot agent execution (`ziggy run`) with streamed progress, secret
  redaction, and a persisted, auditable `result.json` / `events.jsonl` per run
- Add constrained workflow engine (`ziggy workflow run`) for named multi-step
  agent workflows with variable substitution and discovery (`workflow list`)
- Add plan-then-execute orchestration (`ziggy orchestrate`) with structural
  plan validation and constrained routing
- Add ACP server mode (`ziggy serve`) so ACP clients such as Zed can drive
  direct, workflow, and orchestrated runs with permission forwarding
- Add built-in agent registry with pinned adapters for Claude Code, Codex,
  OpenCode, and Devin (`ziggy agents list`)
- Add trusted project configuration with explicit trust acknowledgment and
  guarded permission mediation policy
- Add environment diagnostics (`ziggy doctor`) covering adapter handshake and
  capability capture
- Add run browsing and maintenance commands (`ziggy runs list/show/prune`)
- Add versioned JSON Schemas for `result.json` and `events.jsonl`
  (`ziggy schemas dump`) so run artifacts can be validated without importing
  Ziggy
- Add `ziggy --version` flag
- Add documentation site published to GitHub Pages

[Unreleased]: https://github.com/sequenzia/ziggy/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sequenzia/ziggy/releases/tag/v0.1.0
