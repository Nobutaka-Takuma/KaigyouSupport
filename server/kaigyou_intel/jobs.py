"""Job と Step の記録。

要件 §32：ステップは独立して保存し、失敗したステップから再実行できること。
最初からやり直させると、Web検索の費用も待ち時間ももう一度かかります。

状態は DB が持ちます。worker はプロセスであって記憶ではないので、途中で
落ちても次の worker が続きから拾えなければ意味がありません。
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

import psycopg

from kaigyou_core.analysis import DEFAULT_CATEGORY
from psycopg.types.json import Json

STEP_NAMES = {
    1: "商圏特徴抽出",
    2: "外部コンテクスト調査",
    # 需要形成の分析と経営判断は同じ段です。分けていた頃は、判断の段がタグ付きの
    # 10章レポートも書き、最終段がそれを散文に書き直していました。読み手に届く
    # のは散文だけなので、タグ付きのほうは捨てるために書いていたことになります。
    3: "需要形成・患者分析と経営判断",
    # STEP3 までは根拠を辿れる形（タグと id）で作ります。それは検算のための
    # 形であって、人が読むための形ではありません。顧客に渡す文書は、そこから
    # 起こし直します。
    4: "顧客提出用レポート",
}

#: 競合分析の段（開発指示書「地域競合分析AI MVP」）。
#:
#: **周辺一般の分析とは段の数も中身も違います。** 地域について広く調べる
#: 代わりに、競合 1 院ずつを深く調べます。集計（§4）とポジショニングマップ
#: （§5）は Python がやるので、LLM の段は 2 つです。
COMPETITOR_STEP_NAMES = {
    1: "競合の調査",
    2: "競争環境の要約",
}

#: 種類ごとの段の名前。
STEPS_BY_KIND = {"area": STEP_NAMES, "competitors": COMPETITOR_STEP_NAMES}

#: 既定の種類。既存の行はこれです（マイグレーション 036 の DEFAULT）。
DEFAULT_KIND = "area"


def steps_for(kind: str | None) -> dict[int, str]:
    """その種類の段の構成。知らない種類は周辺一般として扱います。"""
    return STEPS_BY_KIND.get(kind or DEFAULT_KIND, STEP_NAMES)


def create_job(conn: psycopg.Connection, *, lat: float, lng: float, radius_m: int,
               dataset: Mapping[str, Any], base_hash: str,
               business_type: str = DEFAULT_CATEGORY,
               location_name: str | None = None,
               profile: str | None = None,
               user_id: str | None = None,
               analysis_kind: str = DEFAULT_KIND) -> str:
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
                radius_m, profile, base_data, base_data_hash, status,
                analysis_kind
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'queued', %s)
            RETURNING id
            """,
            (user_id, business_type, location_name, lat, lng, radius_m, profile,
             Json(dataset), base_hash, analysis_kind))
        job_id = str(cur.fetchone()["id"])
        for number, name in steps_for(analysis_kind).items():
            cur.execute(
                "INSERT INTO analysis_steps (job_id, step_number, step_name, status) "
                "VALUES (%s, %s, %s, 'pending')", (job_id, number, name))
    conn.commit()
    return job_id


def kind_of(conn: psycopg.Connection, job_id: str) -> str:
    """このジョブの分析の種類。

    段の構成がこれで決まります。**列が無い環境では周辺一般として扱います**
    （デプロイとマイグレーションの間の窓。列が無いのは「まだ当てていない」で
    あって、種類が無いわけではありません）。
    """
    from kaigyou_core.db import column_exists

    if not column_exists(conn, "analysis_jobs", "analysis_kind"):
        return DEFAULT_KIND
    with conn.cursor() as cur:
        cur.execute("SELECT analysis_kind FROM analysis_jobs WHERE id = %s",
                    (job_id,))
        row = cur.fetchone()
    return (row["analysis_kind"] if row else None) or DEFAULT_KIND


def get_job(conn: psycopg.Connection, job_id: str,
            include_base_data: bool = False) -> dict[str, Any] | None:
    """Job 1 件。**種類の列は、無い環境では黙って外します。**

    kind_of が列の有無を見ているのに、ここが素で SELECT していると、
    マイグレーションを当てる前のデプロイでは kind_of に届く前に落ちます
    （デプロイとマイグレーションの間の窓）。守るなら両方です。
    """
    from kaigyou_core.db import column_exists

    columns = ["id", "user_id", "business_type", "location_name",
               "latitude", "longitude", "radius_m", "profile", "base_data_hash",
               "status", "current_step", "error_message", "created_at",
               "started_at", "completed_at"]
    if column_exists(conn, "analysis_jobs", "analysis_kind"):
        columns.insert(3, "analysis_kind")
    if include_base_data:
        columns.append("base_data")
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(columns)} FROM analysis_jobs WHERE id = %s",
                    (job_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def ensure_steps(conn: psycopg.Connection, job_id: str) -> int:
    """ステップの行を、いまの段構成に合わせる。

    段を増やしたとき、既にある Job には行がありません。行が無いと next_step は
    「全部終わった」と読み、増やした段が黙って飛ばされます。worker の起動時に
    ここを通すので、作り直さなくても続きから流れます。

    **減らしたときも同じだけ困ります。** 無くなった段の行が pending のまま
    残ると、next_step はその番号を返し、走らせる実装が無いのでジョブはそこで
    永久に止まります。画面には「順番待ち」とだけ出ます。だから、いま存在
    しない段の行は落とします。済んだ段（completed）は触りません（要件 §32）。
    """
    names = steps_for(kind_of(conn, job_id))
    with conn.cursor() as cur:
        cur.execute("SELECT step_number FROM analysis_steps WHERE job_id = %s",
                    (job_id,))
        have = {int(r["step_number"]) for r in cur.fetchall()}
        obsolete = sorted(n for n in have if n not in names)
        missing = [n for n in names if n not in have]
        for number in missing:
            cur.execute(
                "INSERT INTO analysis_steps (job_id, step_number, step_name, status) "
                "VALUES (%s, %s, %s, 'pending')", (job_id, number, names[number]))
        if obsolete:
            cur.execute("DELETE FROM analysis_steps "
                        "WHERE job_id = %s AND step_number = ANY(%s)",
                        (job_id, obsolete))
        if missing or obsolete:
            cur.execute(
                "UPDATE analysis_jobs SET status = 'queued', completed_at = NULL "
                "WHERE id = %s AND status = 'completed'", (job_id,))
    conn.commit()
    return len(missing) + len(obsolete)


def get_steps(conn: psycopg.Connection, job_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT step_number, step_name, status, output_json, error_message,
                   started_at, completed_at, prompt_version, model, attempts,
                   input_tokens, output_tokens, web_searches,
                   cache_read_tokens, cache_write_tokens
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
    names = steps_for(kind_of(conn, job_id))
    for step in get_steps(conn, job_id):
        number = int(step["step_number"])
        # いまは存在しない段の行（段構成を変える前に作られた Job）。走らせる
        # 実装が無いので、返すとそこで止まります。ensure_steps が消しますが、
        # そちらを通らない経路もあるのでここでも飛ばします。
        if number not in names:
            continue
        if step["status"] == "completed":
            continue
        return number
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


def recover_stale(conn: psycopg.Connection, minutes: float) -> list[str]:
    """途中で消えた実行を、待ち行列に戻す。

    ホスティングされた関数には実行時間の上限があります。上限に当たって
    関数が消えると、Job は running、ステップは running のまま誰も動かしません。
    手元の worker では滅多に起きませんでしたが、関数で回すなら必ず起きます。

    「実行中のステップが N 分以上前に始まっている」を目印にします。1 ステップの
    実測は最長でも 10 分前後なので、それより余裕を見た値を設定に置きます。
    済んだステップは触りません（要件 §32）。
    """
    with conn.cursor() as cur:
        # 消えた実行も**1回の試行として数えます**。数えないと、上限を超える
        # ステップ（Hobby の 300 秒に収まらない STEP2 など）が、記録も残さず
        # 費用だけ増やしながら永久に回り続けます。関数が強制終了されるときは
        # 例外が飛ばないので、_handle_failure を通らず attempts が増えません。
        # 表に出る症状は「経過時間のカウンタが時々0に戻る」だけでした。
        cur.execute(
            """
            UPDATE analysis_steps
            SET status = 'pending', started_at = NULL, attempts = attempts + 1,
                error_message = '実行が最後まで終わりませんでした'
                                '（関数の実行時間の上限に当たった可能性があります）。'
            WHERE status = 'running' AND started_at < now() - make_interval(secs => %s)
            RETURNING job_id
            """, (minutes * 60.0,))
        job_ids = sorted({str(r["job_id"]) for r in cur.fetchall()})
        if job_ids:
            cur.execute(
                "UPDATE analysis_jobs SET status = 'queued', started_at = NULL "
                "WHERE id = ANY(%s) AND status = 'running'", (job_ids,))
        # ステップは動いていないのに Job だけ running で残っている場合も戻します
        # （ステップを始める前に関数が消えたとき）。
        cur.execute(
            """
            UPDATE analysis_jobs SET status = 'queued', started_at = NULL
            WHERE status = 'running' AND started_at < now() - make_interval(secs => %s)
              AND NOT EXISTS (SELECT 1 FROM analysis_steps s
                              WHERE s.job_id = analysis_jobs.id AND s.status = 'running')
            RETURNING id
            """, (minutes * 60.0,))
        job_ids = sorted(set(job_ids) | {str(r["id"]) for r in cur.fetchall()})
    conn.commit()
    return job_ids


def release_job(conn: psycopg.Connection, job_id: str, outcome: str,
                message: str | None = None) -> None:
    """走り終わった Job の状態を戻す。

    これが無いと Job は running のまま残り、``claim_job`` は queued しか
    見ないので二度と拾われません。実測：銀座のジョブが running のまま残り、
    ``analyze --once`` が「0 件を処理しました」と言い続けました。

    ``blocked``（未実装のステップに当たった）は失敗ではありませんが、queued にも
    戻しません。worker は queued を古い順に拾うので、戻すと同じ Job を拾っては
    同じところで止まる、を繰り返します（``--poll`` なら 5 秒ごと）。blocked の
    Job は ``requeue_unblocked`` が、そのステップの実装後に自動で戻します。

    ``failed`` は failed のままにします。壊れたまま自動で拾い直すと、
    同じ失敗に何度も課金されます。
    """
    status = {"completed": "completed", "blocked": "blocked",
              "queued": "queued"}.get(outcome, "failed")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE analysis_jobs SET status = %s, error_message = %s, "
            "completed_at = CASE WHEN %s = 'completed' THEN now() ELSE completed_at END "
            "WHERE id = %s",
            (status, (message or "")[:4000] or None, status, job_id))
    conn.commit()


def requeue_unblocked(conn: psycopg.Connection, implemented: Iterable[int]) -> int:
    """止まっていたステップが実装済みになった Job を、待ち行列に戻す。

    worker の起動時に呼びます。実装したあとに「どのジョブを再開すればいいか」を
    人が思い出す必要は無いはずです。次のステップがまだ未実装のものは、
    blocked のままにします。
    """
    numbers = sorted(set(implemented))
    if not numbers:
        return 0
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM analysis_jobs WHERE status = 'blocked'")
        candidates = [str(r["id"]) for r in cur.fetchall()]
        moved = [job_id for job_id in candidates
                 if next_step(conn, job_id) in numbers]
        if moved:
            cur.execute(
                "UPDATE analysis_jobs SET status = 'queued', error_message = NULL "
                "WHERE id = ANY(%s)", (moved,))
    conn.commit()
    return len(moved)


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
                input_tokens = %s, output_tokens = %s, web_searches = %s,
                cache_read_tokens = %s, cache_write_tokens = %s
            WHERE job_id = %s AND step_number = %s
            """,
            (Json(output), usage.get("input_tokens"), usage.get("output_tokens"),
             usage.get("web_searches"), usage.get("cache_read_tokens"),
             usage.get("cache_write_tokens"), job_id, number))
        if number >= max(steps_for(kind_of(conn, job_id))):
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


def retry_step(conn: psycopg.Connection, job_id: str, number: int,
               message: str) -> int:
    """やり直せる失敗として記録し、そのステップを pending に戻す。

    ``fail_step`` との違いは、Job を failed にしないことです。queued のまま
    なので、次の呼び出しがそのまま拾い直します。人がボタンを押しに行く必要は
    ありません。回数は数えておいて、いつまでも繰り返さないようにします。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE analysis_steps
            SET status = 'pending', attempts = attempts + 1,
                error_message = %s, started_at = NULL
            WHERE job_id = %s AND step_number = %s
            RETURNING attempts
            """, (message[:4000], job_id, number))
        row = cur.fetchone()
    conn.commit()
    return int(row["attempts"]) if row else 0


def last_error(conn: psycopg.Connection, job_id: str, number: int) -> str | None:
    """このステップが前回どう失敗したか。

    やり直しのときにモデルへ渡します。同じプロンプトを投げ直すだけでは、
    **決定的な失敗は何度やっても同じところで落ちます**。実測：STEP5 が
    「1.5万」を書いて検算に落ち、2回やり直して2回とも同じ数字を書きました。
    理由を見せれば、その1点だけ直して書き直せます。

    ``start_step`` が実行のたびに消すので、ここで読めるのは前回ぶんだけです。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT error_message FROM analysis_steps "
                    "WHERE job_id = %s AND step_number = %s", (job_id, number))
        row = cur.fetchone()
    return (row["error_message"] or None) if row else None


def attempts_for(conn: psycopg.Connection, job_id: str, number: int) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT attempts FROM analysis_steps "
                    "WHERE job_id = %s AND step_number = %s", (job_id, number))
        row = cur.fetchone()
    return int(row["attempts"]) if row else 0


def reset_step(conn: psycopg.Connection, job_id: str, number: int) -> None:
    """このステップと、それ以降を pending に戻す。

    後続だけを残すと、古い前提の上に新しい結論が乗ります。Step2 をやり直したら
    Step3・Step4 も作り直すのが筋です。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE analysis_steps
            SET status = 'pending', output_json = NULL,
                -- 起点のステップだけ、失敗の理由を残します。人がこのボタンを
                -- 押した理由がまさにそれで、次の実行はそれを読んで書き直します
                -- （run_step が payload に入れます）。後続は落ちていないので消します。
                error_message = CASE WHEN step_number = %s THEN error_message END,
                started_at = NULL, completed_at = NULL, attempts = 0
            WHERE job_id = %s AND step_number >= %s
            """, (number, job_id, number))
        cur.execute("DELETE FROM analysis_reports WHERE job_id = %s", (job_id,))
        cur.execute(
            # started_at も消します。残すと、やり直した直後の画面に
            # 「経過 25秒」と出ます。数えているのは前回の開始からで、
            # 待っている人が見たいのは今回のぶんです。
            "UPDATE analysis_jobs SET status = 'queued', error_message = NULL, "
            "current_step = %s, started_at = NULL, completed_at = NULL "
            "WHERE id = %s",
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
