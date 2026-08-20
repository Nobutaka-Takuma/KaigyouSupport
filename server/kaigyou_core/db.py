"""Database access.

Thin wrapper over psycopg 3. There is no ORM by design -- the analysis is
PostGIS SQL, and hiding it behind an object mapper would only obscure it.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row

#: Hosted Postgres in front of a transaction-mode pooler. PgBouncer hands each
#: transaction whichever backend is free, so a statement prepared on one
#: connection is missing from the next: ``prepared statement "_pg3_0" does not
#: exist``. Turning preparation off costs a little planning time and makes the
#: pooler usable, which is what a serverless deployment needs.
_POOLER_PORTS = {"6543"}
_POOLER_HINTS = ("pgbouncer=true", "pooler.supabase.com")


def dsn() -> str:
    return os.getenv(
        "DATABASE_URL",
        "postgresql://kaigyou:kaigyou@127.0.0.1:5432/kaigyou",
    )


def is_pooled(url: str | None = None) -> bool:
    """Whether this DSN goes through a transaction-mode connection pooler.

    Explicit ``KAIGYOU_DB_PREPARE=off`` wins; otherwise the port and host are
    the tell. Getting this wrong in the safe direction only loses a little
    speed, so the detection errs towards assuming a pooler.
    """
    override = os.getenv("KAIGYOU_DB_PREPARE")
    if override is not None:
        return override.strip().lower() in {"off", "0", "false", "no"}

    url = url or dsn()
    lowered = url.lower()
    if any(hint in lowered for hint in _POOLER_HINTS):
        return True
    try:
        return (urlsplit(url).port and str(urlsplit(url).port) in _POOLER_PORTS) or False
    except ValueError:
        return False


@contextmanager
def connect(autocommit: bool = False) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(
        dsn(),
        row_factory=dict_row,
        autocommit=autocommit,
        prepare_threshold=None if is_pooled() else 5,
    )
    try:
        yield conn
    finally:
        conn.close()


def fetch_all(conn: psycopg.Connection, sql: str, params: Any = None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(conn: psycopg.Connection, sql: str, params: Any = None) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()
