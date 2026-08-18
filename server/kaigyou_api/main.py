"""FastAPI application entry point."""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from kaigyou_api.routers import analysis, layers, meta

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


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
