"""Phase 5: the command line end to end, through main(), against the lab database."""

from __future__ import annotations

import csv
import json

import pytest

from pagereuse import db
from pagereuse.__main__ import main


def test_run_suite_evaluate_round_trip(tmp_path, capsys):
    out = tmp_path / "results"
    steps = tmp_path / "steps.json"
    steps.write_text(
        json.dumps(
            [
                {"op": "insert", "rows": 300},
                {"op": "delete", "pattern": "oldest", "fraction": 0.5},
                {"op": "vacuum"},
                {"op": "insert", "rows": 150},
            ]
        )
    )

    main(["run", "--table", "prl_test_cli", "--rows", "300", "--steps", str(steps), "--out", str(out)])
    summary = json.loads(capsys.readouterr().out)
    assert set(summary["main_pages"]) == {"00_insert", "01_delete", "02_vacuum", "03_insert"}
    run_dir = out / summary["run_dir"].split("/")[-1]
    assert (run_dir / "result.json").is_file() and (run_dir / "pages.csv").is_file()

    main(["suite", "cycles", "--rows", "300", "--out", str(out)])
    text = capsys.readouterr().out
    assert "cycles vacuum random" in text and "comparison:" in text
    comparison = next(out.glob("comparison_cycles_*.csv"))
    with open(comparison) as f:
        assert {r["label"] for r in csv.DictReader(f)} == {
            "cycles vacuum random",
            "cycles no-vacuum random",
            "cycles vacuum oldest",
            "cycles no-vacuum oldest",
        }

    main(["evaluate", "--results", str(out)])
    text = capsys.readouterr().out
    assert "capacity" in text and (out / "model_eval.csv").is_file()


def test_evaluate_with_no_results_exits(tmp_path):
    with pytest.raises(SystemExit):
        main(["evaluate", "--results", str(tmp_path)])


@pytest.mark.parametrize("argv", [["run", "--table", "users"], ["run", "--fillfactor", "5"]])
def test_run_rejects_unsafe_or_invalid_config(argv, tmp_path):
    with pytest.raises((db.SafetyError, ValueError, SystemExit)):
        main([*argv, "--rows", "10", "--out", str(tmp_path)])
