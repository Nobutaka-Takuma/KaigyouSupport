"""Metadata: scoring model, data acquisition status, disclaimers."""
from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Query

from kaigyou_api.deps import DISCLAIMER, SCORE_DISCLAIMER, get_conn
from kaigyou_core import config as cfg
from kaigyou_core.analysis import DEFAULT_CATEGORY
from kaigyou_core import specialties as vocab
from kaigyou_core.analysis import default_prefecture, loaded_prefectures
from kaigyou_core.db import table_exists
from kaigyou_core.scoring import ScoringModel
from kaigyou_core.status import data_status

router = APIRouter()


@router.get("/misreadings", summary="この画面の数字で、やってはいけない読み方")
def misreadings(category: str = Query(DEFAULT_CATEGORY)) -> dict[str, Any]:
    """**注意書きの寄せ集めではなく、この製品の中身です。**

    jSTAT MAP も RESAS も TerraMap も正しい数字を出します。間違えるのは
    読む側で、ツールは黙って通します——だから誰も「不便だ」と言わず、
    気づかないまま自信を持ちます。

    画面とレポートが同じ文を使うので、ここは設定から返します。画面が
    独自に書くと、地図で見た注意とレポートの注意が食い違います。
    """
    items = cfg.misreadings(category)
    return {
        "items": items,
        # 判断がひっくり返るものだけを別に返します。画面は全部を常時
        # 出せないので、まずここを出して、残りは開いたときに出します。
        "high": [i for i in items if i.get("severity") == "high"],
        "note": ("公表データはどれも正しい値です。取り違えるのは読み方のほうで、"
                 "多くのツールはそれを黙って通します。この一覧は、この画面の"
                 "数字で起こりやすい取り違えを先に示すものです。"),
    }


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
            "地価は国土交通省の地価公示（標準地の1m²あたりの価格・毎年1月1日時点）です。"
            "賃料ではありません。建物・階数・契約条件を含まないため、"
            "テナント賃料そのものの代わりにはなりません。"
            "「地価考慮」プロファイルではコスト軸として使っていますが、"
            "公表されている中で唯一の座標付きの価格指標としての代理であり、"
            "賃料や初期投資額を予測するものではありません。",
            "「地価考慮」プロファイルでは、商圏内に地価公示の標準地が3地点未満の場合、"
            "総合スコアを算出しません。落として残りの重みで計算すると"
            "「コストが分からない」が「コストがかからない」という意味になり、"
            "情報の乏しい場所ほど上位に来てしまうためです"
            "（東京都では約29%のメッシュが該当します）。",
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


@router.get("/specialties", summary="標榜診療科目（読み込み済みのもの）")
def specialties(
    prefecture_code: str | None = None,
    category: str = DEFAULT_CATEGORY,
    conn: psycopg.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """絞り込みに使える標榜科目と、それぞれの医院数。

    一覧を固定値で返さないのは、どの科目が選べるかは告示ではなく「いま
    読み込まれているデータ」が決めるからです。診療科目ファイルを入れていない
    環境では ``available: false`` を返し、UI は絞り込みを出しません。
    """
    if not table_exists(conn, "facility_features"):
        return {"available": False, "specialties": [],
                "note": "診療科目データ（医療情報ネット 032）が未取得です。"}

    where = ["f.facility_category = %s"]
    params: list[Any] = [category]
    if prefecture_code:
        where.append("f.prefecture_code = %s")
        params.append(prefecture_code)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT count(*)::int AS clinics,
                   count(ff.facility_id)::int AS with_data
            FROM facilities f
            LEFT JOIN facility_features ff ON ff.facility_id = f.facility_id
            WHERE {' AND '.join(where)}
            """, params)
        totals = cur.fetchone()

        cur.execute(
            f"""
            SELECT k AS key, count(*)::int AS clinics
            FROM facilities f
            JOIN facility_features ff ON ff.facility_id = f.facility_id,
                 unnest(ff.specialty_keys) AS k
            WHERE {' AND '.join(where)}
            GROUP BY k
            """, params)
        rows = cur.fetchall()

    listed = [
        {"key": r["key"], "label": vocab.label(r["key"]), "clinics": r["clinics"],
         "declared_only": vocab.is_free_text(r["key"]),
         # 歯科以外の標榜科（併設の内科など）。歯科の競合の軸ではないので、
         # 絞り込みの選択肢には出しません。
         "dental": r["key"] != vocab.non_dental_key(),
         "share": (None if not totals["with_data"]
                   else round(r["clinics"] / totals["with_data"], 3))}
        for r in rows
    ]
    listed.sort(key=lambda row: vocab.sort_key(row["key"]))
    return {
        "available": True,
        "prefecture_code": prefecture_code,
        "clinics": totals["clinics"],
        "clinics_with_data": totals["with_data"],
        "coverage": (None if not totals["clinics"]
                     else round(totals["with_data"] / totals["clinics"], 3)),
        "specialties": listed,
        "note": ("declared_only の科目は「その他」欄への自由記載から抽出したものです。"
                 "記載した医院しか数えられないため、件数は実施医院数の下限です。"),
    }
