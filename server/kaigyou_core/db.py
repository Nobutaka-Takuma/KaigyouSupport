"""Database access.

Thin wrapper over psycopg 3. There is no ORM by design -- the analysis is
PostGIS SQL, and hiding it behind an object mapper would only obscure it.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Any, Iterable, Iterator
from weakref import WeakKeyDictionary
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

    # Rejected on the password, having got as far as being rejected. The
    # password itself is usually fine and the shell mangled it on the way in:
    # PowerShell expands $ inside a double-quoted string, so a generated
    # password containing one arrives at psycopg with a piece missing (and
    # "$$" is an automatic variable, which substitutes something else
    # entirely). @ : / ? # have their own meaning inside a URL and need
    # percent-encoding. Both produce this one message and nothing else.
    if "password authentication failed" in lowered:
        return (
            "パスワードが違うと言われています。多くはシェルが接続文字列を"
            "書き換えているだけです。PowerShell では**シングルクォート**を"
            "使ってください（\"...\" だと $ が変数として展開されます）:\n"
            "  $env:DATABASE_URL='postgresql://postgres.<ref>:<password>"
            "@aws-N-<region>.pooler.supabase.com:5432/postgres'\n"
            "パスワードに @ : / ? # が含まれる場合は %40 %3A %2F %3F %23 に"
            "置き換えてください。判断がつかないときは Supabase の "
            "Settings → Database → Reset database password で記号の少ない"
            "パスワードに変えるのが確実です。"
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


#: How long a single ETL statement may run, in milliseconds. Scoring a
#: prefecture is minutes of PostGIS work split across many statements, and a
#: managed database sets a timeout meant for web requests -- Supabase cancels
#: at two minutes, which is shorter than the slowest statement here and turns
#: a long job into a job that never finishes. Only the ETL asks for this; the
#: API keeps the server's own limit, because a request that runs for minutes
#: is a bug there rather than a batch.
ETL_STATEMENT_TIMEOUT_MS = 600_000


def relax_statement_timeout(conn: psycopg.Connection,
                            milliseconds: int = ETL_STATEMENT_TIMEOUT_MS) -> bool:
    """Give a batch job room to finish. Reports whether it was allowed.

    Session-level, so it lasts as long as this connection and affects nothing
    else. Managed providers may refuse; a refusal is not fatal -- the work is
    batched to fit inside a short timeout anyway -- so it is reported rather
    than raised.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {int(milliseconds)}")
        if not conn.autocommit:
            conn.commit()
        return True
    except psycopg.Error:
        conn.rollback()
        return False


#: 接続の取得を何回まで試すか、その間隔（秒）。
#:
#: **同時に押し寄せたときだけの話です。** 地図は 1 回動かすたびに層の数だけ
#: 並行に取りにいき（境界・メッシュ・医院・駅・地価・都市計画）、その裏で
#: cron が毎分 worker を叩きます。worker の 1 回は最大 13 分走れるので、
#: 前の回が終わらないうちに次が始まることがあります。関数 1 つが接続 1 つを
#: 持つので、山のてっぺんで接続が取れないことが起こります。
#:
#: 取れなかった 1 回を諦めると、画面には「レイヤーが出ない」「分析が進まない」
#: として出ます。**待てば取れるものを待たずに失敗させる理由がありません。**
#: 待つのは接続を取るところだけで、問い合わせは再実行しません（同じ問い合わせを
#: 二度流すのは、書き込みがあるときに壊れます）。
_CONNECT_ATTEMPTS = 3
_CONNECT_BACKOFF_S = (0.25, 0.75)

#: 待てば直る見込みのある失敗。認証や権限の誤りは待っても直らないので、
#: そちらは 1 回で諦めます——**間違ったパスワードで 3 回試すのは、
#: 遅くなるだけで何も良くなりません。**
_TRANSIENT = (
    "too many clients",
    "too many connections",
    "connection reset",
    "server closed the connection unexpectedly",
    "connection timed out",
    "timeout expired",
    "could not connect to server",
    "remaining connection slots",
    "sorry, too many clients already",
)


def _is_transient(message: str) -> bool:
    lowered = message.lower()
    return any(word in lowered for word in _TRANSIENT)


@contextmanager
def connect(autocommit: bool = False) -> Iterator[psycopg.Connection]:
    url = dsn()
    conn = None
    for attempt in range(_CONNECT_ATTEMPTS):
        try:
            conn = psycopg.connect(
                url,
                row_factory=dict_row,
                autocommit=autocommit,
                prepare_threshold=None if is_pooled(url) else 5,
            )
            break
        except psycopg.OperationalError as exc:
            last = attempt == _CONNECT_ATTEMPTS - 1
            if not last and _is_transient(str(exc)):
                time.sleep(_CONNECT_BACKOFF_S[attempt])
                continue
            hint = connection_hint(url, str(exc))
            if hint is None:
                raise
            raise psycopg.OperationalError(f"{exc}\n\nヒント: {hint}") from exc
    assert conn is not None      # ループは接続を得るか送出するかのどちらか
    try:
        yield conn
    finally:
        conn.close()


#: スキーマの有無の答えを、**その接続が生きている間だけ**覚えておきます。
#:
#: 同じ表・同じ列の有無を、1 リクエストの中で何十回も訊いていました。実測：
#: ``/api/dataset`` の 114 往復のうち **35 往復がこれ**でした（mesh_scores の
#: 列の有無 35 回、うち facility_category だけで 15 回）。手元では 1 往復が
#: 0.2ms（unix ソケット）なので誰も気づきませんが、**Vercel から Supabase へは
#: 1 往復が 10〜30ms** です。35 往復で 0.4〜1.0 秒、地図をクリックするたびに。
#:
#: 覚えるのは接続ごとです。接続は API ではリクエストごと、worker ではジョブ
#: ごとに開き直すので、**この記憶は次のリクエストまで生き延びません。**
#: マイグレーションを当てた直後の 1 リクエストが古い答えを見ることはあっても、
#: 次のリクエストは新しい接続で訊き直します。デプロイの窓（コードが先、
#: マイグレーションが後）を守るという、この関数の本来の目的は変わりません。
_SCHEMA_CACHE: "WeakKeyDictionary[psycopg.Connection, dict[Any, bool]]" = (
    WeakKeyDictionary())


def forget_schema(conn: psycopg.Connection) -> None:
    """この接続で覚えたスキーマの答えを捨てる。

    同じ接続でマイグレーションを当てたときのため（`kaigyou-etl migrate` は
    1 接続の中で当ててから、当てた表を読みます）。
    """
    _SCHEMA_CACHE.pop(conn, None)


def _remembered(conn: psycopg.Connection, key: Any, ask: Any) -> bool:
    try:
        known = _SCHEMA_CACHE.setdefault(conn, {})
    except TypeError:      # 参照を保持できない接続（テストの偽物など）
        return ask()
    if key not in known:
        known[key] = ask()
    return known[key]


def columns_that_exist(conn: psycopg.Connection, table: str,
                       columns: Iterable[str]) -> set[str]:
    """この表に実在する列を、**1 往復で**まとめて返す。

    ``column_exists`` を 14 個の列に対して呼ぶと 14 往復です。列が違うので
    接続ごとの記憶も効きません。**問いが 14 個あることと、往復が 14 回必要な
    ことは別**なので、まとめて訊きます（実測：``/api/dataset`` の mesh_scores
    の列チェックが 14 往復 → 1 往復）。

    答えは ``column_exists`` と同じ記憶に入れます。あとから 1 個ずつ訊かれても
    往復しません。
    """
    wanted = sorted(set(columns))
    if not wanted:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
              AND column_name = ANY(%s)
            """, (table, wanted))
        found = {r["column_name"] for r in cur.fetchall()}
    try:
        known = _SCHEMA_CACHE.setdefault(conn, {})
        for column in wanted:
            known[("column", table, column)] = column in found
    except TypeError:
        pass
    return found


def tables_that_exist(conn: psycopg.Connection,
                      tables: Iterable[str]) -> set[str]:
    """実在する表を、**1 往復で**まとめて返す（``columns_that_exist`` と同じ理由）。"""
    wanted = sorted(set(tables))
    if not wanted:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ANY(%s)", (wanted,))
        found = {r["table_name"] for r in cur.fetchall()}
    try:
        known = _SCHEMA_CACHE.setdefault(conn, {})
        for table in wanted:
            known[("table", table)] = table in found
    except TypeError:
        pass
    return found


def column_exists(conn: psycopg.Connection, table: str, column: str) -> bool:
    """Whether a column has been added yet.

    Same deploy window as :func:`table_exists`, one level finer: a release that
    adds a column to an existing table reaches the running code before the
    migration reaches the database, and a SELECT naming the missing column
    fails the whole request rather than the one figure it feeds.

    答えは接続ごとに覚えます（``_SCHEMA_CACHE``）。**同じ問いを 1 リクエスト
    の中で何十回も往復させないため**です。
    """
    def ask() -> bool:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) AS n FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                  AND column_name = %s
                """, (table, column))
            return cur.fetchone()["n"] > 0

    return _remembered(conn, ("column", table, column), ask)


def table_exists(conn: psycopg.Connection, table: str) -> bool:
    """Whether a table has been created yet.

    Deployments apply code and migrations at different moments -- a push builds
    within seconds, a migration is run by hand afterwards -- so there is always
    a window where the code knows about a table the database does not have.
    Reads that can happen during that window ask first, and report the dataset
    as unavailable, which the app already knows how to display. Answering "not
    loaded" beats answering 500 to every request until someone runs a command.

    答えは接続ごとに覚えます（``column_exists`` と同じ理由）。
    """
    def ask() -> bool:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s) AS t", (f"public.{table}",))
            return cur.fetchone()["t"] is not None

    return _remembered(conn, ("table", table), ask)


def fetch_all(conn: psycopg.Connection, sql: str, params: Any = None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(conn: psycopg.Connection, sql: str, params: Any = None) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()
