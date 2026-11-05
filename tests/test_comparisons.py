"""Phase 2: controlled comparisons. Each suite varies one factor; the
assertions below encode the direction PostgreSQL's documented behaviour
predicts, checked against real measurements at 2000 rows."""

from __future__ import annotations

import csv

import pytest

from pagereuse.experiment import ExperimentConfig, _expand, run
from pagereuse.export import COMPARISON_COLUMNS, write_comparison

ROWS = 2000


def pages(r, i):
    return r["checkpoints"][i]["relation"]["exact"]["main_pages"]


def total(r, i, key):
    return r["checkpoints"][i]["relation"]["exact"][key]


def test_repeat_expands_in_order():
    steps = _expand([{"op": "insert", "rows": 1},
                     {"op": "repeat", "times": 2, "steps": [{"op": "vacuum"}, {"op": "insert", "rows": 2}]}])
    assert [s["op"] for s in steps] == ["insert", "vacuum", "insert", "vacuum", "insert"]


def test_step_validation():
    with pytest.raises(ValueError):
        ExperimentConfig(steps=[{"op": "truncate"}]).validate()
    with pytest.raises(ValueError):
        ExperimentConfig(steps=[{"op": "update", "column": "id"}]).validate()


def test_lower_fillfactor_costs_pages_at_load_but_enables_hot(ff):
    f100, f90, f70 = (ff[f"fillfactor={x}"] for x in (100, 90, 70))
    # Reserved space makes the freshly loaded table larger...
    assert pages(f100, 0) < pages(f90, 0) < pages(f70, 0)
    # ...but same-page (HOT) updates become possible, and the table ends smaller.
    assert f100["summary"]["hot_updates"] < f90["summary"]["hot_updates"] < f70["summary"]["hot_updates"]
    assert f70["summary"]["final_pages"] < f90["summary"]["final_pages"] < f100["summary"]["final_pages"]


def test_fillfactor_100_first_update_is_never_hot(ff):
    f100 = ff["fillfactor=100"]
    first_update = f100["checkpoints"][1]
    assert first_update["relation"]["estimated"]["n_tup_hot_upd"] == 0
    assert total(f100, 1, "lp_redirect_total") == 0


def test_hot_updates_leave_redirect_line_pointers_after_prune(ff):
    f70 = ff["fillfactor=70"]
    # Second round of updates prunes the first round's dead versions in place.
    assert total(f70, 2, "lp_redirect_total") > 0
    assert total(f70, 2, "lp_redirect_total") == f70["checkpoints"][1]["relation"]["estimated"]["n_tup_hot_upd"]


def test_indexed_column_update_is_never_hot(upd):
    tag = upd["update indexed tag (never HOT)"]
    for c in tag["checkpoints"]:
        assert c["relation"]["estimated"]["n_tup_hot_upd"] == 0
        assert c["relation"]["exact"]["lp_redirect_total"] == 0
    payload = upd["update payload (HOT-eligible)"]
    assert payload["summary"]["hot_updates"] > 0
    assert payload["summary"]["updates"] == tag["summary"]["updates"] == 3 * ROWS // 2


def test_growing_tuples_need_more_pages(upd):
    same = upd["update payload (HOT-eligible)"]
    grow = upd["update payload +100 bytes"]
    assert grow["summary"]["final_pages"] > same["summary"]["final_pages"]
    assert grow["summary"]["hot_updates"] < same["summary"]["hot_updates"]


def test_vacuum_after_each_cycle_holds_size_constant(cyc):
    for label in ("cycles vacuum random", "cycles vacuum oldest"):
        r = cyc[label]
        live = [c["relation"]["exact"]["pgstattuple_tuple_count"] for c in r["checkpoints"]
                if c["operation"]["op"] == "insert"]
        assert set(live) == {ROWS}, label
        assert r["summary"]["peak_pages"] == pages(r, 0), label


def test_without_vacuum_each_cycle_appends_pages(cyc):
    for label in ("cycles no-vacuum random", "cycles no-vacuum oldest"):
        r = cyc[label]
        inserts = [pages(r, i) for i, c in enumerate(r["checkpoints"]) if c["operation"]["op"] == "insert"]
        assert inserts == sorted(inserts) and inserts[-1] > inserts[0], label
        assert r["checkpoints"][-1]["relation"]["exact"]["pgstattuple_dead_tuple_count"] > 0


def test_vacuum_sets_all_visible_and_updates_clear_it(ff):
    f90 = ff["fillfactor=90"]
    assert total(f90, 3, "pages_all_visible") == pages(f90, 3)
    assert total(f90, 4, "pages_all_visible") < pages(f90, 4)


def test_comparison_csv(cyc, tmp_path):
    path = write_comparison(list(cyc.values()), tmp_path / "cmp.csv")
    with open(path) as f:
        rows = list(csv.DictReader(f))
    assert tuple(rows[0]) == COMPARISON_COLUMNS
    assert len(rows) == sum(len(r["checkpoints"]) for r in cyc.values())
    assert {r["label"] for r in rows} == set(cyc)


def test_vacuum_truncates_empty_tail_only_for_oldest_first_churn(cyc):
    """Oldest-first churn: cycle 1 frees the head pages, reinserts refill them,
    so cycle 2's oldest rows sit in the tail and VACUUM truncates it. Random
    churn never empties the tail, so VACUUM never shrinks the file."""
    oldest = cyc["cycles vacuum oldest"]
    vac_pages = [pages(oldest, i) for i, c in enumerate(oldest["checkpoints"]) if c["operation"]["op"] == "vacuum"]
    assert min(vac_pages) < pages(oldest, 0)
    assert oldest["summary"]["final_pages"] == pages(oldest, 0)
    rand = cyc["cycles vacuum random"]
    assert {pages(rand, i) for i in range(len(rand["checkpoints"]))} == {pages(rand, 0)}
