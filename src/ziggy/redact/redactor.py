"""Bounded streaming secret redaction (REQ-006).

Redaction combines three matcher classes, applied to the original text with
non-overlapping leftmost-claim semantics (earlier matchers win a span):

1. exact-match secret *values* (api_key_env values + configured list),
2. built-in regexes for known token formats, each with a declared
   ``max_width`` bounding the longest possible match,
3. user-configured custom patterns; those without ``max_width`` are applied
   only to complete events, never on the streaming path.

Matched spans become ``[REDACTED:<kind>]`` markers. Because all matchers run
against the raw input (never against already-marked text), markers can never
be re-matched or corrupted by later matchers.

Redaction is defense in depth, not a proof that arbitrary secret or
proprietary data cannot appear in captured output; capture minimization and
retention config remain the stronger controls.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from ziggy.models.events import RedactionSummary

#: Exact values shorter than this still redact but record a validation
#: warning, because they can over-redact unrelated text (REQ-006).
MIN_EXACT_VALUE_LENGTH = 6


def _marker(kind: str) -> str:
    return f"[REDACTED:{kind}]"


@dataclass(frozen=True)
class CustomPattern:
    """User-configured redaction pattern (config ``redaction.patterns``).

    ``max_width`` must bound the longest match the regex can produce; a
    pattern that omits it is applied only at complete-event level because the
    streaming carry window cannot be sized for it.
    """

    kind: str
    regex: str
    max_width: int | None = None


@dataclass(frozen=True)
class _Match:
    start: int
    end: int
    kind: str


class _RegexMatcher:
    __slots__ = ("kind", "max_width", "pattern", "tail")

    def __init__(
        self,
        kind: str,
        pattern: re.Pattern[str],
        max_width: int,
        tail: re.Pattern[str] | None = None,
    ) -> None:
        self.kind = kind
        self.pattern = pattern
        self.max_width = max_width
        #: Character class of the token this builtin matches. When a bounded
        #: match ends on a still-continuing run of these characters (its upper
        #: quantifier bound was hit greedily), the span is extended to the end
        #: of that run so no secret tail survives beside the marker.
        self.tail = tail

    def iter_spans(self, text: str) -> Iterator[tuple[int, int]]:
        for match in self.pattern.finditer(text):
            yield match.start(), match.end()

    def extend(self, text: str, end: int) -> int:
        if self.tail is not None and (run := self.tail.match(text, end)) is not None:
            return run.end()
        return end


class _ExactMatcher:
    """Literal substring matcher; immune to regex metacharacters in values."""

    __slots__ = ("kind", "max_width", "value")

    def __init__(self, kind: str, value: str) -> None:
        self.kind = kind
        self.value = value
        self.max_width = len(value)

    def iter_spans(self, text: str) -> Iterator[tuple[int, int]]:
        start = 0
        while (idx := text.find(self.value, start)) != -1:
            yield idx, idx + len(self.value)
            start = idx + len(self.value)

    def extend(self, text: str, end: int) -> int:
        # A literal value is never widened: it has no quantifier bound to exceed.
        return end


_Matcher = _RegexMatcher | _ExactMatcher

#: One-char guard so token prefixes inside larger words (e.g. the "sk-" in
#: "task-...") do not trigger. Losing this context at a stream cut can only
#: over-redact, never leak.
_BOUNDARY = r"(?<![A-Za-z0-9])"


def _builtin(
    kind: str,
    regex: str,
    max_width: int,
    flags: int = 0,
    tail: str | None = None,
) -> _RegexMatcher:
    """Compile a built-in matcher.

    ``max_width`` bounds the base regex match; ``tail`` (a character-class body
    such as ``A-Za-z0-9_-``) names the token charset so a match that ends on a
    still-continuing token run — i.e. its greedy upper bound was hit — is
    widened to the whole run, guaranteeing no secret tail persists next to the
    marker even when the raw token is longer than ``max_width``.
    """
    tail_pattern = re.compile(f"[{tail}]+") if tail is not None else None
    return _RegexMatcher(kind, re.compile(regex, flags), max_width, tail_pattern)


#: Built-in token formats. Quantifiers are explicitly bounded so no *base*
#: match can exceed its declared max_width; the streaming carry logic depends
#: on that invariant, while ``tail`` extension covers over-long real tokens.
#: Order matters: more specific prefixes come first (anthropic before
#: openai-style), and that order also decides the label of a merged span.
BUILTIN_PATTERNS: tuple[_RegexMatcher, ...] = (
    _builtin(
        "anthropic_api_key",
        _BOUNDARY + r"sk-ant-[A-Za-z0-9_-]{8,256}",
        264,
        tail=r"A-Za-z0-9_-",
    ),
    _builtin(
        "openai_api_key",
        _BOUNDARY + r"sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,256}",
        267,
        tail=r"A-Za-z0-9_-",
    ),
    _builtin(
        "github_token",
        _BOUNDARY + r"(?:gh[oprsu]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{22,255})",
        268,
        tail=r"A-Za-z0-9_",
    ),
    _builtin("aws_access_key_id", _BOUNDARY + r"AKIA[0-9A-Z]{16}", 20),
    _builtin(
        "aws_secret_access_key",
        r"aws[a-z0-9_ .\-]{0,24}(?:secret|key|token)[a-z0-9_ .\-]{0,8}"
        r"[=:][ \t]{0,4}[\"']?[A-Za-z0-9/+=]{40}[\"']?",
        96,
        re.IGNORECASE,
    ),
    _builtin("slack_token", _BOUNDARY + r"xox[abps]-[A-Za-z0-9-]{8,64}", 72, tail=r"A-Za-z0-9-"),
    _builtin("google_api_key", _BOUNDARY + r"AIza[0-9A-Za-z_-]{35}", 40, tail=r"0-9A-Za-z_-"),
    _builtin(
        "bearer_token",
        r"authorization[ \t]{0,4}:[ \t]{0,4}bearer[ \t]{1,8}[A-Za-z0-9._~+/-]{4,512}={0,4}",
        560,
        re.IGNORECASE,
        tail=r"A-Za-z0-9._~+/=-",
    ),
    _builtin("private_key", r"-----BEGIN [A-Z ]{0,48}PRIVATE KEY-----", 80),
)


def _find_matches(text: str, matchers: Sequence[_Matcher]) -> list[_Match]:
    """Redaction spans as the merged union of every matcher's candidate spans.

    Every matcher contributes candidates against the raw text; a bounded
    builtin whose match ends on a still-continuing token run is widened to the
    end of that run (see ``_RegexMatcher.extend``). Overlapping candidates are
    then merged into union intervals rather than the later one being discarded,
    so a configured secret that is a substring of a longer wire credential can
    never suppress the builtin covering the rest — adding a configured secret
    only ever grows coverage. Each merged span is labelled by its most-specific
    contributor: the earliest matcher in priority order (exact values first,
    then builtins in table order, then customs).
    """
    candidates: list[tuple[int, int, int, str]] = []
    for priority, matcher in enumerate(matchers):
        for start, end in matcher.iter_spans(text):
            if end <= start:
                continue
            candidates.append((start, matcher.extend(text, end), priority, matcher.kind))
    if not candidates:
        return []
    # Sort by start, then priority so equal-start spans present their most
    # specific (lowest-priority-index) label first.
    candidates.sort(key=lambda c: (c[0], c[2]))
    merged: list[_Match] = []
    cur_start, cur_end, cur_prio, cur_kind = candidates[0]
    for start, end, prio, kind in candidates[1:]:
        if start < cur_end:  # overlap: union the spans, keep the most-specific label
            if end > cur_end:
                cur_end = end
            if prio < cur_prio:
                cur_prio, cur_kind = prio, kind
        else:
            merged.append(_Match(cur_start, cur_end, cur_kind))
            cur_start, cur_end, cur_prio, cur_kind = start, end, prio, kind
    merged.append(_Match(cur_start, cur_end, cur_kind))
    return merged


class Redactor:
    """Run-scoped redactor; counts accumulate across all paths for summary().

    Args:
        secret_values: ``(kind, value)`` pairs to redact by literal match.
            Empty values are ignored; values shorter than
            ``MIN_EXACT_VALUE_LENGTH`` are still redacted but record a
            validation warning.
        custom_patterns: user-configured patterns. Invalid regexes raise
            ``re.error`` here so config validation can surface them.
    """

    def __init__(
        self,
        secret_values: Iterable[tuple[str, str]] = (),
        custom_patterns: Iterable[CustomPattern] = (),
    ) -> None:
        self._warnings: list[str] = []
        exact: list[_ExactMatcher] = []
        for kind, value in secret_values:
            if not value:
                continue
            if len(value) < MIN_EXACT_VALUE_LENGTH:
                self._warnings.append(
                    f"secret value for {kind!r} is shorter than {MIN_EXACT_VALUE_LENGTH} "
                    "characters and may over-redact unrelated text"
                )
            exact.append(_ExactMatcher(kind, value))
        exact.sort(key=lambda m: m.max_width, reverse=True)

        stream_customs: list[_RegexMatcher] = []
        complete_customs: list[_RegexMatcher] = []
        for cp in custom_patterns:
            if cp.max_width is not None and cp.max_width < 1:
                raise ValueError(f"custom pattern {cp.kind!r}: max_width must be >= 1")
            compiled = re.compile(cp.regex)
            if cp.max_width is None:
                complete_customs.append(_RegexMatcher(cp.kind, compiled, 0))
            else:
                stream_customs.append(_RegexMatcher(cp.kind, compiled, cp.max_width))

        self._stream_matchers: tuple[_Matcher, ...] = (
            *exact,
            *BUILTIN_PATTERNS,
            *stream_customs,
        )
        self._all_matchers: tuple[_Matcher, ...] = (
            *self._stream_matchers,
            *complete_customs,
        )
        self._window = max(m.max_width for m in self._stream_matchers)
        self._total = 0
        self._by_kind: dict[str, int] = {}

    def redact_text(self, text: str) -> tuple[str, dict[str, int]]:
        """Complete-event path: applies every matcher, including custom
        patterns without ``max_width``. Returns redacted text and per-kind
        counts for this call (also added to the cumulative summary)."""
        return self._apply(text, self._all_matchers)

    def make_stream(self) -> StreamingRedactor:
        """New per-live-stream redactor sharing this run's cumulative counts."""
        return StreamingRedactor(self)

    def summary(self) -> RedactionSummary:
        """Cumulative counts and warnings across text, payload, and stream paths."""
        return RedactionSummary(
            total_redactions=self._total,
            by_kind=dict(self._by_kind),
            warnings=list(self._warnings),
        )

    def redact_payload(self, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
        """Redact every string value in a payload, recursing through nested
        dicts and lists (complete-event path). Keys are left untouched."""
        counts: dict[str, int] = {}
        redacted = {key: self._walk(value, counts) for key, value in payload.items()}
        return redacted, counts

    def _walk(self, node: Any, counts: dict[str, int]) -> Any:
        if isinstance(node, str):
            redacted, node_counts = self.redact_text(node)
            for kind, n in node_counts.items():
                counts[kind] = counts.get(kind, 0) + n
            return redacted
        if isinstance(node, dict):
            return {key: self._walk(value, counts) for key, value in node.items()}
        if isinstance(node, list):
            return [self._walk(item, counts) for item in node]
        return node

    def _apply(self, text: str, matchers: Sequence[_Matcher]) -> tuple[str, dict[str, int]]:
        matches = _find_matches(text, matchers)
        if not matches:
            return text, {}
        parts: list[str] = []
        pos = 0
        counts: dict[str, int] = {}
        for match in matches:
            parts.append(text[pos : match.start])
            parts.append(_marker(match.kind))
            counts[match.kind] = counts.get(match.kind, 0) + 1
            pos = match.end
        parts.append(text[pos:])
        self._record(counts)
        return "".join(parts), counts

    def _record(self, counts: dict[str, int]) -> None:
        for kind, n in counts.items():
            self._by_kind[kind] = self._by_kind.get(kind, 0) + n
            self._total += n


class StreamingRedactor:
    """Chunk-wise redaction with bounded carry-over.

    The carry window is the max ``max_width`` over all streaming-applicable
    matchers (exact values, built-ins, customs that declare a width). Because
    every such matcher's match length is bounded by the window:

    - any still-incomplete secret prefix lies entirely within the last
      ``window`` characters, which stay buffered raw, and
    - a complete match ending exactly at the buffer end (greedy, might extend
      with the next chunk) always starts inside the held region, so it is
      never finalized early.

    ``feed`` therefore never emits text that could be the prefix of a secret
    still being assembled, and the held buffer is always raw (never partially
    redacted), so no span is counted or marked twice.
    """

    def __init__(self, redactor: Redactor) -> None:
        self._redactor = redactor
        self._matchers = redactor._stream_matchers
        self._window = redactor._window
        self._buf = ""

    @property
    def window(self) -> int:
        """Maximum characters ``feed`` may hold back before emitting."""
        return self._window

    def feed(self, chunk: str) -> str:
        """Append a chunk and return the safe-to-emit redacted prefix.

        A match straddling the emit/hold boundary is complete (its length is
        bounded by the window), so it is redacted now and the emit point moves
        past it; matches wholly inside the held region stay raw for a later
        ``feed`` or ``flush`` to finalize.
        """
        self._buf += chunk
        buflen = len(self._buf)
        cut = buflen - self._window
        if cut <= 0:
            return ""
        parts: list[str] = []
        pos = 0
        counts: dict[str, int] = {}
        hold_from: int | None = None
        for match in _find_matches(self._buf, self._matchers):
            if match.start >= cut:
                break
            if match.end >= buflen:
                # A ``tail``-extended span reaches the buffer end, so the token
                # run may continue in a later chunk. Hold from this span's
                # start (never emit its prefix raw) and finalize it only once a
                # boundary character proves it complete, or at ``flush``.
                hold_from = match.start
                break
            parts.append(self._buf[pos : match.start])
            parts.append(_marker(match.kind))
            counts[match.kind] = counts.get(match.kind, 0) + 1
            pos = match.end
        final_cut = hold_from if hold_from is not None else max(cut, pos)
        parts.append(self._buf[pos:final_cut])
        self._buf = self._buf[final_cut:]
        if counts:
            self._redactor._record(counts)
        return "".join(parts)

    def flush(self) -> str:
        """Redact and return everything still buffered; the stream is over,
        so held partial prefixes are finalized as-is (streaming matchers only)."""
        text, self._buf = self._buf, ""
        redacted, _ = self._redactor._apply(text, self._matchers)
        return redacted
