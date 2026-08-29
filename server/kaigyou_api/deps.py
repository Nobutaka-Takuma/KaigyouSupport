"""Shared request helpers."""
from __future__ import annotations

from typing import Any, Iterator

import psycopg
from fastapi import HTTPException, Query

from kaigyou_core import config as cfg
from kaigyou_core.analysis import DEFAULT_CATEGORY
from kaigyou_core.db import connect
from kaigyou_core.scoring import ScoringModel

# The wording required by the requirements document. Served from the API so the
# web client cannot drift from it.
DISCLAIMER = (
    "本サービスの分析結果は、公開統計およびオープンデータを基にした参考情報です。"
    "特定地域における歯科医院の開業成功、収益性、患者数等を保証するものではありません。"
    "各データには取得時点があります。"
)

SCORE_DISCLAIMER = (
    "スコアは暫定モデルによる相対評価です。重みは仮説値であり、"
    "開業実績による較正は行っていません。"
)


def get_conn() -> Iterator[psycopg.Connection]:
    with connect() as conn:
        yield conn


def get_model(profile: str | None = Query(None, description="scoring profile name"),
              category: str = Query(DEFAULT_CATEGORY),
              ) -> ScoringModel:
    """採点モデル。**重みは業態ごとです**（config/<業態>/scoring.yaml）。

    業態を見ないと、医科の地点を歯科の重み（小児歯科寄り・矯正寄り…）で
    採点することになります。省略時は歯科なので、既存の呼び出しは今までどおり
    です。
    """
    try:
        return ScoringModel(cfg.scoring_config(category), profile)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def parse_bbox(bbox: str | None) -> tuple[float, float, float, float] | None:
    """Parse 'min_lng,min_lat,max_lng,max_lat'."""
    if not bbox:
        return None
    parts = bbox.split(",")
    if len(parts) != 4:
        raise HTTPException(status_code=400,
                            detail="bbox must be min_lng,min_lat,max_lng,max_lat")
    try:
        values = tuple(float(p) for p in parts)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="bbox values must be numbers") from exc
    if values[0] >= values[2] or values[1] >= values[3]:
        raise HTTPException(status_code=400, detail="bbox min must be less than max")
    return values  # type: ignore[return-value]


def feature_collection(features: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": features, **extra}
