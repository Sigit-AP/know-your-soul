"""Security primitives: sanitization, redaction, path containment, bind policy.

Task 9 core of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec section 13.1 and the deferred Task 3 symlink finding.

Everything here is a pure function or a small factory — no sockets, no
environment reads, no provider anything. Task 7 wires these into the HTTP API
surface (Host allowlist middleware, request size limits, bind checks); the
shapes below (``RequestView`` + ``authorize_request`` and the
``make_*`` factories) make that wiring a one-line adapter per server.

Guarantees:

* ``sanitize_text`` strips every Unicode format character (bidi controls,
  zero-width characters, BOM) and every C0/C1/DEL control except tab, LF and
  CR, and fails closed on overlong input (spec 13.1 payload sanitization).
* ``redact_text``/``redact_value`` remove known secret shapes (API keys, AWS
  keys, GitHub/Slack tokens, private-key blocks, bearer/JWT tokens, credential
  assignments) and basic PII (email addresses), on write and read surfaces.
* ``resolve_within_root`` resolves every candidate against one fixed,
  pre-resolved root and fails closed on ``..`` traversal, absolute paths
  outside the root, cross-tree reads, and symlink escapes — the deferred
  Task 3 finding.
* ``assert_bind_allowed``/``authorize_request`` encode the least-privilege
  bind policy: Unix sockets and loopback binds need no key, anything beyond
  loopback requires a configured API key, and the Host allowlist is enforced
  even on wildcard binds.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from soul_knowledge.domain import canonical_json

__all__ = [
    "DEFAULT_MAX_PAYLOAD_BYTES",
    "DEFAULT_MAX_TEXT_LENGTH",
    "SECRET_PATTERNS",
    "BindPolicyError",
    "HostNotAllowedError",
    "OversizedPayloadError",
    "PathEscapeError",
    "RequestView",
    "SecurityError",
    "UnauthorizedError",
    "assert_bind_allowed",
    "authorize_request",
    "ensure_payload_size",
    "ensure_text_size",
    "host_allowed",
    "is_loopback_host",
    "make_host_allowlist",
    "make_request_authorizer",
    "redact_text",
    "redact_value",
    "resolve_within_root",
    "sanitize_text",
]

DEFAULT_MAX_TEXT_LENGTH = 65_536
DEFAULT_MAX_PAYLOAD_BYTES = 1_048_576
REDACTION = "[REDACTED]"


class SecurityError(RuntimeError):
    """Base class for fail-closed security decisions."""


class PathEscapeError(SecurityError):
    """A candidate path resolves outside its fixed root."""


class OversizedPayloadError(SecurityError):
    """A text or payload exceeds its configured byte/length budget."""


class HostNotAllowedError(SecurityError):
    """The request Host header is not on the configured allowlist."""


class UnauthorizedError(SecurityError):
    """A request from beyond loopback presented no valid API key."""


class BindPolicyError(SecurityError):
    """A non-loopback bind was requested without a configured API key."""


# -------------------------------------------------------------- sanitization

# Whitespace controls that survive sanitization; every other control (Cc) and
# every format character (Cf: bidi controls, zero-width characters, BOM,
# word joiner, ...) is stripped.
_KEEP_CONTROLS = frozenset({"\t", "\n", "\r"})


def sanitize_text(text: str, *, max_length: int = DEFAULT_MAX_TEXT_LENGTH) -> str:
    """Strip control, bidi and zero-width characters; cap length, fail closed."""
    if len(text) > max_length:
        raise OversizedPayloadError(f"text of {len(text)} chars exceeds the {max_length}-char cap")
    return "".join(char for char in text if char in _KEEP_CONTROLS or unicodedata.category(char) not in ("Cc", "Cf"))


# ---------------------------------------------------------------- redaction

# The single source of truth for secret shapes: the read-model validator
# (publish gate) scans artifacts with exactly this list — no second copy.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # The lookbehind keeps kebab/identifier text (``task-10-T10-1-…`` contains
    # ``sk-1``) from matching mid-word: real keys are token-boundaried by
    # whitespace, quotes or ``=``/``:`` (SUB-35-F1 class, pinned-pattern side).
    ("openai-style key", re.compile(r"(?<![A-Za-z0-9_\-])sk-[A-Za-z0-9_\-]{16,}")),
    ("aws access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github token", re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")),
    ("slack token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    (
        "private key block",
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL),
    ),
    ("private key header", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("bearer token", re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    (
        "credential assignment",
        re.compile(r"(?i)(?:api[_-]?key|secret|password|token)\s*[:=]\s*[\\\"']?[^\s\\\"',}]{8,}"),
    ),
)

_PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email address", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
)

_CREDENTIAL_KEY_RE = re.compile(r"(?i)(api[_-]?key|secret|password|passwd|token|authorization|credential)")

# ------------------------------------------------- redaction match view
#
# Wave-3 W3-4: pattern matching over the *raw* string is silently bypassed
# by zero-width-split secrets (``AKIA\u200bIOSF…``), fullwidth separators
# (``password：…``) and homoglyph credential keys (Cyrillic ``а`` in
# ``pаssword``). ``redact_text`` therefore takes a second pass over a
# *match view* — NFKC (folds fullwidth/compatibility shapes), every format
# character stripped (zero-width, bidi controls, BOM — re-joining split
# secrets), and common Cyrillic/Greek homoglyphs folded onto their Latin
# lookalikes. The normalized view is returned only when it actually exposes
# a hidden secret shape; clean input stays byte-identical, so the frozen
# token-budget contract is untouched for clean corpora.

# Best-effort homoglyph fold for MATCHING, not a Unicode confusable
# database: the Cyrillic/Greek letters that render as Latin lookalikes.
_HOMOGLYPH_FOLD = str.maketrans(
    {
        "а": "a",
        "А": "A",
        "е": "e",
        "Е": "E",
        "о": "o",
        "О": "O",
        "р": "p",
        "Р": "P",
        "с": "c",
        "С": "C",
        "х": "x",
        "Х": "X",
        "у": "y",
        "У": "Y",
        "н": "h",
        "Н": "H",
        "і": "i",
        "І": "I",
        "ј": "j",
        "Ј": "J",
        "ѕ": "s",
        "Ѕ": "S",
        "ԁ": "d",
        "м": "m",
        "М": "M",
        "т": "t",
        "Т": "T",
        "в": "b",
        "В": "B",
        "к": "k",
        "К": "K",
        "ο": "o",
        "Ο": "O",
        "ρ": "p",
        "Ρ": "P",
        "ν": "v",
        "Ν": "N",
        "χ": "x",
        "Χ": "X",
        "υ": "u",
        "Υ": "Y",
        "τ": "t",
        "Τ": "T",
        "κ": "k",
        "Κ": "K",
        "ι": "i",
        "Ι": "I",
        "α": "a",
        "Α": "A",
        "ε": "e",
        "Ε": "E",
        "σ": "s",
    }
)


def _redaction_match_view(text: str) -> str:
    """NFKC + separator-strip + homoglyph fold view used for secret matching only.

    Every non-ASCII separator character — Zs (NBSP, hair/thin/narrow/
    ideographic space, …), Zl (U+2028 LINE SEPARATOR) and Zp (U+2029
    PARAGRAPH SEPARATOR) — is dropped *before* NFKC, because NFKC would fold
    Zs into plain ASCII spaces that keep a split secret split (SUB-35-F2)
    and leaves Zl/Zp untouched (SUB-43 S-1: publish ``\\s+`` collapsing would
    later reshape them into the very ASCII-space form this view deliberately
    keeps, so the key would publish whole, one space wide). Every Unicode
    format (Cf: zero-width, bidi, BOM) and control (Cc: tab/newline/…)
    character is likewise dropped, re-joining secrets split by invisible or
    whitespace separators (``AKIA\\u200bIOSF…``, ``AKIA\\u200aIOSF…``,
    ``AKIA\\tIOSF…``). Plain ASCII spaces survive: several pinned patterns
    require a ``\\s+`` separator, and the raw-text pass has already matched
    anything the strip would hide behind one.
    """
    joined = "".join(char for char in text if char == " " or unicodedata.category(char) not in ("Zs", "Zl", "Zp"))
    view = unicodedata.normalize("NFKC", joined)
    view = "".join(char for char in view if unicodedata.category(char) not in ("Cf", "Cc"))
    return view.translate(_HOMOGLYPH_FOLD)


# High-entropy token runs (wave-3 W3-4): long base64-ish blobs carry API
# keys and session tokens that match no pinned prefix shape. A run is
# redacted only when it mixes the lower, upper AND digit character classes
# and carries no identifier separator (``-``/``_``): kebab-case paths and
# snake_case names — this repo's own naming conventions — are
# identifier-shaped, not secret-shaped (SUB-35-F1: the old rule counted
# ``-`` toward the symbol class, so dated kebab paths like
# ``docs/superpowers/specs/2026-10-24-…`` were redacted; 35/55 repo docs
# tripped). Pure-hex runs (any single case) remain hash-shaped content —
# sha256 pins, git objects, content digests — as do UUIDs (36 chars, below
# the 40-char floor). Standalone secrets that are pure-hex, all-one-case or
# base64url (``-``/``_`` alphabet) stay reachable only through the pinned
# patterns and the credential-assignment rule — a documented design
# boundary.
_ENTROPY_RUN_RE = re.compile(r"[A-Za-z0-9+/=_\-]{40,}")
_PURE_HEX_RE = re.compile(r"[0-9a-fA-F]+")


def _redact_entropy_runs(text: str) -> str:
    def _sub(match: re.Match[str]) -> str:
        run = match.group(0)
        if _PURE_HEX_RE.fullmatch(run):
            return run
        if "-" in run or "_" in run:
            return run  # identifier-shaped: kebab slugs, snake_case, paths
        secret_shaped = (
            any(char.islower() for char in run)
            and any(char.isupper() for char in run)
            and any(char.isdigit() for char in run)
        )
        return "[REDACTED:high-entropy token]" if secret_shaped else run

    return _ENTROPY_RUN_RE.sub(_sub, text)


def redact_text(text: str) -> str:
    """Replace every known secret shape and basic PII with a redaction marker.

    Two passes (wave-3 W3-4): the pinned patterns plus the high-entropy run
    rule over the raw text, then — only when the normalization view differs
    AND exposes a shape the raw text hid — the redacted *normalized* text is
    returned. Clean input is byte-identical.
    """
    redacted = text
    for name, pattern in (*SECRET_PATTERNS, *_PII_PATTERNS):
        redacted = pattern.sub(f"[REDACTED:{name}]", redacted)
    redacted = _redact_entropy_runs(redacted)
    view = _redaction_match_view(redacted)
    if view != redacted:
        scrubbed = view
        for name, pattern in (*SECRET_PATTERNS, *_PII_PATTERNS):
            scrubbed = pattern.sub(f"[REDACTED:{name}]", scrubbed)
        scrubbed = _redact_entropy_runs(scrubbed)
        if scrubbed != view:
            return scrubbed
    return redacted


def redact_value(value: Any) -> Any:
    """Recursively redact a JSON-shaped value.

    String values are pattern-redacted; mapping keys that name a credential
    (``api_key``, ``password``, ``token``, ``authorization``, ...) have their
    entire value replaced regardless of shape. The input is never mutated —
    a redacted copy is returned, so the store can hold raw payloads while
    every read surface that leaves the process is redacted.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _CREDENTIAL_KEY_RE.search(key):
                redacted[key] = REDACTION
            else:
                redacted[key] = redact_value(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    return value


# -------------------------------------------------------------- size guards


def ensure_text_size(text: str, *, max_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES) -> int:
    """Return the UTF-8 size of *text*; raise when it exceeds *max_bytes*."""
    size = len(text.encode("utf-8"))
    if size > max_bytes:
        raise OversizedPayloadError(f"text of {size} bytes exceeds the {max_bytes}-byte cap")
    return size


def ensure_payload_size(payload: Mapping[str, Any], *, max_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES) -> int:
    """Return the canonical-JSON size of *payload*; raise when oversized."""
    size = len(canonical_json(payload).encode("utf-8"))
    if size > max_bytes:
        raise OversizedPayloadError(f"payload of {size} bytes exceeds the {max_bytes}-byte cap")
    return size


# ----------------------------------------------------- fixed-root resolution


def resolve_within_root(root: str | Path, candidate: str | Path) -> Path:
    """Resolve *candidate* inside the fixed *root*, failing closed on escape.

    ``Path.resolve`` follows symlinks of every existing component, so a
    symlink inside the root that points outside resolves outside and is
    rejected — this closes the deferred Task 3 symlink finding. The root
    itself must be an existing directory; candidates may name files that do
    not exist yet (write paths).
    """
    try:
        root_path = Path(root).resolve(strict=True)
    except OSError as error:
        raise PathEscapeError(f"fixed root {str(root)!r} does not resolve: {error}") from error
    if not root_path.is_dir():
        raise PathEscapeError(f"fixed root {str(root)!r} is not a directory")
    raw = Path(candidate)
    joined = raw if raw.is_absolute() else root_path / raw
    try:
        resolved = joined.resolve(strict=False)
    except OSError as error:
        raise PathEscapeError(f"candidate {str(candidate)!r} does not resolve: {error}") from error
    if resolved != root_path and root_path not in resolved.parents:
        raise PathEscapeError(f"path {str(candidate)!r} resolves outside the fixed root {str(root_path)!r}")
    return resolved


# ------------------------------------------------- bind policy and authN/Z

_LOOPBACK_NAMES = frozenset({"localhost", "localhost.", "ip6-localhost"})


def _split_host_port(bind: str) -> str:
    """Extract the host part from ``host``, ``host:port`` or ``[v6]:port``."""
    text = bind.strip()
    if text.startswith("[") and "]" in text:  # [::1]:8080
        return text[1 : text.index("]")]
    try:
        ipaddress.ip_address(text)  # bare IPv4/IPv6 literal, no port
        return text
    except ValueError:
        pass
    if ":" in text:
        head, _, tail = text.rpartition(":")
        if tail.isdigit():  # name:port, v4:port or bare v6 with a trailing port
            return head
    return text


def is_loopback_host(host: str) -> bool:
    """True when *host* names a loopback interface (IPv4/IPv6 or localhost)."""
    candidate = _split_host_port(host).strip().lower().rstrip(".")
    if candidate in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def assert_bind_allowed(bind: str, *, api_key: str | None) -> None:
    """Least-privilege bind policy for the API server (Task 7 wires this).

    Unix sockets and loopback binds need no key; any wider bind requires a
    configured API key and belongs behind TLS/reverse proxy (operations doc).
    """
    if bind.startswith("unix:"):
        return
    if is_loopback_host(bind):
        return
    if not api_key:
        raise BindPolicyError(f"refusing to bind {bind!r} beyond loopback without a configured API key")


def _normalize_host(host_header: str) -> str:
    return _split_host_port(host_header).strip().lower().rstrip(".")


def host_allowed(host_header: str | None, allowlist: Sequence[str]) -> bool:
    """True when the Host header is on the allowlist; an empty list fails closed."""
    if host_header is None or not allowlist:
        return False
    host = _normalize_host(host_header)
    if not host:
        return False
    return host in {_normalize_host(entry) for entry in allowlist}


@dataclass(frozen=True)
class RequestView:
    """The minimal request shape the policy functions need.

    Task 7's middleware builds one of these per request from the server
    framework's request object and calls :func:`authorize_request` (or the
    ``make_request_authorizer`` factory); nothing here touches the network.
    """

    remote_addr: str
    host: str | None
    api_key: str | None


def authorize_request(request: RequestView, *, api_key: str | None, host_allowlist: Sequence[str]) -> None:
    """Allow or deny one request; raises a typed :class:`SecurityError` on deny.

    The Host allowlist is enforced on every request, including loopback and
    wildcard binds. Beyond loopback a configured API key is mandatory; with
    no key configured at all, only loopback clients are served.
    """
    if not host_allowed(request.host, host_allowlist):
        raise HostNotAllowedError(f"host {request.host!r} is not on the allowlist")
    if is_loopback_host(request.remote_addr):
        return
    if api_key is None:
        raise UnauthorizedError(f"remote client {request.remote_addr!r} denied: no API key is configured")
    if request.api_key is None:
        raise UnauthorizedError(f"remote client {request.remote_addr!r} presented no API key")
    if not hmac.compare_digest(
        hashlib.sha256(request.api_key.encode("utf-8")).digest(),
        hashlib.sha256(api_key.encode("utf-8")).digest(),
    ):
        raise UnauthorizedError(f"remote client {request.remote_addr!r} presented an invalid API key")


def make_host_allowlist(allowlist: Sequence[str]) -> Callable[[str | None], bool]:
    """Middleware factory: Host header -> allow/deny."""
    pinned = tuple(allowlist)
    return lambda host_header: host_allowed(host_header, pinned)


def make_request_authorizer(*, api_key: str | None, host_allowlist: Sequence[str]) -> Callable[[RequestView], None]:
    """Middleware factory: request -> allow, or raise a typed SecurityError."""
    pinned_key = api_key
    pinned_allowlist = tuple(host_allowlist)

    def authorize(request: RequestView) -> None:
        authorize_request(request, api_key=pinned_key, host_allowlist=pinned_allowlist)

    return authorize
