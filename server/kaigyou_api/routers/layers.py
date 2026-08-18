"""Map layers: clinics, stations, meshes, municipalities.

All four return GeoJSON built by PostGIS (``ST_AsGeoJSON``) so the geometry is
never reconstructed in Python.
"""
from __future__ import annotations

import json
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Query

from kaigyou_api.deps import feature_collection, get_conn, parse_bbox
from kaigyou_core import provenance

router = APIRouter()

_BBOX_SQL = "ST_MakeEnvelope(%s, %s, %s, %s, 4326)"


def _features(rows: list[dict[str, Any]], geom_key: str = "geojson") -> list[dict[str, Any]]:
    out = []
    for row in rows:
        geometry = json.loads(row.pop(geom_key))
        out.append({"type": "Feature", "geometry": geometry, "properties": row})
    return out


@router.get("/clinics", summary="歯科医院（施設）レイヤー")
def clinics(
    conn: psycopg.Connection = Depends(get_conn),
    bbox: str | None = Query(None, description="min_lng,min_lat,max_lng,max_lat"),
    category: str = Query("dental_clinic"),
    clinic_type: str | None = Query(None, description="標榜診療科で絞り込み"),
    limit: int = Query(5000, ge=1, le=20000),
) -> dict[str, Any]:
    where = ["facility_category = %s"]
    params: list[Any] = [category]
    box = parse_bbox(bbox)
    if box:
        where.append(f"geom && {_BBOX_SQL}")
        params.extend(box)
    if clinic_type:
        where.append("%s = ANY(clinic_types)")
        params.append(clinic_type)
    params.append(limit)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, facility_id, name, address, facility_category, clinic_types,
                   opening_date, source_id, source_date,
                   ST_AsGeoJSON(geom) AS geojson
            FROM facilities
            WHERE {' AND '.join(where)}
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()

    return feature_collection(
        _features(rows),
        provenance=provenance.for_tables(conn, ["facilities"]),
        truncated=len(rows) >= limit,
    )


@router.get("/stations", summary="駅レイヤー")
def stations(
    conn: psycopg.Connection = Depends(get_conn),
    bbox: str | None = Query(None),
    q: str | None = Query(None, description="駅名の部分一致検索"),
    limit: int = Query(3000, ge=1, le=20000),
) -> dict[str, Any]:
    where = ["true"]
    params: list[Any] = []
    box = parse_bbox(bbox)
    if box:
        where.append(f"geom && {_BBOX_SQL}")
        params.extend(box)
    if q:
        where.append("name ILIKE %s")
        params.append(f"%{q}%")
    params.append(limit)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, station_id, name, operator, railway_line, daily_passengers,
                   source_id, source_date, ST_AsGeoJSON(geom) AS geojson
            FROM stations
            WHERE {' AND '.join(where)}
            ORDER BY daily_passengers DESC NULLS LAST
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()

    return feature_collection(
        _features(rows),
        provenance=provenance.for_tables(conn, ["stations"]),
        truncated=len(rows) >= limit,
    )


@router.get("/meshes", summary="人口メッシュ / スコアメッシュ")
def meshes(
    conn: psycopg.Connection = Depends(get_conn),
    bbox: str | None = Query(None),
    mesh_size_m: int = Query(1000, description="メッシュサイズ。データ側の値と一致させる"),
    profile: str | None = Query(None, description="指定するとスコアも返す"),
    radius_m: int | None = Query(None),
    limit: int = Query(8000, ge=1, le=40000),
) -> dict[str, Any]:
    """Mesh polygons with their population attributes, and -- when a scoring
    profile is named -- the precomputed score for the heat map."""
    where = ["m.mesh_size_m = %s"]
    params: list[Any] = [mesh_size_m]
    box = parse_bbox(bbox)
    if box:
        where.append(f"m.geom && {_BBOX_SQL}")
        params.extend(box)

    join, select = "", ""
    if profile:
        join = """
            LEFT JOIN mesh_scores ms
                   ON ms.mesh_id = m.id AND ms.profile = %s
                  AND (%s::int IS NULL OR ms.radius_m = %s)
        """
        select = (", ms.overall_score, ms.demand_score, ms.competition_score,"
                  " ms.growth_score, ms.accessibility_score, ms.facility_count,"
                  " ms.population_per_facility, ms.area_label, ms.radius_m")
        params = [profile, radius_m, radius_m] + params

    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT m.id, m.mesh_code, m.mesh_size_m, m.population, m.age_0_14,
                   m.age_15_64, m.age_65_plus, m.households, m.population_growth,
                   m.source_id, m.source_date,
                   ST_AsGeoJSON(m.geom) AS geojson{select}
            FROM population_mesh m
            {join}
            WHERE {' AND '.join(where)}
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()

    return feature_collection(
        _features(rows),
        provenance=provenance.for_tables(conn, ["population_mesh"]),
        truncated=len(rows) >= limit,
    )


@router.get("/municipalities", summary="市区町村境界")
def municipalities(
    conn: psycopg.Connection = Depends(get_conn),
    prefecture_code: str | None = Query(None),
    simplify_deg: float = Query(0.0005, ge=0.0, le=0.05,
                                description="表示用の単純化許容誤差（度）"),
) -> dict[str, Any]:
    where, params = ["true"], []
    if prefecture_code:
        where.append("prefecture_code = %s")
        params.append(prefecture_code)
    params.insert(0, simplify_deg)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT municipality_code, name, prefecture_code, prefecture_name,
                   source_id, source_date,
                   ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom, %s)) AS geojson
            FROM municipalities
            WHERE {' AND '.join(where)}
            """,
            params,
        )
        rows = cur.fetchall()

    return feature_collection(
        _features(rows),
        provenance=provenance.for_tables(conn, ["municipalities"]),
    )
