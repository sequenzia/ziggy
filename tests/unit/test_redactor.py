"""Unit tests for the redaction subsystem (REQ-006, §5.3)."""

from __future__ import annotations

import pytest

from ziggy.redact import CustomPattern, Redactor

BUILTIN_SAMPLES = [
    ("anthropic_api_key", "sk-ant-api03-" + "A" * 40),
    ("openai_api_key", "sk-" + "a1B2" * 6),
    ("github_token", "ghp_" + "A" * 36),
    ("github_token", "github_pat_" + "a" * 82),
    ("aws_access_key_id", "AKIAIOSFODNN7EXAMPLE"),
    (
        "aws_secret_access_key",
        'aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"',
    ),
    ("slack_token", "xoxb-1234567890-abcdefghijklm"),
    ("google_api_key", "AIzaSy" + "a" * 33),
    ("bearer_token", "Authorization: Bearer abc123.def456-ghi789"),
    ("private_key", "-----BEGIN RSA PRIVATE KEY-----"),
]


@pytest.mark.parametrize(("kind", "secret"), BUILTIN_SAMPLES)
def test_builtin_kind_redacted(kind: str, secret: str) -> None:
    redactor = Redactor()
    out, counts = redactor.redact_text(f"before {secret} after")
    assert secret not in out
    assert f"[REDACTED:{kind}]" in out
    assert counts.get(kind) == 1


def test_token_prefix_inside_word_not_redacted() -> None:
    redactor = Redactor()
    text = "the task-abcdefghijklmnopqrstuv identifier"
    out, counts = redactor.redact_text(text)
    assert out == text
    assert counts == {}


def test_secret_split_across_two_chunks() -> None:
    redactor = Redactor()
    stream = redactor.make_stream()
    secret = "sk-ant-api03-" + "B" * 40
    emitted = [
        stream.feed("x" * 800 + " " + secret[:20]),
        stream.feed(secret[20:] + " done"),
        stream.flush(),
    ]
    joined = "".join(emitted)
    assert secret not in joined
    assert joined == "x" * 800 + " [REDACTED:anthropic_api_key] done"
    assert redactor.summary().by_kind == {"anthropic_api_key": 1}


def test_secret_split_across_three_chunks() -> None:
    redactor = Redactor()
    stream = redactor.make_stream()
    secret = "ghp_" + "C" * 36
    parts = [secret[:5], secret[5:20], secret[20:]]
    emitted = [stream.feed("intro ")]
    emitted += [stream.feed(part) for part in parts]
    emitted.append(stream.feed(" outro"))
    emitted.append(stream.flush())
    joined = "".join(emitted)
    assert secret not in joined
    assert joined == "intro [REDACTED:github_token] outro"
    assert redactor.summary().by_kind == {"github_token": 1}


def test_stream_mid_buffer_secret_consumed_once() -> None:
    redactor = Redactor()
    stream = redactor.make_stream()
    key = "AKIAIOSFODNN7EXAMPLE"
    joined = (
        stream.feed("x" * 99 + " " + key + " " + "y" * 99) + stream.feed("z" * 500) + stream.flush()
    )
    assert key not in joined
    assert joined == "x" * 99 + " [REDACTED:aws_access_key_id] " + "y" * 99 + "z" * 500
    assert redactor.summary().by_kind == {"aws_access_key_id": 1}
    assert redactor.summary().total_redactions == 1


def test_feed_buffers_below_window() -> None:
    redactor = Redactor()
    stream = redactor.make_stream()
    assert len("short chunk") <= stream.window
    assert stream.feed("short chunk") == ""
    assert stream.flush() == "short chunk"


def test_incomplete_prefix_alone_is_not_a_secret() -> None:
    redactor = Redactor()
    stream = redactor.make_stream()
    assert stream.feed("hello sk-ant-") == ""
    assert stream.flush() == "hello sk-ant-"


def test_exact_value_with_regex_metacharacters() -> None:
    value = "p@ss.word(1)+*?[]^$"
    redactor = Redactor(secret_values=[("env:MY_SECRET", value)])
    out, counts = redactor.redact_text(f"creds: {value} end")
    assert value not in out
    assert out == "creds: [REDACTED:env:MY_SECRET] end"
    assert counts == {"env:MY_SECRET": 1}
    assert redactor.summary().warnings == []


def test_short_exact_value_warns_but_still_redacts() -> None:
    redactor = Redactor(secret_values=[("env:PIN", "ab1")])
    out, counts = redactor.redact_text("pin is ab1 ok")
    assert out == "pin is [REDACTED:env:PIN] ok"
    assert counts == {"env:PIN": 1}
    warnings = redactor.summary().warnings
    assert len(warnings) == 1
    assert "env:PIN" in warnings[0]


def test_empty_exact_value_ignored() -> None:
    redactor = Redactor(secret_values=[("env:EMPTY", "")])
    out, counts = redactor.redact_text("nothing here")
    assert out == "nothing here"
    assert counts == {}
    assert redactor.summary().warnings == []


def test_exact_value_takes_priority_over_builtin() -> None:
    key = "sk-ant-api03-" + "F" * 40
    redactor = Redactor(secret_values=[("env:ANTHROPIC_API_KEY", key)])
    out, counts = redactor.redact_text(f"key={key}")
    assert out == "key=[REDACTED:env:ANTHROPIC_API_KEY]"
    assert counts == {"env:ANTHROPIC_API_KEY": 1}


def test_custom_pattern_with_max_width_applies_to_stream() -> None:
    pattern = CustomPattern(kind="ticket", regex=r"TICKET-[0-9]{6}", max_width=16)
    redactor = Redactor(custom_patterns=[pattern])
    stream = redactor.make_stream()
    joined = stream.feed("see TICKET-12") + stream.feed("3456 now") + stream.flush()
    assert "TICKET-123456" not in joined
    assert joined == "see [REDACTED:ticket] now"


def test_custom_pattern_without_max_width_complete_only() -> None:
    pattern = CustomPattern(kind="license", regex=r"LIC-[0-9]{4}")
    redactor = Redactor(custom_patterns=[pattern])
    stream = redactor.make_stream()
    streamed = stream.feed("code LIC-9999 here") + stream.flush()
    assert "LIC-9999" in streamed

    out, counts = redactor.redact_text("code LIC-9999 here")
    assert out == "code [REDACTED:license] here"
    assert counts == {"license": 1}


def test_redact_payload_recursive() -> None:
    redactor = Redactor(secret_values=[("env:TOKEN", "supersecretvalue")])
    payload = {
        "text": "key sk-ant-api03-" + "D" * 40,
        "nested": {
            "list": ["supersecretvalue", {"deep": "id AKIAIOSFODNN7EXAMPLE"}],
            "num": 7,
        },
        "flag": True,
        "none": None,
    }
    redacted, counts = redactor.redact_payload(payload)
    assert redacted["text"] == "key [REDACTED:anthropic_api_key]"
    assert redacted["nested"]["list"][0] == "[REDACTED:env:TOKEN]"
    assert redacted["nested"]["list"][1]["deep"] == "id [REDACTED:aws_access_key_id]"
    assert redacted["nested"]["num"] == 7
    assert redacted["flag"] is True
    assert redacted["none"] is None
    assert counts == {"anthropic_api_key": 1, "env:TOKEN": 1, "aws_access_key_id": 1}
    assert "supersecretvalue" not in str(redacted)


def test_repeated_secret_counted_per_occurrence() -> None:
    redactor = Redactor()
    key = "AKIAIOSFODNN7EXAMPLE"
    out, counts = redactor.redact_text(f"{key} and {key}")
    assert out == "[REDACTED:aws_access_key_id] and [REDACTED:aws_access_key_id]"
    assert counts == {"aws_access_key_id": 2}


def test_summary_aggregates_text_and_stream_paths() -> None:
    redactor = Redactor()
    redactor.redact_text("a sk-ant-api03-" + "E" * 40 + " b")
    stream = redactor.make_stream()
    stream.feed("x" * 800 + " AKIAIOSFODNN7EXAMPLE more")
    stream.flush()
    summary = redactor.summary()
    assert summary.total_redactions == 2
    assert summary.by_kind == {"anthropic_api_key": 1, "aws_access_key_id": 1}
    assert summary.warnings == []
