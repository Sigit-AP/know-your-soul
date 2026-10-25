# Task 3 finding F1 — silent parallel-link collapse passed the read-model validator

## Evidence

`ReadModelValidator.validate` (src/soul_knowledge/read_model_validator.py) only
failed when the pinned NetworkX loader yielded *more* edges than `graph.json`
declares (`loaded.number_of_edges() > len(links)`), never fewer. A generation
carrying `supports` + `depends_on` on the same node pair kept both links in
`graph.json` but loaded as one simple-graph edge: the collapse was silent, and
a consumer assuming 1:1 pair→edge (Task 4 retrieval) would never learn of the
divergence.

## Root cause

The simple-graph collapse is an accepted design decision (matrix row 39) but
was invisible: no field on `ValidationReport` reported which node pairs carry
parallel typed links.

## Affected global criterion

"Canonical output or a loud gate failure" — the canonical `graph.json` and the
pinned load view diverged without any loud signal.

## Smallest fix applied (ruling F1)

The collapse is now loud, not fatal. `ValidationReport` gained
`collapsed_links: tuple[CollapsedLink, ...]`; each `CollapsedLink` names the
`(source, target)` pair and the sorted relation kinds that collapse to one
edge. `validate` computes it from `graph.json`'s links grouped by ordered node
pair; validation still passes (`ok` is unchanged), and the existing
more-edges-than-declared check stays. Task 4 reads the links list using this
field.

## Regression probes

- `tests/test_read_model_validator.py::test_parallel_link_collapse_is_reported_loudly`
  (fresh generation reports `()`; a supports+depends_on pair is reported with
  both relation kinds while `report.ok` stays true and the pinned loader really
  collapses the pair to one edge)

## Rollback note

Remove `CollapsedLink` and the `collapsed_links` field plus the grouping block
in `validate`; no stored data or artifact changes (the field is derived at
validation time), so rollback is lossless.
