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
    DEFAULT_CATEGORY,
    DEFAULT_MESH_SIZE_M,
    analyze_point,
    facility_counts,
    load_distributions,
)
from kaigyou_core.scoring import ScoringModel, scope_key

router = APIRouter()

ANALYSIS_TABLES = ["population_mesh", "facilities", "stations"]


def _dataset_warnings(prov: dict[str, Any]) -> list[str]:
    """Warn about combinations that make a derived figure meaningless.

    Ratios like 人口/歯科医院 divide one dataset by another. If one side is real
    and the other synthetic the quotient is not an estimate of anything, and
    saying only "this database contains sample data" is not enough -- the
    number itself has to be called out.
    """
    kinds: dict[str, set[str]] = {}
    for entry in prov.get("sources", []):
        kinds.setdefault(entry["dataset_label"], set()).add(entry["dataset_kind"])

    sample = sorted(label for label, k in kinds.items() if "sample" in k)
    official = sorted(label for label, k in kinds.items() if "official" in k)
    warnings: list[str] = []
    if sample:
        warnings.append(
            "合成（サンプル）データを含みます: " + "、".join(sample) + "。実データではありません。"
        )
    if sample and official:
        warnings.append(
            "実データ（" + "、".join(official) + "）と合成データ（" + "、".join(sample) + "）を"
            "組み合わせた指標（人口/歯科医院、需要スコア等）は意味を持ちません。"
        )
    return warnings


def _analyze(conn: psycopg.Connection, lat: float, lng: float, radius_m: int,
             model: ScoringModel, category: str, mesh_size_m: int,
             prefecture_code: str) -> dict[str, Any]:
    metrics = analyze_point(conn, lat, lng, radius_m, category, mesh_size_m)
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

    return {
        "location": {"lat": lat, "lng": lng},
        "radius_m": radius_m,
        "population": _round(metrics.get("population")),
        "children": _round(metrics.get("age_0_14")),
        "working_age": _round(metrics.get("age_15_64")),
        "elderly": _round(metrics.get("age_65_plus")),
        "households": _round(metrics.get("households")),
        "population_growth": metrics.get("population_growth"),
        "dental_clinics": metrics.get("facility_count"),
        "population_per_clinic": _round(metrics.get("population_per_facility")),
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
    mesh_size_m: int = Query(DEFAULT_MESH_SIZE_M),
    prefecture_code: str = Query("13"),
    conn: psycopg.Connection = Depends(get_conn),
    model: ScoringModel = Depends(get_model),
) -> dict[str, Any]:
    result = _analyze(conn, lat, lng, radius, model, category, mesh_size_m, prefecture_code)

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
                "dental_clinics": metrics.get("facility_count"),
                "population_per_clinic": _round(metrics.get("population_per_facility")),
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
    conn: psycopg.Connection = Depends(get_conn),
    model: ScoringModel = Depends(get_model),
) -> dict[str, Any]:
    radius_m = radius or model.mesh_scoring_radius_m
    where = ["ms.profile = %s", "ms.radius_m = %s", "ms.overall_score IS NOT NULL",
             "COALESCE(ms.population, 0) >= %s"]
    params: list[Any] = [model.profile_name, radius_m, min_population]
    if area:
        where.append("ms.area_label ILIKE %s")
        params.append(f"%{area}%")

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*) AS n FROM mesh_scores ms WHERE {' AND '.join(where)}", params
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
    mesh_size_m: int = Query(DEFAULT_MESH_SIZE_M),
    prefecture_code: str = Query("13"),
    conn: psycopg.Connection = Depends(get_conn),
    model: ScoringModel = Depends(get_model),
) -> dict[str, Any]:
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
        "locations": results,
        "model": model.describe(),
        "provenance": prov,
        "warnings": dataset_warnings,
        "disclaimer": DISCLAIMER,
        "score_disclaimer": SCORE_DISCLAIMER,
    }
