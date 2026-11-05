"""CLI: python -m pagereuse run [options]"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .experiment import ExperimentConfig, run
from .export import write_result


def main(argv: list[str] | None = None) -> None:
    d = ExperimentConfig()
    p = argparse.ArgumentParser(prog="pagereuse")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the insert/delete/vacuum/reinsert experiment")
    r.add_argument("--table", default=d.table)
    r.add_argument("--rows", type=int, default=d.rows)
    r.add_argument("--payload-bytes", type=int, default=d.payload_bytes)
    r.add_argument("--fillfactor", type=int, default=d.fillfactor)
    r.add_argument("--delete-pattern", choices=["stride", "random"], default=d.delete_pattern)
    r.add_argument("--delete-every", type=int, default=d.delete_every)
    r.add_argument("--delete-fraction", type=float, default=d.delete_fraction)
    r.add_argument("--no-vacuum", dest="vacuum", action="store_false")
    r.add_argument("--reinsert-rows", type=int, default=None)
    r.add_argument("--seed", type=int, default=d.seed)
    r.add_argument("--out", type=Path, default=Path("results"))
    args = vars(p.parse_args(argv))
    args.pop("cmd")
    out = args.pop("out")

    result = run(ExperimentConfig(**args))
    run_dir = write_result(result, out)
    print(json.dumps({"run_dir": str(run_dir), **result["summary"]}, indent=2))


if __name__ == "__main__":
    main()
