"""Predicting relation size one step ahead, and scoring the predictions.

Every predictor sees the previous checkpoint (measurements only) and the
operation about to run, and returns the number of main-fork pages it expects
afterwards. Walk-forward evaluation then compares that with what the engine
measured. Nothing here is fitted on the run being scored.

Predictors
  last_value    pages stay what they were
  linear_trend  least-squares line through the page counts so far
  capacity      slotted-page accounting (the physical model, see below)
                plus PostgreSQL's documented autovacuum trigger rule for
                wait_autovacuum steps

The capacity model follows how PostgreSQL places tuples:
* An INSERT asks the free space map for a page with at least
  ``footprint + reserve`` bytes, where ``reserve = BLCKSZ * (100 - fillfactor) / 100``
  and ``footprint`` is the MAXALIGNed tuple length plus a 4-byte line pointer.
  It never inspects pages the FSM does not offer, so reuse is bounded by what
  the FSM recorded at the previous checkpoint. Overflow extends the file.
* A DELETE changes no page count.
* A non-HOT UPDATE (indexed column) places new versions like an INSERT.
* A HOT-eligible UPDATE first tries the tuple's own page using the page's real
  free space, so the model counts measured free bytes, not the FSM.
* VACUUM truncates trailing pages that hold no live tuple.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

LINE_POINTER = 4
PAGE_HEADER = 24
MAXALIGN = 8


def _align(n: float) -> int:
    return int(math.ceil(n / MAXALIGN) * MAXALIGN)


def tuple_footprint(pages: list[dict]) -> int:
    """Aligned length of a typical tuple plus its line pointer, from measured pages."""
    tuples = sum(p["lp_normal"] for p in pages)
    byts = sum(p["normal_tuple_bytes"] for p in pages)
    if tuples == 0:
        raise ValueError("no tuples to size")
    return _align(byts / tuples) + LINE_POINTER


def predict_last_value(history: list[int], prev: dict, op: dict, env: dict, cfg: dict) -> int:
    return history[-1]


def predict_linear_trend(history: list[int], prev: dict, op: dict, env: dict, cfg: dict) -> int:
    n = len(history)
    if n < 2:
        return history[-1]
    xs = range(n)
    mx, my = (n - 1) / 2, sum(history) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, history, strict=True)) / sxx
    return max(0, round(my + slope * (n - mx)))


def predict_capacity(history: list[int], prev: dict, op: dict, env: dict, cfg: dict) -> int:
    pages = prev["pages"]
    npages = len(pages)
    block = env["block_size"]
    reserve = block * (100 - cfg["fillfactor"]) // 100
    kind = op["op"]

    if kind in ("delete", "hold_snapshot", "release_snapshot"):
        return npages

    if kind == "wait_autovacuum":
        # Documented trigger: dead > threshold + scale_factor * reltuples, or
        # inserts since the last vacuum > insert_threshold + insert_scale * reltuples.
        # A never-vacuumed table has reltuples = -1, which the launcher treats as 0.
        est = prev["relation"]["estimated"]
        g = dict(env.get("autovacuum", {}))
        g.update(cfg.get("autovacuum") or {})
        reltuples = max(0.0, float(est["reltuples"]))
        dead_trigger = (
            est["n_dead_tup"]
            > float(g["autovacuum_vacuum_threshold"]) + float(g["autovacuum_vacuum_scale_factor"]) * reltuples
        )
        ins_trigger = (
            est["n_ins_since_vacuum"]
            > float(g["autovacuum_vacuum_insert_threshold"])
            + float(g["autovacuum_vacuum_insert_scale_factor"]) * reltuples
        )
        if not (dead_trigger or ins_trigger):
            return npages
        kind = "vacuum"

    if kind == "vacuum":
        # VACUUM removes only versions older than the xmin horizon; the page
        # record counted those at measurement time.
        live = [p["lp_normal"] - p["dead_removable"] for p in pages]
        trailing_empty = 0
        for n in reversed(live):
            if n > 0:
                break
            trailing_empty += 1
        return npages - trailing_empty

    old_len = tuple_footprint(pages) - LINE_POINTER
    if kind == "update":
        footprint = _align(old_len + op.get("grow_bytes", 0)) + LINE_POINTER
    else:
        # Inserts always carry the run's base payload; after growing updates the
        # average tuple is larger than that, so size them from the first checkpoint.
        footprint = env.get("insert_footprint") or tuple_footprint(pages)
    n = int(op["rows"])
    need = footprint + reserve  # what an INSERT asks the FSM for

    if kind == "update":
        # New versions stay on their own page when they fit (HOT or not); the
        # fillfactor reserve is usable here, and dead versions from earlier
        # updates get pruned in place first.
        live = [p["lp_normal"] - p["normal_xmax_set"] for p in pages]
        per_page = _proportional(n, live)
        placed = 0
        for p, u in zip(pages, per_page, strict=True):
            free = p["exact_free_bytes"] + p["normal_xmax_set"] * old_len
            placed += min(u, free // footprint)
        n -= placed

    # FSM path: only pages whose recorded space covers the request are offered.
    offered = [p["fsm_avail_bytes"] for p in pages if p["fsm_avail_bytes"] >= need - LINE_POINTER]
    if offered:
        slots = sum((f - reserve) // footprint for f in offered)
    else:
        # Nothing offered: the inserter tries the last page before extending.
        last = pages[-1]["exact_free_bytes"]
        slots = (last - reserve) // footprint if last >= need else 0
    overflow = max(0, n - slots)
    per_new_page = max(1, (block - PAGE_HEADER - reserve) // footprint)
    return npages + math.ceil(overflow / per_new_page)


def _proportional(n: int, weights: list[int]) -> list[int]:
    """Split n across pages in proportion to weights; largest remainders take the leftover."""
    total = sum(weights)
    if total == 0:
        return [0] * len(weights)
    raw = [n * w / total for w in weights]
    out = [int(r) for r in raw]
    for i in sorted(range(len(raw)), key=lambda i: raw[i] - out[i], reverse=True)[: n - sum(out)]:
        out[i] += 1
    return out


PREDICTORS = {
    "last_value": predict_last_value,
    "linear_trend": predict_linear_trend,
    "capacity": predict_capacity,
}


def evaluate_run(result: dict) -> list[dict]:
    """One row per (predictor, step >= 1) with prediction, actual and error."""
    cps = result["checkpoints"]
    env, cfg = dict(result["environment"]), result["config"]
    if cps[0]["pages"] and sum(p["lp_normal"] for p in cps[0]["pages"]):
        env["insert_footprint"] = tuple_footprint(cps[0]["pages"])
    rows = []
    for i in range(1, len(cps)):
        history = [c["relation"]["exact"]["main_pages"] for c in cps[:i]]
        actual = cps[i]["relation"]["exact"]["main_pages"]
        for name, fn in PREDICTORS.items():
            pred = fn(history, cps[i - 1], cps[i]["operation"], env, cfg)
            rows.append(
                {
                    "run_id": result["run_id"],
                    "label": cfg.get("label") or cfg["table"],
                    "step": i,
                    "op": cps[i]["operation"]["op"],
                    "predictor": name,
                    "predicted": pred,
                    "actual": actual,
                    "error": pred - actual,
                }
            )
    return rows


def summarize(rows: list[dict]) -> list[dict]:
    """Mean absolute error, max absolute error and exact-hit rate per predictor."""
    out = []
    for name in PREDICTORS:
        errs = [abs(r["error"]) for r in rows if r["predictor"] == name]
        if not errs:
            continue
        out.append(
            {
                "predictor": name,
                "n": len(errs),
                "mae": sum(errs) / len(errs),
                "max_abs_error": max(errs),
                "exact_rate": sum(1 for e in errs if e == 0) / len(errs),
            }
        )
    return out


def write_evaluation(results: list[dict], path: Path) -> tuple[list[dict], list[dict]]:
    rows = [r for res in results for r in evaluate_run(res)]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["run_id"])
        w.writeheader()
        w.writerows(rows)
    return rows, summarize(rows)
