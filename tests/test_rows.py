"""Phase 13: row identity. Each checkpoint records where every live row's
visible version sits and each update step records the ids it targets, so the
``capacity_rows`` predictor can follow rows after they relocate. The
assertions pin what the row-blind ``capacity`` model gets wrong, what the
row-aware one gets right, and the invariants that keep it honest: no data
from the step being predicted, no observer effect from the extra scan, old
results still scored."""

from __future__ import annotations

import copy

import pytest

from pagereuse import db
from pagereuse.experiment import ExperimentConfig, run
from pagereuse.model import PREDICTORS, _tuple_len, evaluate_run, predict_capacity, predict_capacity_rows

ROWS = 1000


def rows_for(result, predictor, op=None):
    return [r for r in evaluate_run(result) if r["predictor"] == predictor and (op is None or r["op"] == op)]


def live(cp):
    return dict(zip(cp["live_rows"]["id"], cp["live_rows"]["blkno"], strict=True))


@pytest.fixture(scope="module")
def held():
    """Repeated payload updates with a snapshot pinning the first round's dead
    versions, then release, VACUUM and one more round."""
    return run(
        ExperimentConfig(
            table="prl_rows_held",
            rows=ROWS,
            fillfactor=90,
            seed=11,
            steps=[
                {"op": "insert", "rows": ROWS},
                {"op": "update", "pattern": "random", "fraction": 0.5},
                {"op": "hold_snapshot"},
                {"op": "update", "pattern": "random", "fraction": 0.5},
                {"op": "update", "pattern": "stride", "every": 3},
                {"op": "release_snapshot"},
                {"op": "update", "pattern": "stride", "every": 3},
                {"op": "vacuum"},
                {"op": "update", "pattern": "oldest", "fraction": 0.3, "grow_bytes": 100},
                {"op": "insert", "rows": ROWS // 2},
            ],
        )
    )


@pytest.fixture(scope="module")
def mixed():
    """Payload sizes straddling the 1-byte/4-byte varlena header boundary, an
    indexed-column update reached by a bitmap scan and one by an index scan."""
    return run(
        ExperimentConfig(
            table="prl_rows_mixed",
            rows=ROWS,
            payload_bytes=64,
            seed=12,
            steps=[
                {"op": "insert", "rows": ROWS},
                {"op": "update", "pattern": "stride", "every": 4, "grow_bytes": 100},
                {"op": "update", "pattern": "stride", "every": 4, "grow_bytes": 40},
                {"op": "update", "pattern": "random", "fraction": 0.1, "column": "tag"},
                {"op": "update", "pattern": "stride", "every": 2, "column": "tag"},
                {"op": "delete", "pattern": "stride", "every": 3},
                {"op": "update", "pattern": "stride", "every": 2},
            ],
        )
    )


def test_live_rows_cover_exactly_the_live_set(ff):
    for res in ff.values():
        for cp in res["checkpoints"]:
            ids = cp["live_rows"]["id"]
            assert len(ids) == len(set(ids)) == cp["relation"]["exact"]["pgstattuple_tuple_count"]
            assert len(cp["live_rows"]["blkno"]) == len(cp["live_rows"]["lp_len"]) == len(ids)
            assert max(cp["live_rows"]["blkno"]) < len(cp["pages"])


def test_relocated_rows_are_tracked_and_the_rest_stay(ff):
    """At fillfactor 100 no page has room, so round 1 moves every updated row
    to a new page; the untouched rows keep their block."""
    r = ff["fillfactor=100"]
    before, after = live(r["checkpoints"][0]), live(r["checkpoints"][1])
    targets = set(r["checkpoints"][1]["operation"]["ids"])
    assert all(after[i] != before[i] for i in targets)
    assert all(after[i] == before[i] for i in before if i not in targets)
    assert min(after[i] for i in targets) >= len(r["checkpoints"][0]["pages"]) - 1  # last page or new


def test_row_scan_does_not_prune(ff, held):
    """The TID scan that locates rows must not change a page; a sequential scan would."""
    for res in list(ff.values()) + [held]:
        for cp in res["checkpoints"]:
            assert cp["observer_effect"]["pages_changed_by_pgstattuple"] == [], cp["name"]


def test_rows_model_exact_where_proportional_model_fails(ff, upd):
    """Round 2 lands on the pages round 1 moved the rows to. ``capacity``
    credits pruned space on the pages they left and predicts too few pages;
    ``capacity_rows`` knows where they are."""
    for res in list(ff.values()) + list(upd.values()):
        old = [r for r in rows_for(res, "capacity", "update") if r["step"] == 2]
        new = [r for r in rows_for(res, "capacity_rows", "update") if r["step"] == 2]
        assert old[0]["error"] < 0 and new[0]["error"] == 0, res["config"]["label"]


def test_rows_model_exact_for_hot_and_non_hot_updates(upd):
    hot, cold = upd["update payload (HOT-eligible)"], upd["update indexed tag (never HOT)"]
    assert hot["summary"]["hot_updates"] > 0 and cold["summary"]["hot_updates"] == 0
    for res in (hot, cold):
        assert all(r["error"] == 0 for r in rows_for(res, "capacity_rows")), res["config"]["label"]


def test_rows_model_exact_for_growing_tuples(upd):
    grow = upd["update payload +100 bytes"]
    lens = grow["checkpoints"][-1]["live_rows"]["lp_len"]
    assert len(set(lens)) > 1  # grown and untouched rows differ in size
    assert all(r["error"] == 0 for r in rows_for(grow, "capacity_rows"))


def test_rows_model_under_snapshot_prune_vacuum(held):
    cps = held["checkpoints"]
    while_held = next(c for c in cps if c["name"] == "04_update")
    after_release = next(c for c in cps if c["name"] == "05_release_snapshot")
    assert sum(p["dead_removable_bytes"] for p in while_held["pages"]) == 0
    assert sum(p["dead_removable_bytes"] for p in after_release["pages"]) > 0
    assert all(r["error"] == 0 for r in rows_for(held, "capacity_rows")), rows_for(held, "capacity_rows")


def test_rows_model_across_header_boundary_and_scan_types(mixed):
    scans = [c["operation"].get("scan") for c in mixed["checkpoints"] if c["operation"]["op"] == "update"]
    assert "bitmap" in scans and scans.count("index") >= 3
    assert all(r["error"] == 0 for r in rows_for(mixed, "capacity_rows")), rows_for(mixed, "capacity_rows")


def test_fsm_root_lags_leaf_values_until_vacuum(mixed, held):
    """Inserters record leaf values; only VACUUM writes the upper levels, so a
    search from the root cannot see space recorded since the last VACUUM.
    ``mixed`` never vacuums: its root stays 0 while leaves hold values."""
    for cp in mixed["checkpoints"]:
        fsm = cp["relation"]["fsm"]
        assert fsm["fsm_root_avail_bytes"] == 0 and fsm["fsm_total_avail_bytes"] > 0, cp["name"]
    after_vacuum = next(c for c in held["checkpoints"] if c["operation"]["op"] == "vacuum")
    assert after_vacuum["relation"]["fsm"]["fsm_root_avail_bytes"] == max(
        p["fsm_avail_bytes"] for p in after_vacuum["pages"]
    )


def test_tuple_len_follows_the_varlena_header_rule(mixed):
    """A 64-byte payload has a 1-byte varlena header; past 126 bytes it needs 4.
    Check the rule against lengths PostgreSQL actually stored."""
    first = set(mixed["checkpoints"][0]["live_rows"]["lp_len"])
    grown = set(mixed["checkpoints"][1]["live_rows"]["lp_len"]) - first
    regrown = set(mixed["checkpoints"][2]["live_rows"]["lp_len"]) - first - grown
    assert first == {24 + 8 + 1 + 64} and grown == {24 + 8 + 4 + 164}
    assert regrown == {24 + 8 + 4 + 204}
    assert _tuple_len(97, 100) == 200 and _tuple_len(97, 40) == 137 and _tuple_len(200, 40) == 240


def test_deterministic_replay(held):
    again = run(ExperimentConfig(**{**held["config"], "label": "replay"}))
    for a, b in zip(held["checkpoints"], again["checkpoints"], strict=True):
        assert a["live_rows"] == b["live_rows"] and a["pages"] == b["pages"], a["name"]
        assert a["operation"].get("ids") == b["operation"].get("ids")
    assert [r["predicted"] for r in rows_for(held, "capacity_rows")] == [
        r["predicted"] for r in rows_for(again, "capacity_rows")
    ]


def test_no_future_data(held):
    """Every update's targets come from the previous checkpoint's live set, and
    the prediction for a step cannot change when that step's own measurement does."""
    cps = held["checkpoints"]
    env, cfg = dict(held["environment"]), held["config"]
    env["insert_footprint"] = 244
    for i in range(1, len(cps)):
        op = cps[i]["operation"]
        if op["op"] == "update":
            assert set(op["ids"]) <= set(cps[i - 1]["live_rows"]["id"])
        prev = copy.deepcopy(cps[i - 1])
        before = predict_capacity_rows([], prev, op, env, cfg)
        assert prev == cps[i - 1]  # the predictor does not touch its inputs
        tampered = copy.deepcopy(held)
        actual = tampered["checkpoints"][i]
        actual["relation"]["exact"]["main_pages"] += 1000
        actual["live_rows"]["blkno"] = [0] * len(actual["live_rows"]["blkno"])
        for p in actual["pages"]:
            p["exact_free_bytes"] = p["fsm_avail_bytes"] = 0
        scored = evaluate_run(tampered)
        after = next(r for r in scored if r["predictor"] == "capacity_rows" and r["step"] == i)
        assert after["predicted"] == before


def test_old_results_without_row_data_still_score(held):
    """Schema 2 results carry no live_rows, ids, page_full or dead_removable_bytes."""
    old = copy.deepcopy(held)
    for cp in old["checkpoints"]:
        del cp["live_rows"]
        cp["operation"].pop("ids", None)
        cp["operation"].pop("scan", None)
        del cp["relation"]["fsm"]["fsm_root_avail_bytes"]
        for p in cp["pages"]:
            del p["page_full"], p["dead_removable_bytes"]
    scored = evaluate_run(old)
    assert {r["predictor"] for r in scored} == set(PREDICTORS)
    for r in scored:
        if r["predictor"] == "capacity_rows" and r["op"] != "insert":
            twin = next(x for x in scored if x["predictor"] == "capacity" and x["step"] == r["step"])
            assert r["predicted"] == twin["predicted"]
    env, cfg = dict(old["environment"]), old["config"]
    cps = old["checkpoints"]
    assert predict_capacity_rows([], cps[0], cps[1]["operation"], env, cfg) == predict_capacity(
        [], cps[0], cps[1]["operation"], env, cfg
    )


def test_live_rows_rejects_non_lab_tables():
    from pagereuse.metrics import live_rows

    with db.connect() as conn, pytest.raises(db.SafetyError):
        live_rows(conn, "pg_class")
