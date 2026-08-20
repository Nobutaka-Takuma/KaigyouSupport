"""FastAPI application entry point."""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

log = logging.getLogger("kaigyou.api")

app = FastAPI(
    title="KaigyouSupport API",
    version="0.1.0",
    description=(
        "歯科開業候補地の商圏分析 API（MVP）。"
        "公開統計・オープンデータに基づく参考情報であり、開業成功や収益を予測するものではありません。"
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in os.getenv(
        "KAIGYOU_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",") if o],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# The routers are imported here rather than at the top of the file so that a
# failure to import one does not take the application down with it.
#
# On a serverless platform an exception during module import happens before any
# handler exists, so the request dies with a generic FUNCTION_INVOCATION_FAILED
# and no way to ask what went wrong. Keeping ``app`` alive means /api/health
# still answers -- and can say that the routers are the thing that is broken.
#
# ``app = FastAPI(...)`` above stays a plain top-level assignment on purpose:
# Vercel finds the application by parsing this file, not by running it.
ROUTER_IMPORT_ERROR: str | None = None
try:
    from kaigyou_api.routers import analysis, layers, meta

    app.include_router(meta.router, prefix="/api", tags=["meta"])
    app.include_router(layers.router, prefix="/api", tags=["layers"])
    app.include_router(analysis.router, prefix="/api", tags=["analysis"])
except Exception as exc:  # noqa: BLE001 - report it rather than die at import
    log.exception("failed to load API routers")
    ROUTER_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


#: Managed platforms set one of these. A public URL should not narrate its own
#: exceptions -- the message can carry a connection string or a table name --
#: so the detail is withheld there unless the operator asks for it back.
_HOSTED_MARKERS = ("VERCEL", "AWS_LAMBDA_FUNCTION_NAME", "K_SERVICE")


def expose_error_detail() -> bool:
    override = os.getenv("KAIGYOU_ERROR_DETAIL")
    if override is not None:
        return override.strip().lower() not in {"", "0", "false", "no", "off"}
    return not any(os.getenv(marker) for marker in _HOSTED_MARKERS)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Answer with the cause instead of a bare 500.

    Config missing, database unreachable and migration not applied all look
    identical from the browser otherwise, which sends the operator hunting in
    the wrong place. Run locally -- one operator, one machine -- naming the
    exception is worth more than hiding it. Deployed, it is not: see
    :func:`expose_error_detail`. Either way the full traceback goes to the log.
    """
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    if expose_error_detail():
        detail = f"{type(exc).__name__}: {exc}"
        hint = "kaigyou-etl doctor を実行すると原因と対処が表示されます。"
    else:
        detail = "サーバ内部エラー"
        hint = "サーバのログに詳細が記録されています。"
    return JSONResponse(status_code=500, content={"detail": detail, "hint": hint})


# Both paths, because only one of them is reachable in each setting: run
# locally the app owns the whole origin, but a deployment that serves the web
# client from the same domain routes only /api/* into the function.
@app.get("/health", tags=["meta"])
@app.get("/api/health", tags=["meta"])
def health() -> dict[str, object]:
    """Liveness, plus the three things a deployment gets wrong.

    Deliberately does not open a connection -- this has to answer even when the
    database is the problem. It reports whether the settings are *present*, not
    what they are: a missing DATABASE_URL and a wrong one look different here,
    and neither prints the credentials.
    """
    from kaigyou_core import config as cfg
    from kaigyou_core.db import dsn, is_pooled

    configured = bool(os.getenv("DATABASE_URL"))
    try:
        config_dir = cfg.config_dir()
        config_found = (config_dir / "sources.yaml").is_file()
    except Exception:  # noqa: BLE001 - health must not raise
        config_dir, config_found = None, False

    return {
        "status": "ok" if ROUTER_IMPORT_ERROR is None else "degraded",
        "routers_loaded": ROUTER_IMPORT_ERROR is None,
        "router_error": ROUTER_IMPORT_ERROR,
        "config_found": config_found,
        "config_dir": str(config_dir) if config_dir else None,
        "database_url_set": configured,
        # Wrong on a serverless platform: the direct connection runs out of
        # slots. Reported so it can be seen without opening a connection.
        "database_pooled": is_pooled(dsn()) if configured else None,
    }
