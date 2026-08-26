"""運営者用。アカウントの発行と、利用状況の確認。

請求書は手で出します。だからここが答えるのは 1 つだけです。

    **今期、誰が何回使ったか。**

これが分かれば請求書は書けますし、原価（LLM の実費）と突き合わせられます。
Stripe を入れるのは、顧客が増えて手作業が割に合わなくなってからで十分です。
"""
from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Body, Depends, HTTPException

from kaigyou_api import accounts as acc
from kaigyou_api.deps import get_conn
from kaigyou_core.db import table_exists
from kaigyou_intel.pricing import total_cost

router = APIRouter()


def _admin(account: acc.Account | None = Depends(acc.current_account)) -> acc.Account | None:
    return acc.require_admin(account)


def _require_tables(conn: psycopg.Connection) -> None:
    if not table_exists(conn, "accounts"):
        raise HTTPException(503, detail="accounts テーブルがありません（migrate）。")


@router.get("/admin/accounts", summary="アカウントと今期の利用状況")
def list_accounts(conn: psycopg.Connection = Depends(get_conn),
                  _: acc.Account | None = Depends(_admin)) -> dict[str, Any]:
    _require_tables(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT user_id, email, display_name, organisation, "
                    "monthly_quota, billing_day, status, is_admin, note, created_at "
                    "FROM accounts ORDER BY created_at")
        rows = [dict(r) for r in cur.fetchall()]

    out = []
    for row in rows:
        start = acc.period_start(int(row["billing_day"]))
        used = acc.usage_in_period(conn, str(row["user_id"]), start)
        out.append({**row, "period_start": start.isoformat(), "used_this_period": used,
                    "remaining": max(0, int(row["monthly_quota"]) - used),
                    "api_cost_this_period_usd": _cost_for(conn, str(row["user_id"]), start)})
    return {"items": out}


def _cost_for(conn: psycopg.Connection, user_id: str, since: Any) -> float | None:
    """その利用者の今期の LLM 実費（概算）。

    請求額の根拠ではなく、**原価が売価を超えていないか**を見るための数字です。
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT s.model, s.input_tokens, s.output_tokens,
                   s.cache_read_tokens, s.cache_write_tokens
            FROM analysis_steps s
            JOIN analysis_jobs j ON j.id = s.job_id
            WHERE j.user_id = %s AND j.created_at >= %s
        """, (user_id, since))
        steps = [dict(r) for r in cur.fetchall()]
    cost = total_cost(steps)
    return None if cost is None else round(cost, 3)


@router.put("/admin/accounts/{user_id}", summary="アカウントを発行・更新する")
def upsert_account(user_id: str,
                   payload: dict[str, Any] = Body(...),
                   conn: psycopg.Connection = Depends(get_conn),
                   _: acc.Account | None = Depends(_admin)) -> dict[str, Any]:
    """自己登録は許していません。ここからだけ発行します。

    ``user_id`` は Supabase Auth のユーザー ID です。Supabase の管理画面で
    利用者を招待し、発行された ID をここに渡します。認証情報をこちらで
    二重に持たないので、パスワードの再設定などは Supabase 側の機能が使えます。
    """
    _require_tables(conn)
    quota = int(payload.get("monthly_quota", 0))
    if quota < 0:
        raise HTTPException(400, detail="monthly_quota は0以上です。")
    billing_day = int(payload.get("billing_day", 1))
    if not 1 <= billing_day <= 28:
        raise HTTPException(400, detail="billing_day は1〜28です（月末は月により無い）。")
    status = str(payload.get("status", "active"))
    if status not in ("active", "suspended"):
        raise HTTPException(400, detail="status は active か suspended です。")

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO accounts (user_id, email, display_name, organisation,
                                  monthly_quota, billing_day, status, is_admin, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                email = EXCLUDED.email,
                display_name = EXCLUDED.display_name,
                organisation = EXCLUDED.organisation,
                monthly_quota = EXCLUDED.monthly_quota,
                billing_day = EXCLUDED.billing_day,
                status = EXCLUDED.status,
                is_admin = EXCLUDED.is_admin,
                note = EXCLUDED.note,
                updated_at = now()
            RETURNING user_id, email, monthly_quota, billing_day, status, is_admin
        """, (user_id, payload.get("email"), payload.get("display_name"),
              payload.get("organisation"), quota, billing_day, status,
              bool(payload.get("is_admin", False)), payload.get("note")))
        row = dict(cur.fetchone())
    conn.commit()
    return row


@router.get("/admin/usage", summary="今期の利用と実費のまとめ")
def usage_summary(conn: psycopg.Connection = Depends(get_conn),
                  _: acc.Account | None = Depends(_admin)) -> dict[str, Any]:
    """請求書を書くための 1 画面。誰に何回ぶん請求するかがここで分かります。"""
    _require_tables(conn)
    accounts = list_accounts(conn, None)["items"]
    billable = sum(a["used_this_period"] for a in accounts)
    costs = [a["api_cost_this_period_usd"] for a in accounts]
    return {
        "accounts": accounts,
        "reports_this_period": billable,
        "api_cost_this_period_usd": (
            None if any(c is None for c in costs) else round(sum(costs), 2)),
        "note": ("請求書は手で発行します。ここは「誰に何回ぶん請求するか」と"
                 "「原価が売価を超えていないか」を見るための画面です。"),
    }
