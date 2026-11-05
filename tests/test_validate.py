"""Phase 10: predictors scored on random workloads they were never tuned on."""

from __future__ import annotations

import csv

from pagereuse.validate import random_config, validate


def test_random_config_is_deterministic_and_valid():
    a, b = random_config(7), random_config(7)
    assert a == b
    a.validate()
    ops = [s["op"] for s in a.resolved_steps()]
    assert ops[0] == "insert" and len(ops) >= 6
    assert ops.count("hold_snapshot") == ops.count("release_snapshot")
    assert {random_config(s).payload_bytes for s in range(40)} > {200}


def test_validation_batch_scores_capacity_ahead_of_baselines(tmp_path):
    report = validate([9001, 9002, 9003], tmp_path, rows=800)
    assert len(report["results"]) == 3
    with open(tmp_path / "validation.csv") as f:
        assert len(list(csv.DictReader(f))) == len(report["rows"])

    def mae(name):
        errs = [abs(r["error"]) for r in report["rows"] if r["predictor"] == name]
        return sum(errs) / len(errs)

    assert mae("capacity") < mae("last_value")
    assert mae("capacity") < mae("linear_trend")
    for row in report["by_operation"]:
        if row["predictor"] == "capacity" and row["op"] in (
            "delete",
            "vacuum",
            "hold_snapshot",
            "release_snapshot",
        ):
            assert row["max_abs_error"] == 0, row
        if row["predictor"] == "capacity" and row["op"] == "insert":
            assert row["max_abs_error"] <= 1, row
