# Graphify MCP and Security Deep Research

- Product context: standalone, deterministic-only knowledge product for Hermes (CLI, local API, MCP). No provider dependencies, credentials, model calls, model assets, sampling handlers, or outbound provider traffic; deterministic runtime guards only.
- Primary source: Graphify clone at `/root/.hermes/cache/scratch/graphify-ref-20261024`, commit `92741d162b2094ffc2fed58cfbd8ee823e9da62c`, package version `0.9.95`.
- Path citations below are relative to that clone root unless an absolute path is shown.
- Runtime probe interpreter: system Python `3.14.9` with `PYTHONPATH=<clone>:/opt/graphify-venv/lib/python3.14/site-packages` (NetworkX `3.7` from the host Graphify venv), `mcp==2.3.0`, `starlette==1.3.1`, `pydantic==2.13.4`. The clone itself is not installed; the probe imported clone sources directly. No network was used by the probes.
- Scope: `graphify/serve.py`, `graphify/security.py`, `SECURITY.md`, MCP tests, path/input validation, cache/hot-reload, resources/tools/capabilities, and optional outbound/network/model surfaces. Code-only AST extractors are out of scope except where they affect packaging/imports.

---

## 1. MCP server: construction, transports, capabilities, cache

### 1.1 One builder, two transports

`_build_server(graph_path)` constructs a low-level `mcp.server.Server` and registers **all** tools and resources once; the caller chooses stdio or Streamable HTTP (`graphify/serve.py:2229-2236`). Stdio is the default and calls `stdio_server()` (`graphify/serve.py:2843-2859`). HTTP is opt-in via `--transport http` and defaults to `127.0.0.1:8080` with mount path `/mcp` (`graphify/serve.py:3061-3127`).

Observed at runtime (in-process ASGI `initialize`):

```json
{"resources":{"listChanged":false,"subscribe":false},"tools":{"listChanged":false}}
```

Server info was `{"name":"graphify","version":"0.9.95"}`. `prompts/list` and `logging/setLevel` both returned JSON-RPC `-32601 Method not found`. This is consistent with the code: only tools/resources handlers are bound (`graphify/serve.py:2791-2838`), and MCP SDK `Server.get_capabilities()` advertises a capability only when the corresponding request handler exists (verified in installed `mcp==2.3.0`). There is **no** sampling handler, prompt handler, logging handler, completion handler, or experimental capability registered anywhere in `serve.py`.

A stdio probe sent a leading blank line, `initialize`, `notifications/initialized`, and `tools/list`; the server returned the `initialize` result (capabilities above) with exit code 0, showing `_filter_blank_stdin()` kept the stream alive (`graphify/serve.py:2151-2175`). The blank-line shim installs an OS pipe on fd 0 and a daemon relay thread, then replaces `sys.stdin` (`graphify/serve.py:2159-2175`); all relay exceptions are swallowed (`graphify/serve.py:2169-2170`). No test invokes this shim (see §5).

### 1.2 Tool surface

Ten tools are declared (`graphify/serve.py:2300-2436`) and dispatched by name (`graphify/serve.py:2689-2700`):

| Tool | Required schema keys | Handler | Notes |
|---|---|---|---|
| `query_graph` | `question` | `graphify/serve.py:2457-2485` | BFS/DFS keyword query; logs queries when enabled |
| `get_node` | none | `graphify/serve.py:2487-2518` | shared resolver with `get_neighbors` |
| `get_neighbors` | none | `graphify/serve.py:2520-2534` | relation filter, budget |
| `get_community` | `community_id` | `graphify/serve.py:2536-2553` | `int()` coercion only |
| `god_nodes` | none | `graphify/serve.py:2555-2564` | degree ranking |
| `graph_stats` | none | `graphify/serve.py:2566-2577` | reads git HEAD from the graph repo |
| `shortest_path` | `source`,`target` | `graphify/serve.py:2601-2602`, `2067-2148` | directed by default |
| `list_prs` | none | `graphify/serve.py:2604-2615` | **outbound `gh` network call** |
| `get_pr_impact` | `pr_number` | `graphify/serve.py:2617-2645` | **outbound `gh` network call** |
| `triage_prs` | none | `graphify/serve.py:2647-2687` | **concurrent outbound `gh` calls** |

Every tool schema also gets an optional `project_path` injected after declaration (`graphify/serve.py:2441-2454`), documented as "Absolute path to a project directory containing graphify-out/graph.json" (`graphify/serve.py:2447-2453`). The tests explicitly assert this property is optional on every tool and that it routes to an arbitrary caller-supplied project directory (`tests/test_serve_http.py:205-229`).

### 1.3 Resource surface

Six resources are declared (`graphify/serve.py:2711-2721`): `graphify://report`, `graphify://stats`, `graphify://god-nodes`, `graphify://surprises`, `graphify://audit`, `graphify://questions`. There are no resource templates and no `resources/subscribe`; observed capability is `subscribe:false`.

`read_resource` handles each URI directly (`graphify/serve.py:2723-2772`). `graphify://report` reads `GRAPH_REPORT.md` from disk and returns the **raw file text** (`graphify/serve.py:2727-2730`). Other resources return computed values; `graphify://surprises` prints raw `source`/`target`/`relation` values (`graphify/serve.py:2741-2744`) and `graphify://questions` prints raw question strings (`graphify/serve.py:2763-2768`) without the `sanitize_label` pass used in node/edge rendering.

Runtime evidence: a report containing `\x1b[31mEVIL\x1b[0m <script>alert(1)</script>` was returned byte-for-byte by `resources/read` (JSON-escaped only), proving the resource path bypasses the label sanitization used for tool output.

### 1.4 Graph context cache, hot reload, per-graph caches

- Capacity: `GRAPHIFY_MAX_CONTEXTS` defaults to 8, invalid/blank → 8, zero/negative → 1 (`graphify/serve.py:123-137`).
- Cache class: one pinned default graph plus a thread-locked LRU of project graphs (`graphify/serve.py:140-199`).
- Cache key: `(st_mtime_ns, st_size)` from `Path(resolved_path).stat()` (`graphify/serve.py:182-186`).
- Pinned default: never counted against the project LRU and never evicted (`graphify/serve.py:177-179`, `186-199`).
- On load, the trigram index is built eagerly ("Warm the index before exposing the graph") (`graphify/serve.py:160-168`).
- Hot reload happens per request: `_select_graph` resolves the path, calls `_load_ctx`, and rebinds `G/communities/active_graph_path` (`graphify/serve.py:2290-2294`); the cache reloads only when the stat key changes (`graphify/serve.py:187-194`).
- Per-graph query caches are attached to `G.graph`: IDF (`graphify/serve.py:377-399`), trigram index (`graphify/serve.py:470-493`), undirected traversal view (`graphify/serve.py:1360-1408`), and shortest-path search graph per direction (`graphify/serve.py:2018-2063`). Because a reload creates a new graph object, these caches cannot outlive their graph.
- Tests pin the reuse and reload behavior: `tests/test_mcp_graph_view_cache.py:60-124`, `tests/test_serve.py:760-800`, `tests/test_serve.py:438-444`.

Behaviour confirmed by probe: a `graph_stats` call with `project_path` pointing at a different tree loaded that tree's `graphify-out/graph.json` and reported its node count; a symlink `graphify-out/graph.json -> /…/secret.json` outside the tree was also loaded and reported. This is analysed as finding F1.

### 1.5 HTTP transport security controls

- Raw ASGI API-key middleware, deliberately not `BaseHTTPMiddleware`, to avoid buffering the SSE stream (`graphify/serve.py:2878-2885`).
- Accepts `X-API-Key` or `Authorization: Bearer …` (scheme case-insensitive) (`graphify/serve.py:2896-2904`); constant-time compare via `hmac.compare_digest` (`graphify/serve.py:2904`); 401 JSON body otherwise (`graphify/serve.py:2905-2915`).
- Empty/whitespace key normalizes to "no auth" (`graphify/serve.py:2955-2957`, tested at `tests/test_serve_http.py:123-128`).
- DNS-rebinding protection: wildcard binds (`0.0.0.0`, `::`, empty) **disable** the protection (`graphify/serve.py:2961-2966`); specific/loopback binds restrict `Host` to the bind address plus localhost/127.0.0.1, with and without port, and handle bracketed IPv6 (`graphify/serve.py:2967-2972`). Tests cover bracketed IPv6 acceptance and foreign-Host 421 (`tests/test_serve_http.py:541-555`).
- Session manager: `json_response`, `stateless`, `session_idle_timeout` (default 3600; forced to `None` in stateless mode) (`graphify/serve.py:2926-2983`).
- CLI exposes `--host`, `--port`, `--api-key`, `--path`, `--json-response`, `--stateless`, `--session-timeout`, but **no TLS options** (`graphify/serve.py:3061-3111`).
- Wildcard bind without an API key only prints a warning and still serves (`graphify/serve.py:3047-3058`).

---

## 2. `security.py` and `SECURITY.md`: stated model vs implemented helpers

### 2.1 Strong, reusable primitives

- URL validation rejects non-http(s) schemes, embedded credentials, invalid/zero ports, missing hostnames, known cloud metadata hosts, and any DNS answer in a private/reserved/loopback/link-local/multicast/unspecified range or RFC 6598 CGN space (`graphify/security.py:20`, `68-76`, `83-102`, `105-158`). NAT64 well-known-prefix addresses are unwrapped to their embedded IPv4 before the check (`graphify/security.py:83-93`).
- SSRF-guarded HTTP/HTTPS connections resolve DNS exactly once, validate the resulting IP, and connect to that validated IP, eliminating the classic DNS-rebind TOCTOU; TLS keeps `server_hostname` as the original hostname for SNI/cert validation (`graphify/security.py:161-229`). Redirect targets are re-validated (`graphify/security.py:246-255`). The opener is built per fetch; no process-global `socket.getaddrinfo` monkey-patching (`graphify/security.py:258-266`).
- `safe_fetch` streams with a 50 MB cap (binary) / 10 MB (`safe_fetch_text`), raises on non-2xx, and never silently treats an error page as content (`graphify/security.py:21-22`, `273-323`).
- Graph-load memory-bomb guard rejects a `graph.json` larger than 512 MiB before `json.loads`; `GRAPHIFY_MAX_GRAPH_BYTES` can raise it and is parsed per call (`graphify/security.py:24-66`, `372-398`).
- `validate_graph_path` resolves the path, requires the base directory to exist, and requires the resolved path to be relative to the base (`graphify/security.py:330-369`).
- `sanitize_label` strips C0 controls and DEL and caps length at 256 (`graphify/security.py:405-406`, `441-452`). `sanitize_metadata` recursively bounds strings (512 chars), list items (50), drops empty keys, and HTML-escapes strings with `quote=True` (`graphify/security.py:459-507`). `redact_secrets` replaces vendor-shaped credentials, PEM headers, Bearer tokens, and URL userinfo with `[redacted]` (`graphify/security.py:409-438`).
- Tests cover scheme/credential/port rejection (`tests/test_security.py:35-84`), fetch size/non-2xx (`tests/test_security.py:102-144`), path traversal and base-existence (`tests/test_security.py:178-224`), label control stripping and the deliberate non-HTML-escaping contract (`tests/test_security.py:231-255`), size caps (`tests/test_security.py:262-371`), metadata bounds (`tests/test_security.py:378-493`), redaction shapes and over-redaction guards (`tests/test_security.py:495-543`), and multicast/unspecified IP blocking (`tests/test_frontmatter_and_ssrf_hardening.py:15-21`).

### 2.2 Documented claims that the MCP path does not satisfy

`SECURITY.md:34` states: "Path traversal in MCP server — `security.validate_graph_path()` resolves paths and requires them to be inside `graphify-out/`." `ARCHITECTURE.md:30` and `ARCHITECTURE.md:97-102` repeat that all external input and graph file paths pass through `security.py`.

That is not true of `serve.py`:

- `graphify/serve.py:16` imports only `sanitize_label`, `check_graph_file_size_cap`, and `_CONTROL_CHAR_RE` from security.
- `grep -c validate_graph_path graphify/serve.py` → **0** (and no transitive import of it in the MCP call path).
- The MCP graph path is built as `Path(project_path) / GRAPHIFY_OUT / "graph.json"` from the caller-supplied `project_path` (`graphify/serve.py:2267-2274`), then resolved and loaded (`graphify/serve.py:2290-2294`, `2257-2265`).
- `_read_graph` checks only that the resolved suffix is `.json`, that it exists, and that it is under the size cap (`graphify/serve.py:44-55`). There is no containment check against an allowed root.
- `tests/test_serve_http.py:219-229` celebrates routing to any caller-supplied project, and `tests/test_serve_http.py:282-303` asserts bad/corrupt project graphs become tool errors rather than process exits — neither test asserts containment.

`SECURITY.md:53-57` lists optional network calls as `ingest`, local PDF extraction, and local watch mode only. It omits the three MCP PR tools that invoke the `gh` CLI, and it omits the optional model backends described in §4.

`SECURITY.md:25` explicitly admits semantic extraction may send content to a configured model/backend; `SECURITY.md:48-51` correctly says no listener by default, no `eval`/`exec` of source, no `shell=True`, and no stored credentials. Those last four statements hold in the code inspected.

---

## 3. MCP path and input validation

### 3.1 Path validation: arbitrary graph selection, traversal, and symlink escape (F1)

Caller-controlled `project_path` is joined without containment, resolved, and loaded. Because `project_path` may be absolute or contain `..`, the server can be pointed at any directory that contains `graphify-out/graph.json`. Because `_read_graph` accepts a symlinked `graph.json`, it can also read any JSON file outside the tree if it is reachable through a `.json` symlink.

Runtime evidence at commit `92741d1`:

1. Default graph had 2 nodes; `graph_stats` with `project_path=/root/.hermes/cache/scratch/gprobe-…/outside-sensitive` returned `Nodes: 7` from that other tree.
2. A separate project whose `graphify-out/graph.json` was a symlink to an external `secret.json` returned `Nodes: 1` with the external file's node count.

Impact: on any shared or HTTP-exposed MCP server, a client can read graph models from unrelated projects/users on the same host, contradicts the documented path guard, and expands memory/CPU exposure (each distinct project consumes an LRU slot and eagerly builds a trigram index). Combined with the 512 MiB per-file cap and 8-context default, a client can force roughly 4 GiB of graph payloads plus index overhead in a long-lived server (`graphify/security.py:32`, `graphify/serve.py:123-137`, `160-168`). There is no node/edge count cap and no aggregate-memory accounting.

### 3.2 Schema and range validation: declared but not enforced (F2, F5, F6)

`inputSchema` is descriptive only. The low-level MCP SDK receives `arguments: dict[str, Any] | None` and does not validate against the declared schema (verified in `mcp==2.3.0` `CallToolRequestParams`); Graphify never sets the SDK's optional `get_tool_input_schema` callback when constructing `Server(...)` (`graphify/serve.py:2831-2838`). Handlers then do ad-hoc coercion:

- `query_graph` reads `arguments["question"]` directly, so a missing key raises `KeyError` (`graphify/serve.py:2460`); `depth = min(int(...), 6)` clamps only the upper bound, so negative depths are accepted (`graphify/serve.py:2462`); `token_budget` is unbounded and unvalidated (`graphify/serve.py:2463`); `mode` is not validated at all (`graphify/serve.py:2461`, dispatch at `graphify/serve.py:1506`).
- `shortest_path` does `int(max_hops)` and `bool(undirected)` (`graphify/serve.py:2106-2107`), so the string `"false"` becomes `True`.
- `get_community` does `int(community_id)` with no range check (`graphify/serve.py:2537`).
- `god_nodes` does `int(top_n)` and optional `float(exclude_hubs_percentile)` with no range check (`graphify/serve.py:2557-2561`).
- `call_tool` wraps every non-`ToolError` exception into a **successful** text result: `return [types.TextContent(type="text", text=f"Error executing {name}: {exc}")]` (`graphify/serve.py:2780-2789`). Only explicitly raised `ToolError` is marked `isError:true` (class at `graphify/serve.py:34-41`; PR tool errors use it, tested at `tests/test_serve_http.py:368-389`).

Runtime evidence at commit `92741d1` (all results had `isError` absent/false):

| Request | Result |
|---|---|
| `query_graph {}` (missing required `question`) | `Error executing query_graph: 'question'` |
| `query_graph {"question": 123}` | `Error executing query_graph: argument of type 'int' is not a container or iterable` |
| `query_graph {"question":"D0","depth":-5}` | `Traversal: BFS depth=-5` with 1 node |
| `query_graph {"question":"D0","token_budget":-100}` | `Complete answer over budget … vs the requested ~-100-token budget` |
| `query_graph {"question":"D0","mode":"bogus"}` | `Traversal: BOGUS` (treated as BFS branch) |
| `query_graph {"question":"D0","context_filter":5}` | `Error executing query_graph: 'int' object is not iterable` |
| `shortest_path {"source":"D1","target":"D0","undirected":"false"}` | returned the undirected path (string coerced true) |
| `get_community {"community_id":"abc"}` | `Error executing get_community: invalid literal for int() with base 10: 'abc'` |
| unknown tool `nope` | `Unknown tool: nope`, successful result |

These outputs violate the product contract in spec §8 (`docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md:99-120`, budget rules at `:114-118`): budget errors must be explicit failures, unknown/out-of-range input must not be silently accepted, and every invocation must be auditable. They also leak raw exception strings to the client.

### 3.3 Resource validation (F4)

Resources have no size cap and no sanitization: the report file is streamed whole (`graphify/serve.py:2727-2730`); unknown URIs raise `ValueError` (`graphify/serve.py:2772`) without a friendly error contract. `graphify://surprises`/`graphify://questions` print values derived from graph labels/questions without a `sanitize_label` pass (`graphify/serve.py:2741-2744`, `2763-2768`). Runtime evidence: ANSI ESC bytes and `<script>` survived `graphify://report` unchanged. Because `sanitize_label` deliberately does not HTML-escape (`graphify/security.py:441-452`; pinned by `tests/test_security.py:231-235`), any HTML consumer of these resources must escape independently.

### 3.4 Other input/robustness limits

- `depth` is clamped to 6 but never to a positive floor (`graphify/serve.py:2462`).
- `token_budget` negative is formatted into the response, not rejected; there is no minimum-budget error (`graphify/serve.py:1126`, `1248-1307`).
- `_filter_blank_stdin` mutates global stdin and swallows all relay errors (`graphify/serve.py:2151-2175`); no test covers it.
- The event loop performs synchronous `stat`, JSON load, and trigram index build without offloading (`graphify/serve.py:2257-2265`, `160-168`), so a large/malicious graph blocks all concurrent MCP calls.
- Graph load forces `directed:true` and repairs `multigraph:false` with parallel links (`graphify/serve.py:58-70`); dangling `_src`/`_tgt` are guarded during render (`graphify/serve.py:1222-1229`, test at `tests/test_serve.py:1628-1637`). These are read-model robustness positives, not security fixes.
- A corrupted graph produces a clear recovery message only through `_load_graph`; MCP project loads convert `SystemExit` into `RuntimeError` so other graphs remain served (`graphify/serve.py:102-110`, `149-159`; tests `tests/test_serve_http.py:282-303`).

---

## 4. Outbound, network, and model surfaces

### 4.1 Exposed through MCP

The tool list itself contains three outbound surfaces: `list_prs`, `get_pr_impact`, and `triage_prs` (`graphify/serve.py:2391-2435`). Their handlers call `graphify.prs`, which shells out to `gh` (`graphify/prs.py:141-154`, `269-282`), `git symbolic-ref` (`graphify/prs.py:158-182`), and threads up to 8 concurrent `gh` fetches (`graphify/serve.py:2647-2687`). `graph_stats` also shells out to `git rev-parse HEAD` via `graphify.export._git_head` (`graphify/serve.py:2579-2599`, `graphify/export.py:217-235`). `gh` uses ambient GitHub credentials; `SECURITY.md:48-57` does not list these as network surfaces. Runtime probe confirmed `gh` is absent in this environment, which only means the call would fail locally; the surface is still advertised and would perform network I/O where `gh` is authenticated.

`graphify/serve.py` also imports `graphify.reflect.load_learning_overlay` while loading a graph (`graphify/serve.py:94-98`), and `graphify.reflect` imports `graphify.ingest` (`graphify/reflect.py:37`), the module that owns `safe_fetch`/`validate_url` (`graphify/ingest.py:11`, `92`, `123`, `246`, `250-260`). It imports `graphify.export` for git HEAD (`graphify/export.py:217-235`), and `graphify.export` imports the optional Neo4j/FalkorDB exporters (`graphify/export.py:27`, `graphify/exporters/graphdb.py:1-120`). None of these make a call during normal graph queries, but importing `graphify.serve` pulls network-capable modules into the process; a deterministic-only distribution must not rely on "no call happened" alone.

### 4.2 Optional package extras (dormant but present)

`pyproject.toml` declares:

- `mcp = ["mcp>=1,<3", "starlette>=1.3.1,<2"]` (`pyproject.toml:70`) — `uvicorn` is not declared despite the HTTP path importing it (`graphify/serve.py:3026-3032`); it currently arrives transitively (`mcp==2.3.0` requires `uvicorn>=0.31.1` on non-emscripten). This is brittle across the allowed `mcp>=1,<3` range.
- `neo4j`, `falkordb` (`pyproject.toml:71-72`), `pdf` (`73`), `watch` (`74`), `svg` (`75`), `leiden` (`76-79`), `office` (`80`), `video` (`86`), and model provider extras `kimi`, `ollama`, `bedrock`, `anthropic`, `gemini`, `openai` (`87-92`).
- `all` is the union, including every provider package (`pyproject.toml:128`).
- `jieba`/`jieba-py` is an optional Chinese tokenizer (`pyproject.toml:93-96`) imported at `serve` module import with a syntax-warning suppress (`graphify/serve.py:20-31`), with a bigram fallback when absent (`graphify/serve.py:236-249`, tests `tests/test_serve.py:1181-1191`).
- Entry points: `graphify` and `graphify-mcp = graphify.serve:_main` (`pyproject.toml:130-132`).

### 4.3 Model/provider surfaces in installed base package

Even with no provider extras installed, `graphify/llm.py` ships in the base package:

- At module import it tries `import tiktoken` and `tiktoken.get_encoding("cl100k_base")`, whose first use can trigger a **network download** inside `except Exception: return None` (`graphify/llm.py:44-62`). It also reads `~/.graphify/providers.json` and merges custom providers into `BACKENDS` at import (`graphify/llm.py:292-323`).
- `BACKENDS` hard-codes provider base URLs and API-key environment variables for Anthropic, Moonshot/Kimi, Ollama, Gemini, OpenAI, DeepSeek, Azure, and Bedrock (`graphify/llm.py:125-220`).
- `provider_base_url_ok` rejects non-http(s) schemes but otherwise allows any http(s) host and only warns for plaintext non-loopback; it explicitly does **not** apply the ingest SSRF guard (`graphify/llm.py:255-289`). Project-local provider config is ignored unless `GRAPHIFY_ALLOW_LOCAL_PROVIDERS` is set; global config is trusted (`graphify/llm.py:292-323`; tests `tests/test_provider_registry.py:74-145`).
- `detect_backend_with_source()` auto-selects a provider from ambient API keys in this order: gemini → kimi → claude → openai → deepseek → azure → bedrock → ollama → custom (`graphify/llm.py:3398-3444`). `GRAPHIFY_BACKEND=none/off/disable/disabled` is the explicit off switch (`graphify/llm.py:3379-3396`; tests `tests/test_backend_opt_in.py:1-70`).
- If no API key exists but the `claude` CLI is on `PATH`, community labeling silently falls back to `claude -p` subprocess calls (`graphify/llm.py:3455-3468`, `3775-3830`; subprocess at `graphify/llm.py:1795` and `1911`).
- Ollama base URLs are hard-blocked only for link-local/cloud-metadata addresses; general private/LAN and plaintext HTTP targets warn but are allowed (`graphify/llm.py:3267-3340`).
- `graphify/transcribe.py` uses `faster-whisper` model assets (`graphify/transcribe.py:14`, `23-30`, `145-151`) and `yt-dlp` network download (`graphify/transcribe.py:34-40`, `50-90`).
- `graphify/ingest.py` is the explicit outbound URL fetcher (`graphify/ingest.py:11`, `250-260`), using `safe_fetch` (`graphify/ingest.py:92`, `123`, `246`).

The model/provider surfaces are therefore not merely "optional extras": the base distribution contains provider configuration, import-time provider discovery, an ambient-key auto-selection path, and a model-asset/network path in transcription. A deterministic product must exclude or hard-disable `llm`, `ingest`, `transcribe`, `prs`, and `graphdb`/`export` provider paths at import time, not just uninstall extras.

### 4.4 Query logging

`querylog` is **opt-in and off by default** (`graphify/querylog.py:17-33`). When enabled it appends timestamp, kind, raw `question`, `corpus` path, node count, char count, duration (`graphify/querylog.py:55-78`); full responses are added only when `GRAPHIFY_QUERY_LOG_RESPONSES` is set (`graphify/querylog.py:36-37`, `76-77`). MCP `query_graph` logs every call when enabled (`graphify/serve.py:2475-2484`). `README.md:436` claims every CLI and MCP query "is logged to `~/.cache/graphify-queries.log`" and that users can "opt out" with `GRAPHIFY_QUERY_LOG_DISABLE=1` — contradicting the code's default-off, opt-in behavior. The code's fail-silent, off-by-default posture is the better model; the documentation drift is a security-communication defect.

---

## 5. MCP and security test coverage

Covered directly:

- In-process Streamable HTTP ASGI tests with `pytest.importorskip("mcp")`/`importorskip("starlette")` (`tests/test_serve_http.py:1-19`): initialize (`63-72`), unknown path 404 (`74-78`), API-key 401/acceptance (`81-139`), custom mount (`142-148`), full initialize→tools/list (`151-172`), per-tool optional `project_path` (`205-217`), arbitrary project routing (`219-229`), LRU/pinned-default behavior (`244-279`), bad/corrupt project as tool error (`282-303`), stateless/session-timeout (`306-327`), CLI routing (`332-365`), `isError` for PR failures (`368-401`), ambiguous labels (`420-433`), commit/HEAD stats (`436-539`), IPv6 Host handling (`541-555`).
- Query-engine unit tests without an MCP dependency (`tests/test_serve.py:1`): scoring, trigram prefilter, determinism, truncation, oversized graph rejection (`723-734`), reload stat key (`760-800`), parallel edges (`1986-2046`).
- MCP-direction tests (`tests/test_query_mcp_direction.py:30-99`) and per-graph traversal/path cache tests (`tests/test_mcp_graph_view_cache.py:60-124`).
- Security unit tests (`tests/test_security.py`) and SSRF-blocklist regression tests (`tests/test_frontmatter_and_ssrf_hardening.py:15-21`).
- MCP config extraction never reads env values (`graphify/mcp_ingest.py:245-267`, `tests/test_mcp_ingest.py:119-138`), caps config size at 1 MiB (`graphify/mcp_ingest.py:77`, `96-101`), and sanitizes labels (`graphify/mcp_ingest.py:335`).

Important gaps:

1. **No stdio end-to-end test.** `tests/test_serve_http.py:4-5` says "The stdio path is unchanged and covered elsewhere", but no test invokes `serve()`, `stdio_server`, or the blank-line shim; the only stdio coverage is CLI argument routing (`tests/test_serve_http.py:332-340`). The blank-line pipe/thread rewrite in `graphify/serve.py:2151-2175` is untested.
2. **No capability-contract test.** Nothing asserts the advertised capabilities are exactly tools+resources and that `sampling`, `prompts`, `logging`, and `completion` are absent. The plan for the product explicitly requires this (`docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md:30`, `225`).
3. **No `validate_graph_path` integration test with MCP.** The guard is unit-tested (`tests/test_security.py:178-224`) but never asserted to be in the MCP load path — because it is not.
4. **No negative/range input tests.** No test covers missing required keys, wrong types, negative budget/depth, invalid mode, unknown tool as an error, or string booleans.
5. **No resource-sanitization or resource-size tests.** No test reads `graphify://report`, `graphify://surprises`, or `graphify://questions` and asserts control-character stripping, HTML escaping, size cap, or error envelope behavior.
6. **No memory/concurrency tests.** No aggregate-context cap, node/edge cap, or event-loop-blocking test exists; context cache tests cover LRU order only.
7. **No test that MCP tool output redacts credentials.** `redact_secrets` is applied only to rationale labels during extraction (`graphify/extract.py:1766`), not to MCP output.
8. **No test for the README query-log claim**; the code default-off behavior is tested only indirectly if at all.

---

## 6. Findings

| ID | Severity | Finding | Primary evidence |
|---|---|---|---|
| F1 | High for shared/Host-exposed servers | Caller `project_path` selects an arbitrary `graphify-out/graph.json`; traversal and `.json` symlinks read graphs outside any allowed root. Contradicts `SECURITY.md:34` and `ARCHITECTURE.md:101`. | `graphify/serve.py:2267-2274`, `2290-2294`, `44-55`; `tests/test_serve_http.py:219-229`; runtime probe |
| F2 | High | Tool input schemas are not enforced; missing/wrong input becomes a successful result with raw exception text; only PR `ToolError`s set `isError`. | `graphify/serve.py:2774-2789`, `34-41`; `mcp==2.3.0` `CallToolRequestParams.arguments: dict`; runtime probe |
| F3 | High for deterministic-only product | Three MCP tools perform outbound `gh` network calls with ambient credentials; not listed as a network surface in `SECURITY.md`. | `graphify/serve.py:2391-2435`, `2604-2687`; `graphify/prs.py:141-154` |
| F4 | High for model consumers | `graphify://report` returns raw, unsanitized, uncapped file text (ANSI, HTML, prompt-injection text); surprises/questions resources emit raw labels. | `graphify/serve.py:2727-2730`, `2741-2744`, `2763-2768`; runtime probe |
| F5 | Medium | Negative `depth`, negative `token_budget`, and invalid `mode` are accepted; negative budgets are reported as "complete answer over budget" rather than an error. | `graphify/serve.py:2461-2463`, `1506`, `1248-1307`; runtime probe |
| F6 | Medium | JSON-schema types are not enforced; `undirected:"false"` is true and wrong types produce success-with-error-text. | `graphify/serve.py:2106-2107`, `2788-2789`; runtime probe |
| F7 | Medium | 512 MiB per graph × 8-context LRU, eager trigram index, no node/edge or aggregate-memory cap, and synchronous load on the event loop allow memory/CPU exhaustion. | `graphify/security.py:32`; `graphify/serve.py:123-137`, `160-168`, `2257-2265` |
| F8 | Medium | Cache key is `(mtime_ns, size)`, not content hash or projection high-water mark; a same-size/same-mtime rewrite can serve a stale graph, and there is no CAS publication guard. | `graphify/serve.py:182-194`; contrast with spec §7 |
| F9 | Medium | README says query logging is on by default and opt-out; code is opt-in and default-off. Implemented behavior is safer, but the documentation is wrong and could mislead operators about proprietary query retention. | `README.md:436`; `graphify/querylog.py:17-33`, `55-78` |
| F10 | Medium | HTTP has no TLS, and `0.0.0.0`/`::` disables DNS-rebinding Host checks; an unset/blank API key leaves the server unauthenticated with only a warning. | `graphify/serve.py:2961-2966`, `3047-3058`, `3061-3111` |
| F11 | Low/Medium | `sanitize_label` strips only C0/DEL and caps 256; it deliberately does not HTML-escape and does not strip Unicode bidi/zero-width characters. Raw resources bypass it entirely. | `graphify/security.py:405-406`, `441-452`; `tests/test_security.py:231-235` |
| F12 | Low/Medium | `redact_secrets` is shape-based, is applied only to rationale labels at extraction time, and is not applied to MCP output/resources, so credentials already in a graph or report can be served. | `graphify/security.py:409-438`; `graphify/extract.py:1766` |
| F13 | Low | `_filter_blank_stdin` rewrites fd 0, spawns a daemon thread, swallows all errors, and is untested. | `graphify/serve.py:2151-2175` |
| F14 | Low for current runtime, High for product constraint | Base package includes model provider config, import-time tiktoken network attempt, ambient-key auto-detection, and `claude -p` fallback; transcription adds model assets/network. | `graphify/llm.py:44-62`, `125-220`, `3398-3444`, `3775-3830`; `graphify/transcribe.py:14`, `145-151` |
| F15 | Low | `mcp` extra does not declare `uvicorn` even though HTTP requires it; relies on transitive dependency within `mcp>=1,<3`. | `pyproject.toml:70`; `graphify/serve.py:3026-3032` |
| F16 | Info | No stdio e2e, capabilities, negative-input, resource-sanitization, or memory/concurrency tests. | §5 |
| F17 | Info | `sanitize_metadata` HTML-escapes values, which is a useful bound but also means metadata strings cannot be treated as raw JSON strings by consumers. | `graphify/security.py:464-507` |

---

## 7. Adopt / Reject / Adapt for the deterministic product

The product plan already creates its own `mcp_server.py`, `security.py`, and projection layer (`docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md:212-229`, `231-250`). The recommendations below are therefore **design reuse**, not imports of `graphify.serve`.

### 7.1 Adopt (reusable positives)

| # | Positive | Source | How to reuse |
|---|---|---|---|
| A1 | One server builder serving both transports; registration in a single place | `graphify/serve.py:2229-2236`, `2843-2859`, `3003-3058` | Keep the product MCP adapter thin: build one `Server`, register exactly the ten tools, run stdio by default and optional Streamable HTTP. |
| A2 | Minimal capability surface: tools + resources only; no sampling/prompts/logging registration | observed `initialize` result; `graphify/serve.py:2791-2838` | Preserve and make it contractual: `test_mcp_server.py` asserts the exact capability object and `-32601` for `sampling/createMessage`, `prompts/list`, `logging/setLevel`, `completion/complete`. |
| A3 | `ToolError` → `isError:true` distinction | `graphify/serve.py:34-41`; `tests/test_serve_http.py:368-389` | Use the same public contract but raise it for **every** validation/business failure, not only PR failures. |
| A4 | Pinned default + bounded LRU project contexts with thread-safe reload | `graphify/serve.py:123-199`; `tests/test_serve_http.py:244-279` | Do not need multi-project at all; keep the "one active projection, no per-request path" shape and keep a bounded cache only for read-model generations. |
| A5 | Per-graph caches attached to the graph object and invalidated by object replacement | `graphify/serve.py:377-399`, `470-493`, `1360-1408`, `2018-2063`; `tests/test_mcp_graph_view_cache.py:60-124` | Apply the same rule to the active generation: caches keyed on a content-addressed generation object, never surviving activation swaps. |
| A6 | Content-addressed / atomic activation concepts already exist in the product spec | `docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md:82-97`; `docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md:88-108` | Use Graphify's cache tests as negative examples of keying on mtime/size; implement hash + high-water CAS per spec. |
| A7 | Raw-ASGI auth middleware for streaming SSE + constant-time key comparison + Bearer/X-API-Key parsing | `graphify/serve.py:2878-2916`; `tests/test_serve_http.py:81-139` | Adopt for the local API/MCP HTTP path; add TLS/Unix-socket boundary and require a key whenever not loopback. |
| A8 | DNS-rebinding Host allowlist, IPv6 bracket handling, stateless mode, session idle timeout | `graphify/serve.py:2961-2983`; `tests/test_serve_http.py:306-327`, `541-555` | Adopt the SDK settings; never disable Host checks automatically on wildcard bind. |
| A9 | URL/SSRF guard semantics: scheme allowlist, credential/port/host checks, private/reserved/CGN/metadata blocklist, resolve-once connect, redirect revalidation | `graphify/security.py:83-158`, `173-266` | If any outbound path ever exists, reuse the *semantics* and port the tests; for this product the correct action is to deny sockets at runtime and keep these tests as guard oracles. |
| A10 | Streaming fetch caps, non-2xx rejection, graph-file size cap | `graphify/security.py:273-323`, `372-398` | Adopt caps for any file/read-model ingest (e.g. import, backup/restore, resource reads), with product-specific limits and explicit errors. |
| A11 | Path containment guard semantics | `graphify/security.py:330-369`; `tests/test_security.py:178-224` | Reimplement as the product's single `resolve_within(root, candidate)` guard and require it on every path-valued input and every projected resource. |
| A12 | Label/control-character sanitization, metadata bounds, shape-based secret redaction | `graphify/security.py:405-438`, `459-507`; `tests/test_security.py:231-543` | Adopt control stripping/length caps and the regex set as defense-in-depth; extend to Unicode bidi/zero-width and apply on **read** as well as write. |
| A13 | Deterministic rendering: seed-first ordering, stable tie-breaks, top-of-output truncation notice with counts, honest over-budget notice | `graphify/serve.py:1120-1310`; `tests/test_serve.py:1483-1600`, `1547-1573` | Adopt disclosure semantics, but replace the 3-char/token estimate with the spec's exact `ceil(utf8_bytes/4)` and return `BUDGET_TOO_SMALL` instead of over-budget success. |
| A14 | Read-model loader hardening: suffix check, existence, size cap, edge/list alias, directed recovery, parallel-edge repair, dangling endpoint guard | `graphify/serve.py:44-99`, `1222-1229`; `tests/test_serve.py:1628-1637`, `1986-2046` | Adopt as validation of imported/projected artifacts; the SQLite event store remains the source of truth. |
| A15 | Secret-avoidance principle in MCP config extraction: env names only, args not persisted, 1 MiB cap, labels sanitized | `graphify/mcp_ingest.py:40-47`, `77-101`, `245-267`; `tests/test_mcp_ingest.py:119-138` | Adopt if the product ever indexes configuration files; not required for the initial ten operations. |
| A16 | Query log privacy posture: opt-in, fail-silent, responses off by default | `graphify/querylog.py:17-37`, `55-78` | Adopt fail-silent/off-by-default behavior for any diagnostic sink; the product's append-only audit events remain the required record. |
| A17 | Offline, in-process ASGI contract tests; `importorskip`; `base_url="http://127.0.0.1"` for Host checks | `tests/test_serve_http.py:1-19`, `57-61` | Adopt these testing techniques for the product MCP/HTTP tests. |
| A18 | Bigram fallback when the optional tokenizer is absent | `graphify/serve.py:236-249`; `tests/test_serve.py:1181-1191` | If non-Latin term segmentation is needed, implement dependency-free fallback logic; do not add `jieba` to a deterministic product unless the lock and import guards explicitly allow it. |

### 7.2 Reject

| # | Rejected surface | Source | Reason |
|---|---|---|---|
| R1 | `graphify.llm` in any form (provider registry, API calls, tokenizer download, ambient key detection, `claude -p` fallback) | `graphify/llm.py:1-8`, `44-62`, `125-220`, `3398-3444`, `3775-3830` | Violates no-model/no-provider/no-outbound constraint; even dormant import has import-time side effects. |
| R2 | Provider extras and `[all]`: `openai`, `anthropic`, `gemini`, `kimi`, `ollama`, `bedrock`, `tiktoken`, `boto3` | `pyproject.toml:87-92`, `128` | Provider/model dependencies must be absent from the locked environment. |
| R3 | `graphify.transcribe` / `faster-whisper` / `yt-dlp` | `graphify/transcribe.py:14`, `23-40`, `50-90`, `145-151`; `pyproject.toml:86` | Downloads model assets and network media. |
| R4 | `graphify.ingest` outbound URL fetch | `graphify/ingest.py:11`, `250-260` | Outbound traffic and fetched untrusted content; not required by the ten knowledge operations. |
| R5 | MCP PR tools: `list_prs`, `get_pr_impact`, `triage_prs` | `graphify/serve.py:2391-2435`, `2604-2687`; `graphify/prs.py:141-154` | Outbound `gh` network, ambient credentials, non-deterministic external state. |
| R6 | `graphify.export` / `graphify.exporters.graphdb` provider push paths | `graphify/export.py:27`; `graphify/exporters/graphdb.py:1-120` | Neo4j/FalkorDB are outbound external stores; not a deterministic local source of truth. |
| R7 | MCP `project_path` per-request arbitrary graph selection | `graphify/serve.py:2267-2274`, `2441-2454`; `tests/test_serve_http.py:219-229` | Bypasses containment and enables cross-project disclosure (F1). |
| R8 | Raw resource surface (`graphify://report`, `surprises`, `questions`) | `graphify/serve.py:2711-2772` | Unsanitized, uncapped, prompt-injectable content (F4). If resources are used, they must be generated from sanitized projection JSON with a size cap and content hash. |
| R9 | HTTP wildcard bind with DNS-rebinding protection disabled and optional auth | `graphify/serve.py:2961-2966`, `3047-3058` | Exposes unauthenticated graph/audit data; product spec requires Unix socket default and loopback-only HTTP. |
| R10 | mtime/size cache key and 8-context LRU as the publication mechanism | `graphify/serve.py:123-199`; F8 | Not a consistency guarantee per spec §7 (`docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md:82-97`); replace with generation manifest + CAS high-water publication. |
| R11 | Importing `graphify.serve` wholesale | `graphify/serve.py:16-31`, `94-98`, `2579-2599` | Pulls `reflect`→`ingest`, `export`→graphdb, optional `jieba`, and PR/network modules into the process; defeats a clean forbidden-import boundary. |
| R12 | Graphify's HTML/report exporters as the product read model | `graphify/export.py:16-27`, `170-172` | Large, HTML/XSS-sensitive, model/embedding-oriented output; product read model is canonical JSON from SQLite. |
| R13 | `sanitize_label` alone as HTML/XSS or prompt-injection defense | `graphify/security.py:441-452`; `tests/test_security.py:231-235` | It explicitly does not HTML-escape and does not strip Unicode bidi/zero-width. |
| R14 | `redact_secrets` as a guarantee | `graphify/security.py:409-411`, `429-438`; `graphify/extract.py:1766` | Shape-based risk reduction only, and only applied to rationale labels. |

### 7.3 Adapt (mandatory changes before reuse)

| # | Adaptation | Owns (plan task) | Acceptance test |
|---|---|---|---|
| X1 | Replace per-request `project_path` with a startup-configured single project/root or an explicit allowlisted registry. Resolve every path through a `resolve_within(root, candidate)` guard; refuse symlinks or resolve and re-verify after resolution; never join user input into a graph path. | Task 1 (guard), Task 3/8 (adapter) | Traversal, absolute-path, `..`, and symlinked-`graph.json` probes all fail closed; a test mirrors the F1 runtime probe and expects an error, not a cross-tree node count. |
| X2 | Validate all tool arguments with a strict schema (unknown properties rejected, required keys enforced, types and ranges checked) and raise `ToolError` so `isError:true`; never return exception text as a successful result. | Task 8 | Missing `question`, wrong types, negative `depth`/`token_budget`, invalid `mode`, unknown tool, and `undirected:"false"` all return typed fail-closed errors with audit IDs; no raw exception strings. |
| X3 | Enforce the product budget contract: integer budget in `[128, configured_max]`; `depth` in `[1,6]`; mode in `{bfs,dfs}`; `estimated_tokens = ceil(utf8_bytes/4)` over the complete canonical JSON payload; undersized budget → `BUDGET_TOO_SMALL`; metadata overflow → error. | Task 3A, Task 4 | Frozen token-budget fixtures plus negative cases: `-1`, `0`, `127`, over-max, metadata-overflow; assert errors and no payload over budget. |
| X4 | Replace mtime/size cache validation with content hash + `source_knowledge_high_watermark` + `projection_version`; activate generations via atomic `CURRENT` pointer; readers validate hash/watermark and retry/repair once, then return retryable unavailable (`docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md:87-95`). | Task 3 | Paused/competing projectors, interrupted activation, stale read, and same-mtime/same-size content swap tests; monotonic recovery test. |
| X5 | Cap memory deterministically: one active graph (no 8-project LRU), explicit max graph bytes, max nodes/edges, max contexts, and aggregate memory accounting; load/stat/hash off the event loop. | Task 1, Task 3, Task 9 | Oversized graph, too many nodes, and concurrent load probes fail closed with bounded memory; event loop remains responsive. |
| X6 | Sanitize everything on the read path: strip C0/DEL and Unicode bidi/zero-width, cap lengths, JSON-encode; never serve raw report files or raw labels. If a resource is exposed, it is a bounded, sanitized JSON view with content hash and size limit. | Task 9, Task 8 | Report/ANSI/script injection probe returns sanitized text or is absent; resource size cap enforced. |
| X7 | Move secret redaction from a shape heuristic to a declared no-secrets policy: reject/scrub credential-shaped input on write and on read; keep `redact_secrets` patterns as defense-in-depth; add output redaction tests. | Task 9 | Credential-shaped evidence/import is rejected or redacted in every response surface (CLI/API/MCP) with a test per surface. |
| X8 | Keep the capability surface minimal and test it as a contract: tools + resources only (or tools only), `subscribe:false`, `listChanged:false`; assert `-32601` for sampling, prompts, logging, completion; assert no provider fields in server instructions/config. | Task 8 | Exact capability object equality plus negative method tests; fresh-process stdio e2e discovery in addition to in-process ASGI tests. |
| X9 | Harden the transport boundary: stdio default; Unix socket default for local API; loopback HTTP optional; when HTTP is enabled require an API key, keep Host allowlisting even on wildcard binds, add request-size/timeout/concurrency limits and an explicit TLS/reverse-proxy requirement. | Task 7, Task 9 | Unauthorized bind, missing key, foreign Host, wildcard bind, oversized request, and concurrent request tests. |
| X10 | Do not import `graphify.serve` or any Graphify network/model module. Pin the MCP SDK exactly, declare `uvicorn` directly, and implement the MCP adapter against the product service. | Task 1, Task 8 | Forbidden-import and entry-point guards fail closed for `graphify.llm`, `graphify.ingest`, `graphify.transcribe`, `graphify.prs`, `graphify.exporters.graphdb`; `graphify.serve` absent; locked env has no provider/vector/model packages. |
| X11 | Replace Graphify's 3-chars/token estimator and over-budget success with the spec budget; keep the truncation-disclosure UX (top banner, accurate counts, explicit complete-over-budget only when the canonical payload genuinely fits but metadata pushes over, otherwise error). | Task 3A, Task 4 | Cross-adapter canonical payload fixtures for CLI/API/MCP; truncation status and counts asserted. |
| X12 | Replace Graphify query logging with the product's append-only audit event. Keep fail-silent/off-by-default for any optional diagnostic sink, and correct the documentation drift so no operator believes queries are logged by default. | Task 2, Task 9 | Exactly one audit event per valid invocation; read audit excluded from scoring/knowledge watermark; diagnostic sink default-off test. |
| X13 | Apply `sanitize_metadata`-style bounds to all nested metadata and keep HTML-escaping on **render** boundaries only; never double-escape canonical JSON payloads. | Task 4, Task 9 | Metadata bound/escape tests plus canonical JSON fixture equality across transports. |
| X14 | Replace optional `jieba` model/tokenizer dependency with a dependency-free tokenizer or explicit excluded feature; if non-Latin segmentation is required, use the bigram fallback pattern. | Task 1, Task 4 | Locked environment contains no `jieba`, no model assets; non-Latin query fixtures pass on the dependency-free path. |
| X15 | Treat `graphify` solely as an inspected reference and optional read-model generator inside the isolated environment; do not expose its CLI/MCP entry points or allow upgrades of the host installation. | Task 1, Task 3 | Version pin equals `0.9.95`; host Graphify unchanged; lock includes only required extras. |

### 7.4 Feature-matrix consequences

- Applicable Graphify MCP capabilities to adopt/wrap: stdio + HTTP transport shape, capability minimalism, tool error contract, graph cache/hot-reload semantics (adapted), budget/truncation disclosure (adapted), HTTP auth/Host protections (adapted), label/metadata sanitization, path/size guard semantics (reimplemented), offline in-process contract tests.
- Explicit exclusions with reasons: all code-only AST language extractors; all model/semantic extraction and provider backends; `ingest` URL fetching; `transcribe`; PR tools; graphdb exporters; raw report/resource exporters; multi-project arbitrary `project_path`.
- No coverage credit may be claimed for a Graphify feature merely because its source was inspected. Adoption counts only when the product adapter and its contract test pass, per spec §2 (`docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md:22`) and plan Global Constraints (`docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md:13-22`).

---

## 8. Verification evidence

Probe commands and observed results were produced against commit `92741d162b2094ffc2fed58cfbd8ee823e9da62c` with the interpreter/paths in the header. No outbound network was used by these probes. Distinct evidence:

1. **Capabilities:** `initialize` returned exactly `{"resources":{"listChanged":false,"subscribe":false},"tools":{"listChanged":false}}`; `prompts/list` and `logging/setLevel` returned `-32601`.
2. **Tools:** `tools/list` returned the ten names in §1.2, each with `project_path` optional.
3. **Resources:** `resources/list` returned the six URIs in §1.3; `resources/read` of `graphify://report` returned raw ANSI/script bytes.
4. **F1 path:** `graph_stats` with an out-of-tree `project_path` returned that tree's node count (`Nodes: 7`); a symlinked external `secret.json` was loaded (`Nodes: 1`).
5. **F2/F5/F6:** the request matrix in §3.2 returned successful `isError:false` results with raw error text or silently coerced input.
6. **Stdio:** leading blank line + `initialize` produced a valid `initialize` result and exit code 0.
7. **Security unit tests** and their line references in §2.1 are the primary tests for the URL/path/label primitives; they were read, not re-executed, because the clone's own environment lacks `networkx` and the host Graphify venv lacks `mcp`/`starlette`; the probe combined the two only for runtime evidence, and it did not install or modify anything.

Residual uncertainties:

- `mcp==2.3.0` behaviour was verified from the installed SDK; Graphify also supports `mcp` 1.x (`pyproject.toml:66-70`), and the 1.x decorator path (`graphify/serve.py:2791-2798`) was not executed. Capability derivation should be equivalent because both paths register the same four handlers, but the product must pin one SDK version and test it.
- The HTTP session manager's internal concurrency/limits were not stress-tested; only wiring, auth, Host, LRU, and error isolation were exercised.
- Host resources prevented running the clone's full pytest suite; security conclusions therefore combine source/tests inspection with targeted runtime probes, and every high-severity finding has a runtime or code-evidence basis.