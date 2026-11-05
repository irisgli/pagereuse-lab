"""Step-driven storage experiments.

A workload is a list of steps. After every step the table is measured.
Step dicts (JSON-friendly):

    {"op": "insert", "rows": N}
    {"op": "delete", "pattern": "stride"|"random"|"oldest", "every": K, "fraction": F}

  stride: ids divisible by ``every``; random: seeded sample of ``fraction``;
  oldest: the first ``fraction`` of live ids (queue-like churn).
    {"op": "update", "pattern": ..., "column": "payload"|"tag", "grow_bytes": G}
    {"op": "vacuum"}
    {"op": "repeat", "times": T, "steps": [...]}

* ``payload`` is not indexed, so updating it can be a HOT update when the
  page has room. ``tag`` is indexed, so updating it is never HOT.
* ``grow_bytes`` lengthens the payload on update, so the new version needs
  more room than the old one.

Methodology (recorded in every result file):

* Autovacuum is disabled on the experiment table; VACUUM runs only as a step.
* Every step runs in a fresh backend, so per-backend state such as the
  cached insertion target block does not leak between steps.
* Payloads are fixed-length ASCII far below the TOAST threshold.
* Page metrics are read via pageinspect before pgstattuple runs, because
  pgstattuple's scan can opportunistically prune pages. Pages it changed are
  reported under ``observer_effect``.
"""

from __future__ import annotations

import random
import string
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from psycopg import sql

from . import db, metrics

SCHEMA_VERSION = 2


def default_steps(rows: int, delete_every: int, vacuum: bool) -> list[dict]:
    """The Phase 1 workload: load, delete every k-th row, optional vacuum, reinsert."""
    steps = [
        {"op": "insert", "rows": rows},
        {"op": "delete", "pattern": "stride", "every": delete_every},
    ]
    if vacuum:
        steps.append({"op": "vacuum"})
    steps.append({"op": "insert", "rows": rows // delete_every})
    return steps


@dataclass(frozen=True)
class ExperimentConfig:
    table: str = "prl_reuse"
    rows: int = 10_000
    payload_bytes: int = 200
    fillfactor: int = 100
    index_tag: bool = True
    delete_every: int = 2
    vacuum: bool = True
    seed: int = 42
    label: str = ""
    steps: list[dict] | None = None

    def validate(self) -> None:
        db.check_table_name(self.table)
        if self.rows <= 0 or self.payload_bytes <= 0:
            raise ValueError("rows and payload_bytes must be positive")
        if self.payload_bytes > 1000:
            raise ValueError("payload_bytes > 1000 risks TOAST; not supported")
        if not 10 <= self.fillfactor <= 100:
            raise ValueError("fillfactor must be in [10, 100]")
        if self.delete_every < 1:
            raise ValueError("delete_every must be >= 1")
        for s in self.resolved_steps():
            _validate_step(s)

    def resolved_steps(self) -> list[dict]:
        steps = self.steps if self.steps is not None else default_steps(
            self.rows, self.delete_every, self.vacuum)
        return _expand(steps)


def _expand(steps: list[dict]) -> list[dict]:
    out: list[dict] = []
    for s in steps:
        if s.get("op") == "repeat":
            for _ in range(int(s["times"])):
                out.extend(_expand(s["steps"]))
        else:
            out.append(dict(s))
    return out


def _validate_step(s: dict) -> None:
    op = s.get("op")
    if op == "insert":
        if int(s["rows"]) <= 0:
            raise ValueError("insert rows must be positive")
    elif op in ("delete", "update"):
        if s.get("pattern", "stride") not in ("stride", "random", "oldest"):
            raise ValueError("pattern must be 'stride', 'random' or 'oldest'")
        if int(s.get("every", 2)) < 1 or not 0 <= float(s.get("fraction", 0.5)) <= 1:
            raise ValueError("bad every/fraction")
        if op == "update" and s.get("column", "payload") not in ("payload", "tag"):
            raise ValueError("update column must be 'payload' or 'tag'")
    elif op != "vacuum":
        raise ValueError(f"unknown step op {op!r}")


def _payloads(rng: random.Random, n: int, size: int) -> list[str]:
    alphabet = string.ascii_letters + string.digits
    return ["".join(rng.choices(alphabet, k=size)) for _ in range(n)]


def _select_ids(live_ids: list[int], s: dict, rng: random.Random) -> list[int]:
    pattern = s.get("pattern", "stride")
    if pattern == "stride":
        k = int(s.get("every", 2))
        return [i for i in live_ids if i % k == 0]
    n = round(len(live_ids) * float(s.get("fraction", 0.5)))
    if pattern == "oldest":
        return live_ids[:n]
    return sorted(rng.sample(live_ids, n))


def _measure(conninfo, table: str, idx: int, operation: dict) -> dict:
    with db.connect(conninfo) as conn:
        pages = metrics.page_metrics(conn, table)
        rel = metrics.relation_metrics(conn, table)  # runs pgstattuple (may prune)
        after = metrics.page_metrics(conn, table)
    changed = [b["blkno"] for b, a in zip(pages, after) if a != b]
    rel["fsm"] = {
        "fsm_total_avail_bytes": sum(p["fsm_avail_bytes"] for p in pages),
        "pages_with_fsm_space": sum(1 for p in pages if p["fsm_avail_bytes"] > 0),
    }
    rel["exact"]["page_free_bytes_total"] = sum(p["exact_free_bytes"] for p in pages)
    rel["exact"]["pages_all_visible"] = sum(1 for p in pages if p["all_visible"])
    rel["exact"]["lp_redirect_total"] = sum(p["lp_redirect"] for p in pages)
    rel["exact"]["lp_dead_total"] = sum(p["lp_dead"] for p in pages)
    rel["exact"]["lp_unused_total"] = sum(p["lp_unused"] for p in pages)
    return {
        "step": idx,
        "name": f"{idx:02d}_{operation['op']}",
        "operation": operation,
        "relation": rel,
        "observer_effect": {"pages_changed_by_pgstattuple": changed},
        "pages": pages,
    }


class _State:
    """Logical table contents the workload needs to pick rows: ids and payload lengths."""

    def __init__(self) -> None:
        self.next_id = 1
        self.live: dict[int, int] = {}  # id -> current payload length


def _run_step(cfg: ExperimentConfig, conninfo, s: dict, st: _State, rng: random.Random) -> dict:
    t = sql.Identifier(cfg.table)
    op = s["op"]
    with db.connect(conninfo) as conn:
        if op == "insert":
            n = int(s["rows"])
            rows = [(st.next_id + i, (st.next_id + i) % 97, p)
                    for i, p in enumerate(_payloads(rng, n, cfg.payload_bytes))]
            with conn.transaction(), conn.cursor() as cur:
                cur.executemany(
                    sql.SQL("INSERT INTO {} (id, tag, payload) VALUES (%s, %s, %s)").format(t), rows)
            for i, _, p in rows:
                st.live[i] = len(p)
            st.next_id += n
            return {"op": op, "rows": n}

        if op == "delete":
            ids = _select_ids(sorted(st.live), s, rng)
            cnt = conn.execute(sql.SQL("DELETE FROM {} WHERE id = ANY(%s)").format(t), (ids,)).rowcount
            for i in ids:
                del st.live[i]
            return {"op": op, "rows": cnt, "pattern": s.get("pattern", "stride")}

        if op == "update":
            ids = _select_ids(sorted(st.live), s, rng)
            col = s.get("column", "payload")
            grow = int(s.get("grow_bytes", 0))
            if col == "tag":
                cnt = conn.execute(
                    sql.SQL("UPDATE {} SET tag = tag + 1000 WHERE id = ANY(%s)").format(t), (ids,)
                ).rowcount
            else:
                # Same-length payload unless grow_bytes > 0; executemany keeps lengths exact.
                new = [(_payloads(rng, 1, st.live[i] + grow)[0], i) for i in ids]
                with conn.transaction(), conn.cursor() as cur:
                    cur.executemany(sql.SQL("UPDATE {} SET payload = %s WHERE id = %s").format(t), new)
                cnt = len(new)
                for p, i in new:
                    st.live[i] = len(p)
            return {"op": op, "rows": cnt, "column": col, "grow_bytes": grow,
                    "pattern": s.get("pattern", "stride")}

        if op == "vacuum":
            conn.execute(sql.SQL("VACUUM {}").format(t))
            return {"op": op}

    raise ValueError(op)


def run(cfg: ExperimentConfig, conninfo: str | None = None) -> dict:
    cfg.validate()
    rng = random.Random(cfg.seed)
    t = sql.Identifier(cfg.table)
    steps = cfg.resolved_steps()

    with db.connect(conninfo) as conn:
        server_version = conn.execute("SHOW server_version").fetchone()[0]
        block_size = int(conn.execute("SHOW block_size").fetchone()[0])
        conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(t))
        conn.execute(
            sql.SQL(
                "CREATE TABLE {} (id integer PRIMARY KEY, tag integer NOT NULL, "
                "payload text NOT NULL) "
                "WITH (fillfactor = {}, autovacuum_enabled = false, "
                "toast.autovacuum_enabled = false)"
            ).format(t, sql.Literal(cfg.fillfactor))
        )
        if cfg.index_tag:
            conn.execute(sql.SQL("CREATE INDEX {} ON {} (tag)").format(
                sql.Identifier(f"{cfg.table}_tag_idx"), t))

    st = _State()
    checkpoints = []
    for i, s in enumerate(steps):
        operation = _run_step(cfg, conninfo, s, st, rng)
        checkpoints.append(_measure(conninfo, cfg.table, i, operation))

    pages = [c["relation"]["exact"]["main_pages"] for c in checkpoints]
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "config": asdict(cfg),
        "steps": steps,
        "environment": {"server_version": server_version, "block_size": block_size},
        "methodology": {
            "autovacuum": "disabled on experiment table",
            "step_isolation": "fresh backend per step",
            "schema": "id int PK, tag int (indexed unless index_tag=false), payload text",
            "hot_updates": "payload updates are HOT-eligible; tag updates never HOT when indexed",
            "page_reader": "pageinspect get_raw_page/page_header/heap_page_items",
            "fsm_reader": "pg_freespacemap pg_freespace(rel, blkno)",
            "tuple_stats": "pgstattuple (exact scan; may prune, see observer_effect)",
        },
        "summary": {
            "main_pages": {c["name"]: p for c, p in zip(checkpoints, pages)},
            "peak_pages": max(pages),
            "final_pages": pages[-1],
            "growth_vs_first_pages": pages[-1] - pages[0],
            "final_live_tuples": checkpoints[-1]["relation"]["exact"]["pgstattuple_tuple_count"],
            "hot_updates": checkpoints[-1]["relation"]["estimated"]["n_tup_hot_upd"],
            "updates": checkpoints[-1]["relation"]["estimated"]["n_tup_upd"],
        },
        "checkpoints": checkpoints,
    }
