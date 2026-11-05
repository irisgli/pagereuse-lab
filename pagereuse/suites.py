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


SUITES = {
    "fillfactor": fillfactor_sweep,
    "updates": update_patterns,
    "cycles": delete_cycles,
}
