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
    __slots__ = ("kind", "max_width", "pattern")

    def __init__(self, kind: str, pattern: re.Pattern[str], max_width: int) -> None:
        self.kind = kind
        self.pattern = pattern
        self.max_width = max_width

    def iter_spans(self, text: str) -> Iterator[tuple[int, int]]:
        for match in self.pattern.finditer(text):
            yield match.start(), match.end()


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


_Matcher = _RegexMatcher | _ExactMatcher

#: One-char guard so token prefixes inside larger words (e.g. the "sk-" in
#: "task-...") do not trigger. Losing this context at a stream cut can only
#: over-redact, never leak.
_BOUNDARY = r"(?<![A-Za-z0-9])"


def _builtin(kind: str, regex: str, max_width: int, flags: int = 0) -> _RegexMatcher:
    return _RegexMatcher(kind, re.compile(regex, flags), max_width)


#: Built-in token formats. Quantifiers are explicitly bounded so no match can
#: exceed its declared max_width; the streaming carry logic depends on that
#: invariant. Order matters: more specific prefixes come first (anthropic
#: before openai-style).
BUILTIN_PATTERNS: tuple[_RegexMatcher, ...] = (
    _builtin("anthropic_api_key", _BOUNDARY + r"sk-ant-[A-Za-z0-9_-]{8,256}", 264),
    _builtin("openai_api_key", _BOUNDARY + r"sk-[A-Za-z0-9]{20,256}", 260),
    _builtin(
        "github_token",
        _BOUNDARY + r"(?:gh[oprsu]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{22,255})",
        268,
    ),
    _builtin("aws_access_key_id", _BOUNDARY + r"AKIA[0-9A-Z]{16}", 20),
    _builtin(
        "aws_secret_access_key",
        r"aws[a-z0-9_ .\-]{0,24}(?:secret|key|token)[a-z0-9_ .\-]{0,8}"
        r"[=:][ \t]{0,4}[\"']?[A-Za-z0-9/+=]{40}[\"']?",
        96,
        re.IGNORECASE,
    ),
    _builtin("slack_token", _BOUNDARY + r"xox[abps]-[A-Za-z0-9-]{8,64}", 72),
    _builtin("google_api_key", _BOUNDARY + r"AIza[0-9A-Za-z_-]{35}", 40),
    _builtin(
        "bearer_token",
        r"authorization[ \t]{0,4}:[ \t]{0,4}bearer[ \t]{1,8}[A-Za-z0-9._~+/-]{4,512}={0,4}",
        560,
        re.IGNORECASE,
    ),
    _builtin("private_key", r"-----BEGIN [A-Z ]{0,48}PRIVATE KEY-----", 80),
)


def _overlaps(claimed: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(s < end and start < e for s, e in claimed)


def _find_matches(text: str, matchers: Sequence[_Matcher]) -> list[_Match]:
    """Non-overlapping matches across all matchers, earlier matchers winning."""
    claimed: list[tuple[int, int]] = []
    found: list[_Match] = []
    for matcher in matchers:
        for start, end in matcher.iter_spans(text):
            if end <= start or _overlaps(claimed, start, end):
                continue
            claimed.append((start, end))
            found.append(_Match(start, end, matcher.kind))
    found.sort(key=lambda m: m.start)
    return found


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
        cut = len(self._buf) - self._window
        if cut <= 0:
            return ""
        parts: list[str] = []
        pos = 0
        counts: dict[str, int] = {}
        for match in _find_matches(self._buf, self._matchers):
            if match.start >= cut:
                break
            parts.append(self._buf[pos : match.start])
            parts.append(_marker(match.kind))
            counts[match.kind] = counts.get(match.kind, 0) + 1
            pos = match.end
        final_cut = max(cut, pos)
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
