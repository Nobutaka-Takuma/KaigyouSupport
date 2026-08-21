"""Candidate-location analysis, ranking and comparison.

The candidate analysis is the product's centre of gravity: pick a point, get
its trade area described. The ranking is a by-product of the same engine run
over every mesh, not a separate model.
"""
from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query

from kaigyou_api.deps import DISCLAIMER, SCORE_DISCLAIMER, get_conn, get_model
from kaigyou_core import provenance
from kaigyou_core.analysis import (
    DEFAULT_CATCHMENT,
    DEFAULT_CATEGORY,
    DEFAULT_MESH_SIZE_M,
    analyze_point,
    catchment_geojson,
    default_prefecture,
    facility_counts,
    load_distributions,
    resolve_mesh_size,
    walk_network_status,
)
from kaigyou_core.scoring import ScoringModel, scope_key

router = APIRouter()

ANALYSIS_TABLES = ["population_mesh", "mesh_business", "facilities", "stations"]


# Which score components each dataset feeds. Used to say precisely what is
# affected when one dataset is synthetic, instead of condemning the whole
# response -- a real population figure divided by a real clinic count stays
# meaningful even while the station data is still a placeholder.
_DATASET_COMPONENTS = {
    "population_mesh": ("需要", "成長", "競合"),
    "mesh_business": ("需要",),
    "facilities": ("競合",),
    "stations": ("アクセス",),
}


def _dataset_warnings(prov: dict[str, Any]) -> list[str]:
    """Say which figures on this response rest on synthetic data."""
    kinds: dict[str, set[str]] = {}
    labels: dict[str, str] = {}
    for entry in prov.get("sources", []):
        kinds.setdefault(entry["dataset"], set()).add(entry["dataset_kind"])
        labels[entry["dataset"]] = entry["dataset_label"]

    sample = sorted(d for d, k in kinds.items() if "sample" in k)
    if not sample:
        return []

    affected: list[str] = []
    for dataset in sample:
        affected.extend(_DATASET_COMPONENTS.get(dataset, ()))
    affected = sorted(set(affected))

    warnings = [
        "合成（サンプル）データを含みます: "
        + "、".join(labels.get(d, d) for d in sample)
        + "。実データではありません。"
    ]
    if affected:
        warnings.append(
            "このため " + "・".join(f"{a}スコア" for a in affected)
            + " と総合スコアは実データに基づきません。"
        )
    unaffected = sorted(
        set().union(*(set(v) for v in _DATASET_COMPONENTS.values())) - set(affected)
    )
    if unaffected:
        warnings.append(
            "実データのみに基づく指標: " + "・".join(f"{a}スコア" for a in unaffected)
            + "、および商圏人口・歯科医院数・最寄り歯科距離。"
        )
    return warnings


def _analyze(conn: psycopg.Connection, lat: float, lng: float, radius_m: int,
             model: ScoringModel, category: str, mesh_size_m: int,
             prefecture_code: str, catchment: str = DEFAULT_CATCHMENT) -> dict[str, Any]:
    metrics = analyze_point(conn, lat, lng, radius_m, category, mesh_size_m, catchment)
    scope = scope_key(mesh_size_m, radius_m, prefecture_code)
    distributions = load_distributions(conn, scope)
    scores = model.score(metrics, distributions)

    warnings: list[str] = []
    if not distributions:
        warnings.append(
            f"スコア基準（{scope}）が未計算のため、相対スコアを算出できません。"
            "`kaigyou-etl refresh-stats` を実行してください。"
        )
    if not metrics.get("mesh_count"):
        warnings.append("この地点の商圏に人口メッシュデータがありません。")
    if metrics.get("nearest_station") is None:
        warnings.append("駅データが未取得のため、アクセス指標を算出できません。")

    # The census counts where people live, not where they are during business
    # hours. In office and entertainment districts the two differ by an order of
    # magnitude and the population-per-clinic ratio understates demand badly.
    #
    # What to say about that depends on whether the economic census is loaded.
    # Warning that daytime demand is invisible, when it is sitting in the
    # response, would be worse than saying nothing -- so the two cases are
    # separated rather than the old text being left to go stale.
    population = metrics.get("population")
    workers = metrics.get("workers")
    facility_count = metrics.get("facility_count") or 0
    resident_light = (population is not None and facility_count >= 5
                      and population / facility_count < 800)

    if workers is None:
        if resident_light:
            warnings.append(
                "人口は国勢調査の常住人口（夜間人口）です。この地点は歯科医院数に対して"
                "常住人口が極端に少なく、オフィス街・繁華街の可能性があります。"
                "昼間人口は含まれていないため、競合・需要スコアは実態を過小評価します。"
            )
    elif population and workers / population >= 3:
        warnings.append(
            f"この地点は常住人口 {population:,.0f} 人に対して従業者 {workers:,.0f} 人"
            f"（約{workers / population:.0f}倍）の就業地です。"
            "需要スコアには従業者数を織り込んでいますが、"
            "従業者数は昼間人口そのものではありません"
            "（通学者・来街者は含まれません）。"
        )

    return {
        "location": {"lat": lat, "lng": lng},
        "radius_m": radius_m,
        "population": _round(metrics.get("population")),
        "children": _round(metrics.get("age_0_14")),
        "working_age": _round(metrics.get("age_15_64")),
        "elderly": _round(metrics.get("age_65_plus")),
        "households": _round(metrics.get("households")),
        "population_growth": metrics.get("population_growth"),
        # Which shape these numbers came from. A population figure is not
        # interpretable without it: the same point can differ threefold.
        "catchment_kind": metrics.get("catchment_kind"),
        "catchment_area_km2": _round(metrics.get("catchment_area_km2"), 3),
        # Daytime side. None when the economic census is not loaded, which the
        # scoring layer treats as "unknown", not as "nobody works here".
        "workers": _round(metrics.get("workers")),
        "establishments": _round(metrics.get("establishments")),
        "dental_clinics": metrics.get("facility_count"),
        "population_per_clinic": _round(metrics.get("population_per_facility")),
        "workers_per_clinic": _round(metrics.get("workers_per_facility")),
        "nearest_clinic": {
            "name": metrics.get("nearest_facility_name"),
            "distance_m": _round(metrics.get("nearest_facility_distance_m"), 1),
        },
        "nearest_station": {
            "name": metrics.get("nearest_station"),
            "distance_m": _round(metrics.get("station_distance_m"), 1),
            "daily_passengers": metrics.get("daily_passengers"),
        },
        "mesh_count": metrics.get("mesh_count"),
        "scores": scores,
        "warnings": warnings,
    }


def _round(value: Any, digits: int = 0) -> Any:
    if value is None:
        return None
    return round(float(value), digits) if digits else int(round(float(value)))


@router.get("/candidate-analysis", summary="任意地点の商圏分析")
def candidate_analysis(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    radius: int = Query(1000, ge=100, le=10000, description="商圏半径（m）"),
    all_radii: bool = Query(True, description="設定済みの全半径について人口・競合も返す"),
    category: str = Query(DEFAULT_CATEGORY),
    mesh_size_m: int | None = Query(None, description="省略時は読み込み済みデータから自動判定"),
    prefecture_code: str | None = Query(
        None, description="省略時は読み込み済みデータから自動判定（人口が最大の都道府県）"),
    catchment: str = Query(DEFAULT_CATCHMENT, pattern="^(circle|walk)$",
                           description="商圏の形。circle=直線距離の円 / walk=街路網に沿った徒歩圏"),
    conn: psycopg.Connection = Depends(get_conn),
    model: ScoringModel = Depends(get_model),
) -> dict[str, Any]:
    prefecture_code = default_prefecture(conn, prefecture_code)
    mesh_size_m = resolve_mesh_size(conn, mesh_size_m, prefecture_code)
    result = _analyze(conn, lat, lng, radius, model, category, mesh_size_m,
                      prefecture_code, catchment)
    result["mesh_size_m"] = mesh_size_m
    # Which prefecture's normalisation these scores came from. Without it the
    # reader cannot tell that a 70 in Shizuoka is not a 70 in Tokyo.
    result["prefecture_code"] = prefecture_code
    # The shape itself, so the map draws what the numbers were measured in
    # rather than a circle of its own.
    result["catchment"] = catchment_geojson(conn, lat, lng, radius, catchment)
    if catchment == "walk" and result.get("catchment_kind") != "walk":
        status = walk_network_status(conn)
        result["warnings"].insert(0, {
            "not_migrated": "徒歩圏の算出に必要なテーブルがありません（kaigyou-etl migrate）。",
            "pgrouting_unavailable": "このデータベースでは pgRouting が使えないため、徒歩圏を算出できません。",
            "network_not_loaded": "街路ネットワーク（OpenStreetMap）が未取得のため、徒歩圏を算出できません。",
        }.get(status.get("reason", ""),
              "この地点の周辺に街路データがないため、徒歩圏を算出できませんでした。")
        + "円（直線距離）で算出しています。")

    if all_radii:
        by_radius = {}
        for r in model.radii:
            metrics = analyze_point(conn, lat, lng, r, category, mesh_size_m)
            by_radius[str(r)] = {
                "population": _round(metrics.get("population")),
                "children": _round(metrics.get("age_0_14")),
                "working_age": _round(metrics.get("age_15_64")),
                "elderly": _round(metrics.get("age_65_plus")),
                "households": _round(metrics.get("households")),
                # Daytime side. Null, not zero, when the economic census has not
                # been loaded -- the UI shows a dash rather than an empty street.
                "workers": _round(metrics.get("workers")),
                "establishments": _round(metrics.get("establishments")),
                "dental_clinics": metrics.get("facility_count"),
                "population_per_clinic": _round(metrics.get("population_per_facility")),
                "workers_per_clinic": _round(metrics.get("workers_per_facility")),
            }
        result["by_radius"] = by_radius
        result["clinic_counts"] = facility_counts(conn, lat, lng, model.radii, category)

    result["model"] = model.describe()
    result["provenance"] = provenance.for_tables(conn, ANALYSIS_TABLES)
    result["warnings"] = _dataset_warnings(result["provenance"]) + result["warnings"]
    result["disclaimer"] = DISCLAIMER
    result["score_disclaimer"] = SCORE_DISCLAIMER
    return result


@router.get("/rankings", summary="メッシュ単位の候補地ランキング")
def rankings(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    radius: int | None = Query(None, description="省略時は設定の mesh_scoring_radius_m"),
    min_population: int = Query(0, ge=0),
    area: str | None = Query(None, description="エリア名の部分一致で絞り込み"),
    prefecture_code: str | None = Query(
        None, description="省略時は読み込み済みデータから自動判定（人口が最大の都道府県）"),
    conn: psycopg.Connection = Depends(get_conn),
    model: ScoringModel = Depends(get_model),
) -> dict[str, Any]:
    radius_m = radius or model.mesh_scoring_radius_m
    # Scores from two prefectures are normalised against their own populations,
    # so a combined table would rank a Shizuoka mesh against a Tokyo one on
    # scales that were never comparable. One prefecture at a time, always.
    prefecture_code = default_prefecture(conn, prefecture_code)
    where = ["ms.profile = %s", "ms.radius_m = %s", "ms.overall_score IS NOT NULL",
             "COALESCE(ms.population, 0) >= %s", "pm.prefecture_code = %s"]
    params: list[Any] = [model.profile_name, radius_m, min_population, prefecture_code]
    if area:
        where.append("ms.area_label ILIKE %s")
        params.append(f"%{area}%")

    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT count(*) AS n FROM mesh_scores ms
                JOIN population_mesh pm ON pm.id = ms.mesh_id
                WHERE {' AND '.join(where)}""", params
        )
        total = cur.fetchone()["n"]

        cur.execute(
            f"""
            SELECT rank() OVER (ORDER BY ms.overall_score DESC) AS rank,
                   pm.mesh_code, ms.area_label,
                   ST_Y(pm.centroid) AS lat, ST_X(pm.centroid) AS lng,
                   ms.overall_score, ms.demand_score, ms.competition_score,
                   ms.growth_score, ms.accessibility_score,
                   ms.population, ms.age_0_14, ms.age_65_plus, ms.households,
                   ms.population_growth, ms.facility_count, ms.population_per_facility,
                   ms.nearest_station, ms.station_distance_m, ms.daily_passengers
            FROM mesh_scores ms
            JOIN population_mesh pm ON pm.id = ms.mesh_id
            WHERE {' AND '.join(where)}
            ORDER BY ms.overall_score DESC
            LIMIT %s OFFSET %s
            """,
            params + [limit, offset],
        )
        rows = cur.fetchall()

    items = []
    for row in rows:
        item = dict(row)
        for key in ("population", "age_0_14", "age_65_plus", "households",
                    "population_per_facility", "station_distance_m"):
            item[key] = _round(item.get(key))
        items.append(item)

    if not items:
        message = ("メッシュスコアが未計算です。"
                   "`kaigyou-etl refresh-stats && kaigyou-etl compute-scores` を実行してください。")
    else:
        message = None

    ranking_prov = provenance.for_tables(conn, ANALYSIS_TABLES + ["municipalities"])
    return {
        "items": items,
        "total": total,
        "mesh_size_m": resolve_mesh_size(conn, prefecture_code=prefecture_code),
        "prefecture_code": prefecture_code,
        "warnings": _dataset_warnings(ranking_prov),
        "limit": limit,
        "offset": offset,
        "radius_m": radius_m,
        "model": model.describe(),
        "message": message,
        "provenance": ranking_prov,
        "disclaimer": DISCLAIMER,
        "score_disclaimer": SCORE_DISCLAIMER,
    }


@router.get("/compare", summary="複数候補地の比較（最大3地点）")
def compare(
    points: str = Query(..., description="lat,lng をセミコロン区切りで最大3地点"),
    radius: int = Query(1000, ge=100, le=10000),
    labels: str | None = Query(None, description="地点名をセミコロン区切りで"),
    category: str = Query(DEFAULT_CATEGORY),
    mesh_size_m: int | None = Query(None, description="省略時は読み込み済みデータから自動判定"),
    prefecture_code: str | None = Query(
        None, description="省略時は読み込み済みデータから自動判定（人口が最大の都道府県）"),
    conn: psycopg.Connection = Depends(get_conn),
    model: ScoringModel = Depends(get_model),
) -> dict[str, Any]:
    prefecture_code = default_prefecture(conn, prefecture_code)
    mesh_size_m = resolve_mesh_size(conn, mesh_size_m, prefecture_code)
    parsed: list[tuple[float, float]] = []
    for chunk in points.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            lat_s, lng_s = chunk.split(",")
            parsed.append((float(lat_s), float(lng_s)))
        except ValueError as exc:
            raise HTTPException(status_code=400,
                                detail=f"invalid point {chunk!r}; expected lat,lng") from exc
    if not parsed:
        raise HTTPException(status_code=400, detail="at least one point is required")
    if len(parsed) > 3:
        raise HTTPException(status_code=400, detail="compare accepts at most 3 points")

    names = [n.strip() for n in (labels or "").split(";")] if labels else []
    results = []
    for i, (lat, lng) in enumerate(parsed):
        item = _analyze(conn, lat, lng, radius, model, category, mesh_size_m, prefecture_code)
        item["label"] = names[i] if i < len(names) and names[i] else f"候補地 {chr(65 + i)}"
        results.append(item)

    prov = provenance.for_tables(conn, ANALYSIS_TABLES)
    dataset_warnings = _dataset_warnings(prov)
    for item in results:
        item["warnings"] = dataset_warnings + item["warnings"]

    return {
        "radius_m": radius,
        "mesh_size_m": mesh_size_m,
        "locations": results,
        "model": model.describe(),
        "provenance": prov,
        "warnings": dataset_warnings,
        "disclaimer": DISCLAIMER,
        "score_disclaimer": SCORE_DISCLAIMER,
    }
