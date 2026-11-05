"""CLI:
python -m pagereuse run   [options]        one experiment
python -m pagereuse suite <name> [options] a named comparison suite
python -m pagereuse serve [options]        results viewer at http://127.0.0.1:8765
python -m pagereuse evaluate [options]     score size predictors on saved results
python -m pagereuse validate [options]     score predictors on random, unseen workloads
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from .experiment import ExperimentConfig, run
from .export import write_comparison, write_result
from .model import write_evaluation
from .server import index_runs, load_run, serve
from .suites import SUITES
from .validate import validate


def main(argv: list[str] | None = None) -> None:
    d = ExperimentConfig()
    p = argparse.ArgumentParser(prog="pagereuse")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run one insert/delete/vacuum/reinsert experiment")
    r.add_argument("--table", default=d.table)
    r.add_argument("--rows", type=int, default=d.rows)
    r.add_argument("--payload-bytes", type=int, default=d.payload_bytes)
    r.add_argument("--fillfactor", type=int, default=d.fillfactor)
    r.add_argument("--delete-every", type=int, default=d.delete_every)
    r.add_argument("--no-vacuum", dest="vacuum", action="store_false")
    r.add_argument("--no-index-tag", dest="index_tag", action="store_false")
    r.add_argument("--seed", type=int, default=d.seed)
    r.add_argument("--steps", type=Path, default=None, help="JSON file with a step list")
    r.add_argument("--out", type=Path, default=Path("results"))

    s = sub.add_parser("suite", help="run a comparison suite")
    s.add_argument("name", choices=sorted(SUITES))
    s.add_argument("--rows", type=int, default=d.rows)
    s.add_argument("--out", type=Path, default=Path("results"))

    v = sub.add_parser("serve", help="serve the results viewer")
    v.add_argument("--results", type=Path, default=Path("results"))
    v.add_argument("--port", type=int, default=8765)

    e = sub.add_parser("evaluate", help="walk-forward evaluation of page-count predictors")
    e.add_argument("--results", type=Path, default=Path("results"))

    va = sub.add_parser("validate", help="run random workloads and score predictors by operation")
    va.add_argument("--n", type=int, default=12)
    va.add_argument("--seed", type=int, default=1000, help="first seed; workloads use seed..seed+n-1")
    va.add_argument("--rows", type=int, default=None, help="fixed row count (default: random per workload)")
    va.add_argument("--out", type=Path, default=Path("results/validation"))

    args = p.parse_args(argv)
    if args.cmd == "validate":
        report = validate(list(range(args.seed, args.seed + args.n)), args.out, args.rows)
        print(f"{'predictor':14s} {'op':18s} {'n':>4s} {'MAE':>7s} {'max':>5s} {'exact':>6s}")
        for row in report["by_operation"]:
            print(
                f"{row['predictor']:14s} {row['op']:18s} {row['n']:4d} {row['mae']:7.2f} "
                f"{row['max_abs_error']:5d} {row['exact_rate']:6.0%}"
            )
        for name in ("last_value", "linear_trend", "capacity"):
            errs = [abs(r["error"]) for r in report["rows"] if r["predictor"] == name]
            exact = sum(e == 0 for e in errs) / len(errs)
            print(f"{name:14s} overall MAE {sum(errs) / len(errs):.2f}, exact {exact:.0%}")
        print(f"rows: {len(report['rows'])} -> {args.out / 'validation.csv'}")
        return
    if args.cmd == "evaluate":
        results = [load_run(args.results, r["run_id"]) for r in index_runs(args.results)]
        if not results:
            raise SystemExit(f"no results in {args.results}")
        rows, summary = write_evaluation(results, args.results / "model_eval.csv")
        print(f"{'predictor':14s} {'n':>5s} {'MAE':>8s} {'max':>6s} {'exact':>7s}")
        for s_ in summary:
            print(
                f"{s_['predictor']:14s} {s_['n']:5d} {s_['mae']:8.2f} "
                f"{s_['max_abs_error']:6d} {s_['exact_rate']:7.0%}"
            )
        print(f"rows: {len(rows)} -> {args.results / 'model_eval.csv'}")
        return
    if args.cmd == "serve":
        serve(args.results, port=args.port)
        return
    if args.cmd == "run":
        kw = vars(args).copy()
        kw.pop("cmd")
        out = kw.pop("out")
        steps_path = kw.pop("steps")
        if steps_path is not None:
            kw["steps"] = json.loads(Path(steps_path).read_text())
        result = run(ExperimentConfig(**kw))
        run_dir = write_result(result, out)
        print(json.dumps({"run_dir": str(run_dir), **result["summary"]}, indent=2))
        return

    results = []
    for cfg in SUITES[args.name](args.rows):
        res = run(cfg)
        write_result(res, args.out)
        results.append(res)
        print(
            f"{cfg.label:40s} pages first/peak/final "
            f"{res['checkpoints'][0]['relation']['exact']['main_pages']}/"
            f"{res['summary']['peak_pages']}/{res['summary']['final_pages']}  "
            f"hot {res['summary']['hot_updates']}/{res['summary']['updates']}"
        )
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    path = write_comparison(results, args.out / f"comparison_{args.name}_{stamp}.csv")
    print(f"comparison: {path}")


if __name__ == "__main__":
    main()
