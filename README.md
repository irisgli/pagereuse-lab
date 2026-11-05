<div align="center">

# PageReuse Lab

**Controlled experiments on when PostgreSQL heap relations grow despite potentially reusable space.**

![PostgreSQL 16](https://img.shields.io/badge/PostgreSQL-16.4-336791) ![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB) ![Phase 1](https://img.shields.io/badge/phase-1%20experiment%20engine-informational)

</div>

## Quick start

```bash
docker compose up -d --wait
uv sync
uv run pytest -v
uv run python -m pagereuse run --rows 10000 --fillfactor 100
```

Each run writes `results/<run_id>/result.json` (config, environment, methodology, per-checkpoint metrics) and `results/<run_id>/pages.csv` (one row per page per checkpoint).

## Experiment

| Phase | Action | Checkpoint |
|---|---|---|
| load | `rows` single-row INSERTs, one transaction | `after_load` |
| delete | stride (`id % k = 0`) or seeded random | `after_delete` |
| vacuum | plain `VACUUM` (skip with `--no-vacuum`) | `after_vacuum` |
| reinsert | same tuple size, new ids | `after_reinsert` |

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
- Destructive SQL runs only in database `pagereuse_lab` and on tables matching `^prl_[a-z0-9_]+$`.
