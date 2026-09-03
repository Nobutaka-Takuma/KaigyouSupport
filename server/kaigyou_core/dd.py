"""プレDD レポートの**事実**を、DB のデータだけから確定する。

**LLM を呼びません。** ここで出るものは全部、既に自社 DB に入っている静的
データの数え直しです。動的なもの（競合サイトの中身）だけが別で、それは
LLM が読んで観測として渡してきます。

    静的（人口・メッシュ・医院の位置と開設年・駅・地価・都市計画）
        → 事前に DB へ。ここで数えるだけ。API を叩かない。
    動的（各院サイトの訴求・価格・資格）
        → 都度 LLM。ただし読み取りだけ。

この分け方が効くのは費用の話だけではありません。**同じ地点なら何度実行しても
同じ数字が出る**ので、「先週と今週で結論が違う」が起きません。DD の文書で
それが起きると、文書そのものが信用されなくなります。

章立ては config/<業態>/dd.yaml の `chapters` が決めます。ここが返すのは
章ごとの「事実の束」で、散文は書きません。散文は LLM が、この束だけを見て
書きます。**束に無い数字は書けません。**
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from kaigyou_core import config as cfg
from kaigyou_core.analysis import DEFAULT_CATEGORY


class UnknownRiskCheck(ValueError):
    """設定のリスクが、実装のない判定を指している。

    黙って無視すると、**そのリスクは永久に該当しません。** 引っかからない
    のか、判定していないのかが区別できないので、起動時に落とします。
    """


def fact_pack(dataset: Mapping[str, Any],
              survey: Mapping[str, Any] | None = None,
              category: str = DEFAULT_CATEGORY) -> dict[str, Any]:
    """レポート 1 本ぶんの事実。章ごとに束ねて返します。"""
    conf = cfg.dd_config(category)
    return {
        "chapters": conf.get("chapters") or [],
        "location": _place(dataset),
        "trade_area": trade_area(dataset),
        "competition": competition(dataset, survey),
        "location_quality": location_quality(dataset),
        "demand": demand(dataset),
        "outlook": outlook(dataset),
        "risks": risks(dataset, conf),
        "growth": growth(dataset, survey, conf),
        "further_dd": further_dd(dataset, survey, conf),
    }


def _place(dataset: Mapping[str, Any]) -> dict[str, Any]:
    location = dataset.get("location") or {}
    query = dataset.get("query") or {}
    return {
        "name": location.get("name"),
        "prefecture": location.get("prefecture_name"),
        "municipality": location.get("municipality_name"),
        "lat": location.get("lat"), "lng": location.get("lng"),
        "radius_m": query.get("radius_m"),
        "profile": query.get("active_profile"),
        "generated_at": dataset.get("generated_at"),
    }


# ------------------------------------------------------------------ 第2章
def trade_area(dataset: Mapping[str, Any]) -> dict[str, Any]:
    """商圏の広さ・人口・かたち。**合計だけでは商圏を語れません。**"""
    catchment = dataset.get("catchment") or {}
    residents = ((dataset.get("demand") or {}).get("residents") or {})
    by_radius = residents.get("by_radius") or {}
    distribution = (dataset.get("demand") or {}).get("distribution") or {}
    site = dataset.get("site") or {}

    rings = []
    for radius in sorted(by_radius, key=lambda r: int(r)):
        row = by_radius[radius] or {}
        rings.append({"radius_m": int(radius),
                      "population": row.get("population"),
                      "households": row.get("households")})
    return {
        "kind": catchment.get("kind"),
        "description": catchment.get("description"),
        "area_km2": catchment.get("area_km2"),
        "rings": rings,
        "meshes": distribution.get("meshes"),
        "largest_mesh_share": distribution.get("largest_mesh_share"),
        "population_largest_mesh": distribution.get("population_largest_mesh"),
        "meshes_with_no_residents": distribution.get("meshes_with_no_residents"),
        "concentration": site.get("concentration"),
        "shape_note": distribution.get("definition"),
    }


# ------------------------------------------------------------------ 第3章
def competition(dataset: Mapping[str, Any],
                survey: Mapping[str, Any] | None) -> dict[str, Any]:
    """競合。**数（DB）と中身（LLM の観測）を分けて出します。**"""
    comp = dataset.get("competition") or {}
    surveyed = list((survey or {}).get("competitors") or [])
    return {
        "by_radius": comp.get("by_radius") or {},
        "proximity": comp.get("proximity") or {},
        "nearest": comp.get("nearest") or {},
        "by_specialty": comp.get("by_specialty") or {},
        "hours": comp.get("hours") or {},
        "vintage": comp.get("vintage") or {},
        # 中身まで調べた医院。**調べていなければ空**で、そう書きます。
        "surveyed": surveyed,
        "surveyed_count": len(surveyed),
        "not_surveyed": (survey or {}).get("not_surveyed"),
        "total_in_radius": ((comp.get("clinics_in_radius") or {}).get("count")),
    }


# ------------------------------------------------------------------ 第4章
def location_quality(dataset: Mapping[str, Any]) -> dict[str, Any]:
    """立地。駅・視認性・規制・コスト。**駅は説明変数の一つ**にすぎません。"""
    access = dataset.get("access") or {}
    station = access.get("nearest_station") or {}
    site = dataset.get("site") or {}
    return {
        "station": {
            "name": station.get("name"),
            "distance_m": station.get("distance_m"),
            "walk_minutes": (station.get("band") or {}).get("walk_minutes"),
            "band": (station.get("band") or {}).get("label"),
            "band_note": (station.get("band") or {}).get("note"),
            "daily_passengers": station.get("daily_passengers"),
            "direction": (station.get("direction") or {}).get("statement"),
            "population_side": station.get("population_side"),
        } if station.get("name") else None,
        "stations_in_radius": access.get("stations_in_radius") or {},
        "regulation": dataset.get("regulation") or {},
        "cost": dataset.get("cost") or {},
        "concentration": site.get("concentration"),
        "resolution_note": site.get("resolution_note"),
    }


# ------------------------------------------------------------------ 第5章
def demand(dataset: Mapping[str, Any]) -> dict[str, Any]:
    """人口と需要。常住・昼間・年齢構成。"""
    d = dataset.get("demand") or {}
    return {
        "residents": d.get("residents") or {},
        "daytime": d.get("daytime") or {},
        "distribution": d.get("distribution") or {},
        "measures": [m for m in ((dataset.get("measures") or {}).get("items") or [])
                     if m.get("layer") in (None, "demand", "population")],
        "insight_metrics": dataset.get("insight_metrics") or [],
    }


# ------------------------------------------------------------------ 第6章
def outlook(dataset: Mapping[str, Any]) -> dict[str, Any]:
    """将来性。**推計が無いなら「無い」と書きます。**

    実績（2015→2020）を将来の話として出すのがいちばん危ない読み違いなので、
    推計の有無をこの章の先頭に置きます。
    """
    d = (dataset.get("demand") or {}).get("outlook") or {}
    vintage = (dataset.get("competition") or {}).get("vintage") or {}
    growth_measure = next(
        (m for m in ((dataset.get("measures") or {}).get("items") or [])
         if m.get("key") == "population_growth"), None)
    return {
        "projection_available": bool(d.get("available")),
        "projection_note": d.get("note"),
        "years": d.get("years") or [],
        # 実績。**将来推計ではありません。**
        "observed_growth": growth_measure,
        # 競合の入れ替わり。これも「将来」の一部です。
        "opened_recently": vintage.get("opened_recently"),
        "opened_within_years": vintage.get("opened_within_years"),
        "opened_long_ago": vintage.get("opened_long_ago"),
        "opened_over_years_ago": vintage.get("opened_over_years_ago"),
        "median_opening_year": vintage.get("median_opening_year"),
        "vintage_coverage": vintage.get("coverage"),
        "vintage_note": vintage.get("note"),
    }


# ------------------------------------------------------------------ 第7章
def risks(dataset: Mapping[str, Any],
          conf: Mapping[str, Any]) -> list[dict[str, Any]]:
    """該当したリスクだけ。**該当しなかったものは並べません。**

    並べると調べた量が多く見えます。読む人が知りたいのは引っかかった項目です。
    """
    order = {"high": 0, "medium": 1, "low": 2}
    found: list[dict[str, Any]] = []
    for rule in conf.get("risks") or []:
        name = rule.get("check")
        check = _CHECKS.get(name)
        if check is None:
            raise UnknownRiskCheck(
                f"config の risk `{rule.get('key')}` が指す check `{name}` は"
                f"実装されていません。使えるのは {', '.join(sorted(_CHECKS))} です。")
        hit = check(dataset, rule)
        if not hit:
            continue
        found.append({
            "key": rule.get("key"), "label": rule.get("label"),
            "severity": rule.get("severity", "medium"),
            "why": rule.get("why"), "verify": rule.get("verify"),
            # **何の値で引っかかったか**を残します。閾値を動かすときに要ります。
            "observed": hit if isinstance(hit, str) else None,
        })
    return sorted(found, key=lambda r: order.get(r["severity"], 9))


def _measure(dataset: Mapping[str, Any], key: str) -> dict[str, Any] | None:
    for m in (dataset.get("measures") or {}).get("items") or []:
        if m.get("key") == key:
            return m
    return None


def _population_growth_below(dataset, rule) -> str | bool:
    m = _measure(dataset, "population_growth")
    value = (m or {}).get("value")
    if value is None:
        return False
    return f"{value:+.1f}%" if float(value) < float(rule["threshold"]) else False


def _axis_percentile_above(dataset, rule) -> str | bool:
    for axis in (dataset.get("positioning") or {}).get("axes") or []:
        if axis.get("key") != rule.get("axis"):
            continue
        score = axis.get("score")
        if score is None:
            return False
        return (f"{axis.get('label')} {score}（上位 {100 - int(score)}%）"
                if float(score) > float(rule["threshold"]) else False)
    return False


def _opened_recently_at_least(dataset, rule) -> str | bool:
    v = (dataset.get("competition") or {}).get("vintage") or {}
    n = v.get("opened_recently")
    if n is None:
        return False
    return (f"直近 {v.get('opened_within_years')} 年に {n} 件"
            if int(n) >= int(rule["threshold"]) else False)


def _largest_mesh_share_above(dataset, rule) -> str | bool:
    d = (dataset.get("demand") or {}).get("distribution") or {}
    share = d.get("largest_mesh_share")
    if share is None:
        return False
    return (f"最大メッシュに {float(share) * 100:.0f}%"
            if float(share) > float(rule["threshold"]) else False)


def _station_band_is(dataset, rule) -> str | bool:
    band = (((dataset.get("access") or {}).get("nearest_station") or {})
            .get("band") or {})
    return band.get("label") if band.get("label") == rule.get("value") else False


def _median_opening_year_before(dataset, rule) -> str | bool:
    v = (dataset.get("competition") or {}).get("vintage") or {}
    year = v.get("median_opening_year")
    if year is None:
        return False
    return f"開設年の中央値 {year} 年" if int(year) < int(rule["threshold"]) else False


def _outlook_unavailable(dataset, _rule) -> str | bool:
    d = (dataset.get("demand") or {}).get("outlook") or {}
    return "将来推計人口が未取得" if not d.get("available") else False


#: 設定から呼べる判定。**ここに無い名前は起動時に落とします。**
_CHECKS: dict[str, Callable[[Mapping[str, Any], Mapping[str, Any]], Any]] = {
    "population_growth_below": _population_growth_below,
    "axis_percentile_above": _axis_percentile_above,
    "opened_recently_at_least": _opened_recently_at_least,
    "largest_mesh_share_above": _largest_mesh_share_above,
    "station_band_is": _station_band_is,
    "median_opening_year_before": _median_opening_year_before,
    "outlook_unavailable": _outlook_unavailable,
}


# ------------------------------------------------------------------ 第8章
def growth(dataset: Mapping[str, Any], survey: Mapping[str, Any] | None,
           conf: Mapping[str, Any]) -> dict[str, Any]:
    """成長余地と KSF。

    **「競合が少ない＝機会」とは書きません。** 空いている領域は、まだ誰も
    やっていないのか、やってみて成立しなかったのか区別できません。だから
    機会ではなく**確かめるべき仮説**として出します。
    """
    positioning = dataset.get("positioning") or {}
    thin: list[dict[str, Any]] = []
    tally = (survey or {}).get("tally") or {}
    for row in tally.get("products") or []:
        if int(row.get("count") or 0) == 0:
            thin.append({"label": row.get("label"), "kind": "product"})
    for row in tally.get("place") or []:
        if int(row.get("count") or 0) == 0:
            thin.append({"label": row.get("label"), "kind": "place"})
    return {
        # GIS が計算した「周囲と比べた強み・弱み」。
        "axes": [a for a in (positioning.get("axes") or []) if a.get("score") is not None],
        "gaps": [g for g in (positioning.get("gaps") or []) if g.get("present")],
        "tags": positioning.get("tags") or [],
        "region_type": positioning.get("region_type"),
        # 周辺で掲げている医院が無かった領域。**調べた範囲の中の話です。**
        "thin_areas": thin,
        "surveyed_count": len((survey or {}).get("competitors") or []),
        "ksf_frame": (conf.get("ksf") or {}).get("frame") or [],
    }


# ------------------------------------------------------------------ 第9章
def further_dd(dataset: Mapping[str, Any], survey: Mapping[str, Any] | None,
               conf: Mapping[str, Any]) -> list[dict[str, Any]]:
    """追加DDで確認すべきこと。

    **原理的に取れないもの（設定）と、今回取れなかったもの（データ）を
    分けて出します。** 前者は次に走らせても出てきません。後者は出てきます。
    """
    items = [{**row, "reason": "public_data_gap"}
             for row in (conf.get("further_dd") or [])]

    quality = dataset.get("data_quality") or {}
    for gap in quality.get("unavailable_datasets") or []:
        label = gap.get("label") or gap.get("key") if isinstance(gap, Mapping) else gap
        items.append({"key": f"dataset:{label}", "label": str(label),
                      "why": "このデータセットが取り込まれていません。",
                      "how": "取り込めば次回から自動で出ます。",
                      "reason": "not_loaded"})

    not_surveyed = int((survey or {}).get("not_surveyed") or 0)
    if not_surveyed:
        items.append({
            "key": "unsurveyed_competitors",
            "label": f"未調査の競合 {not_surveyed} 件",
            "why": "近い順に上限で切っています。**存在しないという意味ではありません。**",
            "how": "上限を上げて再実行するか、現地で確認",
            "reason": "budget"})

    for c in (survey or {}).get("competitors") or []:
        if str(c.get("not_confirmed") or "").strip():
            items.append({
                "key": f"competitor:{c.get('name')}",
                "label": f"{c.get('name')}について確認できなかったこと",
                "why": str(c["not_confirmed"]),
                "how": "現地確認、または電話での問い合わせ",
                "reason": "not_on_web"})
    return items


def numbers_in(pack: Mapping[str, Any]) -> set[str]:
    """事実の束に実在する数値の一覧（検算用）。

    レポートの本文は LLM が書きます。**束に無い数字が本文に出ていたら、
    それは作られた数字です。** 検算はこの集合との照合で行います。
    """
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            found.add(_normalise(node))

    walk(pack)
    return found


def _normalise(value: float) -> str:
    if isinstance(value, int) or float(value).is_integer():
        return str(int(value))
    return f"{float(value):.4f}".rstrip("0").rstrip(".")
