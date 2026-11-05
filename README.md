<div align="center">

# PageReuse Lab

**Controlled experiments on when PostgreSQL heap relations grow despite potentially reusable space.**

![PostgreSQL 16](https://img.shields.io/badge/PostgreSQL-16.4-336791) ![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB) ![Phase 2](https://img.shields.io/badge/phase-2%20storage%20analysis-informational)

</div>

## Quick start

```bash
docker compose up -d --wait
uv sync
uv run pytest -v
uv run python -m pagereuse run --rows 10000 --fillfactor 100
uv run python -m pagereuse suite fillfactor
```

Each run writes `results/<run_id>/result.json` (config, environment, methodology, per-checkpoint metrics) and `results/<run_id>/pages.csv` (one row per page per checkpoint). A suite also writes `results/comparison_<suite>_<stamp>.csv` with one row per run and checkpoint.

## Workloads

A workload is a list of steps; the table is measured after every step. Test table: `id int PK, tag int (indexed), payload text`, autovacuum off.

| Step | Parameters | Notes |
|---|---|---|
| `insert` | `rows` | fixed-length payload, one transaction |
| `delete` | `pattern` stride / random / oldest, `every`, `fraction` | oldest = queue-like churn |
| `update` | `pattern`, `column` payload / tag, `grow_bytes` | `payload` is HOT-eligible, `tag` is indexed so never HOT |
| `vacuum` | | plain VACUUM |
| `repeat` | `times`, `steps` | expands inline |

`run` uses load, delete every k-th row, vacuum, reinsert. `run --steps file.json` takes any step list.

## Suites

| Suite | Varies | Holds fixed |
|---|---|---|
| `fillfactor` | 100 / 90 / 70 | three rounds of same-size payload updates to half the rows, VACUUM before the third |
| `updates` | payload (HOT-eligible) / indexed tag / payload +100 bytes | fillfactor 90, same update schedule |
| `cycles` | VACUUM vs none, random vs oldest-first deletes | three delete-half / reinsert-half cycles, live row count constant |

## Measurements and provenance

| Group | Source | Nature |
|---|---|---|
| `exact` | `pg_relation_size`, `pgstattuple`, `pageinspect` (`pd_upper - pd_lower`, line pointer states) | Read from the relation's bytes |
| `fsm` | `pg_freespace(rel, blkno)` | Recorded value in 32-byte categories, rounded down when written. Written by VACUUM and when an inserter leaves a full page; later inserts do not update it, so it can overstate free space |
| `estimated` | `pg_class.relpages/reltuples`, `pg_stat_user_tables` | Planner estimates and cumulative stats |

## Assumptions and limitations

- Autovacuum is disabled on experiment tables; every phase uses a fresh backend.
- `exact_free_bytes` is unused space between line pointers and tuples. It is not all reusable: a new tuple also needs a 4-byte line pointer, and fillfactor reserves space that INSERT will not use.
- `pgstattuple` scans the heap and can opportunistically prune pages. Page metrics are read before it, and pages it changed are listed in `observer_effect`.
- `normal_xmax_set` counts tuple headers with a nonzero xmax. It is a raw header observation, not a visibility judgment.
- In `cycles`, the live row count is constant by construction, so page growth there is reuse failure and not data growth. An earlier stride-based variant grew because it deleted fewer rows each cycle; it was removed for that reason.
- Destructive SQL runs only in database `pagereuse_lab` and on tables matching `^prl_[a-z0-9_]+$`.
