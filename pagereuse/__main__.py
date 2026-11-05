"""CLI:
    python -m pagereuse run   [options]        one experiment
    python -m pagereuse suite <name> [options] a named comparison suite
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from .experiment import ExperimentConfig, run
from .export import write_comparison, write_result
from .suites import SUITES


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

    args = p.parse_args(argv)
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
        print(f"{cfg.label:40s} pages first/peak/final "
              f"{res['checkpoints'][0]['relation']['exact']['main_pages']}/"
              f"{res['summary']['peak_pages']}/{res['summary']['final_pages']}  "
              f"hot {res['summary']['hot_updates']}/{res['summary']['updates']}")
    stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    path = write_comparison(results, args.out / f"comparison_{args.name}_{stamp}.csv")
    print(f"comparison: {path}")


if __name__ == "__main__":
    main()
