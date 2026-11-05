"""Storage measurements for one experiment table at one point in time.

Every value is tagged by provenance, because they are not equally trustworthy:

* ``exact``      read from the relation's actual bytes (pageinspect, pgstattuple,
                 pg_relation_size). These are measurements.
* ``fsm``        pg_freespace(): the free space map's *recorded* value. It is
                 quantized to BLCKSZ/256 byte categories, rounded down, and only
                 updated by VACUUM (and a few other paths), so it can be stale.
* ``estimated``  pg_class.relpages/reltuples and pg_stat_user_tables counters.
                 Planner estimates and cumulative statistics, not measurements.
"""

from __future__ import annotations

from psycopg import Connection

from .db import check_table_name

# lp_flags values from src/include/storage/itemid.h
LP_UNUSED, LP_NORMAL, LP_REDIRECT, LP_DEAD = 0, 1, 2, 3
PD_ALL_VISIBLE = 0x0004  # src/include/storage/bufpage.h

PAGE_COLUMNS = (
    "blkno",
    "pd_lower",
    "pd_upper",
    "exact_free_bytes",
    "fsm_avail_bytes",
    "all_visible",
    "lp_total",
    "lp_unused",
    "lp_normal",
    "lp_redirect",
    "lp_dead",
    "normal_xmax_set",
    "dead_removable",
    "normal_tuple_bytes",
)

_PAGES_SQL = f"""
SELECT b.blkno,
       h.lower AS pd_lower,
       h.upper AS pd_upper,
       h.upper - h.lower AS exact_free_bytes,
       pg_freespace(%(rel)s::regclass, b.blkno) AS fsm_avail_bytes,
       (h.flags & {PD_ALL_VISIBLE}) <> 0 AS all_visible,
       i.lp_total, i.lp_unused, i.lp_normal, i.lp_redirect, i.lp_dead,
       i.normal_xmax_set, i.dead_removable, i.normal_tuple_bytes
FROM generate_series(0, (pg_relation_size(%(rel)s::regclass)
                         / current_setting('block_size')::int) - 1) AS b(blkno)
CROSS JOIN LATERAL (SELECT get_raw_page(%(rel)s::text, b.blkno::int) AS raw) AS p
CROSS JOIN LATERAL page_header(p.raw) AS h
CROSS JOIN LATERAL (
    SELECT count(*)                                         AS lp_total,
           count(*) FILTER (WHERE lp_flags = {LP_UNUSED})   AS lp_unused,
           count(*) FILTER (WHERE lp_flags = {LP_NORMAL})   AS lp_normal,
           count(*) FILTER (WHERE lp_flags = {LP_REDIRECT}) AS lp_redirect,
           count(*) FILTER (WHERE lp_flags = {LP_DEAD})     AS lp_dead,
           count(*) FILTER (WHERE lp_flags = {LP_NORMAL} AND t_xmax <> '0') AS normal_xmax_set,
           count(*) FILTER (WHERE lp_flags = {LP_NORMAL} AND t_xmax <> '0'
                            AND (%(horizon)s::bigint IS NULL
                                 OR t_xmax::text::bigint < %(horizon)s::bigint)) AS dead_removable,
           coalesce(sum(lp_len) FILTER (WHERE lp_flags = {LP_NORMAL}), 0)::int AS normal_tuple_bytes
    FROM heap_page_items(p.raw)
) AS i
ORDER BY b.blkno
"""


def _row(cur) -> dict:
    cols = [d.name for d in cur.description]
    return dict(zip(cols, cur.fetchone(), strict=True))


def relation_metrics(conn: Connection, table: str) -> dict:
    rel = check_table_name(table)
    sizes = _row(
        conn.execute(
            """SELECT current_setting('block_size')::int AS block_size,
                  pg_relation_size(%(r)s::regclass, 'main') AS main_bytes,
                  pg_relation_size(%(r)s::regclass, 'fsm')  AS fsm_bytes,
                  pg_relation_size(%(r)s::regclass, 'vm')   AS vm_bytes""",
            {"r": rel},
        )
    )
    sizes["main_pages"] = sizes["main_bytes"] // sizes["block_size"]

    tuples = _row(conn.execute("SELECT * FROM pgstattuple(%s::regclass)", (rel,)))

    # Flush this backend's pending stats and drop any cached stats snapshot so
    # the cumulative counters reflect the work we just did.
    conn.execute("SELECT pg_stat_force_next_flush()")
    conn.execute("SELECT pg_stat_clear_snapshot()")
    est = _row(
        conn.execute(
            """SELECT c.relpages, c.reltuples,
                  s.n_live_tup, s.n_dead_tup, s.n_tup_ins, s.n_tup_del,
                  s.n_tup_upd, s.n_tup_hot_upd, s.vacuum_count,
                  s.autovacuum_count, s.n_ins_since_vacuum, s.last_autovacuum::text AS last_autovacuum
           FROM pg_class c JOIN pg_stat_user_tables s ON s.relid = c.oid
           WHERE c.oid = %s::regclass""",
            (rel,),
        )
    )

    return {
        "exact": {**sizes, **{f"pgstattuple_{k}": v for k, v in tuples.items()}},
        "estimated": est,
    }


def xmin_horizon(conn: Connection) -> int | None:
    """Oldest backend_xmin among other sessions: VACUUM cannot remove tuples
    whose xmax is at or beyond it. None when no other session pins a snapshot."""
    row = conn.execute(
        "SELECT min(backend_xmin::text::bigint) FROM pg_stat_activity WHERE pid <> pg_backend_pid()"
    ).fetchone()
    return row[0]


def page_metrics(conn: Connection, table: str) -> list[dict]:
    """Per-page records. ``dead_removable`` counts xmax-set tuples older than the
    xmin horizon at read time, which is what a VACUUM run now could remove."""
    rel = check_table_name(table)
    cur = conn.execute(_PAGES_SQL, {"rel": rel, "horizon": xmin_horizon(conn)})
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
