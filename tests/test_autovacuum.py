"""Phase 9: autovacuum under its documented trigger rule, with naptime at 1 s.

Load 2000 rows, delete 10%, reinsert 10%, delete 40% of the rest, reinsert.
Default thresholds: 50 + 0.2 * reltuples dead tuples; insert threshold
1000 + 0.2 * reltuples, where a never-vacuumed table counts as 0 rows."""

from __future__ import annotations

from pagereuse.model import evaluate_run

ROWS = 2000


def waits(r):
    return [c for c in r["checkpoints"] if c["operation"]["op"] == "wait_autovacuum"]


def pages(c):
    return c["relation"]["exact"]["main_pages"]


def est(c, key):
    return c["relation"]["estimated"][key]


def test_load_triggers_insert_autovacuum_on_never_vacuumed_table(av):
    for r in av.values():
        first = waits(r)[0]
        # It may finish before or after the load's own checkpoint; the count is what counts.
        assert est(first, "autovacuum_count") == 1
        assert est(first, "n_ins_since_vacuum") == 0
        assert est(first, "reltuples") == ROWS  # the vacuum set it; was -1 before
        assert est(r["checkpoints"][0], "reltuples") == -1


def test_small_delete_under_default_threshold_leads_to_growth(av):
    r = av["autovacuum, default thresholds"]
    second = waits(r)[1]
    assert est(second, "autovacuum_count") == 1
    assert est(second, "n_dead_tup") == ROWS // 10  # 200 < 50 + 0.2 * 2000
    inserts = [c for c in r["checkpoints"] if c["operation"]["op"] == "insert"]
    assert pages(inserts[1]) > pages(inserts[0])
    assert inserts[1]["relation"]["exact"]["pgstattuple_dead_tuple_count"] == ROWS // 10


def test_large_delete_crosses_threshold_and_space_is_reused(av):
    r = av["autovacuum, default thresholds"]
    third = waits(r)[2]
    assert est(third, "autovacuum_count") == 2
    assert third["relation"]["exact"]["pgstattuple_dead_tuple_count"] == 0
    inserts = [c for c in r["checkpoints"] if c["operation"]["op"] == "insert"]
    assert pages(inserts[2]) == pages(inserts[1])


def test_aggressive_scale_factor_vacuums_the_small_delete(av):
    r = av["autovacuum, scale factor 0.02"]
    second = waits(r)[1]
    assert est(second, "autovacuum_count") == 2  # 200 > 50 + 0.02 * 2000
    assert est(waits(r)[2], "autovacuum_count") == 3
    inserts = [c for c in r["checkpoints"] if c["operation"]["op"] == "insert"]
    assert pages(inserts[1]) == pages(inserts[0])
    assert r["summary"]["peak_pages"] == pages(inserts[0])


def test_capacity_model_predicts_autovacuum_decisions(av):
    for r in av.values():
        rows = [x for x in evaluate_run(r) if x["predictor"] == "capacity"]
        assert all(x["error"] == 0 for x in rows), [x for x in rows if x["error"]]
