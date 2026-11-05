"""Out-of-sample validation: random workloads the predictors were never tuned on.

``random_config`` draws a workload from a small grammar (row count, payload
size, fillfactor, a mix of deletes, updates, inserts, VACUUMs and snapshot
holds). ``validate`` runs a batch, scores every predictor one step ahead and
breaks the error down by operation, so a weakness on one kind of step is
reported as such instead of being averaged away.
"""

from __future__ import annotations

import csv
import random
from collections import defaultdict
from pathlib import Path

from .experiment import ExperimentConfig, run
from .export import write_result
from .model import PREDICTORS, evaluate_run

PAYLOADS = (64, 120, 200, 400, 700)
FILLFACTORS = (100, 90, 80, 70)
PATTERNS = ("stride", "random", "oldest")


def random_config(seed: int, rows: int | None = None) -> ExperimentConfig:
    rng = random.Random(seed)
    rows = rows or rng.choice((1000, 1500, 2000, 3000))
    steps: list[dict] = [{"op": "insert", "rows": rows}]
    held = False
    for _ in range(rng.randint(5, 8)):
        kind = rng.choices(("delete", "update", "insert", "vacuum", "snapshot"), (3, 3, 2, 3, 1))[0]
        if kind == "delete":
            steps.append(
                {
                    "op": "delete",
                    "pattern": rng.choice(PATTERNS),
                    "every": rng.choice((2, 3, 4)),
                    "fraction": round(rng.uniform(0.1, 0.5), 2),
                }
            )
        elif kind == "update":
            steps.append(
                {
                    "op": "update",
                    "pattern": rng.choice(PATTERNS),
                    "every": rng.choice((2, 3, 4)),
                    "fraction": round(rng.uniform(0.1, 0.5), 2),
                    "column": rng.choice(("payload", "payload", "tag")),
                    "grow_bytes": rng.choice((0, 0, 40, 100)),
                }
            )
        elif kind == "insert":
            steps.append({"op": "insert", "rows": rng.choice((rows // 4, rows // 2, rows))})
        elif kind == "vacuum":
            steps.append({"op": "vacuum"})
        elif held:
            steps.append({"op": "release_snapshot"})
            held = False
        else:
            steps.append({"op": "hold_snapshot"})
            held = True
    if held:
        steps.append({"op": "release_snapshot"})
    return ExperimentConfig(
        table=f"prl_val_{seed}",
        rows=rows,
        payload_bytes=rng.choice(PAYLOADS),
        fillfactor=rng.choice(FILLFACTORS),
        seed=seed,
        label=f"random workload {seed}",
        steps=steps,
    )


def by_operation(rows: list[dict]) -> list[dict]:
    """MAE, max error and exact rate per (predictor, op)."""
    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for r in rows:
        groups[(r["predictor"], r["op"])].append(abs(r["error"]))
    out = []
    for name in PREDICTORS:
        for (pred, op), errs in sorted(groups.items()):
            if pred != name:
                continue
            out.append(
                {
                    "predictor": pred,
                    "op": op,
                    "n": len(errs),
                    "mae": sum(errs) / len(errs),
                    "max_abs_error": max(errs),
                    "exact_rate": sum(1 for e in errs if e == 0) / len(errs),
                }
            )
    return out


def validate(seeds: list[int], out_dir: Path, rows: int | None = None, conninfo: str | None = None) -> dict:
    results = []
    for seed in seeds:
        res = run(random_config(seed, rows), conninfo)
        write_result(res, out_dir)
        results.append(res)
    rows_ = [r for res in results for r in evaluate_run(res)]
    out_dir = Path(out_dir)
    with open(out_dir / "validation.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_[0]))
        w.writeheader()
        w.writerows(rows_)
    return {"results": results, "rows": rows_, "by_operation": by_operation(rows_)}
