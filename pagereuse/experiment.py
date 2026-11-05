"""Insert / delete / (vacuum) / reinsert experiment.

Methodology choices (recorded in every result file):

* Autovacuum is disabled on the experiment table so that VACUUM happens only
  when the workload says so.
* Every workload phase runs in a fresh backend, so per-backend state such as
  the cached insertion target block does not leak between phases.
* Payloads are fixed-length ASCII well under the TOAST threshold, so every
  heap tuple has the same size and nothing is compressed or moved out of line.
* Measurements at each checkpoint read pages via pageinspect first (does not
  modify pages). pgstattuple runs afterwards because its sequential scan can
  opportunistically prune pages; any page it changed is counted and reported
  under ``observer_effect`` instead of being silently absorbed.
"""

from __future__ import annotations

import random
import string
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from psycopg import sql

from . import db, metrics

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ExperimentConfig:
    table: str = "prl_reuse"
    rows: int = 10_000
    payload_bytes: int = 200
    fillfactor: int = 100
    delete_pattern: str = "stride"  # "stride" (id % delete_every == 0) or "random"
    delete_every: int = 2
    delete_fraction: float = 0.5
    vacuum: bool = True
    reinsert_rows: int | None = None  # default: same as rows deleted
    seed: int = 42

    def validate(self) -> None:
        db.check_table_name(self.table)
        if self.rows <= 0 or self.payload_bytes <= 0:
            raise ValueError("rows and payload_bytes must be positive")
        if self.payload_bytes > 1000:
            raise ValueError("payload_bytes > 1000 risks TOAST; not supported in Phase 1")
        if not 10 <= self.fillfactor <= 100:
            raise ValueError("fillfactor must be in [10, 100]")
        if self.delete_pattern not in ("stride", "random"):
            raise ValueError("delete_pattern must be 'stride' or 'random'")
        if self.delete_every < 1 or not 0 <= self.delete_fraction <= 1:
            raise ValueError("bad delete parameters")


def _payloads(rng: random.Random, n: int, size: int) -> list[str]:
    alphabet = string.ascii_letters + string.digits
    return ["".join(rng.choices(alphabet, k=size)) for _ in range(n)]


def _ids_to_delete(cfg: ExperimentConfig, rng: random.Random) -> list[int]:
    ids = range(1, cfg.rows + 1)
    if cfg.delete_pattern == "stride":
        return [i for i in ids if i % cfg.delete_every == 0]
    k = round(cfg.rows * cfg.delete_fraction)
    return sorted(rng.sample(list(ids), k))


def _insert(conninfo, table: str, first_id: int, payloads: list[str]) -> None:
    stmt = sql.SQL("INSERT INTO {} (id, payload) VALUES (%s, %s)").format(sql.Identifier(table))
    with db.connect(conninfo) as conn, conn.cursor() as cur:
        with conn.transaction():
            cur.executemany(stmt, [(first_id + i, p) for i, p in enumerate(payloads)])


def _measure(conninfo, table: str, name: str, operation: dict) -> dict:
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
    return {
        "name": name,
        "operation": operation,
        "relation": rel,
        "observer_effect": {"pages_changed_by_pgstattuple": changed},
        "pages": pages,
    }


def run(cfg: ExperimentConfig, conninfo: str | None = None) -> dict:
    cfg.validate()
    rng = random.Random(cfg.seed)
    t = sql.Identifier(cfg.table)

    with db.connect(conninfo) as conn:
        server_version = conn.execute("SHOW server_version").fetchone()[0]
        block_size = int(conn.execute("SHOW block_size").fetchone()[0])
        conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(t))
        conn.execute(
            sql.SQL(
                "CREATE TABLE {} (id integer PRIMARY KEY, payload text NOT NULL) "
                "WITH (fillfactor = {}, autovacuum_enabled = false, "
                "toast.autovacuum_enabled = false)"
            ).format(t, sql.Literal(cfg.fillfactor))
        )

    checkpoints = []

    load_payloads = _payloads(rng, cfg.rows, cfg.payload_bytes)
    _insert(conninfo, cfg.table, 1, load_payloads)
    checkpoints.append(_measure(conninfo, cfg.table, "after_load", {"inserted": cfg.rows}))

    doomed = _ids_to_delete(cfg, rng)
    with db.connect(conninfo) as conn:
        deleted = conn.execute(
            sql.SQL("DELETE FROM {} WHERE id = ANY(%s)").format(t), (doomed,)
        ).rowcount
    checkpoints.append(_measure(conninfo, cfg.table, "after_delete", {"deleted": deleted}))

    if cfg.vacuum:
        with db.connect(conninfo) as conn:
            conn.execute(sql.SQL("VACUUM {}").format(t))
        checkpoints.append(_measure(conninfo, cfg.table, "after_vacuum", {"vacuum": True}))

    n_re = deleted if cfg.reinsert_rows is None else cfg.reinsert_rows
    _insert(conninfo, cfg.table, cfg.rows + 1, _payloads(rng, n_re, cfg.payload_bytes))
    checkpoints.append(_measure(conninfo, cfg.table, "after_reinsert", {"inserted": n_re}))

    first, last = checkpoints[0], checkpoints[-1]
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "config": asdict(cfg),
        "environment": {"server_version": server_version, "block_size": block_size},
        "methodology": {
            "autovacuum": "disabled on experiment table",
            "phase_isolation": "fresh backend per phase",
            "insert_method": "single-row INSERT via executemany, one transaction",
            "delete_method": "single DELETE ... WHERE id = ANY(ids)",
            "page_reader": "pageinspect get_raw_page/page_header/heap_page_items",
            "fsm_reader": "pg_freespacemap pg_freespace(rel, blkno)",
            "tuple_stats": "pgstattuple (exact scan; may prune, see observer_effect)",
        },
        "summary": {
            "main_pages": {c["name"]: c["relation"]["exact"]["main_pages"] for c in checkpoints},
            "pages_added_by_reinsert": last["relation"]["exact"]["main_pages"]
            - checkpoints[-2]["relation"]["exact"]["main_pages"],
            "growth_vs_load_pages": last["relation"]["exact"]["main_pages"]
            - first["relation"]["exact"]["main_pages"],
        },
        "checkpoints": checkpoints,
    }
