"""Connection handling and safety guards.

Every destructive statement in this project goes through ``check_table_name``
and a connection whose database is verified to be the dedicated lab database.
"""

from __future__ import annotations

import os
import re

import psycopg

DEFAULT_DSN = "postgresql://pagereuse:pagereuse@127.0.0.1:55432/pagereuse_lab"
LAB_DATABASE = "pagereuse_lab"
TABLE_PREFIX = "prl_"
_TABLE_RE = re.compile(r"^prl_[a-z0-9_]{1,50}$")
EXTENSIONS = ("pgstattuple", "pg_freespacemap", "pageinspect")


class SafetyError(RuntimeError):
    """Raised when an operation could touch something outside the lab sandbox."""


def dsn() -> str:
    return os.environ.get("PAGEREUSE_DSN", DEFAULT_DSN)


def check_table_name(name: str) -> str:
    if not _TABLE_RE.fullmatch(name):
        raise SafetyError(
            f"refusing to operate on {name!r}: experiment tables must match {_TABLE_RE.pattern}"
        )
    return name


def connect(conninfo: str | None = None) -> psycopg.Connection:
    """Open an autocommit connection to the lab database and verify where we are.

    Autocommit is required: VACUUM cannot run inside a transaction block, and
    long-lived transactions would hold back the xmin horizon and stop VACUUM
    from removing the dead tuples we are trying to observe.
    """
    conn = psycopg.connect(conninfo or dsn(), autocommit=True)
    db = conn.execute("SELECT current_database()").fetchone()[0]
    if db != LAB_DATABASE:
        conn.close()
        raise SafetyError(f"connected to database {db!r}, expected {LAB_DATABASE!r}")
    for ext in EXTENSIONS:
        conn.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
    return conn
