# Versioning & Releasing

## Versioning scheme

Ziggy uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html), pre-1.0:

- **0.MINOR.0** — new features, and (while pre-1.0) any breaking change
- **0.x.PATCH** — bug fixes and security fixes only
- **1.0.0** — declared when the CLI surface and the ACP/run-artifact contracts
  are considered stable

The run-artifact schema versions (`RESULT_SCHEMA_VERSION` etc. in
`ziggy/__init__.py`) are deliberately independent of the package version; they
only change when the artifact contract changes.

## Where the version lives

**Git tags are the single source of truth.** There is no version string to
edit anywhere:

- [hatch-vcs](https://github.com/ofek/hatch-vcs) derives the package version
  from the latest `vX.Y.Z` tag at build/sync time and writes it to
  `src/ziggy/_version.py` (generated, git-ignored).
- A tagged commit builds as exactly `X.Y.Z`. Commits after a tag get a PEP 440
  dev version such as `0.1.1.dev3+g1a2b3c4`, so any installed build is
  traceable to a commit.
- `ziggy --version` and the ACP handshake both report this derived version.
- The version refreshes on `uv sync` (or any build), not on every commit — a
  stale dev version in an editable install just means "re-sync".

## Changelog conventions

`CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and is **curated by hand** — it is not generated from commit messages:

- Update the `[Unreleased]` section in the same PR as the change it describes.
- Group entries under `Added` / `Changed` / `Deprecated` / `Removed` /
  `Fixed` / `Security`, in that order.
- Write for users, in imperative mood: what can they do now, what behaves
  differently, what got fixed. Skip internal refactors, CI, and test-only
  changes.

## Cutting a release

1. **Gate check.** For 0.1.0 specifically, complete
   [RELEASE-CHECKLIST.md](RELEASE-CHECKLIST.md). For any release: working tree
   clean on `main`, CI green.
2. **Roll the changelog.** Rename `[Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`,
   add a fresh empty `[Unreleased]` section above it, and update the link
   references at the bottom:

   ```markdown
   [Unreleased]: https://github.com/sequenzia/ziggy/compare/vX.Y.Z...HEAD
   [X.Y.Z]: https://github.com/sequenzia/ziggy/releases/tag/vX.Y.Z
   ```

   (Later releases use `compare/vPREV...vX.Y.Z` instead of `releases/tag`.)
3. **Commit and push** the changelog roll (e.g. `Release: v0.1.0 changelog`).
4. **Tag the release commit** with an annotated tag and push it:

   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```

5. **The `release` workflow does the rest**: runs ruff + the non-live pytest
   suite, verifies the changelog has a section for the tag, builds the sdist
   and wheel, and creates a GitHub Release with this version's changelog
   section as notes and the artifacts attached.
6. **Verify the install** on a clean machine:

   ```bash
   uv tool install git+https://github.com/sequenzia/ziggy@vX.Y.Z
   ziggy --version
   ```

If the workflow fails after the tag was pushed, fix the problem, delete the
tag locally and remotely (`git tag -d vX.Y.Z && git push origin :vX.Y.Z`),
and re-tag. Never reuse a tag that produced a published GitHub Release —
bump the patch version instead.
