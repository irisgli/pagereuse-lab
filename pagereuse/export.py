"""Write experiment results as JSON (full record) and CSV (one row per page per checkpoint)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .metrics import PAGE_COLUMNS


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
