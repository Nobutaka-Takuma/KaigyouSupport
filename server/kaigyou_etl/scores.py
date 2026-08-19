"""Normalisation statistics and precomputed mesh scores.

Two batch jobs, run after every load:

``refresh-stats``   sweeps every mesh centroid at each configured radius and
                    stores the observed spread of each raw metric. Scores are
                    relative, so the scale has to come from the data that is
                    actually loaded -- not from constants.

``compute-scores``  applies a scoring profile to every mesh and stores the
                    result, which is what the ranking and the score heat map
                    read.
"""
from __future__ import annotations

from typing import Any

import psycopg

from kaigyou_core import config as cfg
from kaigyou_core.analysis import (
    DEFAULT_CATEGORY,
    has_official_boundaries,
    mesh_catchments,
    municipality_names_from_facilities,
    resolve_mesh_size,
)
from kaigyou_core.scoring import DISTRIBUTION_METRICS, ScoringModel, distributions_from_rows, scope_key


def refresh_stats(conn: psycopg.Connection, *, radii: list[int] | None = None,
                  mesh_size_m: int | None = None,
                  prefecture_code: str = "13",
                  facility_category: str = DEFAULT_CATEGORY) -> dict[str, Any]:
    mesh_size_m = resolve_mesh_size(conn, mesh_size_m, prefecture_code)
    if mesh_size_m is None:
        raise RuntimeError("no population mesh data loaded; nothing to compute statistics from")
    model = ScoringModel(cfg.scoring_config())
    radii = radii or sorted(set(model.radii + [model.mesh_scoring_radius_m]))
    summary: dict[str, Any] = {"radii": radii, "mesh_size_m": mesh_size_m, "scopes": {}}

    for radius in radii:
        rows = mesh_catchments(conn, radius, mesh_size_m=mesh_size_m,
                               prefecture_code=prefecture_code,
                               facility_category=facility_category)
        scope = scope_key(mesh_size_m, radius, prefecture_code)
        written = 0
        for metric in DISTRIBUTION_METRICS:
            values = sorted(
                float(r[metric]) for r in rows
                if r.get(metric) is not None
            )
            if len(values) < 2:
                continue
            stats = _describe(values)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO metric_distributions (
                        metric, scope, min_value, p05, p50, p95, max_value,
                        mean_value, stddev_value, sample_count, computed_at
                    ) VALUES (
                        %(metric)s, %(scope)s, %(min_value)s, %(p05)s, %(p50)s,
                        %(p95)s, %(max_value)s, %(mean_value)s, %(stddev_value)s,
                        %(sample_count)s, now()
                    )
                    ON CONFLICT (metric, scope) DO UPDATE SET
                        min_value = EXCLUDED.min_value,
                        p05 = EXCLUDED.p05, p50 = EXCLUDED.p50, p95 = EXCLUDED.p95,
                        max_value = EXCLUDED.max_value,
                        mean_value = EXCLUDED.mean_value,
                        stddev_value = EXCLUDED.stddev_value,
                        sample_count = EXCLUDED.sample_count,
                        computed_at = now()
                    """,
                    {"metric": metric, "scope": scope, **stats},
                )
            written += 1
        summary["scopes"][scope] = {"meshes": len(rows), "metrics": written}
    conn.commit()
    return summary


def compute_mesh_scores(conn: psycopg.Connection, *, profile: str | None = None,
                        radius_m: int | None = None,
                        mesh_size_m: int | None = None,
                        prefecture_code: str = "13",
                        facility_category: str = DEFAULT_CATEGORY) -> dict[str, Any]:
    mesh_size_m = resolve_mesh_size(conn, mesh_size_m, prefecture_code)
    if mesh_size_m is None:
        raise RuntimeError("no population mesh data loaded; nothing to score")
    model = ScoringModel(cfg.scoring_config(), profile)
    radius = radius_m or model.mesh_scoring_radius_m
    scope = scope_key(mesh_size_m, radius, prefecture_code)

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM metric_distributions WHERE scope = %s", (scope,))
        distributions = distributions_from_rows(cur.fetchall())
    if not distributions:
        raise RuntimeError(
            f"no normalisation statistics for scope {scope}; run refresh-stats first"
        )

    rows = mesh_catchments(conn, radius, mesh_size_m=mesh_size_m,
                           prefecture_code=prefecture_code,
                           facility_category=facility_category)

    # Area names for the ranking table. Official boundary polygons are the
    # right source; without them, fall back to the municipality recorded on
    # the nearby facilities, which is real published data either way.
    use_boundaries = has_official_boundaries(conn)
    labels = ({} if use_boundaries
              else _derive_area_labels(conn, mesh_size_m, prefecture_code))

    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM mesh_scores WHERE profile = %s AND radius_m = %s",
            (model.profile_name, radius),
        )
        for row in rows:
            scored = model.score(row, distributions)
            cur.execute(
                """
                INSERT INTO mesh_scores (
                    mesh_id, profile, radius_m, population, age_0_14, age_65_plus,
                    households, population_growth, facility_count,
                    population_per_facility, nearest_facility_distance_m,
                    nearest_station, station_distance_m, daily_passengers,
                    demand_score, competition_score, growth_score,
                    accessibility_score, overall_score, area_label, computed_at
                ) VALUES (
                    %(mesh_id)s, %(profile)s, %(radius_m)s, %(population)s,
                    %(age_0_14)s, %(age_65_plus)s, %(households)s,
                    %(population_growth)s, %(facility_count)s,
                    %(population_per_facility)s, %(nearest_facility_distance_m)s,
                    %(nearest_station)s, %(station_distance_m)s, %(daily_passengers)s,
                    %(demand)s, %(competition)s, %(growth)s, %(accessibility)s,
                    %(overall)s, %(area_label)s, now()
                )
                ON CONFLICT (mesh_id, profile, radius_m) DO UPDATE SET
                    overall_score = EXCLUDED.overall_score,
                    computed_at = now()
                """,
                {
                    "mesh_id": row["mesh_id"],
                    "profile": model.profile_name,
                    "radius_m": radius,
                    "population": row.get("population"),
                    "age_0_14": row.get("age_0_14"),
                    "age_65_plus": row.get("age_65_plus"),
                    "households": row.get("households"),
                    "population_growth": row.get("population_growth"),
                    "facility_count": row.get("facility_count"),
                    "population_per_facility": row.get("population_per_facility"),
                    "nearest_facility_distance_m": row.get("nearest_facility_distance_m"),
                    "nearest_station": row.get("nearest_station"),
                    "station_distance_m": row.get("station_distance_m"),
                    "daily_passengers": row.get("daily_passengers"),
                    "demand": scored["demand"],
                    "competition": scored["competition"],
                    "growth": scored["growth"],
                    "accessibility": scored["accessibility"],
                    "overall": scored["overall"],
                    "area_label": (
                        _boundary_label(conn, row["mesh_id"]) if use_boundaries
                        else labels.get(row["mesh_id"])
                    ),
                },
            )
    conn.commit()
    return {"profile": model.profile_name, "radius_m": radius,
            "area_label_source": ("municipalities" if use_boundaries
                                  else "derived_from_facility_addresses"),
            "mesh_size_m": mesh_size_m, "meshes_scored": len(rows)}


def _describe(values: list[float]) -> dict[str, Any]:
    """Percentiles and moments of a sorted list."""
    n = len(values)

    def pct(p: float) -> float:
        idx = min(n - 1, max(0, int(round(p * (n - 1)))))
        return values[idx]

    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    return {
        "min_value": values[0],
        "p05": pct(0.05),
        "p50": pct(0.50),
        "p95": pct(0.95),
        "max_value": values[-1],
        "mean_value": mean,
        "stddev_value": variance ** 0.5,
        "sample_count": n,
    }


def _boundary_label(conn: psycopg.Connection, mesh_id: int) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT mu.name
            FROM municipalities mu
            JOIN data_sources ds ON ds.id = mu.source_id AND ds.dataset_kind = 'official'
            JOIN population_mesh pm ON pm.id = %s
            WHERE ST_Contains(mu.geom, pm.centroid)
            LIMIT 1
            """,
            (mesh_id,),
        )
        row = cur.fetchone()
    return row["name"] if row else None


def _derive_area_labels(conn: psycopg.Connection, mesh_size_m: int,
                        prefecture_code: str) -> dict[int, str]:
    """Label each mesh with the municipality most of its nearby clinics are in."""
    names = municipality_names_from_facilities(conn, prefecture_code)
    if not names:
        return {}

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (m.id) m.id AS mesh_id, x.municipality_code
            FROM population_mesh m
            CROSS JOIN LATERAL (
                SELECT f.municipality_code, count(*) AS n
                FROM facilities f
                JOIN data_sources ds
                  ON ds.id = f.source_id AND ds.dataset_kind = 'official'
                WHERE f.municipality_code IS NOT NULL
                  AND ST_DWithin(f.geom::geography, m.centroid::geography, 1500)
                GROUP BY f.municipality_code
                ORDER BY n DESC
                LIMIT 1
            ) x
            WHERE m.mesh_size_m = %s AND m.prefecture_code = %s
            """,
            (mesh_size_m, prefecture_code),
        )
        rows = cur.fetchall()

    return {r["mesh_id"]: names[r["municipality_code"]]
            for r in rows if r["municipality_code"] in names}
