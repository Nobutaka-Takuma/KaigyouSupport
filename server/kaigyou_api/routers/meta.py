"""Metadata: scoring model, data acquisition status, disclaimers."""
from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends

from kaigyou_api.deps import DISCLAIMER, SCORE_DISCLAIMER, get_conn
from kaigyou_core import config as cfg
from kaigyou_core.analysis import default_prefecture, loaded_prefectures
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
        "caveats": [
            "「人口」は国勢調査の常住人口（夜間人口）、「従業者数」は経済センサスの"
            "従業地ベースの就業者数です。両者は別々に集計しており、合算していません"
            "（通勤者を二重に数えないため）。",
            "従業者数は昼間人口そのものではありません。昼間人口には通学者・来街者も"
            "含まれますが、メッシュ単位で公表されているのは従業者数までです。"
            "繁華街の来街需要は依然として捕捉できていません。",
            "商圏は直線距離の円です。鉄道・河川・幹線道路による分断は考慮していません。",
            "歯科医院数は施設数であり、規模・ユニット数・診療実績は考慮していません。",
            "事業所メッシュのうち常住人口ゼロのもの（東京都で286メッシュ・従業者の約2.5%）は、"
            "ランキングの候補地点には現れません。周辺地点の商圏には算入されます。",
            "国勢調査メッシュ統計では、小規模なメッシュの値が秘匿処理により"
            "隣接メッシュへ合算されています。合計値は保たれますが、"
            "局所的には人口の配置が1メッシュ分ずれることがあります。",
            "人口増減率は2015年→2020年の変化であり、直近の動向とは異なる場合があります。",
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


@router.get("/prefectures", summary="分析できる都道府県（読み込み済みのもの）")
def prefectures(conn: psycopg.Connection = Depends(get_conn)) -> dict[str, Any]:
    """What is in the database, not what the code was written for.

    The app began as a Tokyo tool with "13" written into it in a dozen places.
    Which prefectures can be analysed is a property of what has been loaded,
    so the client asks rather than assumes -- and gets somewhere to point the
    map, since a prefecture it has never heard of still has an extent.
    """
    found = loaded_prefectures(conn)
    return {
        "prefectures": found,
        "default": default_prefecture(conn),
        "note": ("スコアは都道府県ごとに正規化しています。"
                 "異なる都道府県のスコアを直接比べることはできません。"),
    }
