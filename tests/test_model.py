"""Phase 4: size predictors scored walk-forward on real runs.

The assertions pin what the capacity model gets exactly right (inserts,
deletes, VACUUM truncation, the first round of updates) and the limitation
it has (repeat updates after the hot rows were relocated)."""

from __future__ import annotations

import csv

from pagereuse.model import (
    PREDICTORS,
    _proportional,
    evaluate_run,
    predict_linear_trend,
    summarize,
    tuple_footprint,
    write_evaluation,
)


def _rows(results, predictor, ops=None):
    return [
        r
        for res in results
        for r in evaluate_run(res)
        if r["predictor"] == predictor and (ops is None or r["op"] in ops)
    ]


def test_tuple_footprint_is_aligned_length_plus_line_pointer():
    pages = [
        {"lp_normal": 34, "normal_tuple_bytes": 34 * 232},
        {"lp_normal": 2, "normal_tuple_bytes": 2 * 230},
    ]
    assert tuple_footprint(pages) == 232 + 4  # mean 231.9 aligns up to 232


def test_proportional_split_sums_to_n():
    out = _proportional(10, [3, 3, 3, 0])
    assert sum(out) == 10 and out[3] == 0 and max(out) - min(out[:3]) <= 1


def test_linear_trend_extrapolates():
    assert predict_linear_trend([10, 20, 30], None, None, None, None) == 40
    assert predict_linear_trend([7], None, None, None, None) == 7


def test_capacity_exact_for_insert_delete_vacuum(cyc):
    rows = _rows(cyc.values(), "capacity", {"insert", "delete", "vacuum"})
    assert len(rows) == 2 * 9 + 2 * 6  # two vacuum runs (10 checkpoints), two without (7)
    assert all(r["error"] == 0 for r in rows), [r for r in rows if r["error"]]


def test_capacity_predicts_vacuum_truncation(cyc):
    r = cyc["cycles vacuum oldest"]
    rows = [x for x in evaluate_run(r) if x["predictor"] == "capacity" and x["op"] == "vacuum"]
    assert any(x["actual"] < r["checkpoints"][0]["relation"]["exact"]["main_pages"] for x in rows)
    assert all(x["error"] == 0 for x in rows)


def test_capacity_exact_for_first_update_round(ff, upd):
    rows = [
        x
        for res in list(ff.values()) + list(upd.values())
        for x in evaluate_run(res)
        if x["predictor"] == "capacity" and x["step"] == 1
    ]
    assert rows and all(x["op"] == "update" and x["error"] == 0 for x in rows), rows


def test_capacity_underestimates_repeat_updates_on_relocated_rows(ff):
    """Known limitation: after round 1 moved most hot rows to fresh pages,
    round 2 lands on those full pages. The model, which has no row identities,
    credits pruned space on the pages the rows left, so it predicts too few."""
    r = ff["fillfactor=90"]
    step2 = next(x for x in evaluate_run(r) if x["predictor"] == "capacity" and x["step"] == 2)
    assert step2["error"] < 0


def test_capacity_beats_statistical_baselines(ff, upd, cyc):
    results = list(ff.values()) + list(upd.values()) + list(cyc.values())
    summary = {s["predictor"]: s for s in summarize([x for res in results for x in evaluate_run(res)])}
    assert set(summary) == set(PREDICTORS)
    assert summary["capacity"]["mae"] < summary["last_value"]["mae"]
    assert summary["capacity"]["mae"] < summary["linear_trend"]["mae"]
    assert summary["capacity"]["exact_rate"] > 0.8


def test_write_evaluation_csv(cyc, tmp_path):
    rows, summary = write_evaluation(list(cyc.values()), tmp_path / "eval.csv")
    with open(tmp_path / "eval.csv") as f:
        on_disk = list(csv.DictReader(f))
    assert len(on_disk) == len(rows) == 3 * sum(len(r["checkpoints"]) - 1 for r in cyc.values())
    assert [s["predictor"] for s in summary] == list(PREDICTORS)
