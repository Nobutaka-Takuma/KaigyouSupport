"""アカウントと、月あたりの利用上限。

認証そのものは Supabase Auth に任せます。ここがやるのは、その先の 3 つです。

1. 送られてきた JWT が本物かを確かめる（誰であるか）
2. そのアカウントが有効で、今月まだ枠が残っているかを見る（何回使えるか）
3. 自分のジョブだけを見せる（他人のレポートを読ませない）

**API がここでの唯一の境界です。** フロントは Supabase を直接触らないので、
RLS ではなくこちらで守ります。ここを通らない経路を作らないでください。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import psycopg
from fastapi import Depends, Header, HTTPException

from kaigyou_api.deps import get_conn
from kaigyou_core.db import table_exists

#: Supabase の JWT を検証する鍵（Project Settings → API → JWT Secret）。
#: 未設定なら、アカウント機能は「無効」です。手元で動かすときに毎回
#: 認証を通す必要はありません。
JWT_SECRET_ENV = "SUPABASE_JWT_SECRET"


@dataclass
class Account:
    user_id: str
    email: str | None
    monthly_quota: int
    billing_day: int
    status: str
    is_admin: bool
    used_this_period: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.monthly_quota - self.used_this_period)

    @property
    def active(self) -> bool:
        return self.status == "active"


def accounts_enabled() -> bool:
    """アカウント機能を使うかどうか。

    鍵が無い環境（手元・CI）では素通りさせます。「設定し忘れたら全部通す」に
    見えますが、課金の入口は別の仕掛けで塞いであります（intel.py の
    ``_authorise`` は、ホスティング環境で共有シークレット未設定なら 503）。
    """
    return bool(os.getenv(JWT_SECRET_ENV))


# --------------------------------------------------------------------- JWT
def _b64(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def decode_jwt(token: str, secret: str) -> dict[str, Any]:
    """Supabase の JWT（HS256）を検証して中身を返す。

    ライブラリを足さずに書いています。HS256 の検証は署名の作り直しと
    定数時間比較だけで、依存を1つ増やすほどのことではありません。
    アルゴリズムは HS256 だけ受け付けます。``alg: none`` を受けると
    誰でも管理者になれます。
    """
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
        header = json.loads(_b64(header_b64))
        payload = json.loads(_b64(payload_b64))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(401, detail="トークンの形式が不正です。") from exc

    if header.get("alg") != "HS256":
        raise HTTPException(401, detail="対応していない署名方式です。")
    expected = hmac.new(secret.encode(), f"{header_b64}.{payload_b64}".encode(),
                        hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _b64(signature_b64)):
        raise HTTPException(401, detail="トークンの署名が一致しません。")

    exp = payload.get("exp")
    if exp and datetime.now(timezone.utc).timestamp() > float(exp):
        raise HTTPException(401, detail="トークンの有効期限が切れています。")
    if not payload.get("sub"):
        raise HTTPException(401, detail="トークンに利用者IDがありません。")
    return payload


# ----------------------------------------------------------------- 期間と枠
def period_start(billing_day: int, today: date | None = None) -> date:
    """いまの請求期間の開始日。

    「毎月1日」に固定しません。契約日を締め日にしたいことがあるためです。
    29〜31 日は月によって存在しないので、締め日は 1〜28 に限っています。
    """
    today = today or datetime.now(timezone.utc).date()
    day = min(billing_day, monthrange(today.year, today.month)[1])
    start = today.replace(day=day)
    if today < start:  # まだ今月の締め日が来ていない → 先月から
        previous = start.replace(day=1) - timedelta(days=1)
        day = min(billing_day, monthrange(previous.year, previous.month)[1])
        start = previous.replace(day=day)
    return start


def usage_in_period(conn: psycopg.Connection, user_id: str,
                    since: date) -> int:
    """この期間に作られたジョブの数。

    **取り下げたものは数えません。** 押し間違いで枠が減ると、使う側は
    ボタンを押すのが怖くなります。失敗したものは数えます（API 費用は
    発生しているため）。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM analysis_jobs "
            "WHERE user_id = %s AND created_at >= %s AND status <> 'cancelled'",
            (user_id, since))
        return int(cur.fetchone()["n"])


def load(conn: psycopg.Connection, user_id: str,
         email: str | None = None) -> Account | None:
    if not table_exists(conn, "accounts"):
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT user_id, email, monthly_quota, billing_day, status, is_admin "
            "FROM accounts WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
    if row is None:
        return None
    account = Account(
        user_id=str(row["user_id"]), email=row["email"] or email,
        monthly_quota=int(row["monthly_quota"]),
        billing_day=int(row["billing_day"]), status=str(row["status"]),
        is_admin=bool(row["is_admin"]))
    account.used_this_period = usage_in_period(
        conn, account.user_id, period_start(account.billing_day))
    return account


# ------------------------------------------------------------- 依存関係の口
def current_account(
    authorization: str | None = Header(None),
    conn: psycopg.Connection = Depends(get_conn),
) -> Account | None:
    """呼び出し元のアカウント。鍵が未設定の環境では None（＝素通り）。"""
    secret = os.getenv(JWT_SECRET_ENV)
    if not secret:
        return None
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, detail="ログインが必要です。")

    claims = decode_jwt(token, secret)
    account = load(conn, str(claims["sub"]), claims.get("email"))
    if account is None:
        # 認証は通ったがアカウントが無い。自己登録を許していないので、
        # これは「発行されていない」状態です。黙って通しません。
        raise HTTPException(
            403, detail="このアカウントには利用権限が付与されていません。"
                        "管理者にお問い合わせください。")
    return account


def require_quota(account: Account | None) -> None:
    """分析を始めてよいか。**始める前に**見ます。

    走らせてから止めても、API 費用は戻りません。
    """
    if account is None:
        return
    if not account.active:
        raise HTTPException(403, detail="このアカウントは停止中です。")
    if account.remaining <= 0:
        start = period_start(account.billing_day)
        raise HTTPException(
            429,
            detail=(f"今期のレポート生成回数の上限（{account.monthly_quota}回）に"
                    f"達しました。{start:%Y年%m月%d日}からの期間で"
                    f"{account.used_this_period}回使用しています。"))


def require_admin(account: Account | None) -> Account | None:
    if account is not None and not account.is_admin:
        raise HTTPException(403, detail="管理者のみが利用できます。")
    return account
