"""Map layers: clinics, stations, meshes, municipalities.

All four return GeoJSON built by PostGIS (``ST_AsGeoJSON``) so the geometry is
never reconstructed in Python.

Two things keep these usable over a phone connection:

* **Only what is on screen.** Every layer takes a bbox. Tokyo's 51,384 clinics
  are 7MB; the thousand or so inside a city-level viewport are well under one.
* **Only what is drawn.** The map needs a name and a position, not the opening
  hours -- most of that 7MB was properties, not coordinates. ``fields=full``
  asks for the rest when something actually needs it.
"""
from __future__ import annotations

import json
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Query

from kaigyou_api.deps import feature_collection, get_conn, parse_bbox
from kaigyou_core import provenance
from kaigyou_core.db import table_exists

router = APIRouter()

_BBOX_SQL = "ST_MakeEnvelope(%s, %s, %s, %s, 4326)"

#: Decimal places kept in emitted coordinates. Six is ~10cm, far finer than
#: anything drawn at these zooms, and a third smaller than the default nine.
COORD_DIGITS = 6


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
    fields: str = Query("points", pattern="^(points|minimal|full)$"),
    limit: int = Query(5000, ge=1, le=20000),
) -> dict[str, Any]:
    # At city zoom the viewport holds ~3,500 clinics. Their names and addresses
    # are a megabyte that nothing on screen draws -- the dots need a position.
    # The popup fetches the one record it is about.
    select = {
        "points": "id",
        "minimal": "id, name, address",
        "full": ("id, facility_id, name, address, facility_category, clinic_types, "
                 "opening_date, founder_type, attributes, source_id, source_date"),
    }[fields]

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
            SELECT {select}, ST_AsGeoJSON(geom, {COORD_DIGITS}) AS geojson
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


@router.get("/clinics/{facility_id}", summary="歯科医院1件の詳細")
def clinic_detail(
    facility_id: int,
    conn: psycopg.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """One facility, for the map popup."""
    from fastapi import HTTPException

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.id, f.facility_id, f.name, f.address, f.facility_category,
                   f.clinic_types, f.opening_date, f.founder_type, f.attributes,
                   f.source_date, ds.name AS source_name
            FROM facilities f
            JOIN data_sources ds ON ds.id = f.source_id
            WHERE f.id = %s
            """,
            (facility_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return row


@router.get("/stations", summary="駅レイヤー")
def stations(
    conn: psycopg.Connection = Depends(get_conn),
    bbox: str | None = Query(None),
    q: str | None = Query(None, description="駅名の部分一致検索"),
    fields: str = Query("minimal", pattern="^(minimal|full)$"),
    limit: int = Query(3000, ge=1, le=20000),
) -> dict[str, Any]:
    select = ("id, name, daily_passengers" if fields == "minimal" else
              "id, station_id, name, operator, railway_line, daily_passengers, "
              "passengers_year, attributes, source_id, source_date")

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
            SELECT {select}, ST_AsGeoJSON(geom, {COORD_DIGITS}) AS geojson
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
    mesh_size_m: int | None = Query(None, description="省略時は読み込み済みデータから自動判定"),
    profile: str | None = Query(None, description="指定するとスコアも返す"),
    radius_m: int | None = Query(None),
    limit: int = Query(4000, ge=1, le=40000),
) -> dict[str, Any]:
    """Mesh polygons with their population attributes, and -- when a scoring
    profile is named -- the precomputed score for the heat map."""
    from kaigyou_core.analysis import resolve_mesh_size

    box = parse_bbox(bbox)
    # From the viewport, not from the whole database. Two prefectures can be
    # published at different resolutions, and one answer for the lot draws one
    # of them and leaves the other blank as you pan into it.
    mesh_size_m = resolve_mesh_size(conn, mesh_size_m, bbox=box) or \
        resolve_mesh_size(conn, mesh_size_m)
    where = ["m.mesh_size_m = %s"]
    params: list[Any] = [mesh_size_m]
    if box:
        where.append(f"m.geom && {_BBOX_SQL}")
        params.extend(box)

    # Workers live in their own table with their own mesh set, so this is a
    # LEFT JOIN on the code: a mesh with residents and no businesses keeps a
    # null here rather than a zero it never reported.
    #
    # Only when the table is there. A deployment applies code before someone
    # runs the migration, and joining a table that does not exist yet would
    # turn the whole map into a 500 during that window.
    join, select = "", ""
    if table_exists(conn, "mesh_business"):
        join = """
            LEFT JOIN mesh_business b
                   ON b.mesh_code = m.mesh_code AND b.mesh_size_m = m.mesh_size_m
        """
        select = ", b.workers, b.establishments"
    if profile:
        join += """
            LEFT JOIN mesh_scores ms
                   ON ms.mesh_id = m.id AND ms.profile = %s
                  AND (%s::int IS NULL OR ms.radius_m = %s)
        """
        select += (", ms.overall_score, ms.demand_score, ms.competition_score,"
                   " ms.growth_score, ms.accessibility_score, ms.facility_count,"
                   " ms.population_per_facility, ms.area_label")
        params = [profile, radius_m, radius_m] + params

    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT m.id, m.mesh_code, m.population, m.age_0_14, m.age_65_plus,
                   m.households, m.population_growth,
                   ST_AsGeoJSON(m.geom, {COORD_DIGITS}) AS geojson{select}
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
        mesh_size_m=mesh_size_m,
        provenance=provenance.for_tables(conn, ["population_mesh", "mesh_business"]),
        truncated=len(rows) >= limit,
    )


@router.get("/municipalities", summary="市区町村境界")
def municipalities(
    conn: psycopg.Connection = Depends(get_conn),
    prefecture_code: str | None = Query(None),
    bbox: str | None = Query(None, description="min_lng,min_lat,max_lng,max_lat"),
    simplify_deg: float = Query(0.0005, ge=0.0, le=0.05,
                                description="表示用の単純化許容誤差（度）"),
) -> dict[str, Any]:
    """Boundaries, simplified for display and clipped to the viewport.

    Unsimplified these are 12MB -- Ogasawara alone is 234k vertices. Most of
    what survives simplification is island coastline a thousand kilometres from
    the mainland, so the bbox matters more than the tolerance does.
    """
    where, params = ["true"], []
    if prefecture_code:
        where.append("prefecture_code = %s")
        params.append(prefecture_code)
    box = parse_bbox(bbox)
    if box:
        where.append(f"geom && {_BBOX_SQL}")
        params.extend(box)
    params.insert(0, simplify_deg)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT municipality_code, name, prefecture_code, prefecture_name,
                   source_id, source_date,
                   ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom, %s),
                                {COORD_DIGITS}) AS geojson
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
