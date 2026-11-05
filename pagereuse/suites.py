"""Named sets of configurations whose results are meant to be compared.

Each suite varies one thing at a time. ``rows`` scales every suite so tests
can run a small version of the same comparison.
"""

from __future__ import annotations

from .experiment import ExperimentConfig

REPEAT_CYCLES = 3


def _cycle(rows: int, vacuum: bool, pattern: str) -> list[dict]:
    """Delete half the live rows, optionally vacuum, insert the same number back.
    Live tuple count is constant across cycles, so any page growth is reuse failure."""
    body: list[dict] = [{"op": "delete", "pattern": pattern, "fraction": 0.5}]
    if vacuum:
        body.append({"op": "vacuum"})
    body.append({"op": "insert", "rows": rows // 2})
    return [{"op": "insert", "rows": rows}, {"op": "repeat", "times": REPEAT_CYCLES, "steps": body}]


def _update(rows: int, column: str, grow: int = 0) -> list[dict]:
    return [
        {"op": "insert", "rows": rows},
        {"op": "update", "pattern": "stride", "every": 2, "column": column, "grow_bytes": grow},
        {"op": "update", "pattern": "stride", "every": 2, "column": column, "grow_bytes": grow},
        {"op": "vacuum"},
        {"op": "update", "pattern": "stride", "every": 2, "column": column, "grow_bytes": grow},
    ]


def fillfactor_sweep(rows: int) -> list[ExperimentConfig]:
    """Same HOT-eligible update workload at different fillfactors."""
    return [
        ExperimentConfig(
            table=f"prl_ff{ff}",
            fillfactor=ff,
            rows=rows,
            label=f"fillfactor={ff}",
            steps=_update(rows, "payload"),
        )
        for ff in (100, 90, 70)
    ]


def update_patterns(rows: int) -> list[ExperimentConfig]:
    """HOT-eligible vs indexed-column vs growing updates, fillfactor 90."""
    return [
        ExperimentConfig(
            table="prl_upd_payload",
            fillfactor=90,
            rows=rows,
            label="update payload (HOT-eligible)",
            steps=_update(rows, "payload"),
        ),
        ExperimentConfig(
            table="prl_upd_tag",
            fillfactor=90,
            rows=rows,
            label="update indexed tag (never HOT)",
            steps=_update(rows, "tag"),
        ),
        ExperimentConfig(
            table="prl_upd_grow",
            fillfactor=90,
            rows=rows,
            label="update payload +100 bytes",
            steps=_update(rows, "payload", 100),
        ),
    ]


def delete_cycles(rows: int) -> list[ExperimentConfig]:
    """Repeated delete/reinsert cycles: with and without VACUUM, random vs oldest-first."""
    return [
        ExperimentConfig(
            table="prl_cyc_vac_rand",
            rows=rows,
            label="cycles vacuum random",
            steps=_cycle(rows, True, "random"),
        ),
        ExperimentConfig(
            table="prl_cyc_novac_rand",
            rows=rows,
            label="cycles no-vacuum random",
            steps=_cycle(rows, False, "random"),
        ),
        ExperimentConfig(
            table="prl_cyc_vac_old",
            rows=rows,
            label="cycles vacuum oldest",
            steps=_cycle(rows, True, "oldest"),
        ),
        ExperimentConfig(
            table="prl_cyc_novac_old",
            rows=rows,
            label="cycles no-vacuum oldest",
            steps=_cycle(rows, False, "oldest"),
        ),
    ]


def _churn(rows: int, hold: bool) -> list[dict]:
    """Delete half, VACUUM, reinsert half; optionally under a snapshot held from
    before the delete and released before a second VACUUM and reinsert."""
    steps: list[dict] = [{"op": "insert", "rows": rows}]
    if hold:
        steps.append({"op": "hold_snapshot"})
    steps += [
        {"op": "delete", "pattern": "random", "fraction": 0.5},
        {"op": "vacuum"},
        {"op": "insert", "rows": rows // 2},
    ]
    if hold:
        steps += [{"op": "release_snapshot"}, {"op": "vacuum"}, {"op": "insert", "rows": rows // 2}]
    return steps


def long_snapshots(rows: int) -> list[ExperimentConfig]:
    """Same churn with and without a long-running REPEATABLE READ snapshot."""
    return [
        ExperimentConfig(
            table="prl_snap_none", rows=rows, label="churn, no snapshot", steps=_churn(rows, False)
        ),
        ExperimentConfig(
            table="prl_snap_held", rows=rows, label="churn under held snapshot", steps=_churn(rows, True)
        ),
    ]


def concurrent_inserts(rows: int) -> list[ExperimentConfig]:
    """Load with 1 vs 4 concurrent sessions, then one delete/VACUUM/reinsert cycle."""
    out = []
    for n in (1, 4):
        out.append(
            ExperimentConfig(
                table=f"prl_conc_{n}",
                rows=rows,
                label=f"load with {n} session{'s' if n > 1 else ''}",
                steps=[
                    {"op": "insert", "rows": rows, "sessions": n},
                    {"op": "delete", "pattern": "random", "fraction": 0.5},
                    {"op": "vacuum"},
                    {"op": "insert", "rows": rows // 2, "sessions": n},
                ],
            )
        )
    return out


def _overlap(rows: int, hold: bool) -> list[dict]:
    """Two deletes of a quarter each, each covered by its own snapshot taken just
    before it. Reinserts total what was deleted, so the live count ends where it
    began; the snapshots are released one at a time with a VACUUM after each."""
    quarter = rows // 4
    snap = lambda op, name: [{"op": op, "name": name}] if hold else []  # noqa: E731
    return [
        {"op": "insert", "rows": rows},
        *snap("hold_snapshot", "a"),
        {"op": "delete", "pattern": "random", "fraction": 0.25},
        *snap("hold_snapshot", "b"),
        {"op": "delete", "pattern": "random", "fraction": 1 / 3},
        {"op": "vacuum"},
        {"op": "insert", "rows": quarter},
        *snap("release_snapshot", "a"),
        {"op": "vacuum"},
        {"op": "insert", "rows": quarter},
        *snap("release_snapshot", "b"),
        {"op": "vacuum"},
    ]


def overlapping_snapshots(rows: int) -> list[ExperimentConfig]:
    """Staggered snapshot lifetimes: VACUUM can reclaim only what died before the oldest one."""
    return [
        ExperimentConfig(
            table="prl_ovl_none",
            rows=rows,
            label="staggered churn, no snapshots",
            steps=_overlap(rows, False),
        ),
        ExperimentConfig(
            table="prl_ovl_two", rows=rows, label="staggered churn, two snapshots", steps=_overlap(rows, True)
        ),
    ]


SUITES = {
    "overlap": overlapping_snapshots,
    "snapshots": long_snapshots,
    "concurrency": concurrent_inserts,
    "fillfactor": fillfactor_sweep,
    "updates": update_patterns,
    "cycles": delete_cycles,
}
