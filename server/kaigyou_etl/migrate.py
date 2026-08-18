"""Apply the SQL migrations in ``db/migrations`` in filename order."""
from __future__ import annotations

from pathlib import Path

import psycopg

from kaigyou_core import config as cfg


def migrations_dir() -> Path:
    return cfg.repo_root() / "db" / "migrations"


def applied(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('public.schema_migrations') IS NOT NULL AS present"
        )
        if not cur.fetchone()["present"]:
            return set()
        cur.execute("SELECT filename FROM schema_migrations")
        return {r["filename"] for r in cur.fetchall()}


def migrate(conn: psycopg.Connection, *, force: bool = False) -> list[str]:
    done = set() if force else applied(conn)
    run: list[str] = []
    for path in sorted(migrations_dir().glob("*.sql")):
        if path.name in done:
            continue
        with conn.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
            cur.execute(
                """
                INSERT INTO schema_migrations (filename) VALUES (%s)
                ON CONFLICT (filename) DO NOTHING
                """,
                (path.name,),
            )
        conn.commit()
        run.append(path.name)
    return run
