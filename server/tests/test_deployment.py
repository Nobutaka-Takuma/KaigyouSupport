"""Behaviour that only differs once the app is not on the operator's laptop.

Two settings change between a local run and a hosted one, and both fail
quietly if they are wrong: a pooled connection that still prepares statements
answers correctly until PgBouncer happens to reuse a backend, and a 500 that
names its exception is a debugging aid locally and a disclosure publicly.
"""
from __future__ import annotations

import pytest

from kaigyou_api import main as api_main
from kaigyou_core import db


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    for name in ("KAIGYOU_DB_PREPARE", "KAIGYOU_ERROR_DETAIL",
                 "VERCEL", "AWS_LAMBDA_FUNCTION_NAME", "K_SERVICE"):
        monkeypatch.delenv(name, raising=False)


# ------------------------------------------------------- pooled connections
@pytest.mark.parametrize("url, pooled", [
    # Supabase's transaction pooler: statements must not be prepared.
    ("postgresql://u:p@aws-0-ap-northeast-1.pooler.supabase.com:6543/postgres", True),
    # Its session pooler holds one backend per client, but shares the hostname;
    # treating it as pooled only costs a little planning time.
    ("postgresql://u:p@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres", True),
    ("postgresql://u:p@host/db?pgbouncer=true", True),
    # A direct connection, hosted or local, prepares as usual.
    ("postgresql://u:p@db.abcdefgh.supabase.co:5432/postgres", False),
    ("postgresql://kaigyou:kaigyou@127.0.0.1:5432/kaigyou", False),
])
def test_a_pooler_is_recognised_from_its_dsn(url, pooled):
    assert db.is_pooled(url) is pooled


def test_the_operator_can_override_the_detection(monkeypatch):
    monkeypatch.setenv("KAIGYOU_DB_PREPARE", "off")
    assert db.is_pooled("postgresql://u:p@127.0.0.1:5432/db") is True
    monkeypatch.setenv("KAIGYOU_DB_PREPARE", "on")
    assert db.is_pooled("postgresql://u:p@host:6543/db") is False


def test_a_malformed_dsn_does_not_raise():
    """A bad DATABASE_URL should fail at connect, with psycopg's message."""
    assert db.is_pooled("postgresql://u:p@host:not-a-port/db") is False


# --------------------------------------------------------- error disclosure
def test_exceptions_are_named_when_running_locally():
    assert api_main.expose_error_detail() is True


@pytest.mark.parametrize("marker", ["VERCEL", "AWS_LAMBDA_FUNCTION_NAME", "K_SERVICE"])
def test_exceptions_are_withheld_on_a_hosting_platform(monkeypatch, marker):
    monkeypatch.setenv(marker, "1")
    assert api_main.expose_error_detail() is False


def test_the_operator_can_ask_for_detail_back(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("KAIGYOU_ERROR_DETAIL", "1")
    assert api_main.expose_error_detail() is True


def test_and_can_turn_it_off_locally(monkeypatch):
    monkeypatch.setenv("KAIGYOU_ERROR_DETAIL", "off")
    assert api_main.expose_error_detail() is False


# ------------------------------------------------------------ batched loads
class RecordingCursor:
    """Counts round trips the way the network sees them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def executemany(self, sql, rows):
        self.calls.append(("executemany", len(list(rows))))

    def execute(self, sql, params=None):
        self.calls.append(("execute", 1))


def test_rows_go_out_in_batches_not_one_at_a_time():
    """51,384 clinics at one round trip each is thirteen minutes of latency."""
    from kaigyou_etl.adapters.base import BATCH_ROWS, SourceAdapter

    cur = RecordingCursor()
    rows = [{"n": i} for i in range(BATCH_ROWS * 2 + 7)]
    assert SourceAdapter.insert_many(cur, "INSERT ...", rows) == len(rows)

    assert [c[0] for c in cur.calls] == ["executemany"] * 3
    assert [c[1] for c in cur.calls] == [BATCH_ROWS, BATCH_ROWS, 7]


def test_no_rows_means_no_round_trips():
    from kaigyou_etl.adapters.base import SourceAdapter

    cur = RecordingCursor()
    assert SourceAdapter.insert_many(cur, "INSERT ...", []) == 0
    assert cur.calls == []


# ------------------------------------------------------------ boot failures
def _drive(app, path="/api/meta"):
    """Call a bare ASGI app the way a server would, and return status + body."""
    import asyncio
    import json as _json

    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    asyncio.run(app({"type": "http", "method": "GET", "path": path,
                     "headers": []}, receive, send))
    return sent[0]["status"], _json.loads(sent[1]["body"])


def test_a_failed_import_still_answers_with_the_reason():
    """Vercel's FUNCTION_INVOCATION_FAILED page names nothing at all.

    An import error never reaches FastAPI's handlers, so the entry point keeps
    a dependency-free app that says which module was missing.
    """
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
    from api.index import _boot_failure_app

    app = _boot_failure_app(ModuleNotFoundError("No module named 'fastapi'"))
    status, body = _drive(app)
    assert status == 503
    assert "No module named 'fastapi'" in body["error"]
    assert "Logs" in body["hint"]


def test_the_boot_failure_app_answers_on_every_path():
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
    from api.index import _boot_failure_app

    app = _boot_failure_app(RuntimeError("boom"))
    for path in ("/api/meta", "/api/health", "/api/clinics"):
        assert _drive(app, path)[0] == 503


# ------------------------------------------------------------------- health
def test_health_reports_the_settings_without_printing_them(monkeypatch):
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app

    secret = "postgresql://postgres:hunter2@aws-0-ap-northeast-1.pooler.supabase.com:6543/postgres"
    monkeypatch.setenv("DATABASE_URL", secret)
    body = TestClient(app).get("/api/health").json()

    assert body["status"] == "ok"
    assert body["database_url_set"] is True
    assert body["database_pooled"] is True
    assert body["config_found"] is True
    # The whole point is that this is safe to paste into a chat window.
    assert "hunter2" not in str(body)


def test_health_says_so_when_the_database_url_is_missing(monkeypatch):
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app

    monkeypatch.delenv("DATABASE_URL", raising=False)
    body = TestClient(app).get("/api/health").json()
    assert body["database_url_set"] is False
    assert body["database_pooled"] is None
