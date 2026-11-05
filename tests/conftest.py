"""Shared fixtures: each comparison suite runs once per test session at 2000 rows."""

from __future__ import annotations

import psycopg
import pytest

from pagereuse import db
from pagereuse.experiment import run
from pagereuse.suites import SUITES

ROWS = 2000


@pytest.fixture(scope="session", autouse=True)
def lab_db():
    try:
        db.connect().close()
    except psycopg.OperationalError as e:
        pytest.fail(f"lab PostgreSQL not reachable at {db.dsn()}: run `docker compose up -d --wait`\n{e}")


def _suite(name):
    return {cfg.label: run(cfg) for cfg in SUITES[name](ROWS)}


@pytest.fixture(scope="session")
def ff():
    return _suite("fillfactor")


@pytest.fixture(scope="session")
def upd():
    return _suite("updates")


@pytest.fixture(scope="session")
def cyc():
    return _suite("cycles")


@pytest.fixture(scope="session")
def snap():
    return _suite("snapshots")


@pytest.fixture(scope="session")
def conc():
    return _suite("concurrency")
