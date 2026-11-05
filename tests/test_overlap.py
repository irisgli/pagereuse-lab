"""Phase 8: two snapshots with different lifetimes. VACUUM reclaims exactly the
versions that died before the oldest snapshot still held, and the per-page
``dead_removable`` count predicts that before VACUUM runs."""

from __future__ import annotations

from pagereuse.model import evaluate_run

ROWS = 2000
QUARTER = ROWS // 4


def steps(r, op):
    return [c for c in r["checkpoints"] if c["operation"]["op"] == op]


def removable(c):
    return sum(p["dead_removable"] for p in c["pages"])


def xmax_set(c):
    return sum(p["normal_xmax_set"] for p in c["pages"])


def pages(c):
    return c["relation"]["exact"]["main_pages"]


def test_vacuum_reclaims_only_versions_older_than_the_oldest_snapshot(ovl):
    two = ovl["staggered churn, two snapshots"]
    v1, v2, v3 = steps(two, "vacuum")
    assert v1["operation"]["snapshots_held"] == ["a", "b"]
    assert v1["operation"]["dead_not_yet_removable"] == 2 * QUARTER
    assert v2["operation"]["snapshots_held"] == ["b"]
    assert v2["operation"]["dead_not_yet_removable"] == QUARTER  # deleted after b was taken
    assert v3["operation"]["snapshots_held"] == []
    assert v3["operation"]["dead_not_yet_removable"] == 0
    assert v3["relation"]["exact"]["pgstattuple_dead_tuple_count"] == 0


def test_page_records_predict_removability_before_vacuum(ovl):
    two = ovl["staggered churn, two snapshots"]
    rel_a, rel_b = steps(two, "release_snapshot")
    # Right after releasing a: the first delete's rows are now older than the horizon.
    assert removable(rel_a) == QUARTER and xmax_set(rel_a) == 2 * QUARTER
    # Right after releasing b: the remaining dead rows become removable.
    assert removable(rel_b) == QUARTER and xmax_set(rel_b) == QUARTER
    # While both were held nothing was removable.
    assert removable(steps(two, "vacuum")[0]) == 0


def test_growth_happens_only_while_snapshots_block_reuse(ovl):
    two = ovl["staggered churn, two snapshots"]
    none = ovl["staggered churn, no snapshots"]
    first = pages(two["checkpoints"][0])
    i1, i2 = steps(two, "insert")[1:]
    assert pages(i1) > first  # both snapshots held: the reinsert had to extend
    assert pages(i2) == pages(i1)  # a released and vacuumed: the next reinsert fit
    assert none["summary"]["peak_pages"] == pages(none["checkpoints"][0])
    assert two["summary"]["final_live_tuples"] == none["summary"]["final_live_tuples"] == ROWS


def test_capacity_model_exact_under_overlapping_snapshots(ovl):
    for r in ovl.values():
        rows = [x for x in evaluate_run(r) if x["predictor"] == "capacity"]
        assert all(x["error"] == 0 for x in rows), [x for x in rows if x["error"]]
