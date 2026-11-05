# Methodology, assumptions and limitations

This document records the choices behind every number the lab produces, so a result can be reproduced or disputed on its merits.

## The question

PostgreSQL relations sometimes grow although space inside them could, in principle, hold the new data. The lab treats that as a hypothesis to test under controlled conditions, not as a defect to confirm. The mechanisms that decide whether space is reused are all documented PostgreSQL behaviour: MVCC leaves dead versions behind; VACUUM reclaims them and records free space in the free space map (FSM); inserts consult the FSM; updates try the tuple's own page first; fillfactor reserves per-page room for exactly those updates; HOT avoids index maintenance when the new version stays on the page.

## Environment

- PostgreSQL 16.4 by default (`postgres:16.4`); 17.11 and 18.6 via `PG_IMAGE`. Default 8 kB block size, default configuration.
- Extensions: `pageinspect` (raw page reads), `pgstattuple` (exact tuple scan), `pg_freespacemap` (FSM reads).
- Database `pagereuse_lab`; only tables matching `^prl_[a-z0-9_]+$` are ever created, dropped or modified.

## Workload control

- Autovacuum is disabled on every experiment table unless the run's `autovacuum` reloptions enable it. For such runs the lab sets `autovacuum_naptime` to 1 s with `ALTER SYSTEM` (the default 60 s nap would dominate every wait); this is server-wide, is recorded in `environment.autovacuum`, and is not reverted. Otherwise VACUUM runs only as an explicit step.
- Every step runs on a fresh connection. A backend caches its last insertion target block; a fresh backend starts from the FSM, which keeps the FSM, not session state, as the thing under test.
- Payloads are fixed-length random ASCII, seeded, far below the TOAST threshold, so all tuples of a run have the same size and nothing is compressed or stored out of line.
- Row choice for deletes and updates is deterministic given the seed. `stride` picks ids divisible by k, `random` a seeded sample, `oldest` the first fraction of live ids. Payload updates run one statement per id in id order; a tag update is one statement whose plan the planner picks (an index scan for long id lists, a bitmap heap scan for short ones).
- The same seed gives byte-identical page layouts across runs; a test checks this.

## Measurements and their provenance

| Group | Source | How to read it |
|---|---|---|
| `exact` | `pg_relation_size`, `pgstattuple`, `pageinspect` | Read from the relation's bytes at that moment. `exact_free_bytes` is `pd_upper - pd_lower`. |
| `fsm` | `pg_freespace(rel, blkno)`, and the FSM root page via `get_raw_page(rel, 'fsm', 0)` | What the FSM recorded per page, in 32-byte categories rounded down. Written by VACUUM and when an inserter leaves a page; not updated by later inserts into that page, so it can overstate. The root value (`fsm_root_avail_bytes`) is the most space the tree's top claims anywhere; only VACUUM raises it. |
| `estimated` | `pg_class.relpages`, `reltuples`, `pg_stat_user_tables` | Planner estimates and cumulative counters. Used for `n_tup_hot_upd` and nothing load-bearing. |

Per page the lab records the line-pointer state counts (unused, normal, redirect, dead), the xmin horizon at read time (`min(backend_xmin)` over other sessions in `pg_stat_activity`), how many xmax-set tuples are older than it (`dead_removable`) and the MAXALIGNed bytes they occupy (`dead_removable_bytes`), how many normal tuples carry a nonzero `xmax`, the bytes those tuples occupy, and the `PD_PAGE_FULL` and `PD_ALL_VISIBLE` flags.

Each checkpoint also records `live_rows`: for every live row, its id, the block its visible version is on and that version's stored length. `pageinspect` lists every normal tuple on every page; a TID scan over those positions then lets the server apply MVCC visibility, so a row updated several times maps to exactly one version even while its older versions and HOT chain are still on the page. The count is checked against `pgstattuple`. Each update step records the ids it targets, chosen from the previous checkpoint's live set before the statement runs, and for a single-statement update the scan type `EXPLAIN` reports beforehand (index, bitmap or sequential), since that decides the order in which pages are reached and when they are pruned.

## Observer effects

`pgstattuple` scans the heap, and a heap scan can opportunistically prune a page. Page metrics are therefore read with `pageinspect` before `pgstattuple` and the row-location query run, then read again, and every page that changed is listed under `observer_effect`. In all recorded runs that list is empty.

The row-location query deliberately avoids a sequential scan: a plain `SELECT id, ctid` is executed in page-at-a-time mode and calls `heap_page_prune_opt` on every page, which pruned every full page in a trial run and would have handed the next step pages it should have pruned itself. A TID scan fetches tuples without pruning; `enable_seqscan` is switched off for that one statement because the planner otherwise prefers a sequential scan for long TID lists. (`pgstattuple` is safe for a different reason: it scans without page mode and that path never prunes.)

## What "free" means

`exact_free_bytes` is unused space between the line-pointer array and the tuples. It is not all reusable:

- a new tuple also needs a 4-byte line pointer;
- an INSERT only uses a page the FSM offers for `tuple length + fillfactor reserve` bytes, and if the FSM offers none, the relation's last page;
- an UPDATE may use the reserve on the tuple's own page, but not on other pages;
- space held by dead versions is unavailable until pruning or VACUUM.

The viewer's "FSM minus measured" colouring exists to make the gap between recorded and actual free space visible rather than averaged away.

## Two FSM searches

An inserter with no cached target page searches the FSM from the root, descending only through nodes that claim enough space. Those upper nodes are written by `FreeSpaceMapVacuum`, which VACUUM runs; the per-page values an inserter records when it leaves a full page go into the leaf only. Measured: after loading a table that has never been vacuumed, every leaf holds a value but the root page's category is 0, and no search from the root finds anything, so new versions go to the last page and then to fresh pages even when a pruned page with kilobytes free has an FSM entry. After VACUUM the root carries the largest leaf category. The second search is the one an inserter runs when its cached target page fills: it records the page's space and searches that page's own FSM leaf page directly, where current values are visible. `capacity_rows` models both; `capacity` treats every leaf value as searchable.

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

## Overlapping snapshots

With two snapshots taken before two successive deletes and released one at a time, VACUUM behaved exactly as the xmin horizon predicts: while both were held it removed nothing and reported all 1000 deleted versions as "dead but not yet removable"; after the older one was released it removed the 500 versions deleted before the younger snapshot and reported the other 500 as not yet removable; after the younger was released it removed the rest. The per-page `dead_removable` count, read before each VACUUM, matched what VACUUM then removed, and the capacity model was exact on every step of both runs. The relation grew only on the reinsert made while both snapshots were held; once space had been reclaimed the next reinsert added no pages.

A side observation from the control run: the second DELETE opportunistically pruned most of the first delete's dead versions on the pages it visited (they became `LP_DEAD` line pointers, which `pgstattuple` no longer counts as dead tuples). With a snapshot held that pruning did not happen. "Dead tuple count" from `pgstattuple` therefore undercounts dead space once pruning has run; the line-pointer counts are the complete picture.

## Autovacuum

The trigger rule is the documented one: a table is vacuumed when dead tuples exceed `autovacuum_vacuum_threshold + autovacuum_vacuum_scale_factor * reltuples`, or inserts since the last vacuum exceed the insert equivalents. One detail matters for fresh tables: `reltuples` is -1 until the first VACUUM or ANALYZE, and the launcher treats that as 0. Measured: loading 2000 rows into a new table triggered an insert-driven autovacuum within seconds (2000 > 1000 + 0), which set `reltuples`. After that, deleting 10% (200 dead, under 50 + 0.2 × 2000 = 450) triggered nothing, and reinserting 200 rows extended the relation from 61 to 67 pages with 200 dead tuples' worth of space present. Deleting 40% more crossed the threshold; autovacuum ran within 0.3 s and the next reinsert added no pages. With `autovacuum_vacuum_scale_factor = 0.02` the 10% delete was enough (200 > 90) and the table never grew. The capacity model applies the same rule to predict whether a `wait_autovacuum` step changes anything.

Timing caveat: an autovacuum can complete between a step and its measurement, or during a later step; the lab records `autovacuum_count` at every checkpoint so such runs are visible, and each wait step's baseline is the previous checkpoint's count.

## Out-of-sample validation

The suites were used while building the capacity model, so they cannot show whether it generalises. `pagereuse validate` draws workloads from a seeded grammar (row count 1000 to 3000; payload 64, 120, 200, 400 or 700 bytes; fillfactor 70 to 100; five to eight steps mixing deletes by stride, random or oldest with fractions 0.1 to 0.5, payload or indexed-column updates with 0 to 100 bytes of growth, inserts, VACUUMs and snapshot holds) and scores every predictor one step ahead.

Seeds 1000 to 1011 on 16.4, 225 scored steps: `capacity` MAE 0.91 pages, exact on 85%; `last_value` 20.9 and 64%; `linear_trend` 32.3 and 40%. By operation, `capacity` was exact on all 20 deletes, 16 VACUUMs and 8 snapshot steps; on 14 inserts it was within one page (three overpredictions by one, consistent with the FSM's 32-byte category rounding); on 17 updates its MAE was 3.8 with a maximum of 22, the weak spot already known from the suites. One model change came out of this batch: inserts are now sized from the first checkpoint's tuple length rather than the current average, because after a growing update the average tuple is larger than what an insert carries; before that change the worst insert error was 8 pages.

## Indexes and TOAST

Every checkpoint records, for each index, `pg_relation_size` and `pgstatindex` (tree level, leaf, internal, deleted and empty pages, average leaf density, fragmentation), and for the TOAST table its size and exact `pgstattuple` counts. `pg_total_relation_size` is recorded so the heap-only numbers can be set against the whole.

Measured under three delete-half / VACUUM / reinsert cycles at 1000 rows:

- TOAST behaves like the heap. 3000-byte payloads stored `EXTERNAL` make two chunks per row, four chunks per page, 500 pages. With VACUUM the TOAST table stays at 500 pages through every cycle; without it, 250 pages are appended per cycle and dead chunks accumulate; with oldest-first deletes VACUUM truncates it to 250 pages and the next reinsert extends it back.
- The primary-key B-tree does not reuse like the heap. Keys are monotonically increasing ids, so new entries always go to the rightmost leaf. Random deletes leave every leaf page half full; VACUUM removes the dead index entries but a leaf page is only recycled once it is entirely empty, so leaf pages grow 3, 4, 5, 6 across the cycles while density falls from 82% to 41%. With oldest-first deletes the leftmost leaves empty out, VACUUM marks them deleted (`deleted_pages` rises), and later inserts recycle them, so leaf pages stay at 2 or 3.
- The `tag` index, whose keys cycle through 97 values, never grows under either pattern.
- `pg_total_relation_size` is at least the sum of the recorded pieces; the remainder is the indexes' own FSM and visibility forks, which are not itemised.

## Prior work

- Peter Geoghegan, "The Free Space Map: Problems and Opportunities", pgsql-hackers, 2021: a developer's account of how FSM-driven placement can scatter related tuples and fragment the heap. The `fsm_gap` colouring and the FSM-based insert rule in the capacity model are the lab's way of making that placement visible and testable.
- Bruce Momjian, "MVCC Unmasked": the visibility rules that make the held-snapshot results what they are. The lab's `xmax`-based removability count follows the same xmin-horizon reasoning.
- PostgreSQL documentation on routine vacuuming, fillfactor, HOT updates and `pg_freespacemap`, the source for every expectation the tests encode.

## Predictor evaluation

Predictors are scored walk-forward: at step i they see checkpoint i-1 and the operation of step i, never the actual outcome. No parameter is fitted on the run being scored. The capacity model's known failure, repeat updates after the hot rows were relocated, is pinned by a test so a change in behaviour is noticed.

## Not covered

- Measured on 16.4, 17.11 and 18.6 only, all with default configuration. Non-default settings such as `vacuum_truncate` off or a different block size are not covered.
- Concurrency is limited to parallel inserters and one held snapshot. Concurrent updates, lock contention between writers and autovacuum timing are out of scope.
- Index and TOAST storage are measured at relation level (sizes, `pgstatindex`, `pgstattuple` on the TOAST table), not page by page, and the capacity model predicts heap pages only. Variable-length rows within one run and WAL are not measured.
- The FSM is read through `pg_freespace`, which reports the leaf value for each page; the upper levels of the FSM tree are not inspected.
