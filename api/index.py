"""Vercel entry point for the API.

Vercel routes every ``/api/*`` request into this one function (see
``vercel.json``) and serves the built web client from the same domain, so the
browser never makes a cross-origin request and there is no CORS to configure.

The ETL does not run here. It is a laptop job that writes to the database
directly; this function only reads.

If importing the application fails, this module still exports a working ASGI
app -- one that reports why. A crash at import time never reaches FastAPI, so
the platform answers ``FUNCTION_INVOCATION_FAILED`` and nothing else: no
module name, no line, nothing to act on. Serving the reason costs one small
fallback and saves reading deploy logs to learn that a dependency is missing.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The function's working directory is not guaranteed, and config/ is found by
# walking up from it. Pointing at the bundle root removes the guesswork.
os.environ.setdefault("KAIGYOU_ROOT", str(ROOT))

# Normally the package arrives via ``./server`` in requirements.txt. This covers
# a bundle where the install did not happen but the sources are present.
_server = ROOT / "server"
if _server.is_dir() and str(_server) not in sys.path:
    sys.path.insert(0, str(_server))


def _boot_failure_app(exc: BaseException):
    """A bare ASGI app that answers every request with the import error.

    Bare because the import that failed may well have been FastAPI's: this
    cannot depend on anything the application needs.
    """
    traceback.print_exc()
    detail = f"{type(exc).__name__}: {exc}"

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        body = json.dumps({
            "detail": "APIの起動に失敗しました。",
            "error": detail,
            "hint": ("依存関係またはビルド設定の問題です。"
                     "Vercel の Deployments → 対象のデプロイ → Logs に"
                     "完全なトレースバックが出ています。"),
        }, ensure_ascii=False).encode()
        await send({"type": "http.response.start", "status": 503,
                    "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})

    return app


try:
    from kaigyou_api.main import app
except Exception as exc:  # noqa: BLE001 - any import error must still answer
    app = _boot_failure_app(exc)

__all__ = ["app"]
