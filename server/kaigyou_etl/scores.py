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

from typing import Any, Mapping, Sequence

import psycopg
from psycopg.types.json import Json

from kaigyou_core import config as cfg
from kaigyou_core.analysis import (
    DEFAULT_CATEGORY,
    has_official_boundaries,
    mesh_catchments,
    municipality_names_from_facilities,
    resolve_mesh_size,
)
from kaigyou_core.scoring import (
    DISTRIBUTION_METRICS,
    ScoringModel,
    augment_specialty_metrics,
    competition_specialties,
    derived_metrics,
    distributions_from_rows,
    normalization_reference,
    scope_key,
    specialty_count_metric,
)

#: How far a mesh centre may sit outside every municipality and still borrow
#: the nearest one's name. Waterfront reclaimed land is the case this exists
#: for; beyond it the mesh is left unlabelled rather than guessed at.
BOUNDARY_FALLBACK_RADIUS_M = 3000



def _make_room(conn: psycopg.Connection, progress: Any = None) -> None:
    """Ask the server for a longer statement timeout, and say if it refuses.

    Every statement here is batched to be short, but "short" is relative to a
    laptop; over a pooled connection to a hosted database the same batch can
    take a minute, and Supabase cancels at two. Raising the session limit costs
    nothing when the work is quick and is the difference between finishing and
    never finishing when it is not.
    """
    from kaigyou_core.db import ETL_STATEMENT_TIMEOUT_MS, relax_statement_timeout

    if not relax_statement_timeout(conn) and progress:
        progress("  注意: statement_timeout を延長できませんでした"
                 f"（{ETL_STATEMENT_TIMEOUT_MS // 1000}秒を要求）。"
                 "サーバ側の制限のままで実行します。")


def refresh_stats(conn: psycopg.Connection, *, radii: list[int] | None = None,
                  mesh_size_m: int | None = None,
                  prefecture_code: str = "13",
                  facility_category: str = DEFAULT_CATEGORY,
                  progress: Any = None) -> dict[str, Any]:
    _make_room(conn, progress)
    mesh_size_m = resolve_mesh_size(conn, mesh_size_m, prefecture_code)
    if mesh_size_m is None:
        raise RuntimeError("no population mesh data loaded; nothing to compute statistics from")
    config = cfg.scoring_config()
    model = ScoringModel(config)
    radii = radii or sorted(set(model.radii + [model.mesh_scoring_radius_m]))
    # 科目で絞った比率は、全科目の比率とは別の分布を持ちます。どの科目が要るかは
    # プロファイルの設定が決めるので、ここで設定から引いてきます。
    pairs = competition_specialties(config)
    metrics = list(DISTRIBUTION_METRICS) + derived_metrics(config)
    reference = normalization_reference(config)
    min_reference = int((config.get("normalization") or {}).get("min_reference_sample", 50))
    summary: dict[str, Any] = {"radii": radii, "mesh_size_m": mesh_size_m,
                               "normalization_reference": reference,
                               "specialty_metrics": derived_metrics(config), "scopes": {}}

    for radius in radii:
        if progress:
            progress(f"  半径 {radius}m の分布を集計しています...")
        rows = mesh_catchments(conn, radius, mesh_size_m=mesh_size_m,
                               prefecture_code=prefecture_code,
                               facility_category=facility_category,
                               progress=progress)
        for row in rows:
            augment_specialty_metrics(row, pairs)

        # 目盛りを作る集合。県全域から作ると、農村が大半を占める県では p95 が
        # 市街地の下限より低いところに来て、市街地がどこでも上限に張り付きます
        # （合成した農村県では、人口が 2.3 倍違う市街地 24 件が全部ちょうど
        # 100 点になりました）。開業地を選ぶ人が比べたいのは候補地どうしです。
        scale_rows, used_reference, fallback = _reference_rows(
            rows, reference, min_reference)
        if fallback and progress:
            progress(f"  注意: 歯科医院のある商圏が{len(scale_rows)}件しかないため、"
                     f"目盛りは人口のある商圏すべてから作ります"
                     f"（{min_reference}件以上で候補地のみに切り替わります）。")
        scope = scope_key(mesh_size_m, radius, prefecture_code, used_reference)
        written = 0
        for metric in metrics:
            values = sorted(
                float(r[metric]) for r in scale_rows
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
        summary["scopes"][scope] = {
            "meshes_swept": len(rows),
            "meshes_in_scale": len(scale_rows),
            "reference": used_reference,
            "metrics": written,
        }
    conn.commit()
    return summary



def _reference_rows(rows: Sequence[Mapping[str, Any]], reference: str,
                    minimum: int) -> tuple[list[Mapping[str, Any]], str, bool]:
    """正規化の目盛りを作る行を選ぶ。

    ``with_clinics`` は「歯科医院が実在する商圏」。閾値をひとつも置かずに、
    山林と生活圏を分けられます（誰も山の中では開業していないので）。
    候補地が少なすぎる県では目盛りが作れないので、全件に戻して、戻したことを
    呼び出し元に返します。黙って戻すと、県によって意味の違う目盛りが同じ名前で
    並ぶことになります。
    """
    if reference != "with_clinics":
        return list(rows), "all", False
    candidates = [r for r in rows if (r.get("facility_count") or 0) > 0]
    if len(candidates) < minimum:
        return list(rows), "all", True
    return candidates, "with_clinics", False


def compute_mesh_scores(conn: psycopg.Connection, *, profile: str | None = None,
                        profiles: Sequence[str] | None = None,
                        radius_m: int | None = None,
                        mesh_size_m: int | None = None,
                        prefecture_code: str = "13",
                        facility_category: str = DEFAULT_CATEGORY,
                        progress: Any = None) -> dict[str, Any]:
    """Score every mesh, under one profile or several.

    The expensive part -- sweeping a trade area around all 5,449 mesh centres
    in PostGIS -- does not depend on the profile at all. Only the arithmetic
    afterwards does. So scoring a second profile costs almost nothing as long
    as the sweep is shared, which is why `profiles` exists: the UI offers every
    configured profile, so every configured profile should have numbers.
    """
    _make_room(conn, progress)
    mesh_size_m = resolve_mesh_size(conn, mesh_size_m, prefecture_code)
    if mesh_size_m is None:
        raise RuntimeError("no population mesh data loaded; nothing to score")
    config = cfg.scoring_config()
    models = ([ScoringModel(config, name) for name in profiles] if profiles
              else [ScoringModel(config, profile)])
    radius = radius_m or models[0].mesh_scoring_radius_m
    reference = normalization_reference(config)
    scope = scope_key(mesh_size_m, radius, prefecture_code, reference)

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM metric_distributions WHERE scope = %s", (scope,))
        distributions = distributions_from_rows(cur.fetchall())
    if not distributions:
        # 候補地が少ない県では refresh-stats が all に落としています。同じ
        # 判断をここでも繰り返すより、書かれている方を探すほうが確実です。
        for alternative in ("all", "with_clinics"):
            fallback_scope = scope_key(mesh_size_m, radius, prefecture_code, alternative)
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM metric_distributions WHERE scope = %s",
                            (fallback_scope,))
                distributions = distributions_from_rows(cur.fetchall())
            if distributions:
                scope = fallback_scope
                break
    if not distributions:
        raise RuntimeError(
            f"no normalisation statistics for scope {scope}; run refresh-stats first"
        )

    rows = mesh_catchments(conn, radius, mesh_size_m=mesh_size_m,
                           prefecture_code=prefecture_code,
                           facility_category=facility_category,
                           progress=progress)
    pairs = competition_specialties(config)
    for row in rows:
        augment_specialty_metrics(row, pairs)

    # Area names for the ranking table. Official boundary polygons are the
    # right source; without them, fall back to the municipality recorded on
    # the nearby facilities, which is real published data either way.
    use_boundaries = has_official_boundaries(conn)
    labels = (_boundary_area_labels(conn, mesh_size_m, prefecture_code, progress)
              if use_boundaries
              else _derive_area_labels(conn, mesh_size_m, prefecture_code, progress))

    # One statement, then one batch per profile: 5,449 rows at a round trip each
    # is fine against localhost and minutes of waiting against a hosted database.
    scored_profiles = []
    with conn.cursor() as cur:
        for model in models:
            # This prefecture's scores only. Without the join, scoring
            # Shizuoka deleted Tokyo's ranking -- the same mistake as the
            # unqualified delete in the mesh loaders, one layer up, and just
            # as invisible: the command reports success either way.
            cur.execute(
                """
                DELETE FROM mesh_scores ms
                USING population_mesh pm
                WHERE pm.id = ms.mesh_id
                  AND ms.profile = %s AND ms.radius_m = %s
                  AND pm.prefecture_code = %s
                """,
                (model.profile_name, radius, prefecture_code),
            )
            batch = [_score_row(model, row, distributions, radius, labels)
                     for row in rows]
            for start in range(0, len(batch), 500):
                cur.executemany(
                """
                INSERT INTO mesh_scores (
                    mesh_id, profile, radius_m, land_price_yen_per_sqm, cost_score,
                    facility_specialty_counts, facility_specialty_count,
                    population, age_0_14, age_15_64, age_65_plus, workers, establishments,
                    households, population_growth, facility_count,
                    population_per_facility, nearest_facility_distance_m,
                    nearest_station, station_distance_m, daily_passengers,
                    demand_score, competition_score, growth_score,
                    accessibility_score, overall_score, area_label, computed_at
                ) VALUES (
                    %(mesh_id)s, %(profile)s, %(radius_m)s,
                    %(land_price_yen_per_sqm)s, %(cost)s,
                    %(facility_specialty_counts)s, %(facility_specialty_count)s,
                    %(population)s,
                    %(age_0_14)s, %(age_15_64)s, %(age_65_plus)s, %(workers)s,
                    %(establishments)s, %(households)s,
                    %(population_growth)s, %(facility_count)s,
                    %(population_per_facility)s, %(nearest_facility_distance_m)s,
                    %(nearest_station)s, %(station_distance_m)s, %(daily_passengers)s,
                    %(demand)s, %(competition)s, %(growth)s, %(accessibility)s,
                    %(overall)s, %(area_label)s, now()
                )
                ON CONFLICT (mesh_id, profile, radius_m) DO UPDATE SET
                    overall_score = EXCLUDED.overall_score,
                    cost_score = EXCLUDED.cost_score,
                    land_price_yen_per_sqm = EXCLUDED.land_price_yen_per_sqm,
                    facility_specialty_counts = EXCLUDED.facility_specialty_counts,
                    facility_specialty_count = EXCLUDED.facility_specialty_count,
                    age_15_64 = EXCLUDED.age_15_64,
                    workers = EXCLUDED.workers,
                    establishments = EXCLUDED.establishments,
                    competition_score = EXCLUDED.competition_score,
                    demand_score = EXCLUDED.demand_score,
                    computed_at = now()
                """, batch[start:start + 500])
            scored_profiles.append(model.profile_name)
    conn.commit()

    summary = {"profile": scored_profiles[0], "radius_m": radius,
               "area_label_source": ("municipalities" if use_boundaries
                                     else "derived_from_facility_addresses"),
               "mesh_size_m": mesh_size_m, "meshes_scored": len(rows)}
    if len(scored_profiles) > 1:
        summary["profiles"] = scored_profiles
    return summary


def _score_row(model: ScoringModel, row: Mapping[str, Any],
               distributions: Mapping[str, Any], radius: int,
               labels: Mapping[int, str | None]) -> dict[str, Any]:
    scored = model.score(row, distributions)
    # ランキングに出す件数は、そのプロファイルが競合として数えたものと同じで
    # なければ意味がありません。科目を絞っていないプロファイルでは None。
    specialty = (model.profile.get("competition") or {}).get("specialty")
    return {
        "mesh_id": row["mesh_id"],
        "facility_specialty_counts": Json(row.get("facility_specialty_counts") or {}),
        "facility_specialty_count": (
            row.get(specialty_count_metric(specialty)) if specialty else None),
        "profile": model.profile_name,
        "radius_m": radius,
        "population": row.get("population"),
        "age_0_14": row.get("age_0_14"),
        "age_15_64": row.get("age_15_64"),
        "age_65_plus": row.get("age_65_plus"),
        "workers": row.get("workers"),
        "establishments": row.get("establishments"),
        "households": row.get("households"),
        "population_growth": row.get("population_growth"),
        "facility_count": row.get("facility_count"),
        "population_per_facility": row.get("population_per_facility"),
        "nearest_facility_distance_m": row.get("nearest_facility_distance_m"),
        "nearest_station": row.get("nearest_station"),
        "station_distance_m": row.get("station_distance_m"),
        "daily_passengers": row.get("daily_passengers"),
        "land_price_yen_per_sqm": row.get("land_price_yen_per_sqm"),
        "cost": scored.get("cost"),
        "demand": scored["demand"],
        "competition": scored["competition"],
        "growth": scored["growth"],
        "accessibility": scored["accessibility"],
        "overall": scored["overall"],
        "area_label": labels.get(row["mesh_id"]),
    }


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


#: Meshes per statement in the labelling queries. Same reason as the catchment
#: sweep: one statement over a whole prefecture is minutes of PostGIS work, and
#: a managed database cancels it long before it finishes.
LABEL_BATCH = 1000


def _label_batches(conn: psycopg.Connection, sql: str, params: tuple,
                   mesh_size_m: int, prefecture_code: str,
                   progress: Any = None) -> list[dict[str, Any]]:
    """Run a per-mesh labelling query in keyset-paged batches.

    ``sql`` must open with the paged subquery aliased ``m``, so that the four
    placeholders this supplies -- mesh size, prefecture, last id, page size --
    are the first four in the statement. Any the caller needs come after, in
    the order they appear; psycopg binds positionally.
    """
    say = progress or (lambda _msg: None)
    out: list[dict[str, Any]] = []
    after = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(sql, (mesh_size_m, prefecture_code, after, LABEL_BATCH) + params)
            rows = cur.fetchall()
        if not rows:
            break
        out.extend(rows)
        after = max(r["mesh_id"] for r in rows)
        say(f"    エリア名を付与中: {len(out):,} メッシュ")
        if len(rows) < LABEL_BATCH:
            break
    return out


def _boundary_area_labels(conn: psycopg.Connection, mesh_size_m: int,
                          prefecture_code: str,
                          progress: Any = None) -> dict[int, str]:
    """Name each mesh by the municipality its centre falls in.

    A centre can fall outside every municipality: N03 publishes reclaimed land
    and water whose affiliation is not settled as a separate 所属未定地 entry,
    which is excluded because it is not a municipality. Several of the highest
    scoring waterfront meshes sit there, so those fall back to the nearest
    municipality -- still the right locator for the reader, and never invented.
    """
    rows = _label_batches(
        conn,
        """
            SELECT m.id AS mesh_id,
                   COALESCE(inside.name, nearby.name) AS name
            FROM (
                SELECT id, centroid FROM population_mesh
                WHERE mesh_size_m = %s AND prefecture_code = %s AND id > %s
                ORDER BY id LIMIT %s
            ) m
            LEFT JOIN LATERAL (
                SELECT mu.name
                FROM municipalities mu
                JOIN data_sources ds
                  ON ds.id = mu.source_id AND ds.dataset_kind = 'official'
                WHERE ST_Contains(mu.geom, m.centroid)
                LIMIT 1
            ) AS inside ON true
            LEFT JOIN LATERAL (
                SELECT mu.name
                FROM municipalities mu
                JOIN data_sources ds
                  ON ds.id = mu.source_id AND ds.dataset_kind = 'official'
                WHERE inside.name IS NULL
                  AND ST_DWithin(mu.geom::geography, m.centroid::geography, %s)
                ORDER BY mu.geom::geography <-> m.centroid::geography
                LIMIT 1
            ) AS nearby ON true
            ORDER BY m.id
        """,
        (BOUNDARY_FALLBACK_RADIUS_M,), mesh_size_m, prefecture_code, progress)
    return {r["mesh_id"]: r["name"] for r in rows if r["name"]}


def _derive_area_labels(conn: psycopg.Connection, mesh_size_m: int,
                        prefecture_code: str,
                        progress: Any = None) -> dict[int, str]:
    """Label each mesh with the municipality most of its nearby clinics are in."""
    names = municipality_names_from_facilities(conn, prefecture_code)
    if not names:
        return {}

    rows = _label_batches(
        conn,
        """
            SELECT DISTINCT ON (m.id) m.id AS mesh_id, x.municipality_code
            FROM (
                SELECT id, centroid FROM population_mesh
                WHERE mesh_size_m = %s AND prefecture_code = %s AND id > %s
                ORDER BY id LIMIT %s
            ) m
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
            ORDER BY m.id
        """,
        (), mesh_size_m, prefecture_code, progress)

    return {r["mesh_id"]: names[r["municipality_code"]]
            for r in rows if r["municipality_code"] in names}
