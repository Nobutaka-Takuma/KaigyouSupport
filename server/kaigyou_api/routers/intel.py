"""商圏インテリジェンス・エンジンの API。

分析そのものはここでは動きません。1 リクエストで 4 ステップと Web 検索を
終わらせるのは無理で（要件 §31）、Vercel の関数には実行時間の上限もあります。
ここがやるのは Job を作ることと、進捗を見せることだけです。実行は worker が
別の場所で回します。
"""
from __future__ import annotations

import hmac
import os
from typing import Any

import psycopg
from urllib.parse import quote

from fastapi import (APIRouter, BackgroundTasks, Depends, Header, HTTPException,
                     Query, Response)

from kaigyou_api import accounts as acc
from kaigyou_api.deps import DISCLAIMER, get_conn, get_model
from kaigyou_core import config as cfg
from kaigyou_core.analysis import (
    DEFAULT_CATCHMENT,
    DEFAULT_CATEGORY,
    default_prefecture,
    prefecture_at,
    resolve_mesh_size,
)
from kaigyou_core.dataset import build_dataset
from kaigyou_core.db import table_exists
from kaigyou_core.scoring import ScoringModel
from kaigyou_intel import client as llm
from kaigyou_intel import coverage
from kaigyou_intel import jobs
from kaigyou_intel.pricing import total_cost
from kaigyou_intel.projection import base_data_hash, to_jsonable

router = APIRouter()

#: 設定されていれば X-Analysis-Token を要求します。分析 1 件で LLM の課金が
#: 発生するので、公開URLに認証なしで置くと誰でも財布を開けられます。
_TOKEN_ENV = "KAIGYOU_ANALYSIS_TOKEN"


def _hosted() -> bool:
    return any(os.getenv(v) for v in ("VERCEL", "AWS_LAMBDA_FUNCTION_NAME", "K_SERVICE"))


def _authorise(token: str | None) -> None:
    """課金の伴う操作を守る。

    ホスティング環境でトークン未設定なら**断ります**。警告を出して通すのでは、
    気づいたときには請求が来ています。手元（VERCEL 等が無い環境）では
    設定なしで動かせます。
    """
    expected = os.getenv(_TOKEN_ENV)
    if not expected:
        if _hosted():
            raise HTTPException(
                503,
                detail=f"{_TOKEN_ENV} が未設定のため、この環境では分析を開始できません。"
                       "分析1件ごとにLLMの課金が発生するため、公開URLでは"
                       "共有シークレットを必須にしています。")
        return
    if token != expected:
        raise HTTPException(401, detail="X-Analysis-Token が一致しません。")


def _require_tables(conn: psycopg.Connection) -> None:
    if not table_exists(conn, "analysis_jobs"):
        raise HTTPException(
            503, detail="分析ジョブのテーブルがありません（kaigyou-etl migrate）。")


@router.post("/analysis", summary="商圏分析ジョブを作成する")
def create_analysis(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    radius: int | None = Query(None, ge=100, le=10000),
    catchment: str = Query(DEFAULT_CATCHMENT, pattern="^(circle|walk)$"),
    category: str = Query(DEFAULT_CATEGORY),
    location_name: str | None = Query(None, description="レポートに載せる地点名"),
    profile: str | None = Query(None),
    background: BackgroundTasks = None,  # type: ignore[assignment]
    conn: psycopg.Connection = Depends(get_conn),
    model: ScoringModel = Depends(get_model),
    account: acc.Account | None = Depends(acc.current_account),
    x_analysis_token: str | None = Header(None),
) -> dict[str, Any]:
    """基礎データを固定して Job を作る。分析は worker が回します。

    基礎データはここでスナップショットとして保存します。参照だけ持つと、
    スコアを再計算したあとに「このレポートは何を見て書かれたか」が
    再現できなくなります（要件 §25）。

    枠の確認は**始める前**です。走らせてから止めても API 費用は戻りません。
    """
    # **省略時は業態の既定**（config/<業態>/insights.yaml の
    # catchment.default_radius_m）。歯科は 500m です——半径1km の円は
    # 3.14km² あり、駅から 800m の候補地を中心に置いても駅を飲み込みます。
    radius = radius or cfg.default_radius_m(category)
    if account is None:
        # アカウント機能を使っていない環境（手元）。共有シークレットで守ります。
        _authorise(x_analysis_token)
    else:
        acc.require_quota(account)
    _require_tables(conn)

    prefecture_code = default_prefecture(conn, prefecture_at(conn, lat, lng))
    mesh_size_m = resolve_mesh_size(conn, None, prefecture_code)
    dataset = build_dataset(
        conn, lat, lng, radius, catchment=catchment, category=category,
        prefecture_code=prefecture_code, mesh_size_m=mesh_size_m,
        profile=profile or model.profile_name,
        max_clinics=int((cfg.analysis_config().get("limits") or {})
                        .get("max_clinics_in_projection", 20)),
        disclaimer=DISCLAIMER)
    # 保存とハッシュのために、日付や Decimal を素の JSON 値へ。
    dataset = to_jsonable(dataset)

    job_id = jobs.create_job(
        conn, lat=lat, lng=lng, radius_m=radius, dataset=dataset,
        base_hash=base_data_hash(dataset), business_type=category,
        location_name=location_name, profile=profile or model.profile_name,
        user_id=account.user_id if account else None)

    # **作った直後に、こちらから worker を起こします。**
    #
    # cron は 1 分おきです。それは「1 分に 1 回進む」という意味であって、
    # 「押してから 1 分待つ」という意味ではないはずでした。ところが作った
    # ジョブを最初に拾うのも cron なので、**押してから最初の 1 歩までが
    # 最大 60 秒、平均 30 秒、何も起きません。** 画面には「順番待ち」とだけ
    # 出ます。分析そのものが数分かかるので紛れますが、待たされているのは
    # 事実です。
    #
    # 起こせなくても壊れません。cron が次の分に拾います。
    if background is not None:
        background.add_task(_start_soon)

    return {
        "job_id": job_id,
        "quota": _quota_view(conn, account),
        "status": "queued",
        "steps": [{"step_number": n, "step_name": name, "status": "pending"}
                  for n, name in jobs.STEP_NAMES.items()],
        # worker が動いていないと、いつまでも queued のままです。黙って待たせない。
        "worker_required": True,
        "note": ("分析は worker が実行します。`kaigyou-etl analyze --worker` を"
                 "起動してください。進捗は GET /api/analysis/{job_id} で取得できます。"),
    }


def _start_soon() -> None:
    """作ったジョブを、cron を待たずに 1 歩進める。

    **鍵がある環境でだけ**動きます。手元では API サーバに ANTHROPIC_API_KEY が
    無く、worker（`analyze --worker`）を別の端末で回すのが普通の形です。鍵の
    無い側で走らせると、そのステップは失敗し、やり直し回数だけ減ります。

    自分で接続を開きます。リクエストの接続は応答と一緒に閉じているためです。
    **何が起きても外へ投げません。** ここは応答が返ったあとの処理なので、
    投げても誰も受け取れず、ジョブは既に作られています。cron が拾います。
    """
    from kaigyou_core.db import connect

    if not llm.is_configured():
        return
    try:
        from kaigyou_intel.worker import tick

        with connect() as conn:
            tick(conn)
    except Exception:  # noqa: BLE001 - 応答後の処理。投げても誰も受け取れない
        pass


def _quota_view(conn: psycopg.Connection,
                account: acc.Account | None) -> dict[str, Any] | None:
    """残り回数。画面に出すためのもの。"""
    if account is None:
        return None
    account.used_this_period = acc.usage_in_period(
        conn, account.user_id, acc.period_start(account.billing_day))
    start = acc.period_start(account.billing_day)
    return {"monthly_quota": account.monthly_quota,
            "used": account.used_this_period,
            "remaining": account.remaining,
            "period_start": start.isoformat()}


def _owned(job: dict[str, Any], account: acc.Account | None) -> dict[str, Any]:
    """他人のジョブを見せない。

    ジョブ ID は推測しにくい UUID ですが、「推測しにくい」は権限ではありません。
    管理者は全部見られます（サポートのため）。
    """
    if account is None or account.is_admin:
        return job
    if job.get("user_id") and str(job["user_id"]) != account.user_id:
        raise HTTPException(404, detail="そのジョブはありません。")
    return job


@router.get("/analysis/{job_id}", summary="分析ジョブの進捗と各ステップの結果")
def get_analysis(job_id: str,
                 conn: psycopg.Connection = Depends(get_conn),
                 account: acc.Account | None = Depends(acc.current_account),
                 ) -> dict[str, Any]:
    _require_tables(conn)
    job = jobs.get_job(conn, job_id)
    if job is None:
        raise HTTPException(404, detail="そのジョブはありません。")
    _owned(job, account)

    steps = jobs.get_steps(conn, job_id)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM analysis_sources WHERE job_id = %s", (job_id,))
        sources = cur.fetchone()["n"]
        cur.execute(
            "SELECT report_json, report_markdown, trace_ok, trace_problems "
            "FROM analysis_reports WHERE job_id = %s", (job_id,))
        report = cur.fetchone()
        cur.execute(
            "SELECT url, source_type, pattern_id FROM analysis_sources "
            "WHERE job_id = %s", (job_id,))
        source_rows = [dict(r) for r in cur.fetchall()]

    return {
        "job": job,
        # **output_json は返しません。**どの画面も読んでいないのに、進捗の
        # 問い合わせは 4 秒ごとに走ります。数分の待ちのあいだ、読まれない
        # STEP1〜4 の出力を何十回も往復させることになります。段の中身から
        # 人が見たいものは progress に数えてあり、全文が要るときは
        # `kaigyou-etl analyze --show` が DB から直接読みます。
        "steps": [{k: v for k, v in s.items() if k != "output_json"} for s in steps],
        # 指示書 §25。待っている数分に、**何が分かってきたのかを見せる。**
        "progress": _progress(steps, source_rows),
        "source_count": sources,
        "report_available": report is not None,
        "trace_ok": report["trace_ok"] if report else None,
        # 要件 §34。1レポートいくらだったのかを、後から数えられるように。
        # キャッシュに入ったぶんは input_tokens から抜けるので、足して出します。
        # 引かずに出すと「入力 2 トークン」になり、数え損ねたように見えます。
        "usage": {
            "input_tokens": sum((s["input_tokens"] or 0)
                                + (s["cache_read_tokens"] or 0)
                                + (s["cache_write_tokens"] or 0) for s in steps),
            "cache_read_tokens": sum(s["cache_read_tokens"] or 0 for s in steps),
            "cache_write_tokens": sum(s["cache_write_tokens"] or 0 for s in steps),
            "output_tokens": sum(s["output_tokens"] or 0 for s in steps),
            "web_searches": sum(s["web_searches"] or 0 for s in steps),
            "estimated_cost_usd": total_cost(steps),
        },
        # ホスティング環境では **null**。API サーバは LLM を呼ばないので、
        # ここにキーがあるかどうかは何も意味しません。false を返すと画面に
        # 「キーがありません」と出続けますが、必要なのは worker を動かす端末の
        # ほうです。手元で API と worker を同じ端末で動かしているときだけ、
        # この値は本当のことを言えます。
        "llm_configured": None if _hosted() else llm.is_configured(),
        "quota": _quota_view(conn, account),
        # worker が動いていないと queued のまま止まります。画面で待つ人に、
        # 何を待っているのかが分かるように、状態の意味を添えます。
        "status_note": _STATUS_NOTE.get(job["status"]),
    }


def _progress(steps: list[dict[str, Any]],
              sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    """いまの時点で何が分かっているか（指示書 §25）。

    これまで画面に出ていたのは「STEP2 実行中 3分12秒」だけでした。**動いて
    いるかどうかは分かっても、何かを見つけているのかは分かりません。**
    ところが STEP1 の出力——パターンと問い——は、その時点で既に DB にあります。
    レポートが書き上がるまで誰にも見せていなかっただけです。

    数えるのは coverage.tally で、レポート冒頭の「この分析で確かめたこと」と
    同じ関数です。**待っている間に見た件数と、出来上がった文書の件数が
    食い違わないようにするため**で、食い違えばどちらが正しいのか読み手には
    確かめようがありません。

    済んだ段のぶんだけ返します。走っている最中の段は output_json がまだ
    無いので、勝手に 0 とは数えません（「まだ」と「0 件だった」は違います）。
    """
    done = {int(s["step_number"]): (s.get("output_json") or {})
            for s in steps if s["status"] == "completed"}
    if not done:
        return None
    counts = coverage.tally(
        coverage.inquiry_from_steps(done.get(1), done.get(2)), sources)
    if coverage.is_empty(counts):
        return None
    return {
        **counts,
        # どの段まで数え終わっているか。画面はこれを見て「問い 0 件」と
        # 「まだ問いを立てていない」を区別します。
        "through_step": max(done),
        "researched": 2 in done,
    }


#: 状態の意味。UI で出すためのもので、判定には使いません。
_STATUS_NOTE = {
    "queued": "worker の順番待ちです。worker が動いていないと進みません。",
    "running": "実行中です。Web検索を伴うため数分かかります。",
    "blocked": "未実装のステップに当たって止まっています。失敗ではありません。",
    "failed": "途中で失敗しました。原因を直してから、そのステップだけやり直せます。",
    "completed": "完了しました。",
    "cancelled": "取り下げ済みです。",
}


@router.post("/analysis/{job_id}/steps/{step_number}/retry",
             summary="このステップ以降をやり直す")
def retry_step(job_id: str, step_number: int,
               conn: psycopg.Connection = Depends(get_conn),
               account: acc.Account | None = Depends(acc.current_account),
               x_analysis_token: str | None = Header(None)) -> dict[str, Any]:
    """要件 §32。最初から全部やり直させない。

    指定ステップ**以降**を pending に戻します。後続だけ残すと、古い前提の上に
    新しい結論が乗ります。
    """
    if account is None:
        _authorise(x_analysis_token)
    _require_tables(conn)
    if step_number not in jobs.STEP_NAMES:
        raise HTTPException(400, detail="ステップ番号は 1〜4 です。")
    job = jobs.get_job(conn, job_id)
    if job is None:
        raise HTTPException(404, detail="そのジョブはありません。")
    _owned(job, account)

    jobs.reset_step(conn, job_id, step_number)
    return {"job_id": job_id, "restarted_from": step_number, "status": "queued"}


@router.get("/me", summary="いまサインインしている利用者")
def whoami(conn: psycopg.Connection = Depends(get_conn),
           account: acc.Account | None = Depends(acc.current_account),
           ) -> dict[str, Any]:
    """画面が「誰として動いているか」を知るための1本。

    管理のリンクを全員に見せると、押せない場所への入口が増えます。かといって
    リンクの出し分けのためだけに管理APIを叩かせるのも重い。
    """
    if account is None:
        return {"signed_in": False, "accounts_enabled": acc.accounts_enabled()}
    return {
        "signed_in": True,
        "accounts_enabled": True,
        "email": account.email,
        "is_admin": account.is_admin,
        "quota": _quota_view(conn, account),
    }


@router.get("/analyses", summary="自分のレポート一覧")
def list_analyses(limit: int = Query(50, ge=1, le=200),
                  conn: psycopg.Connection = Depends(get_conn),
                  account: acc.Account | None = Depends(acc.current_account),
                  ) -> dict[str, Any]:
    """過去に作ったレポートを、いつでも取り直せるようにする。

    ファイルを失くしたら再生成、では**運営者の API 費用が増える**だけです。
    DB には残っているので、出す口を用意します。
    """
    _require_tables(conn)
    where, params = "TRUE", []
    if account is not None and not account.is_admin:
        where, params = "j.user_id = %s", [account.user_id]
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT j.id, j.location_name, j.latitude, j.longitude, j.radius_m,
                   j.profile, j.status, j.created_at, j.completed_at,
                   r.created_at AS report_at, r.trace_ok,
                   (r.report_json ->> 'title') AS title,
                   (r.report_json -> 'verdict' ->> 'label') AS verdict
            FROM analysis_jobs j
            LEFT JOIN analysis_reports r ON r.job_id = j.id
            WHERE {where} AND j.status <> 'cancelled'
            ORDER BY j.created_at DESC LIMIT %s
        """, (*params, limit))
        rows = [dict(r) for r in cur.fetchall()]
    return {"items": rows, "quota": _quota_view(conn, account)}


@router.get("/analysis/{job_id}/report.md", summary="レポートをファイルとして取得")
def download_report(job_id: str,
                    conn: psycopg.Connection = Depends(get_conn),
                    account: acc.Account | None = Depends(acc.current_account),
                    ) -> Response:
    """Markdown をそのまま返します。

    ブラウザ側で Blob を組み立てるより、サーバが Content-Disposition を付けて
    返すほうが確実です。**名前を決める場所が 1 か所になります。**
    """
    _require_tables(conn)
    job = jobs.get_job(conn, job_id)
    if job is None:
        raise HTTPException(404, detail="そのジョブはありません。")
    _owned(job, account)

    from kaigyou_intel import report as report_module

    markdown = report_module.markdown_for(conn, job_id)
    if markdown is None:
        raise HTTPException(404, detail="レポートはまだありません。")
    # 名前はサーバが決めます。地図の画面もこの口を通るので、どこから
    # 保存しても同じ名前になります（以前はブラウザ側で Blob を組み立てて
    # いて、そちらだけ「商圏分析レポート.md」で固定でした）。
    name = report_module.file_name_for(conn, job_id)
    return Response(
        content=markdown.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition":
                 "attachment; filename*=UTF-8''" + quote(name)})


#: worker を叩くための鍵。分析トークンとは別にします。片方が漏れても、
#: もう片方の入口は閉じたままにするためです。
_WORKER_TOKEN_ENV = "KAIGYOU_WORKER_TOKEN"


# **GET も受けます。Vercel Cron は GET で叩きます。**
# POST だけにしていたので、cron は毎分きっちり呼んでいたのに 404 が返り、
# worker は一度も起きませんでした。ログを見るまで「cron が動いていない」
# ように見えます（実際には動いていて、断られていた）。
# Supabase の pg_net は POST で送るので、両方受けます。
@router.api_route("/worker/tick", methods=["GET", "POST"],
                  summary="分析を進める（定期実行から呼ぶ）")
def worker_tick(
    x_worker_token: str | None = Header(None),
    authorization: str | None = Header(None),
    conn: psycopg.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """1 回につき 1 ステップ。

    5 段を 1 回の関数呼び出しには収められません（Vercel の実行時間上限は
    Hobby で 300 秒、Pro で 800 秒。分析は通しで 10〜20 分）。**1 呼び出し =
    1 ステップ**にして、続きは次の呼び出しに任せます。状態は全部 DB にあるので、
    途中で関数が消えても続きから拾えます。

    誰が叩くかは環境で変えられます。Vercel Cron（Pro なら1分ごと）でも、
    Supabase の pg_cron からでも、同じことをします。Vercel の Cron は
    ``Authorization: Bearer <CRON_SECRET>`` を送るので、そちらも受けます。
    """
    # **設定されている鍵の「どれか」に一致すればよい**。以前は
    # `A or B` で先に見つかったほうだけを正解にしていたので、両方を別々の値で
    # 設定すると、Vercel Cron が送る Bearer <CRON_SECRET> が
    # KAIGYOU_WORKER_TOKEN と比べられて 401 になりました。cron の失敗は
    # 画面に出ないので、Job が「順番待ち」のまま止まるだけに見えます。
    # 移行期には両方が設定されているのが普通なので、両方を正解にします。
    accepted = [v for v in (os.getenv(_WORKER_TOKEN_ENV), os.getenv("CRON_SECRET")) if v]
    if not accepted:
        raise HTTPException(
            503, detail=f"{_WORKER_TOKEN_ENV} も CRON_SECRET も未設定のため、"
                        "worker を起動できません。")
    offered = [v for v in (x_worker_token,
                           (authorization or "").removeprefix("Bearer ").strip()) if v]
    if not any(hmac.compare_digest(o, e) for o in offered for e in accepted):
        raise HTTPException(401, detail="worker トークンが一致しません。")
    _require_tables(conn)

    from kaigyou_intel.worker import tick

    # 待ち方は worker 側が環境を見て決めます（関数の上限があるかどうか）。
    return tick(conn)


@router.get("/analysis/{job_id}/report", summary="最終レポート")
def get_report(job_id: str,
               conn: psycopg.Connection = Depends(get_conn),
               account: acc.Account | None = Depends(acc.current_account),
               ) -> dict[str, Any]:
    _require_tables(conn)
    job = jobs.get_job(conn, job_id)
    if job is None:
        raise HTTPException(404, detail="そのジョブはありません。")
    _owned(job, account)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT report_json, report_markdown, trace_ok, trace_problems, created_at "
            "FROM analysis_reports WHERE job_id = %s", (job_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, detail="レポートはまだありません。")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT pattern_id, url, title, source_type, retrieved_at "
            "FROM analysis_sources WHERE job_id = %s ORDER BY pattern_id, url", (job_id,))
        sources = [dict(r) for r in cur.fetchall()]

    return {**dict(row), "sources": sources, "disclaimer": DISCLAIMER}
