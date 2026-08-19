"""Catchment analysis.

Thin Python over the PostGIS functions in ``db/migrations/005_functions.sql``.
The spatial work -- metric buffers, area-weighted population apportionment,
nearest-neighbour lookups -- stays in the database; this module only shapes the
results and feeds them to the scoring model.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

import psycopg

from kaigyou_core.scoring import Distribution, ScoringModel, distributions_from_rows, scope_key

DEFAULT_CATEGORY = "dental_clinic"

#: Fallback only. The real value comes from whatever mesh data is loaded --
#: see :func:`resolve_mesh_size`. Hard-coding it would mean that loading 500m
#: meshes silently returns zero population everywhere, which is the worst
#: possible failure mode: plausible numbers that are simply wrong.
DEFAULT_MESH_SIZE_M = 1000

# Column names produced by kg_analyze_point, renamed to the vocabulary the
# scoring model and the API use.
_RENAME = {
    "nearest_station_name": "nearest_station",
    "nearest_station_distance_m": "station_distance_m",
    "nearest_station_passengers": "daily_passengers",
}


def resolve_mesh_size(conn: psycopg.Connection, requested: int | None = None,
                      prefecture_code: str | None = None) -> int | None:
    """The mesh resolution to analyse at.

    An explicit request wins. Otherwise pick the resolution that actually
    carries the most population in the database, so the analysis follows the
    data rather than a constant.
    """
    if requested:
        return requested
    sql = """
        SELECT mesh_size_m
        FROM population_mesh
        {where}
        GROUP BY mesh_size_m
        ORDER BY sum(COALESCE(population, 0)) DESC, mesh_size_m
        LIMIT 1
    """
    params: tuple = ()
    if prefecture_code:
        sql = sql.format(where="WHERE prefecture_code = %s")
        params = (prefecture_code,)
    else:
        sql = sql.format(where="")
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return row["mesh_size_m"] if row else None


def available_mesh_sizes(conn: psycopg.Connection) -> list[int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT mesh_size_m FROM population_mesh ORDER BY mesh_size_m"
        )
        return [r["mesh_size_m"] for r in cur.fetchall()]


def analyze_point(conn: psycopg.Connection, lat: float, lng: float, radius_m: int,
                  facility_category: str = DEFAULT_CATEGORY,
                  mesh_size_m: int = DEFAULT_MESH_SIZE_M) -> dict[str, Any]:
    """Raw catchment metrics for one point. No scoring applied."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM kg_analyze_point(%s, %s, %s, %s, %s)",
            (lat, lng, radius_m, facility_category, mesh_size_m),
        )
        row = cur.fetchone() or {}
    metrics = {_RENAME.get(k, k): v for k, v in row.items()}
    metrics["radius_m"] = radius_m
    return metrics


def facility_counts(conn: psycopg.Connection, lat: float, lng: float,
                    radii: Sequence[int],
                    facility_category: str = DEFAULT_CATEGORY) -> dict[int, int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT radius_m, facility_count FROM kg_facility_counts(%s, %s, %s, %s)",
            (lat, lng, [float(r) for r in radii], facility_category),
        )
        return {int(r["radius_m"]): r["facility_count"] for r in cur.fetchall()}


def load_distributions(conn: psycopg.Connection, scope: str) -> dict[str, Distribution]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM metric_distributions WHERE scope = %s", (scope,))
        return distributions_from_rows(cur.fetchall())


def score_point(conn: psycopg.Connection, lat: float, lng: float, radius_m: int,
                model: ScoringModel, *, facility_category: str = DEFAULT_CATEGORY,
                mesh_size_m: int = DEFAULT_MESH_SIZE_M,
                prefecture_code: str = "13") -> dict[str, Any]:
    """Analyse a point and score it against the observed mesh distribution."""
    metrics = analyze_point(conn, lat, lng, radius_m, facility_category, mesh_size_m)
    scope = scope_key(mesh_size_m, radius_m, prefecture_code)
    distributions = load_distributions(conn, scope)
    scores = model.score(metrics, distributions)
    scores["normalization_scope"] = scope
    scores["normalization_sample_count"] = max(
        (d.sample_count for d in distributions.values()), default=0
    )
    return {"metrics": metrics, "scores": scores}


def mesh_catchments(conn: psycopg.Connection, radius_m: int, *,
                    mesh_size_m: int = DEFAULT_MESH_SIZE_M,
                    prefecture_code: str = "13",
                    facility_category: str = DEFAULT_CATEGORY) -> list[dict[str, Any]]:
    """Catchment metrics for every mesh centroid.

    Expressed as one set-based query with a LATERAL join so PostGIS does the
    whole sweep in a single round trip; this is what feeds both the
    normalisation statistics and the ranking / heat map.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.id AS mesh_id, m.mesh_code,
                   ST_Y(m.centroid) AS lat, ST_X(m.centroid) AS lng,
                   a.*
            FROM population_mesh m
            CROSS JOIN LATERAL kg_analyze_point(
                ST_Y(m.centroid), ST_X(m.centroid), %s, %s, %s
            ) AS a
            WHERE m.mesh_size_m = %s
              AND m.prefecture_code = %s
              AND COALESCE(m.population, 0) > 0
            """,
            (radius_m, facility_category, mesh_size_m, mesh_size_m, prefecture_code),
        )
        rows = cur.fetchall()
    return [{_RENAME.get(k, k): v for k, v in row.items()} for row in rows]


def area_label(conn: psycopg.Connection, mesh_id: int) -> str | None:
    """Name the municipality a mesh centre falls in, for the ranking table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT mu.name
            FROM population_mesh m
            JOIN municipalities mu ON ST_Contains(mu.geom, m.centroid)
            WHERE m.id = %s
            LIMIT 1
            """,
            (mesh_id,),
        )
        row = cur.fetchone()
    return row["name"] if row else None


# --------------------------------------------------------------- area labels
# JIS X0402 name, as it appears inside a full address string.
_MUNICIPALITY_RE = __import__("re").compile(
    r"^(?:.{2,3}?[都道府県])?(.+?[区市町村])"
)


def municipality_names_from_facilities(
    conn: psycopg.Connection, prefecture_code: str | None = None
) -> dict[str, str]:
    """Derive a JIS-code -> municipality-name map from facility addresses.

    A stand-in for the boundary dataset: when 行政区域 polygons have not been
    loaded, the published facility addresses still carry the municipality, and
    naming a mesh "江東区" from real addresses beats leaving it blank. Callers
    are expected to say which of the two produced a label.
    """
    where = ["f.address IS NOT NULL", "f.municipality_code IS NOT NULL"]
    params: list[Any] = []
    if prefecture_code:
        where.append("f.prefecture_code = %s")
        params.append(prefecture_code)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT f.municipality_code, f.address
            FROM facilities f
            JOIN data_sources ds ON ds.id = f.source_id AND ds.dataset_kind = 'official'
            WHERE {' AND '.join(where)}
            """,
            params,
        )
        rows = cur.fetchall()

    tally: dict[str, dict[str, int]] = {}
    for row in rows:
        match = _MUNICIPALITY_RE.match(row["address"].strip())
        if not match:
            continue
        name = match.group(1)
        counts = tally.setdefault(row["municipality_code"], {})
        counts[name] = counts.get(name, 0) + 1

    # The modal spelling wins; stray typos in individual addresses drop out.
    return {code: max(counts.items(), key=lambda kv: kv[1])[0]
            for code, counts in tally.items() if counts}


def has_official_boundaries(conn: psycopg.Connection) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM municipalities m
                JOIN data_sources ds ON ds.id = m.source_id
                WHERE ds.dataset_kind = 'official'
            ) AS present
            """
        )
        return bool(cur.fetchone()["present"])
