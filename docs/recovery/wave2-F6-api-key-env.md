# Wave-2 finding F6 — API key exposed via process argv

## Evidence

Audit F6: `soul-knowledge serve --api-key hunter2 ...` (and
`soul-knowledge-mcp --api-key ...`) put the secret in argv, visible to every
local user via `ps`/procfs; no environment-variable alternative existed.

## Root cause

`--api-key` was the only way to configure the key.

## Affected global criterion

Spec 13.1 credential hygiene on operator surfaces.

## Fix applied

`--api-key-env NAME` (mutually exclusive with `--api-key`) reads the key from
the named environment variable on both `soul-knowledge serve` and
`soul-knowledge-mcp`. A named-but-unset/empty variable fails closed (exit 2,
`CLI_USAGE` envelope on the CLI) rather than serving unauthenticated. The
`--api-key` help text now documents the argv exposure. Payload argv exposure
was already mitigated (`-` reads the payload from stdin).

## Regression probes

- `tests/test_cli.py::test_cli_serve_api_key_env_avoids_argv` (env value
  reaches `serve()` as `api_key`, argv never carries it)
- `test_cli_serve_api_key_env_missing_var_fails_closed` (unset var → exit 2,
  names the variable)
- `test_cli_serve_api_key_and_api_key_env_are_mutually_exclusive`
  (argparse exits 2)

## Rollback note

Remove the mutually exclusive group and env resolution; `--api-key` keeps its
old meaning.
