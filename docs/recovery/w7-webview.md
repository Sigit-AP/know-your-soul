# W7 SUB-101 — Web View (loopback-only, token-auth, read-only)

## What was built

| File | Purpose |
|---|---|
| `src/know_your_soul/serve/__init__.py` | New `serve` package (SUB-100 sibling: superpower daemon). |
| `src/know_your_soul/serve/webview.py` | stdlib `http.server` viewer server + `WebviewApp` (read-only app logic) + `main()` CLI entry. |
| `src/know_your_soul/serve/webview_assets/index.html` | SPA shell: 7 tabs (Graph/Search/Metrics/Health/Analytics/Audit/Proposals), dark theme, no CDN. |
| `src/know_your_soul/serve/webview_assets/style.css` | Dark theme; domain palette P1–P7 mirrors `exporters/html_view.py`. |
| `src/know_your_soul/serve/webview_assets/app.js` | Vanilla JS client: canvas graph (pan/zoom/click→detail), search with kind/lang filters, JSON panel renderers. |
| `tests/test_webview.py` | 165 tests (target was 120+). |
| `src/know_your_soul/cli.py` | `webview [--port N] [--no-open]` subcommand + dispatch (OPS surface, not a knowledge op). |

## Security model (codegraph §4.5, threat model = DNS rebinding)

Guard order on every request: **Host → Origin → method → token**.

1. Bind is hard-coded to `127.0.0.1`; the `Host` header must be
   `localhost|127.0.0.1|[::1]` **with the server's own port**, else 403.
   A port-less Host is refused (browsers always send one off-port).
2. A present `Origin` must be loopback with the same port; no CORS header is
   ever emitted.
3. Only GET/HEAD exist; everything else is 405 (guards run first, so an
   unauthenticated probe learns nothing).
4. Token: per-start `secrets.token_urlsafe(32)`, required as `?token=` on
   every byte (assets included), `hmac.compare_digest`, never echoed in
   responses nor embedded in served assets (pinned by test).
5. Assets are a fixed name allowlist (`index.html`/`app.js`/`style.css`) —
   no request path is ever joined into a filesystem path; traversal probes
   (`..`, `%2e%2e`) are 404.
6. Every JSON payload passes `escape_json_bytes`: sorted keys, fixed
   separators, `&`/`<`/`>` escaped to `\uXXXX` (stored-XSS probe with a
   `<script>alert(1)</script>` node label pinned). The HTML shell carries a
   strict CSP (`default-src 'none'; script-src 'self'; …`). `nosniff` +
   `Cache-Control: no-store` on all responses. The client renders via
   `textContent`/`createElement` only (structural pin: no markup-sink tokens
   in `app.js`).

## Endpoints (all GET, JSON)

`/api/graph` (nodes+edges+**server-side** `viz.layout` positions, caps
`MAX_GRAPH_NODES=4096` with honest `truncated`), `/api/node/<id>` (P/M/E
scores, provenance, relations, incident edges, evidence ids, symbol),
`/api/search?q=&kind=&lang=&limit=` (retrieval_v2 tiered lookup; filters
validated fail-closed), `/api/hierarchy/<id>?depth=` (callers+callees, depth
1–8), `/api/metrics` (`ops.metrics`, redacted), `/api/health`
(`ops.diagnose` report + exit code), `/api/analytics`
(centrality+communities+bridges; `ANALYTICS_MAX_NODES=2048` with `partial`
flag), `/api/audit?limit=` (newest first, `redact_value` on payloads),
`/api/proposals` (review-signal candidates — proposal-only, never mutates),
`/api/refs` (unresolved-refs stats + failure reasons).

## Read-only guarantee

Pinned by tests: the store file's sha256 and the event count are unchanged
after exercising every endpoint. The app runs `SELECT`s and the existing
read APIs only; opening the store fails closed (`StoreUnavailableError`)
when the file is missing.

## Decisions / deviations (engineering judgment, not from the brief)

- Relations/evidence for `/api/node` are queried from `node_relation` /
  `evidence` tables, not the revision payload — a node's stored revision
  predates later `relation.add`/`evidence.append` events.
- `WebviewServer.shutdown()` skips `BaseServer.shutdown()` when
  `serve_forever()` never ran — calling it on an unstarted server deadlocks
  (found by a hanging test).
- `webview_assets/app.js` deliberately avoids even the *words* for markup
  sinks so the structural no-markup pin stays a pure substring check.
- Refs render client-side under the Audit tab footer (Refs shares the API;
  the tab bar pins the 7 brief-mandated tabs).

## Gates (real runs)

- `pytest tests/test_webview.py` → **165 passed** (RED first:
  ModuleNotFoundError, exit 1).
- Full suite (excluding SUB-100's in-flight `tests/test_serve_*.py`):
  **6162 passed, 2 failed** — both failures are sibling in-flight work:
  `test_no_ai_runtime…shell_out` flags SUB-100's `serve/superpower.py`
  (`os.execvpe`), `test_codebase_bench…baseline_gate` flags the ingest
  language changes (`stats.proposed 45 != baseline 43`). 0 failures touch
  webview files.
- Found + worked around a **pre-existing cross-test socket-guard leak**:
  `mcp_server.main()` installs a process-lifetime `SocketGuard`; the wave-3
  adapter tests run `main()` with `anyio.run` monkeypatched, so the guard is
  never uninstalled and denies even loopback AF_INET for everything later in
  the suite. `tests/test_webview.py` carries an autouse truce fixture that
  suspends the leaked guard per test and restores it afterwards (no sibling
  file touched; the leak itself is reported here for the integrator).
- Live E2E: real CLI (`know-your-soul --store … webview --port 8734
  --no-open`) serving a seeded store; curl probes: HTML 200, `/api/graph`
  radial layout from server, `/api/search` FTS hit, `/api/health` ok,
  Host `evil.com` → 403, POST → 405, no token → 401.
- `ruff check` + `ruff format --check` on owned files → 0 findings.
- `pyright` on owned files → 0/0/0. `uv lock --check` OK, `license_guard` 0.
