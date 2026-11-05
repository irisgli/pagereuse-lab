"""Phase 6: a long-running snapshot and concurrent inserters, measured for real."""

from __future__ import annotations

from pagereuse.model import evaluate_run

ROWS = 2000


def cp(r, op, nth=0):
    return [c for c in r["checkpoints"] if c["operation"]["op"] == op][nth]


def pages(c):
    return c["relation"]["exact"]["main_pages"]


def test_held_snapshot_blocks_vacuum_and_forces_growth(snap):
    held = snap["churn under held snapshot"]
    first_vacuum = cp(held, "vacuum", 0)
    assert first_vacuum["operation"]["snapshot_held"] is True
    # The server's own accounting: every deleted row is dead but not yet removable.
    assert first_vacuum["operation"]["dead_not_yet_removable"] == ROWS // 2
    assert first_vacuum["relation"]["exact"]["pgstattuple_dead_tuple_count"] == ROWS // 2
    # Reinserting the same number of rows then has to extend the relation.
    reinsert = cp(held, "insert", 1)
    assert pages(reinsert) > pages(cp(held, "insert", 0))
    assert reinsert["relation"]["exact"]["pgstattuple_dead_tuple_count"] == ROWS // 2


def test_released_snapshot_lets_vacuum_reclaim(snap):
    held = snap["churn under held snapshot"]
    second_vacuum = cp(held, "vacuum", 1)
    assert second_vacuum["operation"]["snapshot_held"] is False
    assert second_vacuum["operation"]["dead_not_yet_removable"] == 0
    assert second_vacuum["relation"]["exact"]["pgstattuple_dead_tuple_count"] == 0
    # The reclaimed space absorbs the next reinsert: no further growth.
    assert pages(cp(held, "insert", 2)) == pages(second_vacuum)


def test_same_churn_without_snapshot_does_not_grow(snap):
    none = snap["churn, no snapshot"]
    assert none["summary"]["peak_pages"] == pages(none["checkpoints"][0])
    assert cp(none, "vacuum")["operation"]["dead_not_yet_removable"] == 0


def test_capacity_model_handles_held_snapshot(snap):
    rows = [r for r in evaluate_run(snap["churn under held snapshot"]) if r["predictor"] == "capacity"]
    assert all(r["error"] == 0 for r in rows), [r for r in rows if r["error"]]


def test_concurrent_sessions_insert_every_row(conc):
    one, four = conc["load with 1 session"], conc["load with 4 sessions"]
    for r in (one, four):
        assert cp(r, "insert", 0)["relation"]["exact"]["pgstattuple_tuple_count"] == ROWS
        assert cp(r, "insert", 1)["relation"]["exact"]["pgstattuple_tuple_count"] == ROWS
    assert cp(four, "insert", 0)["operation"]["sessions"] == 4
    # Four inserters each own a target page, so the load is never tighter than one.
    assert pages(cp(four, "insert", 0)) >= pages(cp(one, "insert", 0))
