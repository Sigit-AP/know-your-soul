"""Structured operational configuration (ATM wave-2 OPS).

One canonical :data:`CONFIG_SPEC` declares every tunable of the operational
surface — store/projection paths, serve binds, watcher cadence, export and
retention knobs, diagnostics switches. Values resolve through four layers,
each strictly overriding the previous one:

1. **defaults** — the declared ``default`` of each field;
2. **file** — an optional TOML document (nested tables or dotted keys);
3. **environment** — ``KNOW_YOUR_SOUL_*`` variables;
4. **cli** — explicit programmatic overrides (the CLI adapter's layer).

Everything fails closed with a typed :class:`ConfigError`: unknown keys
(file or CLI), unreadable or unparsable files, type mismatches, and
range/choice violations are never silently coerced. The only implicit
widening is ``int → float``; ``bool`` is never accepted as ``int``.

Secret fields (``secret=True``) are redacted to :data:`REDACTED_PLACEHOLDER`
on every dump/describe surface unless the caller explicitly opts out, so a
resolved configuration can be logged or committed without leaking keys.

The module documents itself: :func:`describe_spec` renders the whole spec as
a markdown table, :func:`describe_config` annotates it with the resolved
value and source of each field, and :func:`render_example_toml` emits an
annotated, parseable example file whose values equal the defaults.

Deterministic and dependency-free (``tomllib`` is stdlib); this module never
touches the store.
"""

from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from know_your_soul.domain import canonical_json

__all__ = [
    "CONFIG_SPEC",
    "ENV_PREFIX",
    "REDACTED_PLACEHOLDER",
    "SOURCES",
    "ConfigError",
    "ConfigField",
    "ConfigValue",
    "OpsConfig",
    "describe_config",
    "describe_spec",
    "load_config",
    "render_example_toml",
]

ENV_PREFIX = "KNOW_YOUR_SOUL_"
REDACTED_PLACEHOLDER = "<redacted>"
SOURCES = ("default", "file", "env", "cli")

_FIELD_TYPES = ("str", "int", "float", "bool", "path", "str_list")
_INT_TEXT = re.compile(r"^-?\d+$")
_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off"}


class ConfigError(RuntimeError):
    """A configuration contract violation (typed, fail-closed)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class ConfigField:
    """One declared configuration knob."""

    key: str
    type: str
    default: Any
    description: str
    env: str | None = None
    secret: bool = False
    optional: bool = False
    minimum: float | None = None
    minimum_exclusive: bool = False

    def __post_init__(self) -> None:
        if self.type not in _FIELD_TYPES:
            raise ConfigError("TYPE_MISMATCH", f"field {self.key!r} declares unknown type {self.type!r}")
        if not self.key or "." not in self.key or self.key != self.key.lower():
            raise ConfigError("INVALID_SPEC", f"field key {self.key!r} must be dotted lowercase")


CONFIG_SPEC: tuple[ConfigField, ...] = (
    ConfigField(
        key="diagnose.check_permissions",
        type="bool",
        default=True,
        env="KNOW_YOUR_SOUL_CHECK_PERMISSIONS",
        description="Whether diagnostics report group/other-readable store or artifact files.",
    ),
    ConfigField(
        key="export.output_dirname",
        type="str",
        default="exports",
        description="Directory name (inside the containment root) receiving export artifacts.",
    ),
    ConfigField(
        key="ops.actor",
        type="str",
        default="operator",
        description="Actor identity recorded on operational command audit records.",
    ),
    ConfigField(
        key="ops.root",
        type="path",
        default=None,
        optional=True,
        env="KNOW_YOUR_SOUL_OPS_ROOT",
        description="Fixed containment root for backup/restore/export paths (default: the store's directory).",
    ),
    ConfigField(
        key="ops.stale_after_seconds",
        type="float",
        default=300.0,
        minimum=0.0,
        minimum_exclusive=True,
        env="KNOW_YOUR_SOUL_STALE_AFTER_SECONDS",
        description="Seconds after which an in-progress invocation journal is considered stale.",
    ),
    ConfigField(
        key="projection.output_root",
        type="path",
        default="know-your-soul-projection",
        env="KNOW_YOUR_SOUL_OUTPUT_ROOT",
        description="Projection output directory (generations/, CURRENT, exports).",
    ),
    ConfigField(
        key="retention.keep",
        type="int",
        default=3,
        minimum=0,
        env="KNOW_YOUR_SOUL_RETENTION_KEEP",
        description="How many deletable (non-CURRENT, non-pinned) generations retention keeps.",
    ),
    ConfigField(
        key="retention.pinned",
        type="str_list",
        default=(),
        env="KNOW_YOUR_SOUL_RETENTION_PINNED",
        description="Generation names retention must never delete (comma-separated).",
    ),
    ConfigField(
        key="serve.api_key",
        type="str",
        default=None,
        optional=True,
        secret=True,
        env="KNOW_YOUR_SOUL_API_KEY",
        description="API key required for non-loopback binds/clients; prefer serve.api_key_env to keep it out of argv.",
    ),
    ConfigField(
        key="serve.api_key_env",
        type="str",
        default=None,
        optional=True,
        description="Name of the environment variable the API key is read from at serve time.",
    ),
    ConfigField(
        key="serve.host_allowlist",
        type="str_list",
        default=("localhost", "127.0.0.1", "::1"),
        env="KNOW_YOUR_SOUL_HOST_ALLOWLIST",
        description="Host header allowlist for the HTTP surface (comma-separated).",
    ),
    ConfigField(
        key="serve.http_bind",
        type="str",
        default=None,
        optional=True,
        description="Optional loopback HTTP bind as HOST:PORT (e.g. 127.0.0.1:8080); unset serves the Unix socket.",
    ),
    ConfigField(
        key="serve.max_body_bytes",
        type="int",
        default=None,
        optional=True,
        minimum=1,
        env="KNOW_YOUR_SOUL_MAX_BODY_BYTES",
        description="Request body size cap for the HTTP surface; unset uses the built-in default.",
    ),
    ConfigField(
        key="serve.socket_path",
        type="path",
        default="know-your-soul.sock",
        description="Unix socket path for the persistent local API.",
    ),
    ConfigField(
        key="store.migrations_dir",
        type="path",
        default=None,
        optional=True,
        env="KNOW_YOUR_SOUL_MIGRATIONS_DIR",
        description="Override directory holding the SQL migrations (default: the packaged migrations).",
    ),
    ConfigField(
        key="store.path",
        type="path",
        default="know-your-soul.sqlite3",
        env="KNOW_YOUR_SOUL_STORE",
        description="Path to the SQLite event store (the append-only source of truth).",
    ),
    ConfigField(
        key="watch.cooldown_seconds",
        type="float",
        default=300.0,
        minimum=0.0,
        description="Seconds the source watcher pauses after emitting a relearn offer.",
    ),
    ConfigField(
        key="watch.interval_seconds",
        type="float",
        default=30.0,
        minimum=0.0,
        minimum_exclusive=True,
        description="Seconds between source-watcher polls.",
    ),
    ConfigField(
        key="watch.root",
        type="path",
        default=None,
        optional=True,
        description="Corpus root the source watcher observes (detect-only; never auto-applies).",
    ),
    ConfigField(
        key="watch.state_path",
        type="path",
        default=None,
        optional=True,
        description="Watcher snapshot path (must live outside the watched root).",
    ),
)

_SPEC_BY_KEY: dict[str, ConfigField] = {}
for _field in CONFIG_SPEC:
    if _field.key in _SPEC_BY_KEY:
        raise ConfigError("INVALID_SPEC", f"duplicate field key {_field.key!r}")
    _SPEC_BY_KEY[_field.key] = _field
    if _field.env is not None and not _field.env.startswith(ENV_PREFIX):
        raise ConfigError("INVALID_SPEC", f"field {_field.key!r} env {_field.env!r} misses the {ENV_PREFIX} prefix")


@dataclass(frozen=True)
class ConfigValue:
    """A resolved value plus the layer it came from."""

    value: Any
    source: str

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ConfigError("INVALID_SPEC", f"unknown source {self.source!r}")


# ------------------------------------------------------------------ coercion


def _type_error(field: ConfigField, value: Any) -> ConfigError:
    return ConfigError(
        "TYPE_MISMATCH",
        f"field {field.key!r} expects {field.type}, got {type(value).__name__} {value!r}",
    )


def _check_range(field: ConfigField, value: Any) -> Any:
    if value is None:
        if field.optional:
            return None
        raise _type_error(field, value)
    if field.type in {"int", "float"}:
        if field.minimum is not None:
            if field.minimum_exclusive and not value > field.minimum:
                raise ConfigError("OUT_OF_RANGE", f"field {field.key!r} must be > {field.minimum}, got {value}")
            if not field.minimum_exclusive and not value >= field.minimum:
                raise ConfigError("OUT_OF_RANGE", f"field {field.key!r} must be >= {field.minimum}, got {value}")
    if field.type in {"str", "path"} and isinstance(value, str) and not value and not field.optional:
        raise _type_error(field, value)
    return value


def _coerce_typed(field: ConfigField, value: Any) -> Any:
    """Validate an already-typed value (file/CLI layer). No coercion except int→float."""
    if value is None:
        return _check_range(field, None)
    if field.type == "bool":
        if not isinstance(value, bool):
            raise _type_error(field, value)
    elif field.type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise _type_error(field, value)
    elif field.type == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _type_error(field, value)
        value = float(value)
        if not math.isfinite(value):
            raise ConfigError("TYPE_MISMATCH", f"field {field.key!r} must be finite, got {value}")
    elif field.type in {"str", "path"}:
        if not isinstance(value, str):
            raise _type_error(field, value)
    elif field.type == "str_list":
        if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
            raise _type_error(field, value)
        value = tuple(value)
    return _check_range(field, value)


def _coerce_env(field: ConfigField, raw: str) -> Any:
    """Parse an environment string strictly; anything ambiguous fails closed."""
    if field.type == "bool":
        lowered = raw.strip().lower()
        if lowered in _BOOL_TRUE:
            return _check_range(field, True)
        if lowered in _BOOL_FALSE:
            return _check_range(field, False)
        raise ConfigError("INVALID_VALUE", f"env {field.env} for {field.key!r} is not a boolean: {raw!r}")
    if field.type == "int":
        if not _INT_TEXT.match(raw.strip()):
            raise ConfigError("INVALID_VALUE", f"env {field.env} for {field.key!r} is not an integer: {raw!r}")
        return _check_range(field, int(raw.strip()))
    if field.type == "float":
        text = raw.strip()
        try:
            value = float(text)
        except ValueError:
            raise ConfigError("INVALID_VALUE", f"env {field.env} for {field.key!r} is not a float: {raw!r}") from None
        if not text or not math.isfinite(value):
            raise ConfigError("INVALID_VALUE", f"env {field.env} for {field.key!r} is not a finite float: {raw!r}")
        return _check_range(field, value)
    if field.type == "str_list":
        if raw == "":
            return _check_range(field, ())
        return _check_range(field, tuple(part.strip() for part in raw.split(",") if part.strip()))
    # str / path
    if raw == "":
        raise ConfigError("INVALID_VALUE", f"env {field.env} for {field.key!r} is empty")
    return _check_range(field, raw)


# ------------------------------------------------------------------ TOML file


def _flatten(prefix: str, value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key in sorted(value):
            _flatten(f"{prefix}.{key}" if prefix else key, value[key], out)
    else:
        out[prefix] = value


def _load_file_layer(file_path: str | Path) -> dict[str, Any]:
    path = Path(file_path)
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ConfigError("CONFIG_FILE_UNREADABLE", f"cannot read config file {path}: {error}") from error
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ConfigError("CONFIG_FILE_INVALID", f"config file {path} is not valid TOML: {error}") from error
    flat: dict[str, Any] = {}
    _flatten("", document, flat)
    resolved: dict[str, Any] = {}
    for key in sorted(flat):
        spec_field = _SPEC_BY_KEY.get(key)
        if spec_field is None:
            raise ConfigError("UNKNOWN_KEY", f"config file {path} sets unknown key {key!r}")
        resolved[key] = _coerce_typed(spec_field, flat[key])
    return resolved


# ------------------------------------------------------------------ resolution


class OpsConfig:
    """The resolved configuration: one :class:`ConfigValue` per spec field."""

    def __init__(self, values: dict[str, ConfigValue]) -> None:
        unknown = set(values) - set(_SPEC_BY_KEY)
        if unknown:
            raise ConfigError("UNKNOWN_KEY", f"unknown keys: {sorted(unknown)}")
        missing = set(_SPEC_BY_KEY) - set(values)
        if missing:
            raise ConfigError("INVALID_SPEC", f"missing keys: {sorted(missing)}")
        self._values = dict(values)

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._values))

    def value(self, key: str) -> Any:
        entry = self._values.get(key)
        if entry is None:
            raise ConfigError("UNKNOWN_KEY", f"unknown configuration key {key!r}")
        return entry.value

    def source(self, key: str) -> str:
        entry = self._values.get(key)
        if entry is None:
            raise ConfigError("UNKNOWN_KEY", f"unknown configuration key {key!r}")
        return entry.source

    def provenance(self) -> dict[str, str]:
        return {key: self._values[key].source for key in sorted(self._values)}

    def __getitem__(self, key: str) -> Any:
        return self.value(key)

    def dump(self, *, redact_secrets: bool = True) -> dict[str, Any]:
        """Canonical ``{key: {value, source}}`` mapping; secrets redacted by default."""
        dumped: dict[str, Any] = {}
        for key in sorted(self._values):
            entry = self._values[key]
            spec_field = _SPEC_BY_KEY[key]
            value = entry.value
            if redact_secrets and spec_field.secret and value is not None:
                value = REDACTED_PLACEHOLDER
            if spec_field.type == "str_list" and value is not None:
                value = list(value)
            dumped[key] = {"value": value, "source": entry.source}
        return dumped

    def effective(self, *, redact_secrets: bool = True) -> dict[str, Any]:
        """Flat ``{key: value}`` mapping; secrets redacted by default."""
        return {key: entry["value"] for key, entry in self.dump(redact_secrets=redact_secrets).items()}


def load_config(
    *,
    file_path: str | Path | None = None,
    env: dict[str, str] | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> OpsConfig:
    """Resolve the configuration through defaults → file → env → CLI.

    *env* is an explicit mapping (tests); when ``None`` the real process
    environment is read. Only variables declared in the spec are read.
    """
    environ: dict[str, str] = dict(os.environ) if env is None else dict(env)
    file_values = _load_file_layer(file_path) if file_path is not None else {}

    values: dict[str, ConfigValue] = {}
    for spec_field in CONFIG_SPEC:
        resolved = ConfigValue(value=_coerce_typed(spec_field, spec_field.default), source="default")
        if spec_field.key in file_values:
            resolved = ConfigValue(value=file_values[spec_field.key], source="file")
        if spec_field.env is not None and spec_field.env in environ:
            resolved = ConfigValue(value=_coerce_env(spec_field, environ[spec_field.env]), source="env")
        values[spec_field.key] = resolved

    overrides = cli_overrides or {}
    for key in sorted(overrides):
        spec_field = _SPEC_BY_KEY.get(key)
        if spec_field is None:
            raise ConfigError("UNKNOWN_KEY", f"CLI override sets unknown key {key!r}")
        values[key] = ConfigValue(value=_coerce_typed(spec_field, overrides[key]), source="cli")
    return OpsConfig(values)


# ------------------------------------------------------------------ self docs


def _toml_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return canonical_json(value)
    if isinstance(value, tuple):
        return "[" + ", ".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def render_example_toml() -> str:
    """An annotated example file; uncommenting ``# `` lines yields the defaults."""
    lines = [
        "## know-your-soul operational configuration — every line optional.",
        "## Precedence: defaults < this file < environment < CLI overrides.",
        "",
    ]
    section = ""
    for spec_field in CONFIG_SPEC:
        prefix = spec_field.key.split(".", 1)[0]
        if prefix != section:
            section = prefix
            lines.append(f"# [{prefix}]")
        env_note = f" (env: {spec_field.env})" if spec_field.env else ""
        secret_note = " — secret, redacted on dumps" if spec_field.secret else ""
        lines.append(f"## {spec_field.description}{env_note}{secret_note}")
        if spec_field.default is None:
            sample = {"path": '"/example/path"', "str": '"example"', "int": "1"}.get(spec_field.type, '"example"')
            lines.append(f"# {spec_field.key.split('.', 1)[1]} = {sample}  # unset by default")
        else:
            lines.append(f"# {spec_field.key.split('.', 1)[1]} = {_toml_literal(spec_field.default)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def describe_spec() -> str:
    """The whole spec as a deterministic markdown table."""
    lines = [
        "# Configuration Reference",
        "",
        "| key | type | default | env | secret | description |",
        "|---|---|---|---|---|---|",
    ]
    for spec_field in CONFIG_SPEC:
        default = "—" if spec_field.default is None else _toml_literal(spec_field.default)
        env = spec_field.env or "—"
        secret = "yes" if spec_field.secret else "no"
        lines.append(
            f"| `{spec_field.key}` | {spec_field.type} | `{default}` | `{env}` | {secret} | {spec_field.description} |"
        )
    lines.append("")
    return "\n".join(lines)


def describe_config(config: OpsConfig) -> str:
    """The spec annotated with the resolved value and source of each field."""
    dumped = config.dump(redact_secrets=True)
    lines = [
        "# Resolved Configuration",
        "",
        "| key | value | source | description |",
        "|---|---|---|---|",
    ]
    for spec_field in CONFIG_SPEC:
        entry = dumped[spec_field.key]
        value = entry["value"]
        rendered = "—" if value is None else _toml_literal(tuple(value) if isinstance(value, list) else value)
        lines.append(f"| `{spec_field.key}` | `{rendered}` | {entry['source']} | {spec_field.description} |")
    lines.append("")
    return "\n".join(lines)
