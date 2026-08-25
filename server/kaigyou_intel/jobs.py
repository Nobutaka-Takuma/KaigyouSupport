"""Job と Step の記録。

要件 §32：ステップは独立して保存し、失敗したステップから再実行できること。
最初からやり直させると、Web検索の費用も待ち時間ももう一度かかります。

状態は DB が持ちます。worker はプロセスであって記憶ではないので、途中で
落ちても次の worker が続きから拾えなければ意味がありません。
"""
from __future__ import annotations

import json
from typing import Any, Mapping

import psycopg
from psycopg.types.json import Json

STEP_NAMES = {
    1: "商圏特徴抽出",
    2: "外部コンテクスト調査",
    3: "需要形成・患者分析",
    4: "経営判断・レポート生成",
}


def create_job(conn: psycopg.Connection, *, lat: float, lng: float, radius_m: int,
               dataset: Mapping[str, Any], base_hash: str,
               business_type: str = "dental_clinic",
               location_name: str | None = None,
               profile: str | None = None,
               user_id: str | None = None) -> str:
    """Job と、その 4 ステップの空枠を作る。

    ステップの行を最初に作るのは、進捗を「まだ無い」ではなく「pending」として
    表示できるようにするためです。行が無い状態だと、UI は「これから作られる」と
    「作られなかった」を区別できません。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO analysis_jobs (
                user_id, business_type, location_name, latitude, longitude,
                radius_m, profile, base_data, base_data_hash, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'queued')
            RETURNING id
            """,
            (user_id, business_type, location_name, lat, lng, radius_m, profile,
             Json(dataset), base_hash))
        job_id = str(cur.fetchone()["id"])
        for number, name in STEP_NAMES.items():
            cur.execute(
                "INSERT INTO analysis_steps (job_id, step_number, step_name, status) "
                "VALUES (%s, %s, %s, 'pending')", (job_id, number, name))
    conn.commit()
    return job_id


def get_job(conn: psycopg.Connection, job_id: str,
            include_base_data: bool = False) -> dict[str, Any] | None:
    columns = ("id, user_id, business_type, location_name, latitude, longitude, "
               "radius_m, profile, base_data_hash, status, current_step, "
               "error_message, created_at, started_at, completed_at")
    if include_base_data:
        columns += ", base_data"
    with conn.cursor() as cur:
        cur.execute(f"SELECT {columns} FROM analysis_jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def get_steps(conn: psycopg.Connection, job_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT step_number, step_name, status, output_json, error_message,
                   started_at, completed_at, prompt_version, model,
                   input_tokens, output_tokens, web_searches
            FROM analysis_steps WHERE job_id = %s ORDER BY step_number
            """, (job_id,))
        return [dict(r) for r in cur.fetchall()]


def step_output(conn: psycopg.Connection, job_id: str, number: int) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT output_json FROM analysis_steps "
            "WHERE job_id = %s AND step_number = %s AND status = 'completed'",
            (job_id, number))
        row = cur.fetchone()
    return row["output_json"] if row else None


def next_step(conn: psycopg.Connection, job_id: str) -> int | None:
    """次に実行すべきステップ番号。順序は飛ばしません。

    Step2 が失敗している状態で Step3 を走らせても、Step3 は外部事実を
    受け取れません。空の入力で「分析しました」と言われるほうが、
    止まっているより悪い。
    """
    for step in get_steps(conn, job_id):
        if step["status"] == "completed":
            continue
        return int(step["step_number"])
    return None


def claim_job(conn: psycopg.Connection) -> str | None:
    """待っている Job を 1 件つかむ。

    ``FOR UPDATE SKIP LOCKED`` は worker を 2 つ動かしたときの保険です。
    同じ Job を 2 回分析すると、費用が 2 倍かかって結果は 1 つしか残りません。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE analysis_jobs SET status = 'running',
                   started_at = COALESCE(started_at, now())
            WHERE id = (
                SELECT id FROM analysis_jobs
                WHERE status = 'queued'
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id
            """)
        row = cur.fetchone()
    conn.commit()
    return str(row["id"]) if row else None


def claim_specific(conn: psycopg.Connection, job_id: str) -> str | None:
    """この Job を、状態にかかわらず動かす。

    ``claim_job`` は queued しか拾いません。それは worker の自動運転として
    正しいのですが、止まった Job を人が再開する手段が無いと詰みます。
    失敗した Job を勝手に拾い直すと、壊れたまま何度も課金されるので、
    「人が id を指定したときだけ」という形にしてあります。
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE analysis_jobs SET status = 'running', error_message = NULL, "
            "started_at = COALESCE(started_at, now()) "
            "WHERE id = %s AND status <> 'cancelled' RETURNING id", (job_id,))
        row = cur.fetchone()
    conn.commit()
    return str(row["id"]) if row else None


def release_job(conn: psycopg.Connection, job_id: str, outcome: str,
                message: str | None = None) -> None:
    """走り終わった Job の状態を戻す。

    これが無いと Job は running のまま残り、``claim_job`` は queued しか
    見ないので二度と拾われません。実測：銀座のジョブが running のまま残り、
    ``analyze --once`` が「0 件を処理しました」と言い続けました。

    ``blocked``（未実装のステップに当たった）は queued に戻します。失敗では
    ないので、そのステップを実装した次の実行がそのまま続きから拾います。
    ``failed`` は failed のままにします。壊れたまま自動で拾い直すと、
    同じ失敗に何度も課金されます。
    """
    status = {"completed": "completed", "blocked": "queued"}.get(outcome, "failed")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE analysis_jobs SET status = %s, error_message = %s, "
            "completed_at = CASE WHEN %s = 'completed' THEN now() ELSE completed_at END "
            "WHERE id = %s",
            (status, (message or "")[:4000] or None, status, job_id))
    conn.commit()


def start_step(conn: psycopg.Connection, job_id: str, number: int,
               payload: Mapping[str, Any], settings: Mapping[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE analysis_steps
            SET status = 'running', started_at = now(), completed_at = NULL,
                error_message = NULL, input_json = %s,
                prompt_version = %s, model = %s
            WHERE job_id = %s AND step_number = %s
            """,
            (Json(payload), settings.get("prompt_version"), settings.get("model"),
             job_id, number))
        cur.execute(
            "UPDATE analysis_jobs SET status = 'running', current_step = %s, "
            "started_at = COALESCE(started_at, now()) WHERE id = %s",
            (number, job_id))
    conn.commit()


def finish_step(conn: psycopg.Connection, job_id: str, number: int,
                output: Mapping[str, Any], usage: Mapping[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE analysis_steps
            SET status = 'completed', completed_at = now(), output_json = %s,
                input_tokens = %s, output_tokens = %s, web_searches = %s
            WHERE job_id = %s AND step_number = %s
            """,
            (Json(output), usage.get("input_tokens"), usage.get("output_tokens"),
             usage.get("web_searches"), job_id, number))
        if number >= max(STEP_NAMES):
            cur.execute(
                "UPDATE analysis_jobs SET status = 'completed', completed_at = now() "
                "WHERE id = %s", (job_id,))
    conn.commit()


def fail_step(conn: psycopg.Connection, job_id: str, number: int, message: str) -> None:
    """失敗を記録する。Job は failed にしますが、済んだステップは消しません。

    要件 §32 の「Step2 から再実行」は、Step1 の出力が残っていて初めて成立します。
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE analysis_steps SET status = 'failed', completed_at = now(), "
            "error_message = %s WHERE job_id = %s AND step_number = %s",
            (message[:4000], job_id, number))
        cur.execute(
            "UPDATE analysis_jobs SET status = 'failed', error_message = %s WHERE id = %s",
            (message[:4000], job_id))
    conn.commit()


def reset_step(conn: psycopg.Connection, job_id: str, number: int) -> None:
    """このステップと、それ以降を pending に戻す。

    後続だけを残すと、古い前提の上に新しい結論が乗ります。Step2 をやり直したら
    Step3・Step4 も作り直すのが筋です。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE analysis_steps
            SET status = 'pending', output_json = NULL, error_message = NULL,
                started_at = NULL, completed_at = NULL
            WHERE job_id = %s AND step_number >= %s
            """, (job_id, number))
        cur.execute("DELETE FROM analysis_reports WHERE job_id = %s", (job_id,))
        cur.execute(
            "UPDATE analysis_jobs SET status = 'queued', error_message = NULL, "
            "current_step = %s, completed_at = NULL WHERE id = %s",
            (number - 1, job_id))
    conn.commit()


def save_sources(conn: psycopg.Connection, job_id: str, number: int,
                 pattern_id: str | None, sources: list[Mapping[str, Any]]) -> int:
    """外部出典を行として保存する（要件 §29）。

    本文に URL を書き込むだけだと、後から「この主張の出典は」を機械的に
    辿れません。表にしておけば §25 の追跡が SQL で書けます。
    """
    if not sources:
        return 0
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM analysis_steps WHERE job_id = %s AND step_number = %s",
                    (job_id, number))
        row = cur.fetchone()
        step_id = row["id"] if row else None
        stored = 0
        for source in sources:
            url = source.get("url")
            if not url:
                continue
            cur.execute(
                """
                INSERT INTO analysis_sources (
                    job_id, step_id, pattern_id, url, title, source_type, content
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (job_id, step_id, pattern_id or source.get("pattern_id"), url,
                 source.get("title"), classify_source(url), source.get("content")))
            stored += 1
    conn.commit()
    return stored


def classify_source(url: str) -> str:
    """URL を要件 §9 の情報源区分に写す。

    設定の domains を上から順に見て、最初に当たった区分。当たらなければ other。
    LLM に分類させないのは、これが機械的に決まることだからです。
    """
    from kaigyou_core import config as cfg

    host = url.split("//", 1)[-1].split("/", 1)[0].lower()
    for entry in (cfg.analysis_config().get("search") or {}).get("source_types") or []:
        for pattern in entry.get("domains") or []:
            if _host_matches(host, str(pattern)):
                return str(entry.get("type", "other"))
    return "other"


def _host_matches(host: str, pattern: str) -> bool:
    """``pref.*.jp`` のような 1 段のワイルドカードに対応する。"""
    if "*" not in pattern:
        return host == pattern or host.endswith("." + pattern)
    head, _, tail = pattern.partition("*")
    parts = host.split(".")
    for i in range(len(parts)):
        candidate = ".".join(parts[i:])
        if candidate.startswith(head) and candidate.endswith(tail.lstrip(".")):
            return True
    return False
