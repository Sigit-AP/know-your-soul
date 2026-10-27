"""Tests for ``know_your_soul.ops.config`` (ATM wave-2 OPS).

Pins the layered configuration contract: defaults → optional TOML file →
environment → CLI overrides, typed validation with fail-closed
:class:`ConfigError`, secret redaction on every dump/describe surface, and
the self-documenting ``describe``/example renderings. No store is involved —
configuration is pure data.
"""

from __future__ import annotations

import json

import pytest

from know_your_soul.domain import canonical_json
from know_your_soul.ops.config import (
    CONFIG_SPEC,
    REDACTED_PLACEHOLDER,
    ConfigError,
    ConfigField,
    ConfigValue,
    OpsConfig,
    describe_config,
    describe_spec,
    load_config,
    render_example_toml,
)

# ------------------------------------------------------------------ spec


def test_spec_is_non_empty() -> None:
    assert len(CONFIG_SPEC) >= 15


def test_spec_keys_are_unique() -> None:
    keys = [field.key for field in CONFIG_SPEC]
    assert len(keys) == len(set(keys))


def test_spec_keys_are_dotted_and_sorted() -> None:
    keys = [field.key for field in CONFIG_SPEC]
    for key in keys:
        assert "." in key
        assert key == key.lower()
        assert " " not in key
    assert keys == sorted(keys)


def test_spec_env_names_are_unique_and_prefixed() -> None:
    envs = [field.env for field in CONFIG_SPEC if field.env is not None]
    assert len(envs) == len(set(envs))
    for env in envs:
        assert env.startswith("KNOW_YOUR_SOUL_")


def test_spec_types_are_known() -> None:
    known = {"str", "int", "float", "bool", "path", "str_list"}
    for field in CONFIG_SPEC:
        assert field.type in known, field.key


def test_spec_defaults_match_declared_type() -> None:
    for field in CONFIG_SPEC:
        if field.default is None:
            assert field.optional, field.key
            continue
        expected = {
            "str": str,
            "path": str,
            "int": int,
            "float": (int, float),
            "bool": bool,
            "str_list": tuple,
        }[field.type]
        assert isinstance(field.default, expected), field.key
        if field.type == "int":
            assert not isinstance(field.default, bool), field.key


def test_spec_secret_fields_are_optional_strings() -> None:
    for field in CONFIG_SPEC:
        if field.secret:
            assert field.optional, field.key
            assert field.type == "str", field.key


def test_spec_descriptions_are_present() -> None:
    for field in CONFIG_SPEC:
        assert field.description.strip(), field.key


def test_spec_expected_keys_exist() -> None:
    keys = {field.key for field in CONFIG_SPEC}
    for expected in (
        "store.path",
        "projection.output_root",
        "ops.root",
        "serve.api_key",
        "serve.host_allowlist",
        "retention.keep",
        "diagnose.check_permissions",
    ):
        assert expected in keys


# ------------------------------------------------------------------ defaults


def test_defaults_load_without_any_source() -> None:
    config = load_config(env={})
    assert config.value("store.path") == "know-your-soul.sqlite3"
    assert config.value("projection.output_root") == "know-your-soul-projection"
    assert config.value("retention.keep") == 3
    assert config.value("diagnose.check_permissions") is True
    assert config.value("serve.api_key") is None


def test_default_sources_are_marked_default() -> None:
    config = load_config(env={})
    for key in config.keys():
        assert config.source(key) == "default"


def test_keys_cover_the_whole_spec() -> None:
    config = load_config(env={})
    assert set(config.keys()) == {field.key for field in CONFIG_SPEC}


def test_getitem_returns_value() -> None:
    config = load_config(env={})
    assert config["retention.keep"] == 3


def test_unknown_key_access_fails_closed() -> None:
    config = load_config(env={})
    with pytest.raises(ConfigError) as excinfo:
        config.value("nope.nope")
    assert excinfo.value.code == "UNKNOWN_KEY"


# ------------------------------------------------------------------ env layer


@pytest.mark.parametrize(
    ("env_name", "raw", "key", "expected"),
    [
        ("KNOW_YOUR_SOUL_STORE", "/tmp/x.sqlite3", "store.path", "/tmp/x.sqlite3"),
        ("KNOW_YOUR_SOUL_OUTPUT_ROOT", "/tmp/out", "projection.output_root", "/tmp/out"),
        ("KNOW_YOUR_SOUL_OPS_ROOT", "/tmp/ops", "ops.root", "/tmp/ops"),
        ("KNOW_YOUR_SOUL_API_KEY", "supersecret", "serve.api_key", "supersecret"),
        ("KNOW_YOUR_SOUL_RETENTION_KEEP", "7", "retention.keep", 7),
        ("KNOW_YOUR_SOUL_STALE_AFTER_SECONDS", "12.5", "ops.stale_after_seconds", 12.5),
        ("KNOW_YOUR_SOUL_CHECK_PERMISSIONS", "no", "diagnose.check_permissions", False),
        ("KNOW_YOUR_SOUL_HOST_ALLOWLIST", "a,b, c", "serve.host_allowlist", ("a", "b", "c")),
        ("KNOW_YOUR_SOUL_MAX_BODY_BYTES", "4096", "serve.max_body_bytes", 4096),
    ],
)
def test_env_layer_overrides_defaults(env_name: str, raw: str, key: str, expected: object) -> None:
    config = load_config(env={env_name: raw})
    assert config.value(key) == expected
    assert config.source(key) == "env"


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_env_bool_true_spellings(raw: str) -> None:
    config = load_config(env={"KNOW_YOUR_SOUL_CHECK_PERMISSIONS": raw})
    assert config.value("diagnose.check_permissions") is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off"])
def test_env_bool_false_spellings(raw: str) -> None:
    config = load_config(env={"KNOW_YOUR_SOUL_CHECK_PERMISSIONS": raw})
    assert config.value("diagnose.check_permissions") is False


@pytest.mark.parametrize("raw", ["maybe", "2", "", "yesno"])
def test_env_bool_invalid_fails_closed(raw: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(env={"KNOW_YOUR_SOUL_CHECK_PERMISSIONS": raw})
    assert excinfo.value.code == "INVALID_VALUE"


@pytest.mark.parametrize("raw", ["1.5", "abc", "", "0x10", "1_000"])
def test_env_int_invalid_fails_closed(raw: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(env={"KNOW_YOUR_SOUL_RETENTION_KEEP": raw})
    assert excinfo.value.code == "INVALID_VALUE"


@pytest.mark.parametrize("raw", ["abc", "", "nan", "inf", "-inf"])
def test_env_float_invalid_fails_closed(raw: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(env={"KNOW_YOUR_SOUL_STALE_AFTER_SECONDS": raw})
    assert excinfo.value.code == "INVALID_VALUE"


def test_env_int_for_float_field_widens() -> None:
    config = load_config(env={"KNOW_YOUR_SOUL_STALE_AFTER_SECONDS": "10"})
    assert config.value("ops.stale_after_seconds") == 10.0
    assert isinstance(config.value("ops.stale_after_seconds"), float)


def test_env_unknown_variable_is_ignored() -> None:
    config = load_config(env={"KNOW_YOUR_SOUL_TOTALLY_UNKNOWN": "x", "PATH": "/bin"})
    assert config.value("retention.keep") == 3


def test_env_empty_string_for_optional_str_fails_closed() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(env={"KNOW_YOUR_SOUL_API_KEY": ""})
    assert excinfo.value.code == "INVALID_VALUE"


def test_env_str_list_empty_means_empty() -> None:
    config = load_config(env={"KNOW_YOUR_SOUL_RETENTION_PINNED": ""})
    assert config.value("retention.pinned") == ()


# ------------------------------------------------------------------ file layer


def test_file_layer_overrides_defaults(tmp_path) -> None:
    path = tmp_path / "cfg.toml"
    path.write_text("[retention]\nkeep = 9\n", encoding="utf-8")
    config = load_config(file_path=path, env={})
    assert config.value("retention.keep") == 9
    assert config.source("retention.keep") == "file"
    assert config.source("store.path") == "default"


def test_file_supports_dotted_keys(tmp_path) -> None:
    path = tmp_path / "cfg.toml"
    path.write_text('retention.keep = 4\nstore.path = "x.sqlite3"\n', encoding="utf-8")
    config = load_config(file_path=path, env={})
    assert config.value("retention.keep") == 4
    assert config.value("store.path") == "x.sqlite3"


def test_file_all_types_round_trip(tmp_path) -> None:
    path = tmp_path / "cfg.toml"
    path.write_text(
        'store.path = "a.sqlite3"\n'
        'serve.host_allowlist = ["h1", "h2"]\n'
        "retention.keep = 2\n"
        "ops.stale_after_seconds = 3.5\n"
        "diagnose.check_permissions = false\n"
        'serve.api_key = "sekrit"\n',
        encoding="utf-8",
    )
    config = load_config(file_path=path, env={})
    assert config.value("store.path") == "a.sqlite3"
    assert config.value("serve.host_allowlist") == ("h1", "h2")
    assert config.value("retention.keep") == 2
    assert config.value("ops.stale_after_seconds") == 3.5
    assert config.value("diagnose.check_permissions") is False
    assert config.value("serve.api_key") == "sekrit"


def test_missing_file_fails_closed(tmp_path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(file_path=tmp_path / "absent.toml", env={})
    assert excinfo.value.code == "CONFIG_FILE_UNREADABLE"


def test_invalid_toml_fails_closed(tmp_path) -> None:
    path = tmp_path / "cfg.toml"
    path.write_text("this is not toml = = =\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(file_path=path, env={})
    assert excinfo.value.code == "CONFIG_FILE_INVALID"


def test_file_unknown_key_fails_closed(tmp_path) -> None:
    path = tmp_path / "cfg.toml"
    path.write_text("[bogus]\nkey = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(file_path=path, env={})
    assert excinfo.value.code == "UNKNOWN_KEY"


@pytest.mark.parametrize(
    "payload",
    [
        "store.path = 3",  # int for path
        "retention.keep = 1.5",  # float for int
        "retention.keep = true",  # bool for int
        'diagnose.check_permissions = "yes"',  # str for bool
        'serve.host_allowlist = "a,b"',  # str for list
        'serve.host_allowlist = ["a", 3]',  # mixed list
        'ops.stale_after_seconds = "fast"',  # str for float
        "serve.max_body_bytes = 0",  # below minimum
        "retention.keep = -1",  # below minimum
        "ops.stale_after_seconds = 0",  # exclusive minimum
    ],
)
def test_file_type_and_range_violations_fail_closed(tmp_path, payload: str) -> None:
    path = tmp_path / "cfg.toml"
    path.write_text(payload + "\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(file_path=path, env={})
    assert excinfo.value.code in {"TYPE_MISMATCH", "OUT_OF_RANGE"}


def test_file_int_for_float_field_widens(tmp_path) -> None:
    path = tmp_path / "cfg.toml"
    path.write_text("ops.stale_after_seconds = 5\n", encoding="utf-8")
    config = load_config(file_path=path, env={})
    assert config.value("ops.stale_after_seconds") == 5.0


def test_file_must_be_a_table_at_top_level(tmp_path) -> None:
    path = tmp_path / "cfg.toml"
    # A scalar under a known section name colliding with a field is unknown.
    path.write_text("retention = 3\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(file_path=path, env={})
    assert excinfo.value.code == "UNKNOWN_KEY"


# ------------------------------------------------------------------ cli layer


def test_cli_overrides_env_and_file(tmp_path) -> None:
    path = tmp_path / "cfg.toml"
    path.write_text("retention.keep = 2\n", encoding="utf-8")
    config = load_config(
        file_path=path,
        env={"KNOW_YOUR_SOUL_RETENTION_KEEP": "5"},
        cli_overrides={"retention.keep": 11},
    )
    assert config.value("retention.keep") == 11
    assert config.source("retention.keep") == "cli"


def test_env_beats_file(tmp_path) -> None:
    path = tmp_path / "cfg.toml"
    path.write_text("retention.keep = 2\n", encoding="utf-8")
    config = load_config(file_path=path, env={"KNOW_YOUR_SOUL_RETENTION_KEEP": "5"})
    assert config.value("retention.keep") == 5
    assert config.source("retention.keep") == "env"


def test_cli_unknown_key_fails_closed() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(env={}, cli_overrides={"bogus.key": 1})
    assert excinfo.value.code == "UNKNOWN_KEY"


def test_cli_values_are_typed_already_and_validated() -> None:
    config = load_config(env={}, cli_overrides={"retention.keep": 4, "diagnose.check_permissions": False})
    assert config.value("retention.keep") == 4
    assert config.value("diagnose.check_permissions") is False


def test_cli_wrong_type_fails_closed() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(env={}, cli_overrides={"retention.keep": "four"})
    assert excinfo.value.code == "TYPE_MISMATCH"


def test_cli_none_clears_optional_field() -> None:
    config = load_config(
        env={"KNOW_YOUR_SOUL_API_KEY": "x"},
        cli_overrides={"serve.api_key": None},
    )
    assert config.value("serve.api_key") is None
    assert config.source("serve.api_key") == "cli"


# ------------------------------------------------------------------ redaction


def test_dump_redacts_secret_values() -> None:
    config = load_config(env={"KNOW_YOUR_SOUL_API_KEY": "supersecretvalue"})
    dumped = config.dump()
    assert dumped["serve.api_key"]["value"] == REDACTED_PLACEHOLDER
    assert "supersecretvalue" not in json.dumps(dumped)


def test_dump_without_redaction_shows_value() -> None:
    config = load_config(env={"KNOW_YOUR_SOUL_API_KEY": "supersecretvalue"})
    dumped = config.dump(redact_secrets=False)
    assert dumped["serve.api_key"]["value"] == "supersecretvalue"


def test_dump_keeps_provenance_and_is_canonical() -> None:
    config = load_config(env={"KNOW_YOUR_SOUL_RETENTION_KEEP": "6"})
    dumped = config.dump()
    assert dumped["retention.keep"] == {"value": 6, "source": "env"}
    assert list(dumped) == sorted(dumped)
    text = canonical_json(dumped)
    assert text == canonical_json(config.dump())


def test_none_secret_is_not_placeholder() -> None:
    config = load_config(env={})
    assert config.dump()["serve.api_key"]["value"] is None


def test_describe_config_redacts_secrets() -> None:
    config = load_config(env={"KNOW_YOUR_SOUL_API_KEY": "supersecretvalue"})
    text = describe_config(config)
    assert REDACTED_PLACEHOLDER in text
    assert "supersecretvalue" not in text


# ------------------------------------------------------------------ self docs


def test_describe_spec_mentions_every_field() -> None:
    text = describe_spec()
    for field in CONFIG_SPEC:
        assert field.key in text
        assert field.description in text
        if field.env is not None:
            assert field.env in text


def test_describe_spec_marks_secrets() -> None:
    text = describe_spec()
    assert "secret" in text.lower()


def test_describe_config_includes_current_source() -> None:
    config = load_config(env={"KNOW_YOUR_SOUL_RETENTION_KEEP": "8"})
    text = describe_config(config)
    assert "retention.keep" in text
    assert "env" in text


def test_render_example_toml_is_parseable_and_equivalent() -> None:
    example = render_example_toml()
    assert example.strip()
    # The commented example must parse to exactly the defaults when uncommented.
    uncommented = "\n".join(
        line[1:] if line.startswith("# ") else line for line in example.splitlines() if not line.startswith("##")
    )
    import tomllib

    parsed = tomllib.loads(uncommented)
    assert isinstance(parsed, dict)
    config = load_config(env={})
    assert set(config.keys()) == {field.key for field in CONFIG_SPEC}


def test_config_value_is_frozen() -> None:
    value = ConfigValue(value=1, source="cli")
    with pytest.raises(Exception):  # noqa: B017 - frozen dataclass refuses assignment
        value.value = 2  # type: ignore[misc]


def test_config_field_is_frozen() -> None:
    field = CONFIG_SPEC[0]
    with pytest.raises(Exception):  # noqa: B017
        field.key = "mutated"  # type: ignore[misc]


def test_config_error_carries_code_and_message() -> None:
    error = ConfigError("UNKNOWN_KEY", "no such key: bogus")
    assert error.code == "UNKNOWN_KEY"
    assert "bogus" in str(error)


def test_ops_config_is_mapping_like() -> None:
    config = load_config(env={})
    assert isinstance(config, OpsConfig)
    assert len(config.keys()) == len(CONFIG_SPEC)
    assert config.provenance()["store.path"] == "default"


# ------------------------------------------------------------------ hardening


def test_config_field_rejects_unknown_type() -> None:
    with pytest.raises(ConfigError):
        ConfigField(key="a.b", type="set", default=None, optional=True, description="x")


def test_config_field_rejects_bad_key() -> None:
    with pytest.raises(ConfigError):
        ConfigField(key="UPPER", type="str", default="x", description="x")


def test_config_value_rejects_bad_source() -> None:
    with pytest.raises(ConfigError):
        ConfigValue(value=1, source="magic")


def test_ops_config_rejects_unknown_keys() -> None:
    config = load_config(env={})
    values = {key: ConfigValue(value=config.value(key), source="default") for key in config.keys()}
    values["bogus.key"] = ConfigValue(value=1, source="default")
    with pytest.raises(ConfigError) as excinfo:
        OpsConfig(values)
    assert excinfo.value.code == "UNKNOWN_KEY"


def test_ops_config_rejects_missing_keys() -> None:
    with pytest.raises(ConfigError):
        OpsConfig({})


def test_env_range_violation_fails_closed() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(env={"KNOW_YOUR_SOUL_STALE_AFTER_SECONDS": "0"})
    assert excinfo.value.code == "OUT_OF_RANGE"


def test_env_negative_retention_fails_closed() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(env={"KNOW_YOUR_SOUL_RETENTION_KEEP": "-1"})
    assert excinfo.value.code == "OUT_OF_RANGE"


def test_cli_range_violation_fails_closed() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(env={}, cli_overrides={"serve.max_body_bytes": 0})
    assert excinfo.value.code == "OUT_OF_RANGE"


def test_cli_bool_for_int_fails_closed() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(env={}, cli_overrides={"retention.keep": True})
    assert excinfo.value.code == "TYPE_MISMATCH"


def test_effective_mapping_is_flat_and_redacted() -> None:
    config = load_config(env={"KNOW_YOUR_SOUL_API_KEY": "sekrit", "KNOW_YOUR_SOUL_RETENTION_KEEP": "5"})
    effective = config.effective()
    assert effective["retention.keep"] == 5
    assert effective["serve.api_key"] == REDACTED_PLACEHOLDER
    assert "sekrit" not in json.dumps(effective)


def test_process_environ_is_read_when_env_not_given(monkeypatch) -> None:
    monkeypatch.setenv("KNOW_YOUR_SOUL_RETENTION_KEEP", "12")
    config = load_config()
    assert config.value("retention.keep") == 12
    assert config.source("retention.keep") == "env"


def test_file_secret_is_redacted_in_dump_and_describe(tmp_path) -> None:
    path = tmp_path / "cfg.toml"
    path.write_text('serve.api_key = "filesecret"\n', encoding="utf-8")
    config = load_config(file_path=path, env={})
    assert config.value("serve.api_key") == "filesecret"
    assert "filesecret" not in json.dumps(config.dump())
    assert "filesecret" not in describe_config(config)


def test_describe_config_renders_none_as_dash() -> None:
    text = describe_config(load_config(env={}))
    assert "| `serve.api_key` | `—` | default |" in text


def test_example_toml_uncommented_matches_defaults(tmp_path) -> None:
    uncommented = "\n".join(
        line[1:] if line.startswith("# ") else line
        for line in render_example_toml().splitlines()
        if not line.startswith("##")
    )
    path = tmp_path / "example.toml"
    path.write_text(uncommented, encoding="utf-8")
    config = load_config(file_path=path, env={})
    defaults = load_config(env={})
    for key in defaults.keys():
        field_default = defaults.value(key)
        if field_default is None:
            continue  # example files carry sample values for optional fields
        assert config.value(key) == field_default, key


def test_load_config_layers_full_chain(tmp_path) -> None:
    path = tmp_path / "cfg.toml"
    path.write_text('store.path = "from-file.sqlite3"\nretention.keep = 2\n', encoding="utf-8")
    config = load_config(
        file_path=path,
        env={"KNOW_YOUR_SOUL_RETENTION_KEEP": "5"},
        cli_overrides={"ops.actor": "ci"},
    )
    assert config.value("store.path") == "from-file.sqlite3"
    assert config.source("store.path") == "file"
    assert config.value("retention.keep") == 5
    assert config.source("retention.keep") == "env"
    assert config.value("ops.actor") == "ci"
    assert config.source("ops.actor") == "cli"
    assert config.value("projection.output_root") == "know-your-soul-projection"
    assert config.source("projection.output_root") == "default"


def test_cli_none_on_required_field_fails_closed() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(env={}, cli_overrides={"retention.keep": None})
    assert excinfo.value.code == "TYPE_MISMATCH"


def test_source_unknown_key_fails_closed() -> None:
    config = load_config(env={})
    with pytest.raises(ConfigError) as excinfo:
        config.source("nope.nope")
    assert excinfo.value.code == "UNKNOWN_KEY"


def test_file_empty_str_for_required_path_fails_closed(tmp_path) -> None:
    path = tmp_path / "cfg.toml"
    path.write_text('store.path = ""\n', encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(file_path=path, env={})
    assert excinfo.value.code == "TYPE_MISMATCH"
