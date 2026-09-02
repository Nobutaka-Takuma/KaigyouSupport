"""母集団の分布を、あらかじめ計算して貯める。

**専門家が頭の中でやっていた地域比較を、事前に済ませておく**ための ETL です。

実測（銀座1km・手元の PostgreSQL）：母集団の形を測るのに 14 往復・49ms、
商圏の集計そのものは 15ms でした。**「この地点が何人か」より「周りがどう
なっているか」のほうが 3 倍高い。** そして周りは、クリックした地点によって
変わりません。静岡県内の市街地の人口分布は、どこをクリックしても同じです。

地点ごとに測り直していたのを、ここで 1 回にします。
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Sequence

import psycopg
from psycopg.types.json import Json  # noqa: F401  (将来の拡張用)

from kaigyou_core import config as cfg
from kaigyou_core.analysis import DEFAULT_FACILITY_CATEGORY
from kaigyou_core.measures import (
    MEASURE_SPECS,
    MIN_BENCHMARK_SAMPLE,
    BenchmarkScope,
    _category_filter,
    benchmark_scopes,
    viable_floor,
)

#: 全部の値をそのまま貯める上限。これを超えたら分位点の格子に落とします。
#:
#: **「5,448 件中 1 位」を「上位0.1%」と言えるかどうかがここで決まります。**
#: 格子にすると分解能はその刻みまでになるので、小さい母集団（市区町村など）は
#: そのまま持ったほうが安く、かつ正確です。
EXACT_UP_TO = 4000

#: 格子の刻み数。両端を含めて 1001 点＝0.1 パーセンタイル刻み。
GRID_POINTS = 1001


def scope_key_of(scope: BenchmarkScope, prefecture_code: str,
                 municipality: str | None) -> str | None:
    """母集団を一意に決める鍵。**地点に依存する母集団には鍵がありません。**

    ``nearby``（この地点から10km以内）と ``similar_population``（商圏人口が
    同規模）は、クリックした場所で母集団そのものが変わります。事前計算できず、
    ここでは None を返して呼び出し側に飛ばさせます。
    """
    if scope.type in ("nearby", "similar_population"):
        return None
    if scope.type in ("municipality", "neighbourhood"):
        return f"{prefecture_code}:{municipality}" if municipality else None
    return prefecture_code


def compute(conn: psycopg.Connection, *, prefecture_code: str,
            profile: str, radius_m: int,
            facility_category: str = DEFAULT_FACILITY_CATEGORY,
            progress: Callable[[str], None] | None = None) -> int:
    """その県の、事前計算できる母集団すべての分布を貯め直す。

    市区町村ごとの母集団も作ります。**「県内で上位」ではなく「この市の中で
    上位」**を即答できるようにするためで、そこが「周囲と比べてどんな場所か」の
    実体です。
    """
    say = progress or (lambda _m: None)
    analysis = cfg.analysis_config()
    benchmarks = (cfg.insights_config(facility_category).get("benchmarks") or {})
    positioning = cfg.positioning_config(facility_category)
    version = str(positioning.get("benchmark_version") or "v0")

    floor = viable_floor(
        conn, profile=profile, radius_m=radius_m,
        facility_category=facility_category, prefecture_code=prefecture_code,
        percentile=float(benchmarks.get("viable_floor_percentile", 0.10)))
    max_share = float(benchmarks.get("max_share_below_viable_floor", 0.5))
    min_sample = int(benchmarks.get("min_sample", MIN_BENCHMARK_SAMPLE))

    label = _prefecture_label(conn, prefecture_code)
    written = 0
    for municipality in [None] + _municipalities(conn, prefecture_code,
                                                 profile, radius_m,
                                                 facility_category):
        scopes = benchmark_scopes(
            prefecture_code=prefecture_code, prefecture_label=label,
            municipality=municipality, population=None, radius_m=radius_m,
            lat=0.0, lng=0.0, config=benchmarks,
            viable_floor_population=floor,
            neighbours=_neighbours(conn, prefecture_code, municipality))
        for scope in scopes:
            key = scope_key_of(scope, prefecture_code, municipality)
            if key is None:
                continue
            # 県単位の母集団は市区町村を回すたびに出てきます。1 回でよい。
            if municipality is not None and scope.type not in (
                    "municipality", "neighbourhood"):
                continue
            written += _write_scope(
                conn, scope, key, profile=profile, radius_m=radius_m,
                facility_category=facility_category, floor=floor,
                max_share=max_share, min_sample=min_sample, version=version)
        say(f"  {municipality or label}: {written} 行")
    conn.commit()
    return written


def _write_scope(conn: psycopg.Connection, scope: BenchmarkScope, scope_key: str,
                 *, profile: str, radius_m: int, facility_category: str,
                 floor: float | None, max_share: float, min_sample: int,
                 version: str) -> int:
    """1 つの母集団について、全指標の分布を貯める。

    **母集団の性質（大きさ・弁別力）は 1 回だけ測ります。** 指標ごとに測ると、
    同じ答えを 16 回取りに行くことになります。
    """
    _shape(conn, scope, profile=profile, radius_m=radius_m,
           facility_category=facility_category, floor=floor,
           max_share=max_share, min_sample=min_sample)
    if scope.sample_count < min_sample:
        return 0

    category, params = _category_filter(conn, facility_category)
    rows: list[tuple[Any, ...]] = []
    for metric, spec in MEASURE_SPECS.items():
        column = spec["column"]
        with conn.cursor() as cur:
            # **昇順に並べた値そのもの。** count(*) FILTER (WHERE <= 値) を
            # 地点ごとに投げる代わりに、この配列に二分探索で当てます。
            cur.execute(
                f"""
                SELECT count({column})::int AS n,
                       percentile_cont(ARRAY[0.25, 0.5, 0.75])
                           WITHIN GROUP (ORDER BY {column}) AS q,
                       CASE WHEN count({column}) <= %s
                            THEN array_agg({column} ORDER BY {column})
                                 FILTER (WHERE {column} IS NOT NULL)
                            ELSE percentile_cont(%s)
                                 WITHIN GROUP (ORDER BY {column})
                       END AS boundaries,
                       count({column}) <= %s AS is_exact
                FROM mesh_scores ms
                JOIN population_mesh pm ON pm.id = ms.mesh_id
                WHERE ms.profile = %s AND ms.radius_m = %s
                  AND {category}{scope.where}
                """,
                (EXACT_UP_TO, _grid(), EXACT_UP_TO, profile, radius_m)
                + tuple(params) + tuple(scope.params))
            row = cur.fetchone()
        if not row or not row["n"] or not row["boundaries"]:
            continue
        quartiles = row["q"] or [None, None, None]
        rows.append((
            scope.type, scope_key, scope.label, metric, profile, radius_m,
            facility_category, scope.sample_count, int(row["n"]),
            [float(v) for v in row["boundaries"]], bool(row["is_exact"]),
            quartiles[1], quartiles[0], quartiles[2],
            scope.discriminating, scope.not_discriminating_reason,
            scope.share_below_viable_floor, version))

    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO benchmark_distributions (
                scope_kind, scope_key, scope_label, metric, profile, radius_m,
                facility_category, sample_count, value_count, boundaries,
                is_exact, median, p25, p75, discriminating,
                not_discriminating_reason, share_below_viable_floor,
                benchmark_version, computed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, now())
            ON CONFLICT (scope_kind, scope_key, metric, profile, radius_m,
                         facility_category) DO UPDATE SET
                scope_label = EXCLUDED.scope_label,
                sample_count = EXCLUDED.sample_count,
                value_count = EXCLUDED.value_count,
                boundaries = EXCLUDED.boundaries,
                is_exact = EXCLUDED.is_exact,
                median = EXCLUDED.median, p25 = EXCLUDED.p25, p75 = EXCLUDED.p75,
                discriminating = EXCLUDED.discriminating,
                not_discriminating_reason = EXCLUDED.not_discriminating_reason,
                share_below_viable_floor = EXCLUDED.share_below_viable_floor,
                benchmark_version = EXCLUDED.benchmark_version,
                computed_at = now()
            """, rows)
    return len(rows)


def _grid() -> list[float]:
    return [i / (GRID_POINTS - 1) for i in range(GRID_POINTS)]


def _shape(conn: psycopg.Connection, scope: BenchmarkScope, *, profile: str,
           radius_m: int, facility_category: str, floor: float | None,
           max_share: float, min_sample: int) -> None:
    """母集団の大きさと弁別力。measure_scope_shape と同じ判定を使います。"""
    from kaigyou_core.measures import measure_scope_shape

    measure_scope_shape(conn, scope, profile=profile, radius_m=radius_m,
                        facility_category=facility_category, floor=floor,
                        max_share_below=max_share, min_sample=min_sample)


def _prefecture_label(conn: psycopg.Connection, code: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT prefecture_name FROM municipalities "
                    "WHERE prefecture_code = %s LIMIT 1", (code,))
        row = cur.fetchone()
    return (row["prefecture_name"] if row else code) or code


def _municipalities(conn: psycopg.Connection, code: str, profile: str,
                    radius_m: int, facility_category: str) -> list[str]:
    """その県で、実際にメッシュのある市区町村。"""
    category, params = _category_filter(conn, facility_category)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT ms.area_label AS name
            FROM mesh_scores ms
            JOIN population_mesh pm ON pm.id = ms.mesh_id
            WHERE ms.profile = %s AND ms.radius_m = %s AND {category}
                  pm.prefecture_code = %s AND ms.area_label IS NOT NULL
            ORDER BY 1
            """, (profile, radius_m) + tuple(params) + (code,))
        return [r["name"] for r in cur.fetchall()]


def _neighbours(conn: psycopg.Connection, code: str,
                municipality: str | None) -> list[str]:
    if not municipality:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT b.name FROM municipalities a
            JOIN municipalities b ON ST_Touches(a.geom, b.geom)
            WHERE a.prefecture_code = %s AND a.name = %s
            """, (code, municipality))
        return [r["name"] for r in cur.fetchall()]
