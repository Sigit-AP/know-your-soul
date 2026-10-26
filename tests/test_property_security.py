"""Property tests for security primitives: redaction, sanitization, containment.

Pins the security contract (spec section 13.1) on arbitrary inputs:

* ``redact_text`` is idempotent and deterministic; every generated instance of
  every pinned secret shape is removed; clean prose stays byte-identical (the
  frozen token-budget contract depends on that);
* ``sanitize_text`` strips exactly the disallowed categories, keeps tab/LF/CR,
  and fails closed on overlong input;
* ``resolve_within_root`` never returns a path outside its fixed root, however
  the candidate is spelled;
* the bind/host policy fails closed: empty allowlists deny, non-loopback
  clients without a valid key are always unauthorized.

All runs are deterministic (hypothesis ``derandomize=True``).
"""

from __future__ import annotations

import tempfile
import unicodedata
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from soul_knowledge.security import (
    SECRET_PATTERNS,
    HostNotAllowedError,
    OversizedPayloadError,
    PathEscapeError,
    RequestView,
    UnauthorizedError,
    authorize_request,
    ensure_text_size,
    host_allowed,
    is_loopback_host,
    redact_text,
    redact_value,
    resolve_within_root,
    sanitize_text,
)

# ------------------------------------------------------------ redaction


@given(text=st.text(max_size=400))
@settings(derandomize=True, max_examples=60, deadline=None)
def test_redact_text_is_idempotent(text: str) -> None:
    once = redact_text(text)
    assert redact_text(once) == once


@given(text=st.text(max_size=400))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_redact_text_is_deterministic(text: str) -> None:
    assert redact_text(text) == redact_text(text)


_ASCII_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_ASCII_UPPER_DIGIT = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_alnum = st.sampled_from(_ASCII_ALNUM)
_secret_strategies: dict[str, st.SearchStrategy[str]] = {
    "openai-style key": st.text(alphabet=_alnum, min_size=16, max_size=24).map(lambda tail: f"sk-{tail}"),
    "aws access key": st.text(alphabet=st.sampled_from(_ASCII_UPPER_DIGIT), min_size=16, max_size=16).map(
        lambda tail: f"AKIA{tail}"
    ),
    "github token": st.text(alphabet=_alnum, min_size=20, max_size=30).map(lambda tail: f"ghp_{tail}"),
    "slack token": st.text(alphabet=_alnum, min_size=10, max_size=20).map(lambda tail: f"xoxb-{tail}"),
    "private key block": st.text(alphabet=_alnum, min_size=8, max_size=24).map(
        lambda body: f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----"
    ),
    "bearer token": st.text(alphabet=_alnum, min_size=16, max_size=24).map(lambda tail: f"Bearer {tail}"),
    "jwt": st.lists(st.text(alphabet=_alnum, min_size=8, max_size=12), min_size=3, max_size=3).map(
        lambda parts: f"eyJ{parts[0]}.{parts[1]}.{parts[2]}"
    ),
    "credential assignment": st.text(alphabet=_alnum, min_size=8, max_size=20).map(lambda value: f"password = {value}"),
}


@given(
    family=st.sampled_from(sorted(_secret_strategies)),
    data=st.data(),
    prefix=st.text(alphabet="abcdef ", max_size=12),
    suffix=st.text(alphabet="abcdef ", max_size=12),
)
@settings(derandomize=True, max_examples=60, deadline=None)
def test_every_pinned_secret_shape_is_removed(family: str, data: Any, prefix: str, suffix: str) -> None:
    secret = data.draw(_secret_strategies[family], label=family)
    redacted = redact_text(f"{prefix}{secret}{suffix}")
    assert secret not in redacted
    for name, pattern in SECRET_PATTERNS:
        assert not pattern.search(redacted), f"{name} survived redaction in {redacted!r}"


_benign_word = st.text(alphabet=st.characters(whitelist_categories=("Ll",)), min_size=1, max_size=12)


@given(words=st.lists(_benign_word, max_size=30), separator=st.sampled_from([" ", ", ", ". ", "\n"]))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_clean_prose_is_byte_identical_after_redaction(words: list[str], separator: str) -> None:
    text = separator.join(words)
    assert redact_text(text) == text


@given(text=st.text(max_size=200))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_redaction_output_never_contains_private_key_markers(text: str) -> None:
    assert "PRIVATE KEY-----" not in redact_text(text)


# ------------------------------------------------------------ sanitization


@given(text=st.text(max_size=300))
@settings(derandomize=True, max_examples=50, deadline=None)
def test_sanitize_text_strips_every_disallowed_character(text: str) -> None:
    cleaned = sanitize_text(text)
    for char in cleaned:
        assert char in {"\t", "\n", "\r"} or unicodedata.category(char) not in ("Cc", "Cf")


@given(text=st.text(max_size=300))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_sanitize_text_is_idempotent(text: str) -> None:
    once = sanitize_text(text)
    assert sanitize_text(once) == once


@given(text=st.text(min_size=1, max_size=300), max_length=st.integers(min_value=1, max_value=200))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_sanitize_text_fails_closed_exactly_when_overlong(text: str, max_length: int) -> None:
    if len(text) > max_length:
        with pytest.raises(OversizedPayloadError):
            sanitize_text(text, max_length=max_length)
    else:
        assert sanitize_text(text, max_length=max_length) == sanitize_text(text)


@given(text=st.text(max_size=120), budget=st.integers(min_value=0, max_value=240))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_ensure_text_size_returns_utf8_length_or_raises(text: str, budget: int) -> None:
    size = len(text.encode("utf-8"))
    if size > budget:
        with pytest.raises(OversizedPayloadError):
            ensure_text_size(text, max_bytes=budget)
    else:
        assert ensure_text_size(text, max_bytes=budget) == size


# ------------------------------------------------------------ containment


_segment = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="-_ "), min_size=1, max_size=8
)


@given(segments=st.lists(st.one_of(_segment, st.just(".."), st.just(".")), max_size=8))
@settings(derandomize=True, max_examples=50, deadline=None)
def test_resolve_within_root_never_returns_outside_the_root(segments: list[str]) -> None:
    with tempfile.TemporaryDirectory() as root:
        candidate = "/".join(segments) or "."
        try:
            resolved = resolve_within_root(root, candidate)
        except PathEscapeError:
            return
        root_path = Path(root).resolve()
        assert resolved == root_path or root_path in resolved.parents


@given(ups=st.integers(min_value=1, max_value=8), tail=_segment)
@settings(derandomize=True, max_examples=30, deadline=None)
def test_resolve_within_root_rejects_traversal_beyond_the_root(ups: int, tail: str) -> None:
    with tempfile.TemporaryDirectory() as root:
        nested = Path(root) / "a"
        nested.mkdir()
        candidate = "/".join(["a", *([".."] * (ups + 1)), tail])
        with pytest.raises(PathEscapeError):
            resolve_within_root(root, candidate)


@given(tail=_segment)
@settings(derandomize=True, max_examples=20, deadline=None)
def test_resolve_within_root_rejects_absolute_paths_outside(tail: str) -> None:
    with tempfile.TemporaryDirectory() as root:
        with pytest.raises(PathEscapeError):
            resolve_within_root(root, f"/etc/{tail}")


@given(segments=st.lists(_segment, min_size=1, max_size=5))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_resolve_within_root_accepts_plain_relative_paths(segments: list[str]) -> None:
    with tempfile.TemporaryDirectory() as root:
        resolved = resolve_within_root(root, "/".join(segments))
        root_path = Path(root).resolve()
        assert resolved == root_path or root_path in resolved.parents


# ------------------------------------------------------------ bind/host policy


@given(host=st.text(max_size=40))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_empty_host_allowlist_fails_closed(host: str) -> None:
    assert host_allowed(host, []) is False


def test_host_allowlist_matching_is_case_and_port_insensitive() -> None:
    assert host_allowed("Example.COM:8080", ["example.com"])
    assert host_allowed("LOCALHOST.", ["localhost"])
    assert not host_allowed("example.com.evil.test", ["example.com"])


@given(
    a=st.integers(0, 255),
    b=st.integers(0, 255),
    c=st.integers(0, 255),
)
@settings(derandomize=True, max_examples=30, deadline=None)
def test_every_127_address_is_loopback(a: int, b: int, c: int) -> None:
    assert is_loopback_host(f"127.{a}.{b}.{c}")


@given(address=st.ip_addresses(v=4).filter(lambda ip: not ip.is_loopback))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_non_loopback_ipv4_is_not_loopback(address: Any) -> None:
    assert not is_loopback_host(str(address))


@given(host=st.sampled_from(["example.com", "api.internal"]), port=st.integers(1, 65535))
@settings(derandomize=True, max_examples=20, deadline=None)
def test_remote_client_without_configured_key_is_unauthorized(host: str, port: int) -> None:
    request = RequestView(remote_addr="203.0.113.7", host=f"{host}:{port}", api_key=None)
    with pytest.raises(UnauthorizedError):
        authorize_request(request, api_key=None, host_allowlist=[host])


@given(key=st.text(alphabet=_alnum, min_size=8, max_size=24), wrong=st.text(alphabet=_alnum, min_size=8, max_size=24))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_remote_client_with_wrong_key_is_unauthorized(key: str, wrong: str) -> None:
    if key == wrong:
        return
    request = RequestView(remote_addr="203.0.113.7", host="example.com", api_key=wrong)
    with pytest.raises(UnauthorizedError):
        authorize_request(request, api_key=key, host_allowlist=["example.com"])


@given(remote=st.sampled_from(["127.0.0.1", "::1", "localhost"]))
@settings(derandomize=True, max_examples=10, deadline=None)
def test_loopback_client_with_allowed_host_needs_no_key(remote: str) -> None:
    request = RequestView(remote_addr=remote, host="example.com", api_key=None)
    authorize_request(request, api_key=None, host_allowlist=["example.com"])


@given(host=st.text(min_size=1, max_size=30))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_unlisted_host_is_rejected_even_for_loopback(host: str) -> None:
    request = RequestView(remote_addr="127.0.0.1", host=host, api_key=None)
    try:
        authorize_request(request, api_key=None, host_allowlist=["example.com"])
    except HostNotAllowedError:
        return
    assert host_allowed(host, ["example.com"])


# ------------------------------------------------------------ redact_value


@given(value=st.text(alphabet=_alnum, min_size=8, max_size=24))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_redact_value_replaces_credential_keyed_values_entirely(value: str) -> None:
    payload = {"config": {"api_key": value, "note": "plain"}}
    redacted = redact_value(payload)
    assert redacted["config"]["api_key"] == "[REDACTED]"
    assert redacted["config"]["note"] == "plain"
    assert payload["config"]["api_key"] == value  # input never mutated


@given(depth=st.integers(min_value=0, max_value=3))
@settings(derandomize=True, max_examples=10, deadline=None)
def test_redact_value_preserves_structure_shape(depth: int) -> None:
    value: Any = "leaf"
    for _ in range(depth):
        value = {"k": [value]}
    redacted = redact_value(value)
    assert type(redacted) is type(value)
