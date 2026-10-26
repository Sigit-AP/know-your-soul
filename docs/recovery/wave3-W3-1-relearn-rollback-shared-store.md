# Wave 3 — W3-1: `service.rollback(relearn_id=...)` committed durably, then raised an untyped `ProgrammingError` and wedged the whole service

- **Finding**: `.superpowers/sdd/2026-10-24-superpower-knowledge-soul/audit-wave3.md` §W3-1 (Important).
- **Owner**: SUB-32. **Files touched**: `src/soul_knowledge/relearn.py` only (plus tests/docs). `service.py` needed no change — see design below.

## Diagnosis

`RelearnEngine.rollback` closed the *shared* `EventStore` (`relearn.py:729`),
byte-replaced the database file, then rebound a fresh handle **only on the
engine** (`self._store = EventStore(db_path)`). Every other holder of the same
instance — `KnowledgeService._store`, the `GraphProjector`, the `AuditLog`, the
score/review engines, the scheduler — was left pointing at a closed connection.
Control returned to `service.rollback`, `_meta_after` touched the dead handle,
and an untyped `sqlite3.ProgrammingError` escaped (HTTP 500) even though the
rollback had **committed** (`ROLLED_BACK` written, restore hash-verified).
Every subsequent service operation then failed the same way until restart.

All prior tests drove `engine.rollback(...)` directly and reopened the store
manually, so the shipped surface (`service.rollback(relearn_id=...)`) had no
happy-path coverage.

## Design decision: reopen the shared store **in place**

Three options were weighed:

1. **Service rebinds to the engine's new store.** Rejected: the projector is
   constructed by callers with the original store, and callers (and tests, and
   future adapters) may hold their own references. Rebinding inside the service
   heals only the references the service knows about and strands the rest —
   the same wedge, one level down, plus brittle cross-class private rebinds of
   the projector, audit log, score/review engines and scheduler.
2. **Don't close the shared store.** Rejected: the byte-exact restore requires
   replacing the database file; an open SQLite handle would keep reading the
   unlinked inode. Closing is unavoidable.
3. **In-place reopen (chosen).** The store *object* is never replaced. The
   engine holds `store._lock` across close → byte-replace → reopen, captures
   `PRAGMA busy_timeout` before closing, and afterwards transplants a fresh
   connection into the same instance (`_reopen_store_in_place`). The fresh
   `EventStore` constructor still owns the whole connect recipe (WAL,
   `synchronous=FULL`, `foreign_keys`, busy timeout, migrations) — relearn.py
   duplicates none of it. Every holder of the shared instance becomes valid
   again simultaneously, including references this module cannot see; an
   injected `SourceRegistry` also survives (the old code silently replaced it).

Fail-closed properties kept or strengthened:

- Byte-exact verification order is unchanged: staged copy hash-checked before
  rename, post-rename hash, per-artifact generation hashes, `CURRENT` pointer
  hash. `restored_db_sha256` is recorded before any new connection opens.
- Failure path: any restore error still reopens the store (now in place, so
  the service survives a *failed* rollback too) and re-raises.
- Concurrency: holders block on the store lock for the swap window instead of
  dying on a closed handle.

## Evidence

RED (pre-fix, exit 1): `tests/test_audit_wave3_residual.py` —
`test_service_rollback_relearn_typed_success_and_service_stays_live` failed at
`service.py:1302` → `sqlite3.ProgrammingError: Cannot operate on a closed
database`; `test_relearn_rollback_heals_the_shared_store_in_place` failed the
same way on the shared object.

GREEN (post-fix, exit 0): both tests pass; `test_relearn.py`,
`test_service_lifecycle.py`, `test_audit_wave2_chain.py`,
`test_audit_wave2_residual.py`, `test_acceptance_e2e.py` — 96 passed, 1 skipped.

Probe (non-vacuous, `/root/.hermes/cache/scratch/sub32/probe_w31.py`, exit 0):

```
rollback typed: kind=relearn status=restored verified=True
byte-exact: True
get_node after rollback: P1 stale=False
query after rollback: routing=direct results=1
second rollback typed refusal: LifecycleError code=LIFECYCLE_ERROR
shared store pragmas after reopen: journal=wal synchronous=2 busy_timeout=15000 fk=1
shared store object usable without manual reopen: True
```
