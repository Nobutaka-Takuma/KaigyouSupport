"""Metadata: scoring model, data acquisition status, disclaimers."""
from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends

from kaigyou_api.deps import DISCLAIMER, SCORE_DISCLAIMER, get_conn
from kaigyou_core import config as cfg
from kaigyou_core.scoring import ScoringModel
from kaigyou_core.status import data_status

router = APIRouter()


@router.get("/meta", summary="スコアリングモデルと免責事項")
def meta() -> dict[str, Any]:
    scoring = cfg.scoring_config()
    profiles = []
    for name in (scoring.get("profiles") or {}):
        profiles.append(ScoringModel(scoring, name).describe())
    return {
        "active_profile": scoring.get("active_profile"),
        "profiles": profiles,
        "trade_area_radii_m": scoring.get("trade_area_radii_m", [500, 1000, 2000]),
        "mesh_scoring_radius_m": scoring.get("mesh_scoring_radius_m", 1000),
        "disclaimer": DISCLAIMER,
        "score_disclaimer": SCORE_DISCLAIMER,
        "out_of_scope": [
            "開業成功確率の予測", "売上予測", "患者数予測", "家賃予測",
        ],
    }


@router.get("/data-status", summary="データ取得状況（取得できたもの・できなかったもの）")
def data_status_endpoint(conn: psycopg.Connection = Depends(get_conn)) -> dict[str, Any]:
    status = data_status(conn)
    status["disclaimer"] = DISCLAIMER
    if status["contains_sample_data"]:
        status["sample_data_warning"] = (
            "このデータベースには開発用の合成（サンプル）データが含まれています。"
            "実在の統計・医療機関・駅ではありません。"
        )
    if status["official_sources_loaded"] == 0:
        status["no_official_data_warning"] = (
            "公的データを1件も取得できていません。"
            "表示されている数値は実データに基づくものではありません。"
        )
    return status
