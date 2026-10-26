-- 0002_journal_heartbeat_and_chain_anchor.sql — Task 2 review fix round 1.
-- Findings I1/I2/I3 in
-- .superpowers/sdd/2026-10-24-superpower-knowledge-soul/task-2-findings.md.
--
-- Journal liveness (I1): heartbeat_at is refreshed by EventStore.commit around
-- the domain write; recovery only closes journals whose last heartbeat (or
-- started_at) is older than the staleness threshold.
-- Recovery key (I2): effective_idempotency_key persists the key the domain
-- write actually used (idempotency_key, falling back to invocation_id).
-- Chain anchor (I3): system_state gains chain_head_hash / chain_event_count
-- rows, maintained by the store in the same transaction as every event append;
-- the key/value table needs no DDL here.

ALTER TABLE invocation_journal ADD COLUMN heartbeat_at TEXT;
ALTER TABLE invocation_journal ADD COLUMN effective_idempotency_key TEXT;
CREATE INDEX IF NOT EXISTS invocation_journal_heartbeat ON invocation_journal (heartbeat_at);
