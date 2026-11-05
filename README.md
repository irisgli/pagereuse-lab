<div align="center">

# PageReuse Lab

**Controlled experiments on when PostgreSQL heap relations grow despite potentially reusable space.**

[![ci](https://github.com/irisgli/pagereuse-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/irisgli/pagereuse-lab/actions/workflows/ci.yml) ![PostgreSQL 16 to 18](https://img.shields.io/badge/PostgreSQL-16%20%7C%2017%20%7C%2018-336791) ![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB) ![Phase 11](https://img.shields.io/badge/phase-11%20indexes%20and%20TOAST-informational)

</div>

## Quick start

```bash
docker compose up -d --wait              # PG_IMAGE=postgres:18 docker compose up -d --wait for another release
uv sync
uv run pytest -v
uv run python -m pagereuse run --rows 10000 --fillfactor 100
uv run python -m pagereuse suite fillfactor
uv run python -m pagereuse serve        # viewer at http://127.0.0.1:8765
uv run python -m pagereuse evaluate     # score size predictors on results/
uv run python -m pagereuse validate     # score them on 12 random workloads never used for tuning
scripts/bench.sh                        # all suites at 10k rows + evaluation, fixed seeds
```

Each run writes `results/<run_id>/result.json` (config, environment, methodology, per-checkpoint metrics) and `results/<run_id>/pages.csv` (one row per page per checkpoint). A suite also writes `results/comparison_<suite>_<stamp>.csv` with one row per run and checkpoint.

## Workloads

A workload is a list of steps; the table is measured after every step. Test table: `id int PK, tag int (indexed), payload text`, autovacuum off.

| Step | Parameters | Notes |
|---|---|---|
| `insert` | `rows` | fixed-length payload, one transaction |
| `delete` | `pattern` stride / random / oldest, `every`, `fraction` | oldest = queue-like churn |
| `update` | `pattern`, `column` payload / tag, `grow_bytes` | `payload` is HOT-eligible, `tag` is indexed so never HOT |
| `insert` | `sessions` | split across that many connections running at once |
| `vacuum` | | `VACUUM (VERBOSE)`; records the server's "dead but not yet removable" count |
| `wait_autovacuum` | `timeout` | poll until an autovacuum of the table completes; records whether one ran and how long it took |
| `hold_snapshot` / `release_snapshot` | `name` | keep a REPEATABLE READ transaction open on a side connection across later steps; several may be held at once |
| `repeat` | `times`, `steps` | expands inline |

`run` uses load, delete every k-th row, vacuum, reinsert. `run --steps file.json` takes any step list.

## Suites

| Suite | Varies | Holds fixed |
|---|---|---|
| `fillfactor` | 100 / 90 / 70 | three rounds of same-size payload updates to half the rows, VACUUM before the third |
| `updates` | payload (HOT-eligible) / indexed tag / payload +100 bytes | fillfactor 90, same update schedule |
| `cycles` | VACUUM vs none, random vs oldest-first deletes | three delete-half / reinsert-half cycles, live row count constant |
| `snapshots` | a long-running snapshot held across delete and VACUUM, or none | delete half, VACUUM, reinsert half; the held variant then releases, VACUUMs and reinserts again |
| `concurrency` | 1 vs 4 inserting sessions | load, delete half, VACUUM, reinsert half |
| `overlap` | two snapshots with staggered lifetimes, or none | two deletes of a quarter each, VACUUM and reinsert after each release |
| `autovacuum` | default thresholds vs `autovacuum_vacuum_scale_factor = 0.02` | autovacuum on, naptime 1 s; a 10% delete and reinsert, then a 40% delete and reinsert |
| `storage` | 200-byte in-line vs 3000-byte out-of-line payloads, random vs oldest-first deletes | three delete-half / VACUUM / reinsert cycles; indexes and TOAST measured alongside the heap |

## Measurements and provenance

| Group | Source | Nature |
|---|---|---|
| `exact` | `pg_relation_size`, `pgstattuple`, `pageinspect` (`pd_upper - pd_lower`, line pointer states) | Read from the relation's bytes |
| `fsm` | `pg_freespace(rel, blkno)` | Recorded value in 32-byte categories, rounded down when written. Written by VACUUM and when an inserter leaves a full page; later inserts do not update it, so it can overstate free space |
| `estimated` | `pg_class.relpages/reltuples`, `pg_stat_user_tables` | Planner estimates and cumulative stats |

Each checkpoint also records every index's size and `pgstatindex` statistics (leaf, deleted and empty pages, leaf density), the TOAST table's size and exact tuple counts when the table has one, and `pg_total_relation_size`. `storage="external"` on a run forces the payload out of line so TOAST placement is deterministic.

Each page record also carries `dead_removable`: deleted versions older than the xmin horizon (the oldest `backend_xmin` among other sessions), which is what a VACUUM run at that moment could remove.

## Viewer

`pagereuse serve` reads `results/` and serves a dependency-free viewer: a run list grouped by suite, a step strip of page counts, a heatmap with one cell per heap page in file order (colour by measured free bytes, FSM record, FSM minus measured, dead or redirect line pointers, xmax set, all-visible flag), per-page hover details, and an overlay chart of page counts across ticked runs.

## Predicting growth

`pagereuse evaluate` scores three one-step-ahead predictors of the page count on every saved run, walk-forward, and writes `results/model_eval.csv`. Each predictor sees only the previous checkpoint's measurements and the operation about to run.

| Predictor | What it does |
|---|---|
| `last_value` | pages stay what they were |
| `linear_trend` | least-squares line through the page counts so far |
| `capacity` | slotted-page accounting: inserts use pages the FSM offers for `tuple + reserve` bytes, then the last page, then extend; updates stay on their page when the new version fits (reserve usable, dead versions pruned first); VACUUM removes only versions older than the xmin horizon and truncates a tail left with no live tuple |

On the three suites at 2000 rows the capacity model is exact on every insert, delete, VACUUM and first update round, and has mean absolute error under 2 pages against about 10 for `last_value`. It underestimates repeat updates once an earlier round relocated the hot rows to fresh pages, because measurements carry no row identities; the test suite pins this.

`pagereuse validate` scores the predictors on seeded random workloads (1000 to 3000 rows, payloads 64 to 700 bytes, fillfactor 70 to 100, mixed deletes, updates, inserts, VACUUMs and snapshot holds) that were not used while building the model. On 12 such workloads, 225 scored steps:

| Predictor | MAE (pages) | Exact |
|---|---|---|
| `last_value` | 20.9 | 64% |
| `linear_trend` | 32.3 | 40% |
| `capacity` | 0.9 | 85% |

By operation, `capacity` was exact on every delete, VACUUM and snapshot step, within one page on inserts, and weakest on updates (MAE 3.8, max 22 pages, 53% exact). Machine learning is deferred until a workload beats this physical model.

## Beyond the heap

Under three delete-half / VACUUM / reinsert cycles at 1000 rows, the heap stays flat and so does the TOAST table (500 pages for 3000-byte payloads; without VACUUM it grows 250 pages per cycle, and under oldest-first churn VACUUM truncates it exactly like the heap). The primary-key index does not stay flat: with random deletes its leaf pages go 3, 4, 5, 6 across the cycles and leaf density falls from 82% to 41% even though VACUUM ran every time, because new ids always land at the right end of the B-tree and a half-emptied leaf page is not reused until it is completely empty. With oldest-first deletes those pages do empty, appear as `deleted_pages`, and are recycled. The cyclic `tag` index never grows. Heap reuse and index reuse are separate questions with different answers in the same run.

## Across PostgreSQL releases

The five suites at 2000 rows were run on 16.4, 17.11 and 18.6. Every single-session workload produced byte-identical per-page layouts on all three: the same `pd_lower`, `pd_upper`, FSM value and line-pointer states on every page at every checkpoint. The only run that differed was the 4-session load (61, 62 and 63 pages), and that one varies between runs on the same release because it depends on thread scheduling. For these heap-placement mechanisms, nothing measured here changed between 16 and 18.

## Assumptions and limitations

The full account is in [docs/methodology.md](docs/methodology.md). In short:

- Autovacuum is off on experiment tables unless a run enables it; then the lab sets the server's `autovacuum_naptime` to 1 s (server-wide, recorded in the result). Every step uses a fresh backend.
- `exact_free_bytes` is unused space between line pointers and tuples. It is not all reusable: a new tuple also needs a 4-byte line pointer, and fillfactor reserves space that INSERT will not use.
- `pgstattuple` scans the heap and can opportunistically prune pages. Page metrics are read before it, and pages it changed are listed in `observer_effect`.
- `normal_xmax_set` counts tuple headers with a nonzero xmax. It is a raw header observation, not a visibility judgment.
- In `cycles`, the live row count is constant by construction, so page growth there is reuse failure and not data growth. An earlier stride-based variant grew because it deleted fewer rows each cycle; it was removed for that reason.
- VACUUM truncates trailing empty pages. With oldest-first churn the relation can shrink at a VACUUM step and regrow on the next insert; that is truncation plus fresh extension, not reuse.
- Destructive SQL runs only in database `pagereuse_lab` and on tables matching `^prl_[a-z0-9_]+$`.

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
```

CI runs the same three commands against `postgres:16.4`, `postgres:17` and `postgres:18` service containers on every push and pull request. Switching the local container between releases needs `docker compose down -v` first, since the data directory is release-specific.
