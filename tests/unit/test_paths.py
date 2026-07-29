"""Branch-exhaustive tests for policy path containment + sensitive globs (REQ-008).

Spec 10.1 requires 100% branch coverage on path containment: every allow path,
every PathAmbiguity reason, and both glob-matching modes (relative-path and
basename / ``**``-prefixed) are exercised here.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import pytest

from ziggy.policy.paths import (
    DEFAULT_SENSITIVE_GLOBS,
    REASON_OUTSIDE_BASE,
    REASON_SYMLINK_ESCAPE,
    REASON_TRAVERSAL_ESCAPE,
    REASON_UNRESOLVABLE,
    PathAmbiguity,
    glob_matches,
    is_sensitive,
    resolve_contained,
)


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    """A real (non-symlink) workspace directory with one existing subtree."""
    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "file.txt").write_text("data")
    return Path(os.path.realpath(root))


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    root = tmp_path / "outside"
    root.mkdir()
    (root / "target.txt").write_text("secret")
    return Path(os.path.realpath(root))


class TestResolveContainedAllows:
    def test_relative_candidate_resolves_under_base(self, ws: Path) -> None:
        assert resolve_contained(ws, "sub/file.txt") == ws / "sub" / "file.txt"

    def test_absolute_candidate_inside_base(self, ws: Path) -> None:
        candidate = str(ws / "sub" / "file.txt")
        assert resolve_contained(ws, candidate) == ws / "sub" / "file.txt"

    def test_base_itself_is_contained(self, ws: Path) -> None:
        assert resolve_contained(ws, str(ws)) == ws

    def test_empty_candidate_resolves_to_base(self, ws: Path) -> None:
        assert resolve_contained(ws, "") == ws

    def test_nonexistent_leaf_inside_base_validates(self, ws: Path) -> None:
        # An agent creating a new file and reading it back must pass the
        # containment check even though nothing exists yet.
        resolved = resolve_contained(ws, "newdir/deeper/new-file.txt")
        assert resolved == ws / "newdir" / "deeper" / "new-file.txt"
        assert not resolved.exists()

    def test_dotdot_that_stays_inside_is_allowed(self, ws: Path) -> None:
        assert resolve_contained(ws, "sub/../sub/file.txt") == ws / "sub" / "file.txt"

    def test_dotdot_through_nonexistent_dir_staying_inside(self, ws: Path) -> None:
        assert resolve_contained(ws, "ghost/../sub/file.txt") == ws / "sub" / "file.txt"

    def test_internal_symlink_resolves_to_target_inside(self, ws: Path) -> None:
        (ws / "alias").symlink_to(ws / "sub")
        assert resolve_contained(ws, "alias/file.txt") == ws / "sub" / "file.txt"

    def test_base_itself_a_symlink_is_resolved_first(self, tmp_path: Path, ws: Path) -> None:
        link = tmp_path / "link-ws"
        link.symlink_to(ws)
        # Candidate expressed through the symlinked base: contained under the
        # RESOLVED base, so allowed.
        resolved = resolve_contained(link, str(link / "sub" / "file.txt"))
        assert resolved == ws / "sub" / "file.txt"
        assert resolve_contained(link, "sub/file.txt") == ws / "sub" / "file.txt"


class TestResolveContainedDenies:
    def test_plain_absolute_outside_is_outside_base(self, ws: Path, outside: Path) -> None:
        with pytest.raises(PathAmbiguity) as exc_info:
            resolve_contained(ws, str(outside / "target.txt"))
        assert exc_info.value.reason == REASON_OUTSIDE_BASE

    def test_etc_hosts_is_outside_base(self, ws: Path) -> None:
        with pytest.raises(PathAmbiguity) as exc_info:
            resolve_contained(ws, "/etc/hosts")
        assert exc_info.value.reason == REASON_OUTSIDE_BASE

    def test_relative_dotdot_escape(self, ws: Path, outside: Path) -> None:
        with pytest.raises(PathAmbiguity) as exc_info:
            resolve_contained(ws, "../outside/target.txt")
        assert exc_info.value.reason == REASON_TRAVERSAL_ESCAPE

    def test_deep_relative_dotdot_escape(self, ws: Path) -> None:
        with pytest.raises(PathAmbiguity) as exc_info:
            resolve_contained(ws, "sub/../../../etc/passwd")
        assert exc_info.value.reason == REASON_TRAVERSAL_ESCAPE

    def test_absolute_with_dotdot_escape(self, ws: Path, outside: Path) -> None:
        with pytest.raises(PathAmbiguity) as exc_info:
            resolve_contained(ws, f"{ws}/../outside/target.txt")
        assert exc_info.value.reason == REASON_TRAVERSAL_ESCAPE

    def test_symlink_pointing_out_of_base(self, ws: Path, outside: Path) -> None:
        (ws / "escape").symlink_to(outside / "target.txt")
        with pytest.raises(PathAmbiguity) as exc_info:
            resolve_contained(ws, "escape")
        assert exc_info.value.reason == REASON_SYMLINK_ESCAPE

    def test_path_through_escaping_dir_symlink(self, ws: Path, outside: Path) -> None:
        (ws / "escape-dir").symlink_to(outside)
        with pytest.raises(PathAmbiguity) as exc_info:
            resolve_contained(ws, "escape-dir/target.txt")
        assert exc_info.value.reason == REASON_SYMLINK_ESCAPE

    def test_absolute_symlink_escape_inside_base(self, ws: Path, outside: Path) -> None:
        (ws / "escape").symlink_to(outside / "target.txt")
        with pytest.raises(PathAmbiguity) as exc_info:
            resolve_contained(ws, str(ws / "escape"))
        assert exc_info.value.reason == REASON_SYMLINK_ESCAPE

    def test_symlink_escape_through_unresolved_base_alias(
        self, tmp_path: Path, ws: Path, outside: Path
    ) -> None:
        # Candidate lexically inside the UNRESOLVED (symlink) base, escaping
        # via an inner symlink: classified as symlink escape, not outside.
        link = tmp_path / "alias-ws"
        link.symlink_to(ws)
        (ws / "escape").symlink_to(outside / "target.txt")
        with pytest.raises(PathAmbiguity) as exc_info:
            resolve_contained(link, str(link / "escape"))
        assert exc_info.value.reason == REASON_SYMLINK_ESCAPE

    def test_nul_byte_candidate_is_unresolvable(self, ws: Path) -> None:
        with pytest.raises(PathAmbiguity) as exc_info:
            resolve_contained(ws, "bad\x00name")
        assert exc_info.value.reason == REASON_UNRESOLVABLE

    def test_non_path_candidate_is_unresolvable(self, ws: Path) -> None:
        with pytest.raises(PathAmbiguity) as exc_info:
            resolve_contained(ws, None)  # type: ignore[arg-type]
        assert exc_info.value.reason == REASON_UNRESOLVABLE

    def test_default_reason_is_unresolvable(self) -> None:
        assert PathAmbiguity("boom").reason == REASON_UNRESOLVABLE


class TestGlobMatches:
    @pytest.mark.parametrize(
        ("pattern", "rel", "expected"),
        [
            # '**/' patterns match at top level AND nested (both required).
            ("**/.env", ".env", True),
            ("**/.env", "a/b/.env", True),
            ("**/.env", "prod.env", False),
            ("**/.env", ".envrc", False),
            ("**/.env.*", ".env.local", True),
            ("**/.env.*", "cfg/.env.production", True),
            ("**/.env.*", ".env", False),
            ("**/*_key", "api_key", True),
            ("**/*_key", "keys/service_key", True),
            ("**/*_key", "keyring.py", False),
            ("**/*.pem", "certs/server.pem", True),
            ("**/*.pem", "server.pem", True),
            ("**/*.pem", "server.pem.bak", False),
            ("**/id_rsa*", "id_rsa", True),
            ("**/id_rsa*", ".ssh/id_rsa.pub", True),
            # trailing '/**' also matches the directory itself (zero segments).
            ("**/.aws/**", ".aws", True),
            ("**/.aws/**", ".aws/credentials", True),
            ("**/.aws/**", "home/.aws/config", True),
            ("**/.aws/**", "aws/credentials", False),
            ("**/.ssh/**", ".ssh/config", True),
            ("**/.ziggy/config.toml", ".ziggy/config.toml", True),
            ("**/.ziggy/config.toml", "nested/.ziggy/config.toml", True),
            ("**/.ziggy/config.toml", ".ziggy/other.toml", False),
            # bare (no slash) patterns are basename patterns at any depth.
            ("secrets.txt", "secrets.txt", True),
            ("secrets.txt", "deep/dir/secrets.txt", True),
            ("secrets.txt", "secrets.txt.bak", False),
            ("*.sqlite", "db/index.sqlite", True),
            # slashed patterns without '**' are anchored at the workspace root
            # and '*' never crosses '/'.
            ("docs/*.md", "docs/a.md", True),
            ("docs/*.md", "docs/sub/a.md", False),
            ("docs/*.md", "src/docs/a.md", False),
            ("docs/**", "docs/a.md", True),
            ("docs/**", "docs", True),
            ("docs/**", "src/docs.md", False),
            # matching is case-insensitive (deny globs only widen): '.PEM'
            # matches '*.pem' so it cannot slip past on a case-folding FS.
            ("**/*.pem", "certs/server.PEM", True),
            ("**/*.pem", "certs/SERVER.pem", True),
            ("**/.env", ".ENV", True),
            ("**/.env", ".Env", True),
            ("INTERNAL/**", "internal/x", True),
            ("internal/**", "INTERNAL/x", True),
            # degenerate patterns match nothing.
            ("", ".env", False),
            ("/", ".env", False),
        ],
    )
    def test_matrix(self, pattern: str, rel: str, expected: bool) -> None:
        assert glob_matches(pattern, rel) is expected

    def test_accepts_purepath_input(self) -> None:
        assert glob_matches("**/.env", PurePosixPath("a/.env"))
        assert glob_matches("**/.env", Path("a") / ".env")

    def test_workspace_root_matches_nothing(self) -> None:
        assert not glob_matches("**/.env", ".")

    def test_leading_slash_in_pattern_is_ignored(self) -> None:
        assert glob_matches("/docs/*.md", "docs/a.md")


class TestIsSensitive:
    @pytest.mark.parametrize(
        "rel",
        [
            ".env",
            "svc/.env",
            ".env.local",
            "api_key",
            "certs/tls.pem",
            ".ssh/id_rsa",
            "id_rsa.pub",
            ".aws/credentials",
            ".ziggy/config.toml",
        ],
    )
    def test_default_corpus_hits(self, rel: str) -> None:
        assert is_sensitive(rel)

    @pytest.mark.parametrize("rel", ["src/main.py", "README.md", "environment.md", "."])
    def test_default_corpus_misses(self, rel: str) -> None:
        assert not is_sensitive(rel)

    def test_extra_globs_extend_defaults(self) -> None:
        assert not is_sensitive("notes/private.txt")
        assert is_sensitive("notes/private.txt", ["notes/**"])
        # defaults still apply alongside extras
        assert is_sensitive(".env", ["notes/**"])

    @pytest.mark.parametrize(
        "rel",
        [
            ".env",
            ".ENV",
            ".Env",
            ".aws/credentials",
            ".AWS/credentials",
            ".ssh/id_rsa",
            ".SSH/ID_RSA",
            ".ziggy/config.toml",
            ".ziggy/CONFIG.toml",
            "secret.PEM",
            "id_rsa",
        ],
    )
    def test_case_folded_sensitive_hits(self, rel: str) -> None:
        # Deny globs match case-insensitively: an uppercased variant that opens
        # the real secret on APFS/NTFS must still be classified sensitive.
        assert is_sensitive(rel)

    def test_user_extra_glob_matches_case_insensitively(self) -> None:
        assert is_sensitive("INTERNAL/x", ["internal/**"])

    def test_defaults_are_the_documented_eight(self) -> None:
        assert DEFAULT_SENSITIVE_GLOBS == (
            "**/.env",
            "**/.env.*",
            "**/*_key",
            "**/*.pem",
            "**/id_rsa*",
            "**/.aws/**",
            "**/.ssh/**",
            "**/.ziggy/config.toml",
        )
