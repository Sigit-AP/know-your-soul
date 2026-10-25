# SOUL 25 — Measured facts

- **Measured fact SOUL.md size** — 23148 chars / 29733 bytes after the operating-stack section was added (Bagian 9); cap `context_file_max_chars: 32000`; still not truncated.
- **Measured fact loaded prompt** — the live system prompt is 49310 chars and contains the string "PROTOKOL OPERASIONAL"; SOUL.md is 43.9% of that prompt.
- **Measured fact graphify not in prompt** — the live system prompt contains zero occurrences of the string GRAPHIFY; the corpus is reachable only by an explicit tool call.
- **Measured fact graphify version** — graphify 0.9.91 at /usr/local/bin/graphify.
- **Measured fact corpus before this work** — 86 nodes, 101 edges, 8 hyperedges, 9 communities, extraction 85% EXTRACTED / 15% INFERRED.
- **Measured fact graphify tool usage** — 3 of 37 logged tool calls in the activity log's two-day window touched graphify (8.1%), all inside the single session that built the corpus; steady state is lower.
- **Measured fact sessions store** — one session file on disk (sessions.json); no long transcript history available for compliance auditing.
- **Measured fact SOUL never loads in subagents** — children are built with `skip_context_files=True`.
- **Unverified estimate compliance** — the compliance percentages for SOUL.md and graphify are self-reports, not measurements; they cannot be FAKTA without a transcript audit.
- **Blind verification protocol** — a subagent that never receives SOUL.md answers SOUL questions using only this graph; its answers are the independent check on corpus completeness.
- **Vault secret rule** — never type a password, card number, CVC or 2FA code into any tool; secrets live only in the encrypted vault.
- **Measured fact graph after junk prune** — 658 nodes pre-prune -> 524 nodes / 646 links post-prune; removed classes: filename-label 41, heading-label 104 (+ orphans). Rebuild is idempotent (identical node-id signature on two consecutive runs).
- **Measured fact junk nodes before the prune fix** — an independent audit measured 138 of 634 nodes as filename/H1/H2 labels; the hardened classifier (all heading levels + orphans) found 145 of 658, all now pruned (checks.py junk = 0).
- **Measured fact bench after the fix** — 34/34 PASS at ~1.0 s/query and ~1.2k tokens/query; raw SOUL.md is ~5.7k tokens, so one query stays ~4x cheaper than re-reading it. E33/E34 exist because a CLI-only blind test scored 7/8 on unreachable numbers.
- **Measured fact RTK live** — `rtk rewrite "git status"` exits 3; `git status` inside Hermes returns the 2-line compact form while `/usr/bin/git status` returns the 4-line native form, so the pre_tool_call hook is active, not merely configured.
- **Measured fact Headroom live** — `/health` returns status=healthy (v0.41.0); `headroom doctor` reports 227,599 tokens / $0.68 saved lifetime; protected read tools show `savings=none` with `cache_hit_pct=100` by design.
- **Measured fact skill routing wired** — SOUL.md Bagian 9 makes RTK+Headroom+pm/superpowers/mattpocock routing resident; skill `hermes-operating-stack` documents the same rules with evidence commands.
