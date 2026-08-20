"""FastAPI application entry point."""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from kaigyou_api.routers import analysis, layers, meta

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

app.include_router(meta.router, prefix="/api", tags=["meta"])
app.include_router(layers.router, prefix="/api", tags=["layers"])
app.include_router(analysis.router, prefix="/api", tags=["analysis"])


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
def health() -> dict[str, str]:
    """Liveness only -- deliberately does not touch the database."""
    return {"status": "ok"}
