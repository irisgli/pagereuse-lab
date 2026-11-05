"""Phase 11: indexes and TOAST under the same churn as the heap (1000 rows)."""

from __future__ import annotations


def cps(r, op):
    return [c for c in r["checkpoints"] if c["operation"]["op"] == op]


def index(c, suffix):
    return next(i for i in c["relation"]["indexes"] if i["name"].endswith(suffix))


def toast(c):
    return c["relation"]["toast"]


def test_total_size_covers_every_fork_index_and_toast(sto):
    for r in sto.values():
        for c in r["checkpoints"]:
            e = c["relation"]["exact"]
            parts = e["main_bytes"] + e["fsm_bytes"] + e["vm_bytes"]
            parts += sum(i["main_bytes"] for i in c["relation"]["indexes"])
            if toast(c):
                parts += toast(c)["main_bytes"] + toast(c)["index_bytes"]
            assert e["total_bytes"] >= parts
            assert e["total_bytes"] - parts < 64 * 8192  # only index FSM/VM forks are unaccounted


def test_inline_payload_has_no_toast_rows_and_external_does(sto):
    inline = sto["inline 200B, random deletes, vacuum"]
    ext = sto["toast 3000B, random deletes, vacuum"]
    assert toast(inline["checkpoints"][0])["pgstattuple_tuple_count"] == 0
    first = toast(ext["checkpoints"][0])
    assert first["pgstattuple_tuple_count"] == 2 * 1000  # 3000 bytes = two chunks
    assert first["main_pages"] == 500  # four chunks per page
    assert ext["checkpoints"][0]["relation"]["exact"]["main_pages"] < 10  # heap holds only pointers


def test_toast_space_is_reused_with_vacuum_and_not_without(sto):
    vac = sto["toast 3000B, random deletes, vacuum"]
    novac = sto["toast 3000B, random deletes, no vacuum"]
    assert {toast(c)["main_pages"] for c in vac["checkpoints"]} == {500}
    pages = [toast(c)["main_pages"] for c in cps(novac, "insert")]
    assert pages == [500, 750, 1000, 1250]
    assert toast(novac["checkpoints"][-1])["pgstattuple_dead_tuple_count"] == 3000


def test_vacuum_truncates_toast_tail_under_oldest_first_churn(sto):
    old = sto["toast 3000B, oldest deletes, vacuum"]
    toast_pages = [toast(c)["main_pages"] for c in cps(old, "vacuum")]
    heap_pages = [c["relation"]["exact"]["main_pages"] for c in cps(old, "vacuum")]
    assert min(toast_pages) < 500 and min(heap_pages) < 8
    assert toast(old["checkpoints"][-1])["main_pages"] == 500


def test_primary_key_index_grows_under_random_deletes_despite_vacuum(sto):
    """New ids always land at the right end of the B-tree; leaf pages half
    emptied by random deletes are not reused until completely empty."""
    rand = sto["inline 200B, random deletes, vacuum"]
    leaf = [index(c, "pkey")["leaf_pages"] for c in cps(rand, "insert")]
    assert leaf == sorted(leaf) and leaf[-1] > leaf[0]
    dens = [index(c, "pkey")["avg_leaf_density"] for c in cps(rand, "insert")]
    assert dens[-1] < dens[0] / 1.5
    assert rand["summary"]["peak_pages"] == rand["checkpoints"][0]["relation"]["exact"]["main_pages"]


def test_primary_key_index_recycles_pages_under_oldest_first_deletes(sto):
    old = sto["inline 200B, oldest deletes, vacuum"]
    leaf = [index(c, "pkey")["leaf_pages"] for c in cps(old, "insert")]
    assert max(leaf) <= leaf[0] + 1
    assert index(old["checkpoints"][-1], "pkey")["deleted_pages"] > 0


def test_cyclic_tag_index_does_not_grow(sto):
    for label in ("inline 200B, random deletes, vacuum", "inline 200B, oldest deletes, vacuum"):
        leaf = {index(c, "tag_idx")["leaf_pages"] for c in sto[label]["checkpoints"]}
        assert len(leaf) == 1
