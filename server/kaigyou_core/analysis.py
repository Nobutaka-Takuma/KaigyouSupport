"""Catchment analysis.

Thin Python over the PostGIS functions in ``db/migrations/005_functions.sql``.
The spatial work -- metric buffers, area-weighted population apportionment,
nearest-neighbour lookups -- stays in the database; this module only shapes the
results and feeds them to the scoring model.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

import psycopg

from kaigyou_core.db import table_exists
from kaigyou_core.scoring import (
    DEFAULT_FACILITY_CATEGORY,
    Distribution,
    ScoringModel,
    augment_specialty_metrics,
    competition_specialties,
    distributions_from_rows,
    normalization_reference,
    scope_key,
)

#: 別名です。**定義は scoring.DEFAULT_FACILITY_CATEGORY 1 か所。** 目盛りの鍵を
#: 作る側と商圏を数える側で別々に持つと、片方だけ直したときに、歯科の目盛りで
#: 内科を採点する状態になります。
DEFAULT_CATEGORY = DEFAULT_FACILITY_CATEGORY

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


#: Shown when a prefecture has meshes but no boundaries loaded to name it.
#: Only the codes this project has actually been used with are listed; an
#: unknown one is reported by its code rather than guessed at.
_PREFECTURE_NAMES = {"13": "東京都", "22": "静岡県"}


def loaded_prefectures(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Which prefectures have population meshes, and where to look at them.

    The app was written for Tokyo and had 13 written into it in a dozen places.
    That is the wrong shape as soon as there are two: what is analysable is a
    property of the database, not of the source code, and the reader should be
    offered what has been loaded rather than what someone assumed.

    The extent comes from the meshes themselves, so the map can frame a
    prefecture it has never heard of. The name comes from the boundary layer
    when it is loaded, because that is the published spelling.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT m.prefecture_code AS code,
                   count(*)::int     AS mesh_count,
                   sum(COALESCE(m.population, 0))::bigint AS population,
                   ST_YMin(ST_Extent(m.geom)::geometry) AS min_lat,
                   ST_XMin(ST_Extent(m.geom)::geometry) AS min_lng,
                   ST_YMax(ST_Extent(m.geom)::geometry) AS max_lat,
                   ST_XMax(ST_Extent(m.geom)::geometry) AS max_lng,
                   -- Weighted by population, not the middle of the extent.
                   -- Tokyo reaches to Ogasawara, 1,000km south: the centre of
                   -- its bounding box is open ocean, and a map opened there
                   -- shows nothing at all.
                   sum(ST_X(m.centroid) * COALESCE(m.population, 0))
                       / NULLIF(sum(COALESCE(m.population, 0)), 0) AS focus_lng,
                   sum(ST_Y(m.centroid) * COALESCE(m.population, 0))
                       / NULLIF(sum(COALESCE(m.population, 0)), 0) AS focus_lat,
                   (SELECT mu.prefecture_name FROM municipalities mu
                     WHERE mu.prefecture_code = m.prefecture_code
                       AND mu.prefecture_name IS NOT NULL LIMIT 1) AS name
            FROM population_mesh m
            WHERE m.prefecture_code IS NOT NULL
            GROUP BY m.prefecture_code
            ORDER BY sum(COALESCE(m.population, 0)) DESC
        """)
        rows = cur.fetchall()

    return [{
        "code": row["code"],
        "name": row["name"] or _PREFECTURE_NAMES.get(row["code"], f"{row['code']}"),
        "mesh_count": row["mesh_count"],
        "population": row["population"],
        "bbox": [row["min_lng"], row["min_lat"], row["max_lng"], row["max_lat"]],
        "center": ([row["focus_lng"], row["focus_lat"]]
                   if row["focus_lng"] is not None
                   else [(row["min_lng"] + row["max_lng"]) / 2,
                         (row["min_lat"] + row["max_lat"]) / 2]),
    } for row in rows]


def prefecture_at(conn: psycopg.Connection, lat: float, lng: float) -> str | None:
    """Which prefecture a point is in, according to the loaded data.

    A point analysis is about the point. Taking the prefecture from a dropdown
    instead means clicking in Tokyo while the selector says Shizuoka analyses
    Tokyo against Shizuoka's mesh resolution and Shizuoka's normalisation --
    which produces "no population here" for the middle of Chiyoda and gives
    the reader no way to see why.

    Boundaries first, because they are the published answer. Where they are
    not loaded, the nearest mesh within 5km serves: it is the same question
    asked of a coarser map.
    """
    with conn.cursor() as cur:
        if table_exists(conn, "municipalities"):
            cur.execute(
                """
                SELECT prefecture_code AS code FROM municipalities
                WHERE geom && ST_SetSRID(ST_MakePoint(%s, %s), 4326)
                  AND ST_Contains(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
                LIMIT 1
                """, (lng, lat, lng, lat))
            row = cur.fetchone()
            if row:
                return row["code"]

        cur.execute(
            """
            SELECT prefecture_code AS code FROM population_mesh
            WHERE ST_DWithin(centroid::geography,
                             ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, 5000)
            ORDER BY centroid::geography <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
            LIMIT 1
            """, (lng, lat, lng, lat))
        row = cur.fetchone()
    return row["code"] if row else None


def prefecture_name(conn: psycopg.Connection, code: str) -> str:
    """The published spelling where boundaries are loaded, else a known name."""
    for entry in loaded_prefectures(conn):
        if entry["code"] == code:
            return entry["name"]
    return _PREFECTURE_NAMES.get(code, code)


def default_prefecture(conn: psycopg.Connection, requested: str | None = None) -> str:
    """The prefecture to analyse when the caller did not name one.

    An explicit request always wins, including one for a prefecture with no
    data -- answering a different question than the one asked would be worse
    than answering "nothing here". Otherwise: whichever has the most people
    loaded, so a database holding only Shizuoka opens on Shizuoka.
    """
    if requested:
        return requested
    found = loaded_prefectures(conn)
    return found[0]["code"] if found else "13"


def resolve_mesh_size(conn: psycopg.Connection, requested: int | None = None,
                      prefecture_code: str | None = None,
                      bbox: Sequence[float] | None = None) -> int | None:
    """The mesh resolution to analyse at.

    An explicit request wins. Otherwise pick the resolution that actually
    carries the most population in the scope asked about, so the analysis
    follows the data rather than a constant.

    Scope matters once there is more than one prefecture: two of them can be
    published at different resolutions, and a single database-wide answer then
    draws one prefecture's meshes and none of the other's. Where the caller
    knows which part of the map it is asking about -- a prefecture, or a
    viewport -- it says so.
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
    clauses: list[str] = []
    params_list: list[Any] = []
    if prefecture_code:
        clauses.append("prefecture_code = %s")
        params_list.append(prefecture_code)
    if bbox and len(bbox) == 4:
        clauses.append("geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)")
        params_list.extend(float(v) for v in bbox)
    sql = sql.format(where=("WHERE " + " AND ".join(clauses)) if clauses else "")
    params = tuple(params_list)
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


#: Trade-area shapes. ``circle`` is straight-line distance; ``walk`` follows the
#: street network, which is a different and usually much smaller area wherever a
#: river, a railway or a trunk road gets in the way. Asking for ``walk`` where no
#: network is loaded yields a circle -- the answer says which was used.
CATCHMENT_MODES = ("circle", "walk")
DEFAULT_CATCHMENT = "circle"


def supports_catchment_mode(conn: psycopg.Connection) -> bool:
    """Whether kg_analyze_point takes a catchment argument yet.

    Code reaches a deployment before the migration does -- a push builds in
    seconds, migrations are run by hand afterwards. Calling the six-argument
    form against the five-argument one in that window fails the whole request,
    so the shape of the function is checked rather than assumed. One indexed
    catalog lookup; the alternative is a 500 on every analysis until someone
    runs a command.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM pg_proc "
            "WHERE proname = 'kg_analyze_point' AND pronargs >= 6"
        )
        return cur.fetchone()["n"] > 0


def supports_specialties(conn: psycopg.Connection) -> bool:
    """Whether kg_analyze_point takes the 標榜科目 argument yet.

    Same deploy window as :func:`supports_catchment_mode`: the code ships in
    seconds, the migration is run by hand afterwards. Asking the seven-argument
    form of a six-argument function fails every analysis in between.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM pg_proc "
            "WHERE proname = 'kg_analyze_point' AND pronargs >= 7"
        )
        return cur.fetchone()["n"] > 0


def analyze_point(conn: psycopg.Connection, lat: float, lng: float, radius_m: int,
                  facility_category: str = DEFAULT_CATEGORY,
                  mesh_size_m: int = DEFAULT_MESH_SIZE_M,
                  catchment: str = DEFAULT_CATCHMENT,
                  specialty: str | None = None) -> dict[str, Any]:
    """Raw catchment metrics for one point. No scoring applied."""
    with conn.cursor() as cur:
        if supports_specialties(conn):
            cur.execute(
                "SELECT * FROM kg_analyze_point(%s, %s, %s, %s, %s, %s, %s)",
                (lat, lng, radius_m, facility_category, mesh_size_m, catchment,
                 specialty),
            )
        elif supports_catchment_mode(conn):
            cur.execute(
                "SELECT * FROM kg_analyze_point(%s, %s, %s, %s, %s, %s)",
                (lat, lng, radius_m, facility_category, mesh_size_m, catchment),
            )
        else:
            cur.execute(
                "SELECT * FROM kg_analyze_point(%s, %s, %s, %s, %s)",
                (lat, lng, radius_m, facility_category, mesh_size_m),
            )
        row = cur.fetchone() or {}
    metrics = {_RENAME.get(k, k): v for k, v in row.items()}
    metrics["radius_m"] = radius_m
    # 成長は将来推計で見ます。過去の実績（population_growth）も残したまま
    # 並べて返します。置き換えると、レポートから「これまで」が消えます。
    settings = growth_years()
    metrics["population_change_projected"] = projected_change(
        conn, lat, lng, radius_m, mesh_size_m,
        settings["from_year"], settings["to_year"])
    metrics["population_change_from_year"] = settings["from_year"]
    metrics["population_change_to_year"] = settings["to_year"]
    return metrics


def growth_years() -> dict[str, int]:
    """成長を何年から何年で見るか。設定の1か所にあり、コードには書きません。

    プロファイルではなく上位に置いてあります。「小児歯科寄りモデルだけ
    2040 年で見る」に意味は無く、どの推計年で採点するかはデータ全体の性質です。
    """
    from kaigyou_core import config as cfg

    # 成長を見る期間は業態で変わりません（データ全体の設定）。歯科の
    # scoring.yaml から読みますが、どの業態でも同じ値を書きます。
    horizon = (cfg.scoring_config().get("growth_horizon") or {})
    return {"from_year": int(horizon.get("from_year", 2020)),
            "to_year": int(horizon.get("to_year", 2050))}


def catchment_geojson(conn: psycopg.Connection, lat: float, lng: float, radius_m: int,
                      catchment: str = DEFAULT_CATCHMENT,
                      precision: int = 6) -> dict[str, Any] | None:
    """The trade-area polygon itself, for the map to draw.

    The map used to draw its own circle from the radius. That was fine while
    every catchment was a circle; drawing one over a walking analysis would show
    a shape the numbers did not come from.
    """
    with conn.cursor() as cur:
        # Same deploy window: kg_catchment arrives with the same migration.
        cur.execute("SELECT to_regproc('kg_catchment') AS fn")
        if cur.fetchone()["fn"] is None:
            return None
        cur.execute(
            "SELECT ST_AsGeoJSON(geom, %s) AS geojson, kind "
            "FROM kg_catchment(%s, %s, %s, %s)",
            (precision, lat, lng, radius_m, catchment),
        )
        row = cur.fetchone()
    if not row or not row["geojson"]:
        return None
    import json

    return {"geometry": json.loads(row["geojson"]), "kind": row["kind"]}


def walk_network_status(conn: psycopg.Connection) -> dict[str, Any]:
    """Whether walking catchments can be produced here, and why not if not."""
    from kaigyou_core.db import table_exists

    if not table_exists(conn, "walk_network"):
        return {"available": False, "reason": "not_migrated"}
    with conn.cursor() as cur:
        cur.execute("SELECT to_regproc('kg_walk_catchment') AS fn")
        if cur.fetchone()["fn"] is None:
            return {"available": False, "reason": "pgrouting_unavailable"}
        cur.execute("SELECT count(*) AS n FROM walk_network")
        edges = cur.fetchone()["n"]
    if not edges:
        return {"available": False, "reason": "network_not_loaded"}
    return {"available": True, "edges": edges}


def land_prices_near(conn: psycopg.Connection, lat: float, lng: float,
                     radius_m: int, limit: int = 3) -> dict[str, Any] | None:
    """Published land prices around a point, by use division.

    地価公示 is the price of specific surveyed parcels, per square metre, on
    1 January. It is reported here as what it is -- the median and range of the
    points that fall in the trade area, and the nearest few -- and nothing is
    derived from it.

    Deliberately NOT a rent estimate and NOT a score component. Rent depends on
    the building, the floor and the contract, none of which are published here,
    and the requirements rule out predicting cost. A number that looked like a
    rent would be believed as one.

    Split by 用途区分 because 住宅地 and 商業地 in the same ward differ several
    times over: one blended figure would describe neither.
    """
    from kaigyou_core.db import table_exists

    if not table_exists(conn, "land_prices"):
        return None

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT use_category,
                   count(*)::int AS points,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY price_yen_per_sqm)
                       AS median_yen_per_sqm,
                   min(price_yen_per_sqm) AS min_yen_per_sqm,
                   max(price_yen_per_sqm) AS max_yen_per_sqm,
                   avg(change_rate_pct)   AS mean_change_pct,
                   max(survey_year)       AS survey_year
            FROM land_prices
            WHERE ST_DWithin(geom::geography,
                             ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
            GROUP BY use_category
            ORDER BY count(*) DESC
            """,
            (lng, lat, radius_m),
        )
        by_use = [dict(row) for row in cur.fetchall()]

        # The nearest points themselves. An aggregate hides that the median of
        # two points is not a market; naming them lets the reader judge.
        cur.execute(
            """
            SELECT address, municipality_name, use_category, price_yen_per_sqm,
                   change_rate_pct, current_use, zoning, survey_year,
                   ST_Distance(geom::geography,
                               ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) AS distance_m,
                   ST_Y(geom) AS lat, ST_X(geom) AS lng
            FROM land_prices
            ORDER BY geom::geography <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
            LIMIT %s
            """,
            (lng, lat, lng, lat, limit),
        )
        nearest = [dict(row) for row in cur.fetchall()]

    if not by_use and not nearest:
        return None
    return {
        "radius_m": radius_m,
        "by_use": by_use,
        "nearest": nearest,
        "note": ("地価公示の標準地の価格（円/m²・1月1日時点）です。土地の価格で"
                 "あって賃料ではありません（建物・階数・契約条件を含みません）。"
                 "賃料の目安への換算は rent_estimate を参照してください。"
                 "スコアには使用していません。"),
    }


def facility_counts(conn: psycopg.Connection, lat: float, lng: float,
                    radii: Sequence[int],
                    facility_category: str = DEFAULT_CATEGORY,
                    specialty: str | None = None) -> dict[int, int]:
    """Facilities within each radius, optionally only those declaring 標榜科目."""
    with conn.cursor() as cur:
        if specialty is not None and supports_specialties(conn):
            cur.execute(
                "SELECT radius_m, facility_count "
                "FROM kg_facility_counts_by_specialty(%s, %s, %s, %s, %s)",
                (lat, lng, [float(r) for r in radii], facility_category, specialty),
            )
        else:
            cur.execute(
                "SELECT radius_m, facility_count FROM kg_facility_counts(%s, %s, %s, %s)",
                (lat, lng, [float(r) for r in radii], facility_category),
            )
        return {int(r["radius_m"]): r["facility_count"] for r in cur.fetchall()}


def resolve_distributions(conn: psycopg.Connection, mesh_size_m: int, radius_m: int,
                          prefecture_code: str,
                          config: Mapping[str, Any],
                          facility_category: str = DEFAULT_CATEGORY,
                          ) -> tuple[str, dict[str, Distribution]]:
    """設定の目盛りを探し、無ければ実際に書かれている方を使う。

    目盛りは「その業態の施設が実在する商圏」から作るのが既定ですが、候補地が
    少ない県では refresh-stats が全件に落とします。設定どおりの鍵だけを見て
    「未計算」と答えると、実際には目盛りがあるのにスコアが出ません。
    どちらを使ったかは呼び出し元が応答に載せます。

    **業態を跨いで探しません。** 内科の目盛りが無いときに歯科の目盛りを
    使うと、それらしい点が出て、しかも間違っています。
    """
    preferred = normalization_reference(config)
    for reference in (preferred, "all", "with_clinics"):
        scope = scope_key(mesh_size_m, radius_m, prefecture_code, reference,
                          facility_category)
        distributions = load_distributions(conn, scope)
        if distributions:
            return scope, distributions
    return scope_key(mesh_size_m, radius_m, prefecture_code, preferred,
                     facility_category), {}


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
    augment_specialty_metrics(metrics, competition_specialties(model.config))
    scope, distributions = resolve_distributions(
        conn, mesh_size_m, radius_m, prefecture_code, model.config,
        facility_category)
    scores = model.score(metrics, distributions)
    scores["normalization_scope"] = scope
    scores["normalization_sample_count"] = max(
        (d.sample_count for d in distributions.values()), default=0
    )
    return {"metrics": metrics, "scores": scores}


#: Meshes per statement in the catchment sweep. One statement for the whole
#: prefecture is the fastest thing to write and the wrong thing to run: a
#: hosted database applies a statement timeout, and Shizuoka's 18,000 meshes
#: take long enough to hit it -- which cancels the sweep after several minutes
#: of work and leaves the caller with nothing. Batched, no single statement
#: runs long, and the operator sees it advancing.
CATCHMENT_BATCH = 1000



#: 将来推計から「基準年 → 目標年」の変化率を出す。
#:
#: population_growth（2015→2020 の実績）と同じ形の値です。置き換えではなく
#: 並べて持ちます。過去の実績も、それはそれで意味のある事実だからです。
#:
#: **kg_analyze_point は触りません。** あの関数は出力列を変えるのに DROP が
#: 要り、古いシグネチャが残ると「エラーも出ないまま違う数字が返る」という
#: 壊れ方をします（analysis.py:484 の但し書き）。分子と分母が同じ集合なので、
#: 比は別クエリで出しても同じ答えになります。
def _projection_change_sql(alias_geom: str) -> str:
    return f"""
        SELECT SUM(p_to.population * w.share)
               / NULLIF(SUM(p_from.population * w.share), 0) - 1
          FROM (
              SELECT n.mesh_code,
                     LEAST(1.0, GREATEST(0.0,
                         ST_Area(ST_Intersection(n.geom, {alias_geom})::geography)
                         / NULLIF(ST_Area(n.geom::geography), 0))) AS share
                FROM population_mesh n
               WHERE n.mesh_size_m = %(mesh)s
                 AND n.geom && {alias_geom}
                 AND ST_Intersects(n.geom, {alias_geom})
          ) w
          JOIN mesh_population_projection p_from
            ON p_from.mesh_code = w.mesh_code AND p_from.mesh_size_m = %(mesh)s
           AND p_from.projection_year = %(from_year)s
          JOIN mesh_population_projection p_to
            ON p_to.mesh_code = w.mesh_code AND p_to.mesh_size_m = %(mesh)s
           AND p_to.projection_year = %(to_year)s
    """


def projected_change(conn: psycopg.Connection, lat: float, lng: float,
                     radius_m: int, mesh_size_m: int,
                     from_year: int, to_year: int) -> float | None:
    """1 地点の商圏について、基準年から目標年への人口の変化率。

    取り込んでいなければ None。0 ではありません。「分からない」を「増減なし」
    と言い換えると、推計が無い地域の成長スコアが平均点になります。
    """
    from kaigyou_core.db import table_exists

    if not table_exists(conn, "mesh_population_projection"):
        return None
    buffer = ("ST_Buffer(ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography,"
              " %(radius)s)::geometry")
    with conn.cursor() as cur:
        cur.execute(_projection_change_sql(buffer),
                    {"lat": lat, "lng": lng, "radius": radius_m, "mesh": mesh_size_m,
                     "from_year": from_year, "to_year": to_year})
        row = cur.fetchone()
    value = (list(row.values())[0] if row else None)
    return None if value is None else float(value)


def projected_change_by_mesh(conn: psycopg.Connection, radius_m: int, *,
                             mesh_size_m: int, prefecture_code: str,
                             from_year: int, to_year: int,
                             progress: Any = None) -> dict[int, float]:
    """都道府県の全メッシュぶんを 1 文で。

    メッシュごとに 1 往復すると、5,449 メッシュがそのまま 5,449 回の
    問い合わせになります。まとめて出して Python 側で突き合わせます。
    """
    from kaigyou_core.db import table_exists

    say = progress or (lambda _m: None)
    if not table_exists(conn, "mesh_population_projection"):
        say("    将来推計人口が未取得のため、成長は算出できません。")
        return {}

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT m.id AS mesh_id, ({_projection_change_sql("buf.g")}) AS change
              FROM population_mesh m
              CROSS JOIN LATERAL (
                  SELECT ST_Buffer(m.centroid::geography, %(radius)s)::geometry AS g
              ) buf
             WHERE m.mesh_size_m = %(mesh)s
               AND m.prefecture_code = %(prefecture)s
               AND COALESCE(m.population, 0) > 0
            """,
            {"radius": radius_m, "mesh": mesh_size_m, "prefecture": prefecture_code,
             "from_year": from_year, "to_year": to_year})
        rows = cur.fetchall()
    found = {int(r["mesh_id"]): float(r["change"])
             for r in rows if r["change"] is not None}
    say(f"    将来推計から成長を算出: {len(found):,} / {len(rows):,} メッシュ")
    return found


def mesh_catchments(conn: psycopg.Connection, radius_m: int, *,
                    mesh_size_m: int = DEFAULT_MESH_SIZE_M,
                    prefecture_code: str = "13",
                    facility_category: str = DEFAULT_CATEGORY,
                    batch_size: int = CATCHMENT_BATCH,
                    progress: Any = None) -> list[dict[str, Any]]:
    """Catchment metrics for every mesh centroid, in batches.

    A LATERAL join lets PostGIS do the sweep itself rather than one round trip
    per mesh; keyset paging on the mesh id keeps each statement short. Paging
    by id rather than OFFSET because OFFSET would re-run the trade-area
    analysis for every row it then throws away.
    """
    say = progress or (lambda _msg: None)

    # Every argument named, not only the ones that differ from their defaults.
    #
    # 005_functions.sql still creates the original five-argument function, and
    # replaying it -- which the migration repair and the deploy-window tests
    # both do -- leaves that older signature sitting beside the current one. A
    # five-argument call then binds to it exactly, and the sweep silently
    # returns a row without workers, land price or specialties: every mesh
    # scored, no error, the wrong numbers. Asking for the full signature makes
    # the older overload unreachable from here.
    extra: tuple[Any, ...] = ()
    if supports_specialties(conn):
        arguments = "ST_Y(m.centroid), ST_X(m.centroid), %s, %s, %s, %s, %s"
        extra = (DEFAULT_CATCHMENT, None)
    elif supports_catchment_mode(conn):
        arguments = "ST_Y(m.centroid), ST_X(m.centroid), %s, %s, %s, %s"
        extra = (DEFAULT_CATCHMENT,)
    else:
        arguments = "ST_Y(m.centroid), ST_X(m.centroid), %s, %s, %s"

    out: list[dict[str, Any]] = []
    after = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT m.id AS mesh_id, m.mesh_code,
                       ST_Y(m.centroid) AS lat, ST_X(m.centroid) AS lng,
                       a.*
                FROM (
                    SELECT id, mesh_code, centroid
                    FROM population_mesh
                    WHERE mesh_size_m = %s
                      AND prefecture_code = %s
                      AND COALESCE(population, 0) > 0
                      AND id > %s
                    ORDER BY id
                    LIMIT %s
                ) m
                CROSS JOIN LATERAL kg_analyze_point({arguments}) AS a
                ORDER BY m.id
                """,
                (mesh_size_m, prefecture_code, after, batch_size,
                 radius_m, facility_category, mesh_size_m) + extra,
            )
            rows = cur.fetchall()
        if not rows:
            break
        out.extend({_RENAME.get(k, k): v for k, v in row.items()} for row in rows)
        after = rows[-1]["mesh_id"]
        say(f"    商圏を集計中: {len(out):,} メッシュ")
        if len(rows) < batch_size:
            break

    # 将来推計の変化率は 1 文でまとめて出して突き合わせます。メッシュごとに
    # 問い合わせると 5,449 往復になります。
    settings = growth_years()
    changes = projected_change_by_mesh(
        conn, radius_m, mesh_size_m=mesh_size_m, prefecture_code=prefecture_code,
        from_year=settings["from_year"], to_year=settings["to_year"], progress=say)
    for row in out:
        row["population_change_projected"] = changes.get(row.get("mesh_id"))
    return out


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
