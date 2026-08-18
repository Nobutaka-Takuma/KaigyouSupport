"""Database access.

Thin wrapper over psycopg 3. There is no ORM by design -- the analysis is
PostGIS SQL, and hiding it behind an object mapper would only obscure it.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row


def dsn() -> str:
    return os.getenv(
        "DATABASE_URL",
        "postgresql://kaigyou:kaigyou@127.0.0.1:5432/kaigyou",
    )


@contextmanager
def connect(autocommit: bool = False) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(dsn(), row_factory=dict_row, autocommit=autocommit)
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
