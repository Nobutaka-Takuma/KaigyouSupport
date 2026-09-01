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
from kaigyou_core import city_planning as plan
from kaigyou_core import config as cfg
from kaigyou_core.analysis import DEFAULT_CATEGORY
from kaigyou_core.db import column_exists, table_exists

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
    category: str = Query(DEFAULT_CATEGORY),
    clinic_type: str | None = Query(None, description="公表名称そのままの標榜診療科で絞り込み"),
    specialty: str | None = Query(
        None, description="正規化した標榜科目キーで絞り込み（general / pediatric / "
                          "orthodontics / oral_surgery / pediatric_orthodontics ほか）"),
    fields: str = Query("points", pattern="^(points|minimal|full)$"),
    limit: int = Query(5000, ge=1, le=20000),
) -> dict[str, Any]:
    # At city zoom the viewport holds ~3,500 clinics. Their names and addresses
    # are a megabyte that nothing on screen draws -- the dots need a position.
    # The popup fetches the one record it is about.
    select = {
        "points": "f.id",
        "minimal": "f.id, f.name, f.address",
        "full": ("f.id, f.facility_id, f.name, f.address, f.facility_category, "
                 "f.clinic_types, f.opening_date, f.founder_type, f.attributes, "
                 "f.source_id, f.source_date"),
    }[fields]

    # 標榜科目は別テーブルなので、絞り込むときだけ結合します。全件を描くときに
    # 毎回結合すると、市街地ズームの数千件に無駄が乗ります。
    has_specialties = table_exists(conn, "facility_features")
    join = ""
    if specialty and has_specialties:
        join = "JOIN facility_features ff ON ff.facility_id = f.facility_id"
        if fields == "full":
            select += ", ff.specialty_keys, ff.opens_saturday, ff.opens_sunday, ff.opens_evening"

    where = ["f.facility_category = %s"]
    params: list[Any] = [category]
    box = parse_bbox(bbox)
    if box:
        where.append(f"f.geom && {_BBOX_SQL}")
        params.extend(box)
    if clinic_type:
        where.append("%s = ANY(f.clinic_types)")
        params.append(clinic_type)
    if specialty and has_specialties:
        where.append("%s = ANY(ff.specialty_keys)")
        params.append(specialty)
    params.append(limit)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {select}, ST_AsGeoJSON(f.geom, {COORD_DIGITS}) AS geojson
            FROM facilities f
            {join}
            WHERE {' AND '.join(where)}
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()

    result = feature_collection(
        _features(rows),
        provenance=provenance.for_tables(conn, ["facilities"]),
        truncated=len(rows) >= limit,
    )
    # 絞り込みを頼まれたのに絞り込めなかったことは、黙って全件を返すより
    # 言ったほうがいい。地図には「小児歯科だけ」と書いてあるので。
    if specialty and not has_specialties:
        result["specialty_filter_applied"] = False
        result["note"] = ("診療科目データ（医療情報ネット 032）が未取得のため、"
                          "標榜科目での絞り込みはできません。全件を表示しています。")
    elif specialty:
        result["specialty_filter_applied"] = True
    return result


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
                   f.source_date, ds.name AS source_name,
                   ff.specialty_keys, ff.declared_specialties, ff.weekly_open_hours,
                   ff.open_days, ff.latest_close, ff.opens_saturday, ff.opens_sunday,
                   ff.opens_holiday, ff.opens_evening
            FROM facilities f
            JOIN data_sources ds ON ds.id = f.source_id
            LEFT JOIN facility_features ff ON ff.facility_id = f.facility_id
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


@router.get("/land-prices", summary="地価公示の標準地レイヤー")
def land_prices(
    conn: psycopg.Connection = Depends(get_conn),
    bbox: str | None = Query(None),
    use_category_code: str | None = Query(
        None, description="用途区分コード（000 住宅地 / 005 商業地 …）"),
    year: int | None = Query(None, description="省略時は読み込み済みの最新年"),
    limit: int = Query(3000, ge=1, le=20000),
) -> dict[str, Any]:
    """地価公示の標準地。

    Points, not a surface. Interpolating between them would produce a price for
    every address in Tokyo, and 地価公示 does not say that: it says what these
    particular parcels are worth. Drawn as the points they are.
    """
    if not table_exists(conn, "land_prices"):
        return feature_collection([], provenance={"sources": []})

    where = ["true"]
    params: list[Any] = []
    box = parse_bbox(bbox)
    if box:
        where.append(f"geom && {_BBOX_SQL}")
        params.extend(box)
    if use_category_code:
        where.append("use_category_code = %s")
        params.append(use_category_code)
    if year:
        where.append("survey_year = %s")
        params.append(year)
    else:
        where.append("survey_year = (SELECT max(survey_year) FROM land_prices)")
    params.append(limit)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, address, municipality_name, use_category, use_category_code,
                   price_yen_per_sqm, change_rate_pct, current_use, zoning,
                   nearest_station, station_distance_m, survey_year,
                   ST_AsGeoJSON(geom, {COORD_DIGITS}) AS geojson
            FROM land_prices
            WHERE {' AND '.join(where)}
            ORDER BY price_yen_per_sqm DESC
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()

    return feature_collection(
        _features(rows),
        provenance=provenance.for_tables(conn, ["land_prices"]),
        truncated=len(rows) >= limit,
    )


@router.get("/meshes", summary="人口メッシュ / スコアメッシュ")
def meshes(
    conn: psycopg.Connection = Depends(get_conn),
    bbox: str | None = Query(None),
    mesh_size_m: int | None = Query(None, description="省略時は読み込み済みデータから自動判定"),
    profile: str | None = Query(None, description="指定するとスコアも返す"),
    radius_m: int | None = Query(None),
    category: str = Query(DEFAULT_CATEGORY),
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
        # 業態も結合条件に入れます。入れないと、内科を入れたあとに歯科の
        # ヒートマップを引くと、メッシュごとにどちらかの点が当たります。
        #
        # 列が無い環境では入れません。デプロイとマイグレーションの間の窓で、
        # 存在しない列を参照すると地図全体が 500 になります。
        scoped = column_exists(conn, "mesh_scores", "facility_category")
        join += """
            LEFT JOIN mesh_scores ms
                   ON ms.mesh_id = m.id AND ms.profile = %s
        """ + ("AND ms.facility_category = %s\n" if scoped else "") + """
                  AND (%s::int IS NULL OR ms.radius_m = %s)
        """
        select += (", ms.overall_score, ms.demand_score, ms.competition_score,"
                   " ms.growth_score, ms.accessibility_score, ms.facility_count,"
                   " ms.population_per_facility, ms.area_label")
        params = ([profile] + ([category] if scoped else [])
                  + [radius_m, radius_m] + params)

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


#: 地図に出せる都市計画の層と、その並び。**表示順ではなく選択肢の順**です。
#: 上ほど開業判断に効きます——建てられるかどうかが先で、公園はそのあと。
CITY_PLANNING_KINDS: list[tuple[str, str]] = [
    ("youto", "用途地域"),
    ("senbiki", "区域区分（市街化区域・調整区域）"),
    ("ritteki", "立地適正化計画（誘導区域）"),
    ("bouka", "防火・準防火地域"),
    ("chikukei", "地区計画"),
    ("tochiku", "市街地開発事業"),
    ("koudoti", "高度地区"),
    ("koudori", "高度利用地区"),
    ("tkbt", "特別用途地区"),
    ("tokuteiyouto", "特定用途制限地域"),
    ("fuuchichiku", "風致地区"),
    ("kouen", "都市計画公園・緑地"),
    ("tokei", "都市計画区域"),
    ("jyuntoshi", "準都市計画区域"),
]


@router.get("/city-planning/kinds", summary="地図に出せる都市計画の層")
def city_planning_kinds(
    conn: psycopg.Connection = Depends(get_conn),
    prefecture_code: str | None = Query(None),
) -> dict[str, Any]:
    """選べる層と、その県に何件入っているか。

    **固定の一覧を画面に持たせません。** 都市計画を取り込んでいない県で
    「用途地域」と書いた選択肢を出すと、選んでも何も出ない画面になります。
    件数を返すので、0 件の層は画面から消せます。
    """
    if not table_exists(conn, "city_planning_zones"):
        return {"available": False, "kinds": []}

    where, params = ["true"], []
    if prefecture_code:
        where.append("prefecture_code = %s")
        params.append(prefecture_code)
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT zone_kind, count(*)::int AS features
                FROM city_planning_zones WHERE {' AND '.join(where)}
                GROUP BY zone_kind""", params)
        counts = {r["zone_kind"]: r["features"] for r in cur.fetchall()}

    return {
        "available": bool(counts),
        "kinds": [{"kind": kind, "label": label, "features": counts[kind]}
                  for kind, label in CITY_PLANNING_KINDS if counts.get(kind)],
    }


#: この層の座標の小数桁。**面のレイヤーなので 6 桁は要りません。**
#: 6 桁は約 10cm で、市域ほどの大きさの区域を描くのに使う精度ではありません。
CITY_PLANNING_DIGITS = 5

#: 1 応答で返す図形の上限（バイト）。**件数ではなく容量で切ります。**
#: Vercel の Serverless Function は応答が 4.5MB を超えると失敗します。件数の
#: 上限だけでは守れません——同じ 3,000 件でも、区域区分の大きな面と用途地域の
#: 小さな面では容量が桁で違います。「時折エラーが出る」のはこれです。
CITY_PLANNING_BYTE_BUDGET = 3_000_000

#: 画面の広さに応じた単純化の下限（度）。**画面が忘れても効かせます。**
#: 引きの画面ほど粗くしないと、1 リクエストが数MBになります。
#: (bbox の幅 or 高さ, 最低限の許容誤差)
_SIMPLIFY_FLOOR: list[tuple[float, float]] = [
    (0.5, 0.0005),    # 県ほどの広さ … 約 55m
    (0.2, 0.0002),    # 市域ほど     … 約 22m
    (0.0, 0.00005),   # 市街地       … 約  5m
]


def _simplify_floor(box: tuple[float, float, float, float] | None,
                    asked: float) -> float:
    """指定と、広さから決まる下限の、粗いほう。"""
    if not box:
        return asked
    span = max(box[2] - box[0], box[3] - box[1])
    for threshold, floor in _SIMPLIFY_FLOOR:
        if span >= threshold:
            return max(asked, floor)
    return asked


@router.get("/city-planning", summary="都市計画決定情報レイヤー")
def city_planning(
    conn: psycopg.Connection = Depends(get_conn),
    kind: str = Query("youto", description="層（youto / senbiki / ritteki ...）"),
    bbox: str | None = Query(None, description="min_lng,min_lat,max_lng,max_lat"),
    prefecture_code: str | None = Query(None),
    category: str = Query(DEFAULT_CATEGORY),
    simplify_deg: float = Query(0.0002, ge=0.0, le=0.05,
                                description="表示用の単純化許容誤差（度）。既定の 0.0002 は約20m"),
    limit: int = Query(3000, ge=1, le=20000),
) -> dict[str, Any]:
    """1 つの層を、画面に映っている範囲だけ。

    **層を混ぜて返しません。** 用途地域と誘導区域と区域区分は同じ場所に
    重なって存在するので、まとめて塗ると下の色が見えず、押しても
    どれを押したのか分かりません。画面が層を 1 つ選び、それだけを返します。

    説明文は ``zones`` に**区分ごとに 1 回だけ**入れます。1 件ずつの
    ``properties`` に付けると、同じ文が何千回も繰り返されます——実測で、
    静岡県全域の用途地域 2,946 件が 3.5MB になり、そのうち 2.5MB が
    繰り返された説明文でした。**Vercel の応答は 4.5MB で切れる**ので、
    ここは容量の問題であって見た目の問題ではありません。

    説明の出どころは 2 つ。区分の意味は業態に依らない
    ``config/city_planning.yaml``、建てられるかどうかは業態ごとの
    ``config/<業態>/city_planning.yaml`` です。**画面で文言を作ると、
    レポートと違うことを言い出します。**
    """
    if not table_exists(conn, "city_planning_zones"):
        return feature_collection([], zones={}, provenance={
            "sources": [], "contains_sample_data": False, "datasets_unavailable": []})

    where, params = ["zone_kind = %s"], [kind]
    if prefecture_code:
        where.append("prefecture_code = %s")
        params.append(prefecture_code)
    box = parse_bbox(bbox)
    if box:
        where.append(f"geom && {_BBOX_SQL}")
        params.extend(box)
    params.insert(0, _simplify_floor(box, simplify_deg))
    params.append(limit)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT zone_type, zone_name, far, bcr, municipality_name, decided_on,
                   ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom, %s),
                                {CITY_PLANNING_DIGITS}) AS geojson
            FROM city_planning_zones
            WHERE {' AND '.join(where)}
            -- 大きい面を先に。上限で切られたとき、消えるのが小さい面に
            -- なるようにします（大きい面が消えると地図に穴が空きます）。
            ORDER BY ST_Area(geom) DESC
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()

    # **容量で切ります。** 大きい面から並んでいるので、落ちるのは小さい面です。
    # 落としたことは truncated で伝えます（黙って穴を空けない）。
    budget = 0
    kept = []
    for row in rows:
        budget += len(row["geojson"])
        if budget > CITY_PLANNING_BYTE_BUDGET and kept:
            break
        kept.append(row)
    truncated = len(kept) < len(rows) or len(rows) >= limit
    rows = kept

    rules = cfg.city_planning_config(category)

    #: 区分ごとの説明。**1 件ずつではなく 1 回だけ。** 画面は zone_key で引きます。
    zones: dict[str, dict[str, Any]] = {}
    for row in rows:
        raw = row.get("zone_type") or ""
        # 表記の正規化はサーバでだけ行います——画面でもう一度やると、
        # 直したはずの表記ゆれが片側に残ります。
        key = plan.canonical(raw)
        row["zone_key"] = key
        row["far"] = float(row["far"]) if row["far"] is not None else None
        row["bcr"] = float(row["bcr"]) if row["bcr"] is not None else None
        row["decided_on"] = row["decided_on"].isoformat() if row["decided_on"] else None
        if key in zones:
            continue
        rule = plan.rule_for(raw, rules=rules)
        if rule is None and rules:
            # 個別の規則が無い区分は default に落ちます。用途地域の大半
            # （商業・近隣商業・住居系）はここに来ます——建てられるのが
            # 普通なので、1 つずつ書いていません。データセット側の判定
            # （_buildability）と同じ扱いです。
            rule = rules.get("default") or None
        zones[key] = {
            "label": raw,
            "description": plan.describe(raw),
            # 業態の規則そのものが無い環境では可否を書きません。空欄のほうが、
            # 根拠なく「建てられます」と出すより安全です。
            "buildable": bool(rule.get("buildable", True)) if rule else None,
            "note": (rule.get("note") or rule.get("caution")) if rule else None,
        }

    out = feature_collection(
        _features(rows),
        zones=zones,
        facility_label=rules.get("facility_label"),
        provenance=provenance.for_tables(conn, ["city_planning_zones"]),
    )
    out["truncated"] = truncated
    out["disclaimer"] = rules.get("disclaimer")
    return out


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
