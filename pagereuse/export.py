"""Write results as JSON (full record), per-page CSV, and a cross-run comparison CSV."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .metrics import PAGE_COLUMNS

COMPARISON_COLUMNS = (
    "run_id",
    "label",
    "table",
    "fillfactor",
    "step",
    "checkpoint",
    "op",
    "op_rows",
    "main_pages",
    "live_tuples",
    "dead_tuples",
    "exact_free_bytes",
    "fsm_avail_bytes",
    "pages_all_visible",
    "lp_redirect",
    "lp_dead",
    "lp_unused",
    "n_tup_upd",
    "n_tup_hot_upd",
    "pages_changed_by_pgstattuple",
)


def comparison_rows(result: dict) -> list[dict]:
    cfg = result["config"]
    rows = []
    for c in result["checkpoints"]:
        e, est = c["relation"]["exact"], c["relation"]["estimated"]
        rows.append(
            {
                "run_id": result["run_id"],
                "label": cfg["label"],
                "table": cfg["table"],
                "fillfactor": cfg["fillfactor"],
                "step": c["step"],
                "checkpoint": c["name"],
                "op": c["operation"]["op"],
                "op_rows": c["operation"].get("rows", ""),
                "main_pages": e["main_pages"],
                "live_tuples": e["pgstattuple_tuple_count"],
                "dead_tuples": e["pgstattuple_dead_tuple_count"],
                "exact_free_bytes": e["page_free_bytes_total"],
                "fsm_avail_bytes": c["relation"]["fsm"]["fsm_total_avail_bytes"],
                "pages_all_visible": e["pages_all_visible"],
                "lp_redirect": e["lp_redirect_total"],
                "lp_dead": e["lp_dead_total"],
                "lp_unused": e["lp_unused_total"],
                "n_tup_upd": est["n_tup_upd"],
                "n_tup_hot_upd": est["n_tup_hot_upd"],
                "pages_changed_by_pgstattuple": len(c["observer_effect"]["pages_changed_by_pgstattuple"]),
            }
        )
    return rows


def write_result(result: dict, out_dir: Path) -> Path:
    run_dir = Path(out_dir) / result["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "result.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
    with open(run_dir / "pages.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["run_id", "checkpoint", *PAGE_COLUMNS])
        for cp in result["checkpoints"]:
            for page in cp["pages"]:
                w.writerow([result["run_id"], cp["name"], *(page[c] for c in PAGE_COLUMNS)])
    return run_dir


def write_comparison(results: list[dict], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COMPARISON_COLUMNS)
        w.writeheader()
        for r in results:
            w.writerows(comparison_rows(r))
    return path
