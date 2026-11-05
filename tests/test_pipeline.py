"""Integration tests: run the real experiment against the lab PostgreSQL instance.

Requires `docker compose up -d --wait` (or PAGEREUSE_DSN pointing at an
equivalent PostgreSQL 16 database named pagereuse_lab).
"""

from __future__ import annotations

import csv
import json

import pytest

from pagereuse import db
from pagereuse.experiment import ExperimentConfig, run
from pagereuse.export import write_result

ROWS = 2000
FSM_CATEGORY = 32  # BLCKSZ / 256 for the default 8 kB block size
LINE_POINTER = 4  # sizeof(ItemIdData)


@pytest.fixture(scope="module")
def vac():
    return run(ExperimentConfig(table="prl_test_vac", rows=ROWS, seed=7))


@pytest.fixture(scope="module")
def novac():
    return run(ExperimentConfig(table="prl_test_novac", rows=ROWS, seed=7, vacuum=False))


def cp(result, name):
    return next(c for c in result["checkpoints"] if c["name"] == name)


def exact(result, name, key):
    return cp(result, name)["relation"]["exact"][key]


def test_checkpoints_present(vac, novac):
    assert [c["name"] for c in vac["checkpoints"]] == [
        "00_insert", "01_delete", "02_vacuum", "03_insert"]
    assert [c["name"] for c in novac["checkpoints"]] == [
        "00_insert", "01_delete", "02_insert"]
    assert vac["environment"]["server_version"].startswith("16")
    assert vac["environment"]["block_size"] == 8192


def test_exact_tuple_counts_match_workload(vac):
    deleted = cp(vac, "01_delete")["operation"]["rows"]
    assert deleted == ROWS // 2
    assert exact(vac, "00_insert", "pgstattuple_tuple_count") == ROWS
    assert exact(vac, "01_delete", "pgstattuple_tuple_count") == ROWS - deleted
    assert exact(vac, "03_insert", "pgstattuple_tuple_count") == ROWS


def test_page_count_matches_relation_size(vac):
    for c in vac["checkpoints"]:
        assert len(c["pages"]) == c["relation"]["exact"]["main_pages"]
        assert c["relation"]["exact"]["main_bytes"] == len(c["pages"]) * 8192


def test_delete_does_not_shrink_relation(vac):
    assert exact(vac, "01_delete", "main_pages") == exact(vac, "00_insert", "main_pages")


def test_delete_marks_tuples_without_freeing_space(vac):
    load, dele = cp(vac, "00_insert"), cp(vac, "01_delete")
    deleted = dele["operation"]["rows"]
    assert sum(p["normal_xmax_set"] for p in dele["pages"]) == deleted
    assert dele["relation"]["exact"]["pgstattuple_dead_tuple_count"] == deleted
    assert [p["exact_free_bytes"] for p in dele["pages"]] == [
        p["exact_free_bytes"] for p in load["pages"]]


def test_fsm_after_load_records_pages_the_inserter_left(vac):
    """When the insertion target page cannot fit the next tuple, the inserter
    records that page's remaining space in the FSM before moving on. The
    current target (last) page has no entry yet."""
    pages = cp(vac, "00_insert")["pages"]
    *left, target = pages
    assert target["fsm_avail_bytes"] == 0
    for p in left:
        assert 0 <= p["exact_free_bytes"] - p["fsm_avail_bytes"] < LINE_POINTER + FSM_CATEGORY, p


def test_fsm_can_overstate_after_inserts(vac):
    """After VACUUM records free space, later inserts into the last page do not
    update its FSM entry, so the FSM reports more space than the page has."""
    pages = cp(vac, "03_insert")["pages"]
    assert any(p["fsm_avail_bytes"] > p["exact_free_bytes"] for p in pages)


def test_vacuum_removes_dead_tuples(vac):
    c = cp(vac, "02_vacuum")
    assert c["relation"]["exact"]["pgstattuple_dead_tuple_count"] == 0
    assert all(p["normal_xmax_set"] == 0 for p in c["pages"])
    assert c["observer_effect"]["pages_changed_by_pgstattuple"] == []


def test_fsm_is_rounded_down_lower_bound_after_vacuum(vac):
    """VACUUM records PageGetHeapFreeSpace (free bytes minus one line pointer),
    rounded down to a 32-byte category. So, for every page it just processed,
    exact_free - fsm must lie in [4, 4 + 32)."""
    pages = cp(vac, "02_vacuum")["pages"]
    assert pages
    for p in pages:
        gap = p["exact_free_bytes"] - p["fsm_avail_bytes"]
        assert LINE_POINTER <= gap < LINE_POINTER + FSM_CATEGORY, p
        assert p["fsm_avail_bytes"] % FSM_CATEGORY == 0


def test_vacuum_enables_reuse_relative_to_no_vacuum(vac, novac):
    assert novac["summary"]["growth_vs_first_pages"] > 0
    assert vac["summary"]["growth_vs_first_pages"] < novac["summary"]["growth_vs_first_pages"]


def test_same_seed_gives_identical_physical_layout(vac):
    again = run(ExperimentConfig(table="prl_test_vac", rows=ROWS, seed=7))
    for a, b in zip(vac["checkpoints"], again["checkpoints"]):
        assert a["pages"] == b["pages"], a["name"]


def test_export_writes_json_and_csv(vac, tmp_path):
    run_dir = write_result(vac, tmp_path)
    loaded = json.loads((run_dir / "result.json").read_text())
    assert loaded["run_id"] == vac["run_id"]
    with open(run_dir / "pages.csv") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == sum(len(c["pages"]) for c in vac["checkpoints"])
    assert {r["checkpoint"] for r in rows} == {c["name"] for c in vac["checkpoints"]}


@pytest.mark.parametrize("name", ["users", "public.prl_x", "prl_X", "prl_; DROP TABLE t", "pg_class"])
def test_safety_guard_rejects_non_lab_tables(name):
    with pytest.raises(db.SafetyError):
        ExperimentConfig(table=name).validate()
