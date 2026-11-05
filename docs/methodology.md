# Methodology, assumptions and limitations

This document records the choices behind every number the lab produces, so a result can be reproduced or disputed on its merits.

## The question

PostgreSQL relations sometimes grow although space inside them could, in principle, hold the new data. The lab treats that as a hypothesis to test under controlled conditions, not as a defect to confirm. The mechanisms that decide whether space is reused are all documented PostgreSQL behaviour: MVCC leaves dead versions behind; VACUUM reclaims them and records free space in the free space map (FSM); inserts consult the FSM; updates try the tuple's own page first; fillfactor reserves per-page room for exactly those updates; HOT avoids index maintenance when the new version stays on the page.

## Environment

- PostgreSQL 16.4 by default (`postgres:16.4`); 17.11 and 18.6 via `PG_IMAGE`. Default 8 kB block size, default configuration.
- Extensions: `pageinspect` (raw page reads), `pgstattuple` (exact tuple scan), `pg_freespacemap` (FSM reads).
- Database `pagereuse_lab`; only tables matching `^prl_[a-z0-9_]+$` are ever created, dropped or modified.

## Workload control

- Autovacuum is disabled on every experiment table. VACUUM runs only as an explicit step.
- Every step runs on a fresh connection. A backend caches its last insertion target block; a fresh backend starts from the FSM, which keeps the FSM, not session state, as the thing under test.
- Payloads are fixed-length random ASCII, seeded, far below the TOAST threshold, so all tuples of a run have the same size and nothing is compressed or stored out of line.
- Row choice for deletes and updates is deterministic given the seed. `stride` picks ids divisible by k, `random` a seeded sample, `oldest` the first fraction of live ids.
- The same seed gives byte-identical page layouts across runs; a test checks this.

## Measurements and their provenance

| Group | Source | How to read it |
|---|---|---|
| `exact` | `pg_relation_size`, `pgstattuple`, `pageinspect` | Read from the relation's bytes at that moment. `exact_free_bytes` is `pd_upper - pd_lower`. |
| `fsm` | `pg_freespace(rel, blkno)` | What the FSM recorded, in 32-byte categories rounded down. Written by VACUUM and when an inserter leaves a page; not updated by later inserts into that page, so it can overstate. |
| `estimated` | `pg_class.relpages`, `reltuples`, `pg_stat_user_tables` | Planner estimates and cumulative counters. Used for `n_tup_hot_upd` and nothing load-bearing. |

Per page the lab records the line-pointer state counts (unused, normal, redirect, dead), how many normal tuples carry a nonzero `xmax`, the bytes those tuples occupy, and the `PD_ALL_VISIBLE` flag.

## Observer effects

`pgstattuple` scans the heap, and a heap scan can opportunistically prune a page. Page metrics are therefore read with `pageinspect` before `pgstattuple` runs, then read again, and every page that changed is listed under `observer_effect`. In all recorded runs that list is empty.

## What "free" means

`exact_free_bytes` is unused space between the line-pointer array and the tuples. It is not all reusable:

- a new tuple also needs a 4-byte line pointer;
- an INSERT only uses a page the FSM offers for `tuple length + fillfactor reserve` bytes, and if the FSM offers none, the relation's last page;
- an UPDATE may use the reserve on the tuple's own page, but not on other pages;
- space held by dead versions is unavailable until pruning or VACUUM.

The viewer's "FSM minus measured" colouring exists to make the gap between recorded and actual free space visible rather than averaged away.

## Findings pinned by tests

- A DELETE changes no page's free space; it stamps `xmax`. The relation does not shrink.
- Right after VACUUM, every page's FSM value is 4 to 35 bytes below the measured free space (one line pointer plus category rounding).
- After VACUUM, reinserting the deleted rows adds no pages. Without VACUUM, every cycle appends pages.
- With oldest-first churn, VACUUM can truncate an empty tail, so the relation shrinks and regrows; that is truncation plus extension, not reuse.
- Updating an indexed column is never HOT. At fillfactor 100 the first update round is never HOT either, since no page has room.
- Lower fillfactor costs pages at load and saves more under update churn (at 10,000 rows: 304 to 607 pages at fillfactor 100, 435 to 480 at fillfactor 70).

## Multi-session steps

- `hold_snapshot` opens a REPEATABLE READ transaction on a separate connection and runs one SELECT, which pins that backend's xmin. VACUUM cannot remove tuples deleted after that point; the lab records the server's own `VACUUM (VERBOSE)` line, "N are dead but not yet removable", rather than inferring it. Measured: with the snapshot held, reinserting the deleted rows extends the relation even though every deleted tuple's space is physically present; after `release_snapshot` the next VACUUM reclaims it and the following reinsert adds no pages.
- `insert` with `sessions` > 1 splits the rows into contiguous id slices and inserts them from that many connections at once, each in one transaction. Each backend keeps its own insertion target, so pages fill from several writers and the layout is no longer byte-reproducible across runs; only counts and sizes are asserted for these.

## Releases compared

All five suites at 2000 rows were run on 16.4, 17.11 and 18.6 (2026-11-04). For every single-session workload the per-page records were identical across the three releases at every checkpoint, down to FSM values and line-pointer states. Only the 4-session load differed (61, 62, 63 pages), and that workload is not deterministic within one release either. CI runs the test suite on all three. The earlier statement in this document that later releases differ in insert and pruning behaviour was an assumption; it is withdrawn for the mechanisms measured here.

## Predictor evaluation

Predictors are scored walk-forward: at step i they see checkpoint i-1 and the operation of step i, never the actual outcome. No parameter is fitted on the run being scored. The capacity model's known failure, repeat updates after the hot rows were relocated, is pinned by a test so a change in behaviour is noticed.

## Not covered

- Measured on 16.4, 17.11 and 18.6 only, all with default configuration. Non-default settings such as `vacuum_truncate` off or a different block size are not covered.
- Concurrency is limited to parallel inserters and one held snapshot. Concurrent updates, lock contention between writers and autovacuum timing are out of scope.
- TOAST, variable-length rows, indexes' own storage, and WAL are not measured.
- The FSM is read through `pg_freespace`, which reports the leaf value for each page; the upper levels of the FSM tree are not inspected.
