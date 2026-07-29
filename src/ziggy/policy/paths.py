"""Canonical path containment + sensitive-path glob matching (REQ-008).

Two independent, pure primitives used by the guarded mediation engine:

``resolve_contained``
    Fail-closed canonicalization of an agent-supplied path against a trusted
    base directory (the workspace).  Anything that cannot be *proven* to stay
    inside the base raises :class:`PathAmbiguity` with a stable ``reason``.

Glob matching (``glob_matches`` / ``is_sensitive``)
    Deny-glob semantics over paths **relative to the workspace**. Deny globs
    match case-insensitively; this can only add denials, never remove them. A
    deny rule stricter than the underlying (possibly case-insensitive)
    filesystem is safe — the reverse, where ``.ENV`` slips past a ``.env`` deny
    yet opens the real ``.env`` on APFS/NTFS, is a secret leak. Every pattern
    and path segment is folded exactly once via :func:`str.casefold` before
    matching, and the comparison is otherwise as follows:

    1. Patterns and paths are split into POSIX segments; ``.`` segments and a
       leading ``/`` are ignored (patterns are always workspace-relative).
    2. A ``**`` *segment* matches zero or more whole path segments.  So
       ``**/.env`` matches both a top-level ``.env`` and ``a/b/.env``, and
       ``**/.aws/**`` matches ``.aws`` itself as well as anything below any
       ``.aws`` directory at any depth.
    3. Any other pattern segment matches exactly one (case-folded) path segment
       via ``fnmatchcase`` — ``*`` and ``?`` never cross a ``/``.
    4. A pattern containing no ``/`` is a *basename* pattern: it matches the
       final path component at any depth (internally ``p`` ≡ ``**/p``).

    Callers pass paths already canonicalized relative to the workspace (the
    output of :func:`resolve_contained`); ``..`` segments are never expected
    and would be compared literally.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from fnmatch import fnmatchcase
from pathlib import Path, PurePath, PurePosixPath

#: Built-in sensitive-path deny globs (REQ-011 defense in depth). User config
#: may only *add* patterns; nothing can remove these.
DEFAULT_SENSITIVE_GLOBS: tuple[str, ...] = (
    "**/.env",
    "**/.env.*",
    "**/*_key",
    "**/*.pem",
    "**/id_rsa*",
    "**/.aws/**",
    "**/.ssh/**",
    "**/.ziggy/config.toml",
)

#: The candidate resolved cleanly but lies outside the base (e.g. a plain
#: ``/etc/hosts``).  The only reason the mediation layer maps to
#: ``outside-workspace-deny`` instead of ``path-ambiguity-deny``.
REASON_OUTSIDE_BASE = "outside-base"
#: The candidate escaped the base through ``..`` traversal.
REASON_TRAVERSAL_ESCAPE = "traversal-escape"
#: A symlink inside the base carried resolution outside the base.
REASON_SYMLINK_ESCAPE = "symlink-escape"
#: The candidate could not be canonicalized at all (NUL bytes, wrong type,
#: OS-level resolution failure).
REASON_UNRESOLVABLE = "unresolvable"


class PathAmbiguity(Exception):
    """Fail-closed containment failure raised by :func:`resolve_contained`.

    ``reason`` is one of the stable ``REASON_*`` strings; every reason is a
    deny — the distinction only affects which rule id the policy records.
    """

    def __init__(self, message: str, *, reason: str = REASON_UNRESOLVABLE) -> None:
        super().__init__(message)
        self.reason = reason


def resolve_contained(base: Path | str, candidate: Path | str) -> Path:
    """Canonicalize ``candidate`` and prove it is contained in ``base``.

    ``base`` is realpath-resolved first, so a workspace that is itself a
    symlink is fine: containment is judged against the resolved base.  An
    absolute candidate is accepted if it lands inside the base after
    canonicalization; a relative candidate is resolved against the base.

    Canonicalization is ``os.path.realpath(..., strict=False)``: symlinks in
    every *existing* component are resolved (the deepest existing ancestor is
    what actually gets realpath'd) and non-existent trailing components are
    appended lexically — so a not-yet-created file inside the base still
    validates, e.g. an agent creating a new file and reading it back.

    Raises :class:`PathAmbiguity` (see ``REASON_*``) whenever containment
    cannot be proven.  Never returns a path outside the resolved base.
    """
    try:
        resolved_base = Path(os.path.realpath(base))
        raw = Path(candidate)
        joined = raw if raw.is_absolute() else resolved_base / raw
        resolved = Path(os.path.realpath(joined))
    except (TypeError, ValueError, OSError) as exc:
        raise PathAmbiguity(
            f"cannot canonicalize {candidate!r}: {exc}", reason=REASON_UNRESOLVABLE
        ) from exc

    if resolved.is_relative_to(resolved_base):
        return resolved

    # Not contained: classify why, for precise rule attribution. Every branch
    # below raises — there is no allow path past this point.
    if ".." in raw.parts:
        raise PathAmbiguity(
            f"path {candidate!r} escapes the base via '..' traversal",
            reason=REASON_TRAVERSAL_ESCAPE,
        )
    lexical = Path(os.path.normpath(joined))
    lexical_base = Path(os.path.normpath(base))
    if lexical.is_relative_to(resolved_base) or lexical.is_relative_to(lexical_base):
        # Lexically the path sat inside the base; only symlink resolution
        # moved it out.
        raise PathAmbiguity(
            f"path {candidate!r} resolves through a symlink to outside the base",
            reason=REASON_SYMLINK_ESCAPE,
        )
    raise PathAmbiguity(
        f"path {candidate!r} resolves outside the base",
        reason=REASON_OUTSIDE_BASE,
    )


def _path_segments(rel_path: str | PurePath) -> tuple[str, ...]:
    # Case-fold every segment exactly once so deny globs match case-
    # insensitively (see module docstring): '.ENV' must be denied like '.env'.
    text = rel_path.as_posix() if isinstance(rel_path, PurePath) else str(rel_path)
    return tuple(p.casefold() for p in PurePosixPath(text).parts if p != "/")


def _pattern_segments(pattern: str) -> tuple[str, ...] | None:
    if not pattern:
        return None
    # Fold each pattern segment once, consistently with _path_segments; the
    # synthesized '**' marker folds to itself and stays special in _match.
    parts = tuple(seg.casefold() for seg in pattern.split("/") if seg not in ("", "."))
    if not parts:
        return None
    if "/" not in pattern:
        # Basename pattern: match the final component at any depth.
        return ("**", *parts)
    return parts


def _match(pattern: tuple[str, ...], segments: tuple[str, ...]) -> bool:
    # Both pattern and segments are already case-folded by _pattern_segments /
    # _path_segments, so fnmatchcase here compares fold-to-fold.
    if not pattern:
        return not segments
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        return any(_match(rest, segments[i:]) for i in range(len(segments) + 1))
    if not segments:
        return False
    return fnmatchcase(segments[0], head) and _match(rest, segments[1:])


def glob_matches(pattern: str, rel_path: str | PurePath) -> bool:
    """True when ``rel_path`` (workspace-relative) matches one deny glob.

    Semantics are documented in the module docstring. Empty patterns match
    nothing.
    """
    pat = _pattern_segments(pattern)
    if pat is None:
        return False
    return _match(pat, _path_segments(rel_path))


def is_sensitive(path: str | PurePath, globs: Iterable[str] = ()) -> bool:
    """True when ``path`` (relative to the workspace) is a sensitive path.

    Matches against :data:`DEFAULT_SENSITIVE_GLOBS` plus the extra ``globs``
    (user-config additions). Extras can only widen the deny set.
    """
    return any(glob_matches(pattern, path) for pattern in (*DEFAULT_SENSITIVE_GLOBS, *globs))
