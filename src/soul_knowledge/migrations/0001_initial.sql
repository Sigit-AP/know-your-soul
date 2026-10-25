-- 0001_initial.sql — immutable event store, revisions, journals and source registry.
-- Task 2 of docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md; spec sections 7 and 13.3.
--
-- Append-only tables (event_log, node_revision, evidence, node_relation) are
-- protected by BEFORE UPDATE / BEFORE DELETE triggers that RAISE(ABORT): history
-- may only ever grow. A completed invocation journal row is likewise immutable.
-- The whole file is idempotent so it can be re-applied on every connection.

CREATE TABLE IF NOT EXISTS schema_migration (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Global event log: domain and audit events share one monotonic event_seq.
-- Only knowledge-affecting events receive a knowledge_seq.
CREATE TABLE IF NOT EXISTS event_log (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('domain', 'audit')),
    event_type TEXT NOT NULL,
    knowledge_affecting INTEGER NOT NULL CHECK (knowledge_affecting IN (0, 1)),
    knowledge_seq INTEGER,
    node_id TEXT,
    domain TEXT,
    source_version_id TEXT,
    node_version INTEGER,
    actor TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_event_hash TEXT,
    event_hash TEXT NOT NULL,
    idempotency_key TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS event_log_idempotency
    ON event_log (idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS event_log_knowledge_seq ON event_log (knowledge_seq);
CREATE INDEX IF NOT EXISTS event_log_kind ON event_log (kind);

CREATE TRIGGER IF NOT EXISTS event_log_no_update
    BEFORE UPDATE ON event_log BEGIN SELECT RAISE(ABORT, 'event_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS event_log_no_delete
    BEFORE DELETE ON event_log BEGIN SELECT RAISE(ABORT, 'event_log is append-only'); END;

-- Immutable node revisions: one row per version, never rewritten.
CREATE TABLE IF NOT EXISTS node_revision (
    node_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    domain TEXT NOT NULL,
    kind TEXT NOT NULL,
    parent_id TEXT,
    lifecycle_status TEXT NOT NULL,
    source_version_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (node_id, version)
);
CREATE INDEX IF NOT EXISTS node_revision_source_version ON node_revision (source_version_id);

CREATE TRIGGER IF NOT EXISTS node_revision_no_update
    BEFORE UPDATE ON node_revision BEGIN SELECT RAISE(ABORT, 'node revisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS node_revision_no_delete
    BEFORE DELETE ON node_revision BEGIN SELECT RAISE(ABORT, 'node revisions are immutable'); END;

-- Evidence items, each scoped to a source version.
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    source_version_id TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS evidence_node ON evidence (node_id);
CREATE INDEX IF NOT EXISTS evidence_source_version ON evidence (source_version_id);

CREATE TRIGGER IF NOT EXISTS evidence_no_update
    BEFORE UPDATE ON evidence BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_no_delete
    BEFORE DELETE ON evidence BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;

-- Typed relations. Supersession and deletion are relations here, never deletions.
CREATE TABLE IF NOT EXISTS node_relation (
    relation_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_seq INTEGER NOT NULL,
    source_node_id TEXT NOT NULL,
    target_node_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT
);
CREATE INDEX IF NOT EXISTS node_relation_source ON node_relation (source_node_id);
CREATE INDEX IF NOT EXISTS node_relation_target ON node_relation (target_node_id);

CREATE TRIGGER IF NOT EXISTS node_relation_no_update
    BEFORE UPDATE ON node_relation BEGIN SELECT RAISE(ABORT, 'relations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS node_relation_no_delete
    BEFORE DELETE ON node_relation BEGIN SELECT RAISE(ABORT, 'relations are append-only'); END;

-- Durable invocation journals: operational recovery state, not an audit event.
CREATE TABLE IF NOT EXISTS invocation_journal (
    invocation_id TEXT PRIMARY KEY,
    audit_event_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    actor TEXT NOT NULL,
    idempotency_key TEXT,
    started_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('in_progress', 'complete')),
    completed_at TEXT,
    outcome_json TEXT,
    audit_event_seq INTEGER
);
CREATE INDEX IF NOT EXISTS invocation_journal_status ON invocation_journal (status);
CREATE INDEX IF NOT EXISTS invocation_journal_idempotency ON invocation_journal (idempotency_key);

CREATE TRIGGER IF NOT EXISTS invocation_journal_complete_is_immutable
    BEFORE UPDATE ON invocation_journal
    WHEN OLD.status = 'complete'
    BEGIN SELECT RAISE(ABORT, 'a completed invocation journal is immutable'); END;

-- Hash-based source-version registry. Version identity is the fingerprint; rows
-- are retained (SUPERSEDED/DELETED) and never deleted.
CREATE TABLE IF NOT EXISTS source_version (
    source_version_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    byte_length INTEGER NOT NULL,
    char_length INTEGER NOT NULL,
    line_count INTEGER NOT NULL,
    file_count INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    normalization_version TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    scoring_contract_version TEXT NOT NULL,
    source_state TEXT NOT NULL CHECK (source_state IN ('CURRENT', 'SUPERSEDED', 'DELETED')),
    superseded_by TEXT,
    manifest_json TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS source_version_fingerprint
    ON source_version (source_id, fingerprint);
CREATE INDEX IF NOT EXISTS source_version_state ON source_version (source_id, source_state);

CREATE TABLE IF NOT EXISTS relearn_offer (
    offer_id TEXT PRIMARY KEY,
    detected_at TEXT NOT NULL,
    previous_source_version_id TEXT,
    current_source_version_id TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS relearn_offer_current ON relearn_offer (current_source_version_id);