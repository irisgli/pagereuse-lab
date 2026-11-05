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

from psycopg import Connection, sql

from .db import check_table_name

# lp_flags values from src/include/storage/itemid.h
LP_UNUSED, LP_NORMAL, LP_REDIRECT, LP_DEAD = 0, 1, 2, 3
PD_PAGE_FULL, PD_ALL_VISIBLE = 0x0002, 0x0004  # src/include/storage/bufpage.h

PAGE_COLUMNS = (
    "blkno",
    "pd_lower",
    "pd_upper",
    "exact_free_bytes",
    "fsm_avail_bytes",
    "page_full",
    "all_visible",
    "lp_total",
    "lp_unused",
    "lp_normal",
    "lp_redirect",
    "lp_dead",
    "normal_xmax_set",
    "dead_removable",
    "dead_removable_bytes",
    "normal_tuple_bytes",
)

_PAGES_SQL = f"""
SELECT b.blkno,
       h.lower AS pd_lower,
       h.upper AS pd_upper,
       h.upper - h.lower AS exact_free_bytes,
       pg_freespace(%(rel)s::regclass, b.blkno) AS fsm_avail_bytes,
       (h.flags & {PD_PAGE_FULL}) <> 0 AS page_full,
       (h.flags & {PD_ALL_VISIBLE}) <> 0 AS all_visible,
       i.lp_total, i.lp_unused, i.lp_normal, i.lp_redirect, i.lp_dead,
       i.normal_xmax_set, i.dead_removable, i.dead_removable_bytes, i.normal_tuple_bytes
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
           coalesce(sum((lp_len + 7) / 8 * 8)  -- MAXALIGN
                    FILTER (WHERE lp_flags = {LP_NORMAL} AND t_xmax <> '0'
                            AND (%(horizon)s::bigint IS NULL
                                 OR t_xmax::text::bigint < %(horizon)s::bigint)), 0)::int
                                                        AS dead_removable_bytes,
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
                  pg_relation_size(%(r)s::regclass, 'vm')   AS vm_bytes,
                  pg_total_relation_size(%(r)s::regclass)   AS total_bytes""",
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
        "indexes": index_metrics(conn, rel),
        "toast": toast_metrics(conn, rel),
    }


def index_metrics(conn: Connection, rel: str) -> list[dict]:
    """Size and pgstatindex statistics for every index on the table (all B-trees here)."""
    cur = conn.execute(
        """SELECT i.indexrelid::regclass::text AS name,
                  pg_relation_size(i.indexrelid, 'main') AS main_bytes,
                  (pgstatindex(i.indexrelid)).*
           FROM pg_index i WHERE i.indrelid = %s::regclass ORDER BY 1""",
        (rel,),
    )
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def toast_metrics(conn: Connection, rel: str) -> dict | None:
    """Exact statistics for the TOAST table, if the table has one."""
    row = conn.execute(
        """SELECT t.oid::regclass::text AS name,
                  pg_relation_size(t.oid, 'main') AS main_bytes,
                  pg_relation_size(t.oid, 'main') / current_setting('block_size')::int AS main_pages,
                  pg_indexes_size(t.oid) AS index_bytes
           FROM pg_class c JOIN pg_class t ON t.oid = c.reltoastrelid
           WHERE c.oid = %s::regclass""",
        (rel,),
    ).fetchone()
    if row is None:
        return None
    out = dict(zip(("name", "main_bytes", "main_pages", "index_bytes"), row, strict=True))
    out.update(
        {
            f"pgstattuple_{k}": v
            for k, v in _row(conn.execute("SELECT * FROM pgstattuple(%s::regclass)", (out["name"],))).items()
        }
    )
    return out


def fsm_root_avail(conn: Connection, table: str) -> int:
    """The free-space category at the root of the FSM tree, in bytes.

    A search that starts from the root (an inserter with no cached target page)
    descends only through nodes claiming at least the requested space, and the
    upper levels are written by VACUUM, not by the inserters that record leaf
    values. So space recorded since the last VACUUM is invisible to such a
    search until the root says it exists; this is that value. 0 when the fork
    is empty."""
    rel = check_table_name(table)
    return conn.execute(
        """SELECT CASE WHEN pg_relation_size(%(r)s::regclass, 'fsm') = 0 THEN 0
                       ELSE get_byte(get_raw_page(%(r)s::text, 'fsm', 0), 28)
                            * current_setting('block_size')::int / 256 END""",
        {"r": rel},
    ).fetchone()[0]


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


_ITEMS_SQL = f"""
SELECT b.blkno, i.lp, i.lp_len
FROM generate_series(0, (pg_relation_size(%(rel)s::regclass)
                         / current_setting('block_size')::int) - 1) AS b(blkno)
CROSS JOIN LATERAL heap_page_items(get_raw_page(%(rel)s::text, b.blkno::int)) AS i
WHERE i.lp_flags = {LP_NORMAL}
"""


def live_rows(conn: Connection, table: str) -> dict[str, list[int]]:
    """Where every live row's visible version sits: parallel lists of ``id``,
    heap block and stored tuple length (``lp_len``, before MAXALIGN).

    pageinspect lists every normal tuple; a TID scan over those positions then
    lets the server decide MVCC visibility, so each id maps to exactly one
    version even while older versions or HOT chains remain on the page. A TID
    scan does not prune, unlike the sequential scan a plain SELECT would run."""
    rel = check_table_name(table)
    items = {(b, lp): n for b, lp, n in conn.execute(_ITEMS_SQL, {"rel": rel}).fetchall()}
    with conn.transaction():
        # Sequential and index scans both prune on their way to the tuple; only
        # a TID scan does not, and the planner picks one of the others for long lists.
        conn.execute("SET LOCAL enable_seqscan = off; SET LOCAL enable_indexscan = off")
        conn.execute("SET LOCAL enable_bitmapscan = off")
        found = conn.execute(
            sql.SQL("SELECT id, ctid::text FROM {} WHERE ctid = ANY(%s::tid[])").format(sql.Identifier(rel)),
            ([f"({b},{lp})" for b, lp in items],),
        ).fetchall()
    out: dict[str, list[int]] = {"id": [], "blkno": [], "lp_len": []}
    for i, tid in sorted(found):
        b, lp = (int(x) for x in tid.strip("()").split(","))
        out["id"].append(i)
        out["blkno"].append(b)
        out["lp_len"].append(items[b, lp])
    if len(set(out["id"])) != len(out["id"]):
        raise RuntimeError(f"{rel}: a row id is visible in more than one version")
    return out
