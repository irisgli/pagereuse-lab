"""Step-driven storage experiments.

A workload is a list of steps. After every step the table is measured.
Step dicts (JSON-friendly):

    {"op": "insert", "rows": N, "sessions": S}
    {"op": "delete", "pattern": "stride"|"random"|"oldest", "every": K, "fraction": F}

  stride: ids divisible by ``every``; random: seeded sample of ``fraction``;
  oldest: the first ``fraction`` of live ids (queue-like churn).
    {"op": "update", "pattern": ..., "column": "payload"|"tag", "grow_bytes": G}
    {"op": "vacuum"}
    {"op": "hold_snapshot", "name": "a"}     open a REPEATABLE READ transaction
                                 on a side connection and keep it across later
                                 steps; several may be held under different names
    {"op": "release_snapshot", "name": "a"}  close that one
    {"op": "wait_autovacuum", "timeout": 20} poll until an autovacuum of the table
                                 completes or the timeout passes. ``ran`` compares
                                 against the previous checkpoint; an autovacuum
                                 that finished before that checkpoint shows up in
                                 its ``autovacuum_count`` instead
    {"op": "repeat", "times": T, "steps": [...]}

* ``sessions`` > 1 splits an insert across that many connections running at
  the same time, each in its own transaction.
* While a snapshot is held, VACUUM cannot remove tuples deleted after it was
  taken; the VACUUM step records the server's own notice saying so.

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
* Each checkpoint also records where every live row sits (``live_rows``: id,
  block, stored length) and each update step records the ids it targets, so a
  predictor can follow rows across relocations without replaying the workload.
"""

from __future__ import annotations

import random
import re
import string
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import psycopg
from psycopg import sql

from . import db, metrics

SCHEMA_VERSION = 3


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
    autovacuum: dict | None = None  # reloptions, e.g. {"autovacuum_vacuum_scale_factor": 0.02}
    storage: str | None = None  # payload column storage: "external" (out of line, no compression)
    delete_every: int = 2
    vacuum: bool = True
    seed: int = 42
    label: str = ""
    steps: list[dict] | None = None

    def validate(self) -> None:
        db.check_table_name(self.table)
        if self.rows <= 0 or self.payload_bytes <= 0:
            raise ValueError("rows and payload_bytes must be positive")
        if self.payload_bytes > 1000 and self.storage is None:
            raise ValueError("payload_bytes > 1000 is TOASTed; set storage='external' to study that")
        if self.payload_bytes > 6000:
            raise ValueError("payload_bytes > 6000 not supported")
        if self.storage not in (None, "external", "extended", "main", "plain"):
            raise ValueError("storage must be external, extended, main or plain")
        if not 10 <= self.fillfactor <= 100:
            raise ValueError("fillfactor must be in [10, 100]")
        if self.delete_every < 1:
            raise ValueError("delete_every must be >= 1")
        for s in self.resolved_steps():
            _validate_step(s)

    def resolved_steps(self) -> list[dict]:
        steps = (
            self.steps if self.steps is not None else default_steps(self.rows, self.delete_every, self.vacuum)
        )
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
        if int(s.get("sessions", 1)) < 1:
            raise ValueError("insert sessions must be >= 1")
    elif op in ("delete", "update"):
        if s.get("pattern", "stride") not in ("stride", "random", "oldest"):
            raise ValueError("pattern must be 'stride', 'random' or 'oldest'")
        if int(s.get("every", 2)) < 1 or not 0 <= float(s.get("fraction", 0.5)) <= 1:
            raise ValueError("bad every/fraction")
        if op == "update" and s.get("column", "payload") not in ("payload", "tag"):
            raise ValueError("update column must be 'payload' or 'tag'")
    elif op == "wait_autovacuum":
        if float(s.get("timeout", 20)) <= 0:
            raise ValueError("wait_autovacuum timeout must be positive")
    elif op not in ("vacuum", "hold_snapshot", "release_snapshot"):
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
        rows = metrics.live_rows(conn, table)
        fsm_root = metrics.fsm_root_avail(conn, table)
        after = metrics.page_metrics(conn, table)
    changed = [b["blkno"] for b, a in zip(pages, after, strict=True) if a != b]
    if len(rows["id"]) != rel["exact"]["pgstattuple_tuple_count"]:
        raise RuntimeError(f"{table}: live_rows found {len(rows['id'])} rows, pgstattuple disagrees")
    rel["fsm"] = {
        "fsm_total_avail_bytes": sum(p["fsm_avail_bytes"] for p in pages),
        "pages_with_fsm_space": sum(1 for p in pages if p["fsm_avail_bytes"] > 0),
        "fsm_root_avail_bytes": fsm_root,
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
        "live_rows": rows,
    }


class _State:
    """Logical table contents the workload needs to pick rows: ids and payload lengths."""

    def __init__(self) -> None:
        self.next_id = 1
        self.live: dict[int, int] = {}  # id -> current payload length
        self.snapshots: dict[str, psycopg.Connection] = {}
        self.autovacuum_count = 0  # as of the last checkpoint

    def close(self, name: str | None = None) -> None:
        for key in [name] if name is not None else list(self.snapshots):
            self.snapshots.pop(key).close()


def _insert_rows(conninfo, t: sql.Composable, rows: list[tuple]) -> None:
    with db.connect(conninfo) as conn, conn.transaction(), conn.cursor() as cur:
        cur.executemany(sql.SQL("INSERT INTO {} (id, tag, payload) VALUES (%s, %s, %s)").format(t), rows)


def _run_step(cfg: ExperimentConfig, conninfo, s: dict, st: _State, rng: random.Random) -> dict:
    t = sql.Identifier(cfg.table)
    op = s["op"]

    if op == "insert":
        n = int(s["rows"])
        sessions = int(s.get("sessions", 1))
        rows = [
            (st.next_id + i, (st.next_id + i) % 97, p)
            for i, p in enumerate(_payloads(rng, n, cfg.payload_bytes))
        ]
        if sessions == 1:
            _insert_rows(conninfo, t, rows)
        else:
            # Contiguous id slices, one connection each, all running at once.
            size = -(-n // sessions)
            slices = [rows[k : k + size] for k in range(0, n, size)]
            with ThreadPoolExecutor(max_workers=len(slices)) as pool:
                list(pool.map(lambda r: _insert_rows(conninfo, t, r), slices))
        for i, _, p in rows:
            st.live[i] = len(p)
        st.next_id += n
        return {"op": op, "rows": n, "sessions": sessions}

    if op == "wait_autovacuum":
        timeout = float(s.get("timeout", 20))
        # Baseline is the previous checkpoint's count, so a run that finished
        # between that measurement and now still counts as this step's.
        before = st.autovacuum_count
        with db.connect(conninfo) as conn:
            started = time.monotonic()
            while time.monotonic() - started < timeout:
                conn.execute("SELECT pg_stat_clear_snapshot()")
                count = _autovacuum_count(conn, cfg.table)
                if count > before:
                    return {
                        "op": op,
                        "ran": True,
                        "seconds": round(time.monotonic() - started, 2),
                        "count_before": before,
                        "count_after": count,
                    }
                time.sleep(0.25)
        return {
            "op": op,
            "ran": False,
            "seconds": round(timeout, 2),
            "count_before": before,
            "count_after": before,
        }

    name = str(s.get("name", "default"))
    if op == "hold_snapshot":
        if name in st.snapshots:
            raise ValueError(f"snapshot {name!r} is already held")
        conn = db.connect(conninfo)
        conn.autocommit = False
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        snapshot_count = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(t)).fetchone()[0]
        st.snapshots[name] = conn
        return {"op": op, "name": name, "rows_visible_to_snapshot": snapshot_count}

    if op == "release_snapshot":
        if name not in st.snapshots:
            raise ValueError(f"snapshot {name!r} is not held")
        st.close(name)
        return {"op": op, "name": name}

    with db.connect(conninfo) as conn:
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
            scan = "index"  # payload updates are one statement per id, always an index fetch
            if col == "tag":
                stmt = sql.SQL("UPDATE {} SET tag = tag + 1000 WHERE id = ANY(%s)").format(t)
                plan = conn.execute(sql.SQL("EXPLAIN (FORMAT JSON) ") + stmt, (ids,)).fetchone()[0]
                scan = _scan_type(plan)
                cnt = conn.execute(stmt, (ids,)).rowcount
            else:
                # Same-length payload unless grow_bytes > 0; executemany keeps lengths exact.
                new = [(_payloads(rng, 1, st.live[i] + grow)[0], i) for i in ids]
                with conn.transaction(), conn.cursor() as cur:
                    cur.executemany(sql.SQL("UPDATE {} SET payload = %s WHERE id = %s").format(t), new)
                cnt = len(new)
                for p, i in new:
                    st.live[i] = len(p)
            return {
                "op": op,
                "rows": cnt,
                "column": col,
                "grow_bytes": grow,
                "pattern": s.get("pattern", "stride"),
                "ids": ids,  # chosen before the statement ran, from the previous checkpoint's live set
                "scan": scan,
            }

        if op == "vacuum":
            notices: list[str] = []
            conn.add_notice_handler(lambda d: notices.append(d.message_primary or ""))
            conn.execute(sql.SQL("VACUUM (VERBOSE) {}").format(t))
            text = "\n".join(notices)
            return {
                "op": op,
                "snapshot_held": bool(st.snapshots),
                "snapshots_held": sorted(st.snapshots),
                "dead_not_yet_removable": _parse_not_removable(text),
                "pages_removed": _parse_pages_removed(text),
                "notices": text,
            }

    raise ValueError(op)


def _scan_type(plan: list) -> str:
    """How the planner will reach the rows of a single-statement update: "index"
    (id order, pruning when the fetch moves to another page), "bitmap" or "seq"
    (block order, each page pruned once on entry). Read from EXPLAIN before the
    statement runs, so it describes the operation, not its outcome."""
    kinds = {"Index Scan": "index", "Bitmap Heap Scan": "bitmap", "Seq Scan": "seq"}
    node = plan[0]["Plan"]
    while node["Node Type"] not in kinds:
        node = node["Plans"][0]  # Update -> heap scan (a Bitmap Heap Scan's own child is the index)
    return kinds[node["Node Type"]]


def _autovacuum_count(conn, table: str) -> int:
    return conn.execute(
        "SELECT autovacuum_count FROM pg_stat_user_tables WHERE relid = %s::regclass", (table,)
    ).fetchone()[0]


AUTOVACUUM_GUCS = (
    "autovacuum_naptime",
    "autovacuum_vacuum_threshold",
    "autovacuum_vacuum_scale_factor",
    "autovacuum_vacuum_insert_threshold",
    "autovacuum_vacuum_insert_scale_factor",
)


def _parse_pages_removed(verbose: str) -> int | None:
    m = re.search(r"pages: (\d+) removed", verbose)
    return int(m.group(1)) if m else None


def _parse_not_removable(verbose: str) -> int | None:
    """From VACUUM VERBOSE: 'tuples: R removed, L remain, N are dead but not yet removable'."""
    m = re.search(r"(\d+) are dead but not yet removable", verbose)
    return int(m.group(1)) if m else None


def run(cfg: ExperimentConfig, conninfo: str | None = None) -> dict:
    cfg.validate()
    rng = random.Random(cfg.seed)
    t = sql.Identifier(cfg.table)
    steps = cfg.resolved_steps()

    with db.connect(conninfo) as conn:
        server_version = conn.execute("SHOW server_version").fetchone()[0]
        block_size = int(conn.execute("SHOW block_size").fetchone()[0])
        if cfg.autovacuum is not None and conn.execute("SHOW autovacuum_naptime").fetchone()[0] != "1s":
            # The launcher's default 60 s nap would dominate every wait; 1 s keeps
            # runs short. Server-wide, recorded in the result, restored by nobody.
            conn.execute("ALTER SYSTEM SET autovacuum_naptime = '1s'")
            conn.execute("SELECT pg_reload_conf()")
            time.sleep(0.5)
        gucs = {g: conn.execute(f"SHOW {g}").fetchone()[0] for g in AUTOVACUUM_GUCS}
        conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(t))
        options = {"fillfactor": cfg.fillfactor, "autovacuum_enabled": cfg.autovacuum is not None}
        options.update(cfg.autovacuum or {})
        conn.execute(
            sql.SQL(
                "CREATE TABLE {} (id integer PRIMARY KEY, tag integer NOT NULL, payload text NOT NULL) "
                "WITH ({})"
            ).format(
                t,
                sql.SQL(", ").join(
                    sql.SQL("{} = {}").format(sql.Identifier(k), sql.Literal(v)) for k, v in options.items()
                ),
            )
        )
        if cfg.storage is not None:
            conn.execute(
                sql.SQL("ALTER TABLE {} ALTER COLUMN payload SET STORAGE {}").format(t, sql.SQL(cfg.storage))
            )
        if cfg.index_tag:
            conn.execute(
                sql.SQL("CREATE INDEX {} ON {} (tag)").format(sql.Identifier(f"{cfg.table}_tag_idx"), t)
            )

    st = _State()
    checkpoints = []
    try:
        for i, s in enumerate(steps):
            operation = _run_step(cfg, conninfo, s, st, rng)
            checkpoints.append(_measure(conninfo, cfg.table, i, operation))
            st.autovacuum_count = checkpoints[-1]["relation"]["estimated"]["autovacuum_count"]
    finally:
        st.close()

    pages = [c["relation"]["exact"]["main_pages"] for c in checkpoints]
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}",
        "started_at": datetime.now(UTC).isoformat(),
        "config": asdict(cfg),
        "steps": steps,
        "environment": {"server_version": server_version, "block_size": block_size, "autovacuum": gucs},
        "methodology": {
            "autovacuum": "off unless config.autovacuum gives reloptions; then naptime is 1 s",
            "step_isolation": "fresh backend per step; held snapshots live on a separate connection",
            "schema": "id int PK, tag int (indexed unless index_tag=false), payload text",
            "hot_updates": "payload updates are HOT-eligible; tag updates never HOT when indexed",
            "page_reader": "pageinspect get_raw_page/page_header/heap_page_items",
            "fsm_reader": "pg_freespacemap pg_freespace(rel, blkno)",
            "tuple_stats": "pgstattuple (exact scan; may prune, see observer_effect)",
        },
        "summary": {
            "main_pages": {c["name"]: p for c, p in zip(checkpoints, pages, strict=True)},
            "peak_pages": max(pages),
            "final_pages": pages[-1],
            "growth_vs_first_pages": pages[-1] - pages[0],
            "final_live_tuples": checkpoints[-1]["relation"]["exact"]["pgstattuple_tuple_count"],
            "hot_updates": checkpoints[-1]["relation"]["estimated"]["n_tup_hot_upd"],
            "updates": checkpoints[-1]["relation"]["estimated"]["n_tup_upd"],
        },
        "checkpoints": checkpoints,
    }
