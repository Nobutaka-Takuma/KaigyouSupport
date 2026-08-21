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


def connection_hint(url: str, message: str) -> str | None:
    """Turn a connection failure into the thing to actually change.

    Only one case so far, but it is the one that stops people: Supabase's
    direct host has been IPv6-only since early 2024, and most home and office
    connections are IPv4. So the direct host is simply unreachable, and says so
    differently on every platform -- "getaddrinfo failed" on Windows, "Name or
    service not known" on Linux, "network is unreachable" where IPv6 is half
    configured, or nothing useful at all. Matching the wording would work on
    one machine and not the next.

    So this matches the situation instead: a direct Supabase host that we could
    not reach. Authentication errors are excluded, because getting far enough
    to be rejected means the host resolved and the address is not the problem.
    """
    lowered = message.lower()

    # Supavisor answering "tenant or user not found" means the pooler was
    # reached and rejected the username. The project ref is embedded in it
    # (postgres.<ref>), and it has to match both a real project and the region
    # in the hostname -- so a single wrong character, or the right ref against
    # the wrong region's pooler, produces exactly this and nothing else.
    if "tenant" in lowered and "not found" in lowered:
        return (
            "プーラには接続できましたが、ユーザ名のプロジェクトIDが見つかりません。"
            "接続文字列を組み立て直さず、Supabase の Settings → Database → "
            "Connection string → Session pooler に表示されている文字列を"
            "そのままコピーしてください。よくある原因は "
            "(1) postgres.<プロジェクトID> の綴り違い、"
            "(2) ホスト名のリージョン（aws-N-<region>）がプロジェクトのリージョンと不一致、"
            "(3) プロジェクトが一時停止中、の3つです。"
        )

    if ".supabase.co" not in url or "pooler.supabase.com" in url:
        return None
    if any(word in lowered for word in
           ("password", "authentication", "role ", "database ", "permission")):
        return None
    return (
        "Supabase の Direct connection (db.<ref>.supabase.co) は IPv6 専用です。"
        "IPv4 のみの回線からは接続できません。"
        "ダッシュボードの Session pooler "
        "(aws-N-<region>.pooler.supabase.com のポート 5432) に変えてください。"
        "ユーザ名も postgres ではなく postgres.<プロジェクトID> になります。"
    )


@contextmanager
def connect(autocommit: bool = False) -> Iterator[psycopg.Connection]:
    url = dsn()
    try:
        conn = psycopg.connect(
            url,
            row_factory=dict_row,
            autocommit=autocommit,
            prepare_threshold=None if is_pooled(url) else 5,
        )
    except psycopg.OperationalError as exc:
        hint = connection_hint(url, str(exc))
        if hint is None:
            raise
        raise psycopg.OperationalError(f"{exc}\n\nヒント: {hint}") from exc
    try:
        yield conn
    finally:
        conn.close()


def table_exists(conn: psycopg.Connection, table: str) -> bool:
    """Whether a table has been created yet.

    Deployments apply code and migrations at different moments -- a push builds
    within seconds, a migration is run by hand afterwards -- so there is always
    a window where the code knows about a table the database does not have.
    Reads that can happen during that window ask first, and report the dataset
    as unavailable, which the app already knows how to display. Answering "not
    loaded" beats answering 500 to every request until someone runs a command.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) AS t", (f"public.{table}",))
        return cur.fetchone()["t"] is not None


def fetch_all(conn: psycopg.Connection, sql: str, params: Any = None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(conn: psycopg.Connection, sql: str, params: Any = None) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()
