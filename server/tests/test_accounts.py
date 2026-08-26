"""アカウント・利用上限・権限。

**API がここでの唯一の境界です。** フロントは Supabase を直接触らないので、
RLS ではなくこちらで守ります。だからここのテストは「機能が動くか」ではなく
「守れているか」を見ます。

守るものは 3 つです。

1. 他人のレポートを読ませない
2. 枠を超えて走らせない（走ってから止めても API 費用は戻らない）
3. 管理画面を利用者に開けさせない
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import date

import pytest

from kaigyou_api import accounts as acc

SECRET = "test-jwt-secret"


def _jwt(sub: str, *, email: str = "a@example.com", exp: float | None = None,
         alg: str = "HS256", secret: str = SECRET) -> str:
    def part(payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    header = part({"alg": alg, "typ": "JWT"})
    body = part({"sub": sub, "email": email,
                 "exp": exp if exp is not None else time.time() + 3600})
    signature = hmac.new(secret.encode(), f"{header}.{body}".encode(),
                         hashlib.sha256).digest()
    return f"{header}.{body}." + base64.urlsafe_b64encode(signature).decode().rstrip("=")


# ------------------------------------------------------------------ JWT
def test_a_valid_token_is_accepted():
    claims = acc.decode_jwt(_jwt("user-1"), SECRET)
    assert claims["sub"] == "user-1"


def test_a_token_signed_with_another_key_is_refused():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        acc.decode_jwt(_jwt("user-1", secret="someone-elses-key"), SECRET)
    assert caught.value.status_code == 401


def test_the_none_algorithm_is_refused():
    """`alg: none` を受けると、署名なしで誰でも管理者になれます。"""
    from fastapi import HTTPException

    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(
        json.dumps({"sub": "attacker"}).encode()).decode().rstrip("=")
    with pytest.raises(HTTPException):
        acc.decode_jwt(f"{header}.{body}.", SECRET)


def test_an_expired_token_is_refused():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        acc.decode_jwt(_jwt("user-1", exp=time.time() - 10), SECRET)
    assert "有効期限" in caught.value.detail


# ------------------------------------------------------------ 請求期間
def test_the_billing_period_follows_the_contract_day():
    """「毎月1日」に固定しません。契約日を締め日にしたいことがあります。"""
    assert acc.period_start(15, date(2026, 8, 20)) == date(2026, 8, 15)
    # まだ今月の締め日が来ていなければ、先月から。
    assert acc.period_start(15, date(2026, 8, 3)) == date(2026, 7, 15)
    assert acc.period_start(1, date(2026, 8, 3)) == date(2026, 8, 1)


def test_a_late_billing_day_survives_february():
    """29〜31日は月によって存在しないので、締め日は1〜28に限っています。"""
    assert acc.period_start(28, date(2026, 3, 5)) == date(2026, 2, 28)


# ------------------------------------------------------------------ 枠
def _account(**kw) -> acc.Account:
    base = dict(user_id="u1", email="a@example.com", monthly_quota=10,
                billing_day=1, status="active", is_admin=False)
    base.update(kw)
    return acc.Account(**base)


def test_running_out_of_quota_stops_the_job_before_it_costs_anything():
    from fastapi import HTTPException

    account = _account(monthly_quota=3)
    account.used_this_period = 3
    with pytest.raises(HTTPException) as caught:
        acc.require_quota(account)
    assert caught.value.status_code == 429
    assert "上限" in caught.value.detail


def test_a_suspended_account_cannot_start_anything():
    from fastapi import HTTPException

    account = _account(status="suspended")
    with pytest.raises(HTTPException) as caught:
        acc.require_quota(account)
    assert caught.value.status_code == 403


def test_no_account_means_no_check():
    """手元や CI では素通り。課金の入口は共有シークレットで塞いであります。"""
    acc.require_quota(None)
    assert acc.require_admin(None) is None


def test_a_normal_user_cannot_open_the_admin_screen():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        acc.require_admin(_account(is_admin=False))
    assert caught.value.status_code == 403
    assert acc.require_admin(_account(is_admin=True)) is not None


# --------------------------------------------------- 他人のものを見せない
@pytest.fixture
def conn():
    psycopg = pytest.importorskip("psycopg")
    from kaigyou_core.db import connect

    try:
        with connect() as c:
            with c.cursor() as cur:
                cur.execute("SELECT to_regclass('public.accounts') AS t")
                if cur.fetchone()["t"] is None:
                    pytest.skip("021_accounts.sql not applied")
            yield c
    except psycopg.OperationalError as exc:
        pytest.skip(f"database unavailable: {exc}")


def test_one_user_cannot_read_another_users_report(conn, monkeypatch):
    """ジョブ ID は推測しにくい UUID ですが、「推測しにくい」は権限ではありません。"""
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app
    from kaigyou_intel import jobs

    monkeypatch.setenv(acc.JWT_SECRET_ENV, SECRET)
    with conn.cursor() as cur:
        for user in ("owner-1", "stranger-1"):
            cur.execute(
                "INSERT INTO accounts (user_id, monthly_quota) VALUES (%s, 10) "
                "ON CONFLICT (user_id) DO UPDATE SET monthly_quota = 10", (user,))
        conn.commit()

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset={"location": {}}, base_hash="acl",
                             user_id="owner-1")
    client = TestClient(app)
    try:
        mine = client.get(f"/api/analysis/{job_id}",
                          headers={"Authorization": f"Bearer {_jwt('owner-1')}"})
        assert mine.status_code == 200

        theirs = client.get(f"/api/analysis/{job_id}",
                            headers={"Authorization": f"Bearer {_jwt('stranger-1')}"})
        assert theirs.status_code == 404, "他人のジョブの存在を漏らさない"

        for path in (f"/api/analysis/{job_id}/report",
                     f"/api/analysis/{job_id}/report.md"):
            assert client.get(path, headers={
                "Authorization": f"Bearer {_jwt('stranger-1')}"}).status_code == 404

        # ログインしていなければ 401。
        assert client.get(f"/api/analysis/{job_id}").status_code == 401
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM analysis_jobs WHERE id = %s", (job_id,))
            cur.execute("DELETE FROM accounts WHERE user_id IN "
                        "('owner-1','stranger-1')")
            conn.commit()


def test_the_list_only_shows_your_own(conn, monkeypatch):
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app
    from kaigyou_intel import jobs

    monkeypatch.setenv(acc.JWT_SECRET_ENV, SECRET)
    with conn.cursor() as cur:
        for user in ("owner-2", "stranger-2"):
            cur.execute("INSERT INTO accounts (user_id, monthly_quota) VALUES (%s, 10) "
                        "ON CONFLICT (user_id) DO NOTHING", (user,))
        conn.commit()
    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset={"location": {}}, base_hash="acl2",
                             user_id="owner-2")
    client = TestClient(app)
    try:
        mine = client.get("/api/analyses", headers={
            "Authorization": f"Bearer {_jwt('owner-2')}"}).json()
        assert any(str(i["id"]) == job_id for i in mine["items"])
        assert mine["quota"]["monthly_quota"] == 10

        theirs = client.get("/api/analyses", headers={
            "Authorization": f"Bearer {_jwt('stranger-2')}"}).json()
        assert not any(str(i["id"]) == job_id for i in theirs["items"])
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM analysis_jobs WHERE id = %s", (job_id,))
            cur.execute("DELETE FROM accounts WHERE user_id IN ('owner-2','stranger-2')")
            conn.commit()


def test_an_unissued_account_is_told_so(conn, monkeypatch):
    """自己登録を許していないので、認証が通ってもアカウントが無ければ使えません。"""
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app

    monkeypatch.setenv(acc.JWT_SECRET_ENV, SECRET)
    response = TestClient(app).get("/api/analyses", headers={
        "Authorization": f"Bearer {_jwt('never-issued')}"})
    assert response.status_code == 403
    assert "付与されていません" in response.json()["detail"]
