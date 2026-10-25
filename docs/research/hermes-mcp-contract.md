# Hermes Agent MCP contract — external consumer, no sampling, no provider config

Evidence artifact for Task 1 of `docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md`.

- Hermes inspected commit: `a480a27e76dcad8da7665408e54c6ec270c10f14` ("fix(agent): redact Context7 API keys by their vendor prefix"), clone at `/root/.hermes/cache/scratch/hermes-ref-20261024` (`git rev-parse HEAD` verified at inspection time).
- Graphify side of the contract: `docs/research/graphify-mcp-security-deep.md` (adopt/reject/adapt tables) and `docs/research/graphify-feature-matrix.md` adapters A5/A6/A7.
- Scope: this document records **how Hermes consumes an MCP server**, so the product can be a pure external consumer. Hermes source is never modified and its configuration is never hand-edited by this repository.

## 1. Configuration surface (what the operator writes, what Hermes reads)

| Fact | Evidence |
|---|---|
| Servers come from the `mcp_servers` mapping of Hermes' `config.yaml`, read by `_load_mcp_config()`; the function returns `{}` in safe mode or on load error | `tools/mcp_tool_config.py:690-721` |
| A server entry is a dict; `enabled` is parsed as a boolish value (default true) by the single reader of that key | `tools/mcp_tool_common.py:140-144` |
| Transport is stdio when the entry has no `url`; a `url` entry reports `cfg.get("transport", "http")`, so Streamable HTTP is a config choice, not a separate server type | `tools/mcp_tool_discovery.py:866` |
| `${VAR}` interpolation is supported; an unresolved `${VAR}` inside a remote `url`/`headers` fails closed with a `missing_credentials` error instead of sending a literal placeholder | `tools/mcp_tool_config.py:585-599` |
| Entries are pre-filtered for exfiltration/persistence shapes before any spawn | `tools/mcp_tool_config.py:643-660`; `hermes_cli/mcp_security.py:89-139` |
| Tools are discovered by `tools/list` and registered as first-class agent tools named `mcp__<server>__<tool>` (prefix constant, 64-char clamp) | `tools/mcp_tool_discovery.py:899`; `tools/mcp_tool_schema.py:153-173` (`MCP_TOOL_NAME_PREFIX` at `:159`) |
| A server that omits the `tools` capability is tolerated: `tools/list` returning `-32601` does not kill the connection | `tools/mcp_tool_transport.py:158-166` |

Consequence for the product: a server entry needs only `command`/`args` (stdio) or `url` (HTTP). No provider key, base URL or model setting is required anywhere.

## 2. Environment isolation for stdio children

Hermes does **not** hand its own environment to a stdio MCP server:

- `_SAFE_ENV_KEYS` is `PATH`, `HOME`, `USER`, `LANG`, `LC_ALL`, `TERM`, `SHELL`, `TMPDIR` (`tools/mcp_tool_config.py:147`); Windows location variables are added case-insensitively.
- `_build_safe_env()` (`tools/mcp_tool_config.py:199`) keeps that baseline plus `XDG_*` and variables injected by an external secret source, then layers the server entry's own `env`. API keys and tokens held by the agent process are therefore not inherited by the server.
- Unset `${VAR}` references in `env`/`url`/`headers` are surfaced rather than silently passed.

Product rule: the server must not require any `env` entry. `soul_knowledge.runtime_guard.scrubbed_env()` is the second, independent layer — the server itself refuses to run with provider credentials or model-asset variables present in a child it spawns, and `verify_runtime()` proves the surface is clean.

## 3. Sampling and elicitation: what Hermes offers, and our refusal

Hermes registers **client-side** sampling support only when the server config enables it:

| Fact | Evidence |
|---|---|
| `SamplingHandler` implements `sampling/createMessage` for one server, with rate limit, timeout, token cap and audit logging | `tools/mcp_tool_sampling.py:88-186` |
| `session_kwargs()` passes `sampling_callback` and `sampling_capabilities` into the MCP `ClientSession`, which is what advertises the client sampling capability to the server | `tools/mcp_tool_sampling.py:175-182` |
| The handler is only constructed when `config["sampling"]["enabled"]` is not false **and** the SDK types exist | `tools/mcp_tool_server_run.py:268-270` |
| Elicitation is built the same way (`elicitation.enabled`, default true) | `tools/mcp_tool_server_run.py:274-276` |
| A logging callback and a message handler are added when the SDK supports them | `tools/mcp_tool_transport.py:171-186` |
| Sampling usage metrics are reported per server in discovery output | `tools/mcp_tool_discovery.py:872-873` |

Consequence: sampling is **optional on both sides**. Hermes will offer it by default unless the server entry sets `sampling: {enabled: false}`, and the server can only use it if it also registers a `sampling/createMessage` handler. Neither side may be assumed.

### 3.1 Binding no-sampling contract (product side)

The product must satisfy all of the following, and Tasks 8/10 must test them:

1. **Advertised capabilities are exactly `{"tools": …, "resources": …}`.** No `sampling`, `prompts`, `logging`, `completions` or `elicitation` capability is declared in the `initialize` result.
2. **No sampling handler exists in the server.** `soul_knowledge.runtime_guard.assert_no_sampling_handler(server)` reads the server's request-handler registry and raises `CapabilityViolation` if any method in `FORBIDDEN_MCP_METHODS` is registered; `assert_mcp_capabilities()` enforces the exact capability key set. A server object with no verifiable handler registry is also refused (fail closed).
3. **A client request for a forbidden method is refused, not answered.** `sampling/createMessage`, `prompts/list`, `prompts/get`, `logging/setLevel` and `completion/complete` return method-not-found; no model call is attempted anywhere in the process.
4. **No model configuration is accepted.** Tool schemas reject unknown properties, so a caller cannot smuggle model/provider parameters; the server has no config key for a provider, model name, API key or base URL, and it will not read one from the environment (runtime guard).
5. **No outbound sockets.** `soul_knowledge.runtime_guard.deny_outbound_sockets()` denies AF_INET/AF_INET6 connect/connect-ex/sendto/sendmsg/create_connection while leaving AF_UNIX usable for the local API; `verify_runtime()` proves a probe is denied.
6. **Recommended Hermes entry is capability-minimal.** The documented server entry sets `sampling: {enabled: false}` and `elicitation: {enabled: false}` in addition to the server's own refusal, so the sampling capability is never even offered. Neither setting is relied on for correctness: the server refuses regardless.
7. **No `project_path`.** The graph root is one startup-configured allowlisted directory; a caller-supplied path is not a schema property and is rejected as an unknown property. This removes the arbitrary-root disclosure path observed in Graphify (`graphify/serve.py:2267-2274`, research artifact F1 probe).
8. **Tools + sanitized resources only.** No PR tools, no `gh`, no provider imports, no prompts. Resources are generated from the sanitized projection JSON with size caps and a content hash, never from raw Graphify reports.

### 3.2 Why the client may still hold keys — and why that is not our boundary

Hermes is an external consumer that supplies its own model. It may hold provider credentials for its own inference. The boundary enforced here is that **this system** never receives, stores, reads, or uses them: Hermes' stdio env filter keeps them out of the child, the server's scrubber drops any that arrive, `verify_runtime(strict_env=True)` refuses a dirty ambient environment, and no code path opens an outbound socket. This is the operational no-AI boundary recorded in ADR 0001; if an operator rejects it, the correct action is to replace the Graphify distribution with a reviewed deterministic-only one rather than weakening the rule.

## 4. Connection recipe (documentation target for Task 8)

```yaml
mcp_servers:
  soul-knowledge:
    command: /root/superpower-knowledge-soul/.venv/bin/soul-knowledge
    args: ["mcp", "--graph-root", "/root/superpower-knowledge-soul/.local/graph", "--db", "/root/superpower-knowledge-soul/.local/soul.sqlite3"]
    enabled: true
    sampling: {enabled: false}
    elicitation: {enabled: false}
    # no env, no provider keys, no project_path, no HTTP url required
```

Optional Streamable HTTP entry: keep the default loopback/Unix-socket boundary (spec section 5, Task 9) and add no provider configuration. Hermes will register each of the ten knowledge operations as `mcp__soul-knowledge__<operation>` and call them like native tools.

## 5. Residual contract items owned by later tasks

| Item | Owner | Contract test |
|---|---|---|
| Exactly ten tools, strict schemas/ranges, `ToolError` → `isError:true` | Task 8 | `tests/test_mcp_server.py::test_exactly_ten_tools_with_strict_schemas` |
| Fixed root + no `project_path` + symlink re-verification | Task 8 | `tests/test_mcp_server.py::test_fixed_root_containment_rejects_project_path` |
| Exact capability equality and absence of sampling/prompts/logging/completions | Task 8 | `tests/test_mcp_server.py::test_capabilities_are_exactly_tools_and_resources`, `…::test_no_sampling_prompts_logging_or_completion_handlers` |
| Fresh-client stdio discovery and canonical payload parity with CLI/API | Task 8 | `tests/test_mcp_stdio_e2e.py::test_fresh_client_discovers_ten_tools`, `…::test_canonical_payload_parity` |
| Guards re-verified inside a scrubbed fresh process (imports, sockets, capabilities) | Task 10 | `tests/test_acceptance_e2e.py::test_locked_environment_and_scrubbed_runtime` |

Task 1 supplies only `src/soul_knowledge/runtime_guard.py` and the packaging the contract depends on; it does not implement the MCP server.