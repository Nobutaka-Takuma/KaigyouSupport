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

    _ensure_optional_functions(conn)
    return run


#: Migrations whose body is conditional on an extension being present. They are
#: re-applied on every migrate, because "already applied" and "actually did
#: something" are different questions for them.
_CONDITIONAL = ("009_walk_network.sql", "010_walk_network_noding.sql",
                "011_catchment_mode.sql", "032_walk_catchment_speed.sql",
                "033_walk_catchment_chunked.sql")


def _ensure_optional_functions(conn: psycopg.Connection) -> None:
    """Re-apply the pgRouting-conditional migrations once the extension exists.

    Those migrations check for pgrouting and skip the routing function when it
    is absent -- which is right, because migrate has to succeed on a database
    that does not have it. But they are then recorded as applied, so enabling
    pgrouting afterwards leaves the function permanently missing and the app
    reporting that walking catchments are unavailable on a database that could
    do them perfectly well. Ordering the two steps correctly is not something a
    reader should have to know.

    Every statement in them is CREATE OR REPLACE or IF NOT EXISTS, so running
    them again costs a moment and changes nothing when the function is already
    there.

    Everything *after* them is replayed too, and that is not incidental. 011
    defines kg_analyze_point; 014 redefines it with more columns. Replaying 011
    alone silently reverts 014 -- the function loses the columns, the analysis
    starts answering None for them, and nothing reports an error because the
    older definition is perfectly valid SQL. Whatever defined an object last
    has to be what defines it after a repair, so the replay runs in file order
    to the end.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM pg_extension WHERE extname = 'pgrouting'")
        if not cur.fetchone()["n"]:
            return
        cur.execute("SELECT to_regproc('kg_walk_catchment') AS fn")
        if cur.fetchone()["fn"] is not None:
            return

    first = _CONDITIONAL[0]
    for path in sorted(migrations_dir().glob("*.sql")):
        if path.name < first:
            continue
        with conn.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
        conn.commit()
