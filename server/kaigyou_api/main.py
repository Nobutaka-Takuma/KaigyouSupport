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


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Answer with the cause instead of a bare 500.

    Config missing, database unreachable and migration not applied all look
    identical from the browser otherwise, which sends the operator hunting in
    the wrong place. This is a local single-operator tool, so naming the
    exception is worth more than hiding it.
    """
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "detail": f"{type(exc).__name__}: {exc}",
            "hint": "kaigyou-etl doctor を実行すると原因と対処が表示されます。",
        },
    )


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
