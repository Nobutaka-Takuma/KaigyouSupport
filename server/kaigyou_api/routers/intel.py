"""商圏インテリジェンス・エンジンの API。

分析そのものはここでは動きません。1 リクエストで 4 ステップと Web 検索を
終わらせるのは無理で（要件 §31）、Vercel の関数には実行時間の上限もあります。
ここがやるのは Job を作ることと、進捗を見せることだけです。実行は worker が
別の場所で回します。
"""
from __future__ import annotations

import os
from typing import Any

import psycopg
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response

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
    radius: int = Query(1000, ge=100, le=10000),
    catchment: str = Query(DEFAULT_CATCHMENT, pattern="^(circle|walk)$"),
    category: str = Query(DEFAULT_CATEGORY),
    location_name: str | None = Query(None, description="レポートに載せる地点名"),
    profile: str | None = Query(None),
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

    return {
        "job": job,
        "steps": steps,
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
    返すほうが確実です（保存先の名前も揃います）。
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
    name = report_module._file_name(job_id, job)
    return Response(
        content=markdown.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition":
                 "attachment; filename*=UTF-8''" + quote(name)})


#: worker を叩くための鍵。分析トークンとは別にします。片方が漏れても、
#: もう片方の入口は閉じたままにするためです。
_WORKER_TOKEN_ENV = "KAIGYOU_WORKER_TOKEN"


@router.post("/worker/tick", summary="分析を1ステップ進める（定期実行から呼ぶ）")
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
    expected = os.getenv(_WORKER_TOKEN_ENV) or os.getenv("CRON_SECRET")
    if not expected:
        raise HTTPException(
            503, detail=f"{_WORKER_TOKEN_ENV} が未設定のため、worker を起動できません。")
    bearer = (authorization or "").removeprefix("Bearer ").strip()
    if x_worker_token != expected and bearer != expected:
        raise HTTPException(401, detail="worker トークンが一致しません。")
    _require_tables(conn)

    from kaigyou_intel.worker import tick

    settings = cfg.analysis_config().get("worker") or {}
    return tick(conn, stale_after_minutes=float(
        settings.get("stale_after_minutes", 20)))


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
