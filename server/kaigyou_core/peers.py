"""似た規模の土地と、**名前を挙げて**比べる。

既存の比較（``measures``）は分布の中の位置を返します。「静岡県内の1km商圏
5,448 件の中で上位6%」——位置は分かりますが、**相手の顔が見えません。**

沼津駅前の医院数を見た人が次に知りたいのは県内順位ではなく、三島や掛川と
比べてどうかです。東京駅前が激戦だとして、それは新宿・池袋・品川と比べても
激戦なのか。分布の percentile はその問いに答えません。

だからここは、**名前のある比較相手**を選んで横に並べます。選び方は 2 つ。

    乗降客数が同規模の駅前   …… 駅前は駅前と比べる。田畑と比べても何も出ない
    商圏人口が同規模の地点   …… 駅の無い土地でも比べられる

出てくるものの使いみちも 2 つあります。

    1. 同規模の中で**この地点だけ違う軸** → KSF を探す入口
    2. 同規模の中で**供給が薄い地点**     → 候補地の絞り込み

**予測はしません。** 2 の「供給が薄い」は「医院1件あたり人口が多い」という
数え上げであって、そこで開業すれば儲かるという話ではありません。売上・患者数・
成功確率を出さないのはこの製品の前提です。

**DB だけで完結します。** LLM を呼ばず、外部 API も叩きません。同じ地点なら
何度実行しても同じ表が出ます。
"""
from __future__ import annotations

from math import log
from typing import Any, Iterable, Mapping, Sequence

import psycopg

from kaigyou_core.db import columns_that_exist
from kaigyou_core.scoring import DEFAULT_FACILITY_CATEGORY

#: 比較に使ってよい mesh_scores の列。**設定の書き間違いをここで落とします。**
#:
#: 設定に列名を書けるようにした以上、書き間違いは起きます。黙って無視すると
#: その指標だけが表から消え、**表は成立して見えます**——気づくのは、比べた
#: はずの軸が入っていないことに読み手が気づいたときです。
ALLOWED_COLUMNS = frozenset({
    "population", "age_0_14", "age_15_64", "age_65_plus", "households",
    "population_growth", "facility_count", "population_per_facility",
    "nearest_facility_distance_m", "station_distance_m", "daily_passengers",
    "workers", "establishments", "population_change_projected",
    "land_price_yen_per_sqm",
})

#: 実測値（analyze_point の返り）で、mesh_scores とは名前が違う列。
#: 同じ量に 2 つの名前があるのは歴史的な事情で、ここで吸収します。
SITE_ALIASES = {"facility_count": ("facility_count", "dental_clinics"),
                "population_per_facility": ("population_per_facility",
                                            "population_per_clinic")}


class UnknownPeerColumn(ValueError):
    """設定の指標が、mesh_scores に無い列を指している。

    起動時（設定を読んだ時点）に落とします。黙って無視すると、その軸は
    永久に比較されません。
    """


def metric_specs(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """設定の metrics を、検査してから返す。"""
    specs: list[dict[str, Any]] = []
    for raw in config.get("metrics") or []:
        spec = dict(raw)
        for field in ("column", "share_of"):
            name = spec.get(field)
            if name and name not in ALLOWED_COLUMNS:
                raise UnknownPeerColumn(
                    f"peers.yaml の {spec.get('key')} が mesh_scores に無い列"
                    f"「{name}」を指しています。使える列: "
                    + "、".join(sorted(ALLOWED_COLUMNS)))
        specs.append(spec)
    return specs


def comparison(conn: psycopg.Connection, *, lat: float, lng: float,
               radius_m: int, profile: str, site_metrics: Mapping[str, Any],
               config: Mapping[str, Any],
               facility_category: str = DEFAULT_FACILITY_CATEGORY,
               ) -> dict[str, Any] | None:
    """この地点と、似た規模の地点を並べた比較。設定が無ければ ``None``。"""
    if not config:
        return None
    specs = metric_specs(config)
    wanted = {s[k] for s in specs for k in ("column", "share_of") if s.get(k)}
    have = columns_that_exist(conn, "mesh_scores", wanted)
    # 配備の途中で列が無いことがあります。**その軸だけ落として、落としたと
    # 書きます。** 表ごと 500 にするのは割に合いません。
    usable = [s for s in specs
              if s.get("column") in have
              and (not s.get("share_of") or s["share_of"] in have)]
    dropped = [s["label"] for s in specs if s not in usable]

    pool = _pool(conn, profile=profile, radius_m=radius_m,
                 facility_category=facility_category)
    unavailable: list[dict[str, str]] = []
    if dropped:
        unavailable.append({
            "what": "・".join(dropped),
            "why": "この環境のデータベースにその列がまだありません"})

    out: dict[str, Any] = {
        "basis": {
            "radius_m": radius_m,
            "profile": profile,
            "facility_category": facility_category,
            "pool": pool,
            # **比較相手はメッシュ中心での集計です。** この地点だけが指定した
            # 座標での集計で、そこはそろいません。丸めずに書いておきます。
            "note": ("比較相手の数値は、その駅・地点にいちばん近い500mメッシュを"
                     f"中心にした半径{radius_m}mの集計です。この地点だけは"
                     "指定された座標を中心に集計しています。"),
        },
        "site": _site_row(site_metrics, usable),
        "tables": [],
        "unavailable": unavailable,
    }
    if not pool["prefectures"]:
        unavailable.append({
            "what": "類似地点との比較",
            "why": "メッシュを取り込んである都道府県がありません"})
        return out

    for table in (_station_table(conn, lat=lat, lng=lng, radius_m=radius_m,
                                 profile=profile,
                                 facility_category=facility_category,
                                 site_metrics=site_metrics, specs=usable,
                                 config=config),
                  _population_table(conn, lat=lat, lng=lng, radius_m=radius_m,
                                    profile=profile,
                                    facility_category=facility_category,
                                    site_metrics=site_metrics, specs=usable,
                                    config=config)):
        if table is None:
            continue
        if table.get("unavailable"):
            unavailable.append(table["unavailable"])
            continue
        out["tables"].append(table)
    return out


# ------------------------------------------------------------------ 母集団
def _pool(conn: psycopg.Connection, *, profile: str, radius_m: int,
          facility_category: str) -> dict[str, Any]:
    """比較できる範囲。**取り込んである都道府県だけです。**

    全国の駅は入っていますが、メッシュは取り込んだ県のぶんしかありません。
    「同規模の駅」を全国から選んでも、その駅前の人口を数えられません。
    **だから相手が見つからないことがあり、そのときは理由ごと書きます。**
    """
    from kaigyou_core.analysis import prefecture_name

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pm.prefecture_code AS code, count(*) AS meshes
            FROM mesh_scores ms
            JOIN population_mesh pm ON pm.id = ms.mesh_id
            WHERE ms.profile = %s AND ms.radius_m = %s
              AND ms.facility_category = %s
            GROUP BY 1 ORDER BY 2 DESC
            """, (profile, radius_m, facility_category))
        rows = [dict(r) for r in cur.fetchall()]
    return {
        "prefectures": [prefecture_name(conn, r["code"]) or r["code"]
                        for r in rows],
        "meshes": sum(int(r["meshes"]) for r in rows),
    }


# -------------------------------------------------------------- 駅前どうし
def _station_table(conn: psycopg.Connection, *, lat: float, lng: float,
                   radius_m: int, profile: str, facility_category: str,
                   site_metrics: Mapping[str, Any],
                   specs: Sequence[Mapping[str, Any]],
                   config: Mapping[str, Any]) -> dict[str, Any] | None:
    """乗降客数が同規模の駅前。**駅前は駅前と比べます。**"""
    settings = config.get("station_scale") or {}
    station = site_metrics.get("nearest_station")
    passengers = site_metrics.get("daily_passengers")
    distance = site_metrics.get("station_distance_m")
    label = str(settings.get("label") or "乗降客数が同規模の駅")

    if not station or not passengers:
        return {"unavailable": {
            "what": label,
            "why": "この地点の最寄駅に乗降客数のデータがありません"}}
    limit_m = float(settings.get("max_station_distance_m") or 1200)
    if distance is not None and float(distance) > limit_m:
        # 駅から遠い地点で駅前どうしの比較を出すと、**関係のない比較が
        # 「この地点の比較」として読まれます。**
        return {"unavailable": {
            "what": label,
            "why": (f"最寄駅から{float(distance):,.0f}mあり、"
                    f"駅前どうしの比較（{limit_m:,.0f}m以内）にあたりません")}}

    band = config.get("band") or {}
    low = float(passengers) * float(band.get("low") or 0.5)
    high = float(passengers) * float(band.get("high") or 2.0)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH site AS (
                SELECT ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography AS g
            ), candidate AS (
                SELECT s.name, s.geom, s.daily_passengers,
                       abs(ln(s.daily_passengers::double precision
                              / %(passengers)s)) AS scale_gap
                FROM stations s, site
                WHERE s.daily_passengers IS NOT NULL
                  AND s.daily_passengers > 0
                  AND ST_Distance(s.geom::geography, site.g) > %(separation)s
                ORDER BY scale_gap
                LIMIT %(shortlist)s
            )
            SELECT c.name, c.daily_passengers AS station_passengers,
                   c.scale_gap,
                   ST_Distance(c.geom::geography, site.g) AS distance_m,
                   near.*
            FROM candidate c, site
            JOIN LATERAL (
                SELECT {_columns(specs)}, ms.area_label, pm.prefecture_code,
                       ST_Distance(pm.centroid::geography,
                                   c.geom::geography) AS mesh_distance_m
                FROM population_mesh pm
                JOIN mesh_scores ms ON ms.mesh_id = pm.id
                 AND ms.profile = %(profile)s AND ms.radius_m = %(radius)s
                 AND ms.facility_category = %(category)s
                WHERE ST_DWithin(pm.centroid::geography, c.geom::geography,
                                 %(mesh_within)s)
                ORDER BY pm.centroid <-> c.geom
                LIMIT 1
            ) near ON true
            ORDER BY c.scale_gap
            LIMIT %(count)s
            """,
            {"lat": lat, "lng": lng, "passengers": float(passengers),
             "separation": float(config.get("min_separation_m") or 3000),
             "shortlist": int(settings.get("shortlist") or 600),
             "profile": profile, "radius": radius_m,
             "category": facility_category,
             "mesh_within": float(settings.get("mesh_within_m") or 700),
             "count": int(config.get("count") or 5)})
        rows = [dict(r) for r in cur.fetchall()]

    peers = [_peer_row(row, specs, name=f"{row['name']}駅周辺",
                       inside_band=low <= float(row["station_passengers"]) <= high,
                       # 乗降客数は**その駅のもの**を使います（メッシュの列は
                       # 「そのメッシュの最寄駅」の値で、別の駅のことがあります）。
                       overrides={"daily_passengers": row["station_passengers"]})
             for row in rows]
    if not config.get("fill_outside_band", True):
        peers = [p for p in peers if p["inside_band"]]
    return _table(
        key="station_scale",
        label=label,
        reference={
            "station": station,
            "daily_passengers": int(passengers),
            "band": [round(low), round(high)],
            "description": (f"{station}駅の乗降客数 {int(passengers):,}人/日 の"
                            f"{band.get('low')}〜{band.get('high')}倍"),
        },
        peers=peers, site_metrics=site_metrics, specs=specs, config=config,
        empty_reason=("メッシュを取り込んである都道府県に、"
                      "乗降客数が同規模の駅が見つかりませんでした"))


# ------------------------------------------------------- 商圏人口どうし
def _population_table(conn: psycopg.Connection, *, lat: float, lng: float,
                      radius_m: int, profile: str, facility_category: str,
                      site_metrics: Mapping[str, Any],
                      specs: Sequence[Mapping[str, Any]],
                      config: Mapping[str, Any]) -> dict[str, Any] | None:
    """商圏人口が同規模の地点。**駅の無い土地でも比べられます。**

    市区町村ごとに 1 件だけ採ります。同じ市の中から 5 件並べても、それは
    「似た土地との比較」ではなく「同じ町の中の比較」です。
    """
    settings = config.get("population_scale") or {}
    label = str(settings.get("label") or "商圏人口が同規模の地点")
    population = site_metrics.get("population")
    if not population:
        return {"unavailable": {
            "what": label, "why": "この地点の商圏人口が取れていません"}}

    band = config.get("band") or {}
    low = float(population) * float(band.get("low") or 0.5)
    high = float(population) * float(band.get("high") or 2.0)
    # 1 市区町村 1 件。**同じ市の中で人口がいちばん近いメッシュ**を代表に
    # します（その市でいちばん大きい商圏、ではありません——比べたいのは
    # 「同規模」であって「その市の最大」ではないので）。
    pick = ("DISTINCT ON (ms.area_label) " if settings.get("one_per_area", True)
            else "")
    order = ("ms.area_label, abs(ms.population - %(population)s)"
             if settings.get("one_per_area", True)
             else "abs(ms.population - %(population)s)")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH site AS (
                SELECT ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography AS g
            ), picked AS (
                SELECT {pick}{_columns(specs)}, ms.area_label, ms.nearest_station,
                       ms.station_distance_m AS to_station_m,
                       pm.prefecture_code,
                       ST_Distance(pm.centroid::geography, site.g) AS distance_m,
                       abs(ms.population - %(population)s) AS population_gap
                FROM mesh_scores ms
                JOIN population_mesh pm ON pm.id = ms.mesh_id, site
                WHERE ms.profile = %(profile)s AND ms.radius_m = %(radius)s
                  AND ms.facility_category = %(category)s
                  AND ms.population BETWEEN %(low)s AND %(high)s
                  AND ms.area_label IS NOT NULL
                  AND ST_Distance(pm.centroid::geography, site.g) > %(separation)s
                ORDER BY {order}
            )
            SELECT * FROM picked ORDER BY population_gap LIMIT %(count)s
            """,
            {"lat": lat, "lng": lng, "population": float(population),
             "low": low, "high": high,
             "separation": float(config.get("min_separation_m") or 3000),
             "profile": profile, "radius": radius_m,
             "category": facility_category,
             "count": int(config.get("count") or 5)})
        rows = [dict(r) for r in cur.fetchall()]

    peers = [_peer_row(row, specs, name=_place_name(row), inside_band=True)
             for row in rows]
    guarded = _guard(peers, _site_row(site_metrics, specs),
                     settings.get("guards") or [], specs)
    return _table(
        key="population_scale", label=label, caution=guarded,
        reference={
            "population": round(float(population)),
            "band": [round(low), round(high)],
            "description": (f"商圏人口 {float(population):,.0f}人 の"
                            f"{band.get('low')}〜{band.get('high')}倍"
                            "（市区町村ごとに1件）"),
        },
        peers=peers, site_metrics=site_metrics, specs=specs, config=config,
        empty_reason="商圏人口が同規模の地点が、取り込み済みの範囲にありません")


def _place_name(row: Mapping[str, Any]) -> str:
    """比較相手の呼び名。**「メッシュ 12345」では読み手に伝わりません。**"""
    area = str(row.get("area_label") or "").strip()
    station = str(row.get("nearest_station") or "").strip()
    to_station = row.get("to_station_m")
    if station and to_station is not None and float(to_station) <= 1200:
        return f"{area}（{station}駅周辺）" if area else f"{station}駅周辺"
    return area or "（地名不明）"


# ------------------------------------------------------------------ 組み立て
def _columns(specs: Sequence[Mapping[str, Any]]) -> str:
    """SELECT に並べる列。**設定から来た名前は許可リストを通っています。**"""
    names = {s[k] for s in specs for k in ("column", "share_of") if s.get(k)}
    names.add("population")
    return ", ".join(f"ms.{n}" for n in sorted(names & ALLOWED_COLUMNS))


def _value(row: Mapping[str, Any], spec: Mapping[str, Any]) -> float | None:
    """1 つの軸の値。**比率は実数から作ります。**

    65歳以上「人口」をそのまま並べると、人口の違う地点どうしで比べられません。
    設定に ``share_of`` があれば、その列を分母にした百分率にします。
    """
    raw = row.get(str(spec.get("column")))
    if raw is None:
        return None
    value = float(raw)
    denominator = spec.get("share_of")
    if denominator:
        base = row.get(str(denominator))
        if not base:
            return None
        value = value / float(base) * 100
    elif spec.get("scale"):
        value = value * float(spec["scale"])
    places = spec.get("decimals")
    return round(value, int(places)) if places is not None else value


def _peer_row(row: Mapping[str, Any], specs: Sequence[Mapping[str, Any]], *,
              name: str, inside_band: bool,
              overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """比較相手 1 件。

    ``overrides`` は、**メッシュの列より確かな値がある軸**を差し替えます。
    駅前どうしの比較では乗降客数がそれです——メッシュが持つのは「そのメッシュの
    最寄駅」の乗降客数で、比較相手の駅のものとは限りません。実測：東京駅の
    比較相手に並べた新宿駅の行に、199,171 人/日（新宿の実際は 2,713,386）が
    入っていました。**同じ列名の別の量**です。
    """
    values = {str(s["key"]): _value(row, s) for s in specs}
    for key, value in (overrides or {}).items():
        if key in values:
            values[key] = float(value) if value is not None else None
    return {
        "label": name,
        "area": row.get("area_label"),
        "distance_km": (round(float(row["distance_m"]) / 1000, 1)
                        if row.get("distance_m") is not None else None),
        "inside_band": inside_band,
        "values": values,
    }


def _site_row(site_metrics: Mapping[str, Any],
              specs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """この地点の行。**比較相手と同じ軸で、同じ作り方で。**"""
    flat = dict(site_metrics)
    for column, aliases in SITE_ALIASES.items():
        for alias in aliases:
            if flat.get(column) is None and flat.get(alias) is not None:
                flat[column] = flat[alias]
    return {"label": "この地点", "inside_band": True,
            "values": {str(s["key"]): _value(flat, s) for s in specs}}


def _table(*, key: str, label: str, reference: Mapping[str, Any],
           peers: Sequence[Mapping[str, Any]],
           site_metrics: Mapping[str, Any],
           specs: Sequence[Mapping[str, Any]],
           config: Mapping[str, Any],
           empty_reason: str,
           caution: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    if not peers:
        return {"unavailable": {"what": label, "why": empty_reason}}
    site = _site_row(site_metrics, specs)
    comparable = [p for p in peers if p["inside_band"]]
    table = {
        "key": key,
        "label": label,
        "reference": dict(reference),
        "columns": [{"key": s["key"], "label": s["label"],
                     "unit": s.get("unit", "")} for s in specs],
        "peers": list(peers),
        "peer_count": len(peers),
        "comparable_count": len(comparable),
        "site": site,
        # 「その軸では同規模だが、別の軸が桁違い」を黙らせない。
        "caution": list(caution),
    }
    table["ranks"] = _ranks(site, comparable, specs)
    table["standouts"] = _standouts(table["ranks"], specs, config,
                                    len(comparable))
    table["supply_gap"] = _supply_gap(site, comparable, specs, config)
    return table


def _ranks(site: Mapping[str, Any], peers: Sequence[Mapping[str, Any]],
           specs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """軸ごとの、この地点の位置。**帯の外の相手は数に入れません。**

    「参考」として並べた相手を中央値に混ぜると、同規模でない地点との差が
    「同規模の中での差」として読まれます。
    """
    out: list[dict[str, Any]] = []
    for spec in specs:
        key = str(spec["key"])
        value = site["values"].get(key)
        values = [p["values"].get(key) for p in peers]
        values = [float(v) for v in values if v is not None]
        if value is None or len(values) < 2:
            continue
        middle = _median(values)
        ordered = sorted(values + [float(value)], reverse=True)
        rank = ordered.index(float(value)) + 1
        row = {
            "metric": key,
            "label": spec["label"],
            "unit": spec.get("unit", ""),
            "value": value,
            "median": round(middle, int(spec.get("decimals", 1) or 0)),
            "gap_vs_median_pct": (round((float(value) - middle) / abs(middle) * 100, 1)
                                  if middle else None),
            "rank": rank,
            "of": len(ordered),
            "position_label": f"{len(ordered)}地点中{rank}位（高い順）",
        }
        # **率どうしの差は、率で語ると意味が変わります。** 高齢化率 18.5% と
        # 中央値 20.4% の差は「1.9 ポイント」であって「9% 低い」ではありません。
        # 後者は、読み手が人数の話だと受け取ります。
        if str(spec.get("unit", "")) == "%":
            row["gap_points"] = round(float(value) - middle, 1)
        out.append(row)
    return out


def _standouts(ranks: Sequence[Mapping[str, Any]],
               specs: Sequence[Mapping[str, Any]],
               config: Mapping[str, Any], comparable: int) -> list[dict[str, Any]]:
    """同規模の中で**この地点だけ違う軸**。KSF を探す入口です。

    **結論ではありません。** 「高齢層が厚い」までが事実で、そこから何をするかは
    提言（第II部）の仕事です。
    """
    settings = config.get("standout") or {}
    if comparable < int(settings.get("min_peers") or 3):
        return []
    threshold = float(settings.get("gap_pct") or 25)
    points_threshold = float(settings.get("gap_points") or 5)
    reading = {str(s["key"]): (s.get("reading") or {}) for s in specs}
    out = []
    for row in ranks:
        # 率の軸はポイント差で、実数の軸は比で見ます（上の ``_ranks`` と同じ理由）。
        points = row.get("gap_points")
        gap = row.get("gap_vs_median_pct")
        measured = points if points is not None else gap
        limit = points_threshold if points is not None else threshold
        if measured is None or abs(float(measured)) < limit:
            continue
        direction = "high" if float(measured) > 0 else "low"
        out.append({
            "metric": row["metric"], "label": row["label"],
            "unit": row.get("unit", ""), "value": row["value"],
            "median": row["median"], "gap_pct": gap,
            "gap_points": points, "direction": direction,
            "reading": reading.get(row["metric"], {}).get(direction),
        })
    return sorted(out, key=lambda r: -abs(float(
        r["gap_points"] if r.get("gap_points") is not None else r["gap_pct"])))


def _supply_gap(site: Mapping[str, Any], peers: Sequence[Mapping[str, Any]],
                specs: Sequence[Mapping[str, Any]],
                config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """同規模の中で、**この地点より供給が薄い**地点。

    候補地を絞る側の使い方です。**推奨ではありません**——医院1件あたり人口が
    多いという数え上げで、そこで開業すれば患者が来るという話ではありません。
    現地の動線も、医院の中身も、この数字は何も見ていません。
    """
    settings = config.get("supply_gap") or {}
    axis = next((s for s in specs if s.get("supply_gap")), None)
    if axis is None:
        return []
    key = str(axis["key"])
    here = site["values"].get(key)
    if not here:
        return []
    threshold = float(settings.get("gap_pct") or 20)
    out = []
    for peer in peers:
        value = peer["values"].get(key)
        if value is None or not float(here):
            continue
        gap = (float(value) - float(here)) / abs(float(here)) * 100
        if gap < threshold:
            continue
        out.append({"label": peer["label"], "metric": key,
                    "metric_label": axis["label"], "unit": axis.get("unit", ""),
                    "value": value, "here": here, "gap_pct": round(gap, 1),
                    "distance_km": peer.get("distance_km")})
    out.sort(key=lambda r: -float(r["gap_pct"]))
    return out[:int(settings.get("max_listed") or 3)]


def _guard(peers: Sequence[dict[str, Any]], site: Mapping[str, Any],
           guards: Sequence[Mapping[str, Any]],
           specs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """**「その軸だけ同規模」を、同規模と呼ばせない。**

    実測：東京駅前（商圏人口 13,010 人）の「人口が同規模の地点」に、武蔵村山市
    と清瀬市が並びました。住んでいる人の数は確かに同じですが、従業者数は
    571,810 人 対 2,963 人——**193 倍**です。人口だけで似ていると言った表を、
    読み手は「似た土地との比較」として読みます。

    だから守りの軸を置きます。桁が違う相手は表からは消さず、**参考に落として
    統計から外し**、なぜそうしたかを表に添えます。
    """
    notes: list[dict[str, Any]] = []
    labels = {str(s["key"]): s["label"] for s in specs}
    for guard in guards:
        key = str(guard.get("metric") or "")
        here = site["values"].get(key)
        limit = float(guard.get("max_ratio") or 0)
        if not here or limit <= 0:
            continue
        for peer in peers:
            value = peer["values"].get(key)
            if value is None or not float(value):
                continue
            ratio = max(float(here) / float(value), float(value) / float(here))
            if ratio < limit:
                continue
            peer["inside_band"] = False
            peer["guard_reason"] = (
                f"{labels.get(key, key)}が{ratio:,.0f}倍違います")
            notes.append({
                "peer": peer["label"], "metric": key,
                "label": labels.get(key, key), "ratio": round(ratio, 1),
                "why": str(guard.get("why") or "")})
    return notes


def _median(values: Iterable[float]) -> float:
    ordered = sorted(float(v) for v in values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def scale_gap(a: float, b: float) -> float:
    """2 つの規模の隔たり。**比の対数で測ります。**

    差で測ると、乗降客数 4 万の駅にとっての 3 万差と、200 万の駅にとっての
    3 万差が同じ隔たりになります。
    """
    if a <= 0 or b <= 0:
        raise ValueError("規模は正の数でなければ比べられません")
    return abs(log(a / b))
