"""「この街、どんな街？」——街の意外な事実を、データから選ぶ。

**数えるのは Python、驚くのは読み手。** ここは新しい数字を作りません。
データセットが既に確定させた比較（percentile・順位・軸のズレ・同規模の街との
差）の中から、**離れているものを選んで並べ替えるだけ**です。

    値だけ渡された読み手にできるのは、その値を言い換えることだけです。
        「商圏人口 75,494 人」
    比較を添えると、読み手はその街を他の街と並べて考えられます。
        「東京都の 5,448 商圏の中で上位 2.9%（153 位）」

選ぶときに守ることが 4 つあります。

**同じ話題で埋めない。** 人口の話が 5 枚並ぶのは「5 つの発見」ではありません
（`max_per_theme`）。

**根拠が弱いものは出さない。** 母集団 30 件の「上位 10%」は 3 位というだけの
ことで、分布ではありません。確からしさを数として持たせ、閾値で落とします
（`min_confidence`）。

**出せない比較は、出せないと書く。** 全国順位は「意味がないから出さない」の
ではなく「全国のメッシュを取り込んでいないから出せない」のです。読み手には
その区別がつきません。

**「意外」を作るために事実を曲げない。** 出典とデータ時点を必ず添えます。
開業の成否・売上・患者数は、ここでも予測しません。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from kaigyou_core.measures import MEASURE_SPECS

#: 母集団がこれ以上あれば、順位を分布として語ってよい（measures と同じ考え）。
STRONG_SAMPLE = 1000
FAIR_SAMPLE = 200

#: 比較に使う母集団の優先順。**上から、あるものを使います。**
#: 県全体より「同規模の商圏」のほうが読み手には近い比較ですが、母集団が
#: 小さくなりがちなので、確からしさとの兼ね合いで並べています。
SCOPE_PREFERENCE = ("prefecture", "urban", "with_clinics", "nearby",
                    "station_front", "neighbourhood", "municipality",
                    "similar_population")


class UnknownTownMetric(ValueError):
    """設定の指標が、実装の知らない名前を指している。

    黙って無視すると、**その指標は永久にカードにならず**、しかも画面は
    成立して見えます。気づくのは、出るはずの事実が出ないことに誰かが
    気づいたときです。
    """


def diagnose(dataset: Mapping[str, Any],
             config: Mapping[str, Any]) -> dict[str, Any]:
    """1 地点ぶんの「意外な事実」。**選ぶだけで、作りません。**"""
    if not config:
        return {"facts": [], "considered": 0,
                "unavailable": [{"what": "街の事実", "why": "設定がありません"}]}
    _check(config)

    candidates: list[dict[str, Any]] = []
    candidates += _from_measures(dataset, config)
    candidates += _from_gaps(dataset, config)
    candidates += _from_peers(dataset, config)

    facts = _select(candidates, config)
    return {
        "place": _place(dataset),
        "facts": facts,
        # 何件から選んだか。**5 件の裏に何があったかを隠さない。**
        "considered": len(candidates),
        "unavailable": _unavailable(dataset, config),
        "generated_at": dataset.get("generated_at"),
    }


def _check(config: Mapping[str, Any]) -> None:
    for spec in config.get("metrics") or []:
        key = str(spec.get("key") or "")
        if key not in MEASURE_SPECS:
            raise UnknownTownMetric(
                f"town_facts.yaml の「{key}」は指標の一覧にありません。"
                "使えるのは: " + "、".join(sorted(MEASURE_SPECS)))


def _place(dataset: Mapping[str, Any]) -> dict[str, Any]:
    location = dataset.get("location") or {}
    query = dataset.get("query") or {}
    station = ((dataset.get("access") or {}).get("nearest_station") or {})
    return {
        "name": location.get("name"),
        "prefecture": location.get("prefecture_name"),
        "municipality": location.get("municipality_name"),
        "lat": location.get("lat"), "lng": location.get("lng"),
        "radius_m": query.get("radius_m"),
        "nearest_station": station.get("name"),
        "station_distance_m": station.get("distance_m"),
    }


# ------------------------------------------------------------ 指標そのものの高低
def _from_measures(dataset: Mapping[str, Any],
                   config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """percentile が端に寄っている指標を、そのまま事実にする。

    **どの母集団と比べたかを必ず書きます。** 「上位3%」だけでは、何の中の
    3% なのかが分かりません。銀座の人口は東京都の中では下位でも、同じ中央区の
    中ではまた違う位置にあります。
    """
    items = {str(i.get("key")): i for i in
             ((dataset.get("measures") or {}).get("items") or [])}
    scopes = {str(s.get("benchmark_type")): s for s in
              ((dataset.get("measures") or {}).get("benchmark_scopes") or [])}
    out: list[dict[str, Any]] = []

    for spec in config.get("metrics") or []:
        item = items.get(str(spec.get("key")))
        if not item:
            continue
        benchmark = _best_benchmark(item)
        if benchmark is None:
            continue
        percentile = benchmark.get("percentile")
        if percentile is None:
            continue
        percentile = float(percentile)
        high = spec.get("at_least")
        low = spec.get("at_most")
        if high is not None and percentile >= float(high):
            direction = "high"
        elif low is not None and percentile <= float(low):
            direction = "low"
        else:
            continue
        title = spec.get(direction)
        if not title:
            continue

        scope = scopes.get(str(benchmark.get("benchmark_type"))) or {}
        sample = int(benchmark.get("of") or scope.get("sample_count") or 0)
        out.append({
            "id": f"{spec['key']}_{direction}",
            "theme": str(spec.get("theme") or spec["key"]),
            "kind": "level",
            "title": str(title),
            "fact": f"{item.get('label')} {_value(item)}",
            "comparison": _comparison(benchmark, scope),
            "why_interesting": str(spec.get("why") or ""),
            "source": _source(item),
            "confidence": _confidence(sample, bool(benchmark.get("discriminating"))),
            "surprise": abs(percentile - 50) * 2,      # 0〜100
            "detail": {"metric": str(spec["key"]),
                       "benchmark_type": benchmark.get("benchmark_type")},
        })
    return out


def _best_benchmark(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """どの母集団で語るか。**弁別力のあるものの中から、優先順で。**

    母集団の半分が無人メッシュなら、町の中心はどこでも上位に来ます。その集合で
    「上位4.5%」と書くのは、山林と市街地を混ぜた比較です（`discriminating`）。
    """
    usable = [b for b in (item.get("benchmarks") or [])
              if b.get("percentile") is not None and b.get("discriminating")]
    if not usable:
        return None
    ranked = {str(b.get("benchmark_type")): b for b in usable}
    for scope in SCOPE_PREFERENCE:
        if scope in ranked:
            return dict(ranked[scope])
    return dict(usable[0])


def _comparison(benchmark: Mapping[str, Any],
                scope: Mapping[str, Any]) -> str:
    label = str(scope.get("label") or "比較対象")
    sample = benchmark.get("of") or scope.get("sample_count")
    position = benchmark.get("position_label")
    rank = benchmark.get("rank")
    head = f"{label}"
    if sample:
        head += f" {int(sample):,}件の中で"
    if position:
        head += str(position)
    if rank:
        head += f"（{int(rank):,}位）"
    median = benchmark.get("benchmark_value")
    if median is not None:
        head += f"／中央値 {_round(median)}"
    return head


# ---------------------------------------------------------------- 軸どうしのズレ
def _from_gaps(dataset: Mapping[str, Any],
               config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """2 つの軸がそろっていないところ。**単独の高さより意外です。**

    「人口が多い」は地図を見れば分かります。「住んでいる人の規模に対して働きに
    来る人が多い」は、2 つを並べないと出てきません。ズレの計算は positioning
    が済ませてあるので、ここは拾って言い換えるだけです。
    """
    gaps = {str(g.get("key")): g for g in
            ((dataset.get("positioning") or {}).get("gaps") or [])}
    compared = ((dataset.get("positioning") or {}).get("compared_with") or {})
    sample = int(compared.get("sample_count") or 0)
    out: list[dict[str, Any]] = []

    for spec in config.get("gaps") or []:
        gap = gaps.get(str(spec.get("key")))
        if not gap or not gap.get("present") or not gap.get("statement"):
            continue
        a, b = gap.get("a") or {}, gap.get("b") or {}
        out.append({
            "id": f"gap_{spec['key']}",
            "theme": str(spec.get("theme") or spec["key"]),
            "kind": "contrast",
            "title": str(spec.get("title") or gap.get("label")),
            "fact": str(gap.get("statement")),
            "comparison": (
                f"{a.get('label')}が{_position(a.get('percentile'))}、"
                f"{b.get('label')}が{_position(b.get('percentile'))}"
                + (f"（{compared.get('label')} {sample:,}件の中で）" if sample else "")),
            "why_interesting": str(spec.get("why") or "") + (
                f" {gap['note']}" if gap.get("note") else ""),
            "source": "この分析が算出（国勢調査・経済センサス等の集計から）",
            "confidence": _confidence(sample, True),
            "surprise": min(float(gap.get("gap") or 0) * 200, 100),
            "detail": {"gap": str(spec["key"])},
        })
    return out


# ------------------------------------------------------- 同規模の街と比べて
def _from_peers(dataset: Mapping[str, Any],
                config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """名前のある相手と比べて際立っている軸。

    **「県内で上位3%」より「三島・掛川と比べて」のほうが近い話です。** 相手の
    選び方と際立ちの判定は peers が済ませています。ここは言い換えるだけ。
    """
    spec = config.get("peers") or {}
    if not spec:
        return []
    out: list[dict[str, Any]] = []
    for table in ((dataset.get("peers") or {}).get("tables") or []):
        names = [str(p.get("label")) for p in (table.get("peers") or [])
                 if p.get("inside_band")]
        comparable = int(table.get("comparable_count") or 0)
        for item in (table.get("standouts") or []):
            direction = str(item.get("direction"))
            out.append({
                "id": f"peer_{table.get('key')}_{item.get('metric')}",
                "theme": str(spec.get("theme") or "peers"),
                "kind": "peers",
                "title": str(spec.get("title") or "{label}").format(
                    label=item.get("label"),
                    direction=spec.get(f"direction_{direction}", "違う")),
                "fact": (f"{item.get('label')} {_unit(item.get('value'), item.get('unit'))}"
                         f"（同規模の中央値 {_unit(item.get('median'), item.get('unit'))}）"),
                "comparison": (f"{table.get('label')}：" + "・".join(names[:5])
                               + f" と比べて{_diff(item)}"),
                "why_interesting": str(spec.get("why") or "") + (
                    f" {item['reading']}" if item.get("reading") else ""),
                "source": "この分析が算出（同規模の地点との比較）",
                # 相手が少なければ確からしさは下がります。**3 件未満は peers 側で
                # 既に落ちています。**
                "confidence": 0.85 if comparable >= 4 else 0.75,
                "surprise": min(abs(float(
                    item.get("gap_points") if item.get("gap_points") is not None
                    else item.get("gap_pct") or 0)) * (4 if item.get("gap_points")
                                                       is not None else 0.5), 100),
                "detail": {"peers_table": str(table.get("key")),
                           "metric": str(item.get("metric"))},
            })
    return out


# ------------------------------------------------------------------ 選ぶ
def _select(candidates: Sequence[Mapping[str, Any]],
            config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """離れている順に、話題が重ならないように選ぶ。

    重みは設定から。**対比を少し重くしてあります**——「人口が多い」は地図で
    分かることですが、「昼と夜で入れ替わる」は並べないと出てきません。
    """
    weights = config.get("theme_weight") or {}
    floor = float(config.get("min_confidence") or 0)
    per_theme = int(config.get("max_per_theme") or 1)
    count = int(config.get("count") or 5)

    scored = []
    for candidate in candidates:
        if float(candidate.get("confidence") or 0) < floor:
            continue
        weight = float(weights.get(str(candidate.get("kind")), 1.0))
        scored.append({**candidate,
                       "score": round(float(candidate.get("surprise") or 0)
                                      * weight, 1)})
    scored.sort(key=lambda c: (-c["score"], c["id"]))

    taken: dict[str, int] = {}
    chosen: list[dict[str, Any]] = []
    for candidate in scored:
        theme = str(candidate.get("theme"))
        if taken.get(theme, 0) >= per_theme:
            continue
        taken[theme] = taken.get(theme, 0) + 1
        chosen.append(candidate)
        if len(chosen) >= count:
            break
    return chosen


def _unavailable(dataset: Mapping[str, Any],
                 config: Mapping[str, Any]) -> list[dict[str, str]]:
    """出せなかった比較。**空欄にしないための欄です。**

    設定に固定で書いてあるもの（全国比較）と、この地点で実際に取れなかった
    ものの両方を返します。後者はデータセットが理由まで持っています。
    """
    out = [{"what": str(u.get("what")), "why": str(u.get("why"))}
           for u in (config.get("unavailable") or [])]
    outlook = (dataset.get("demand") or {}).get("outlook") or {}
    if outlook.get("available") is False:
        out.append({"what": "将来推計人口との比較",
                    "why": str(outlook.get("note")
                               or "この地域の将来推計人口を取り込んでいません")})
    daytime = (dataset.get("location") or {}).get("daytime") or {}
    if daytime.get("available") is False:
        out.append({"what": "昼夜間人口比率（市区町村）",
                    "why": str(daytime.get("note")
                               or "従業地・通学地集計を取り込んでいません")})
    for gap in ((dataset.get("peers") or {}).get("unavailable") or []):
        out.append({"what": str(gap.get("what")), "why": str(gap.get("why"))})
    return out


# ------------------------------------------------------------------ 表記
def _confidence(sample: int, discriminating: bool) -> float:
    """この比較をどれだけ信じてよいか。**母集団の大きさで決めます。**

    30 件の中の「上位10%」は 3 位というだけのことで、分布ではありません。
    """
    if not discriminating:
        return 0.5
    if sample >= STRONG_SAMPLE:
        return 0.95
    if sample >= FAIR_SAMPLE:
        return 0.85
    if sample >= 30:
        return 0.7
    return 0.5


def _value(item: Mapping[str, Any]) -> str:
    return _unit(item.get("value"), item.get("unit"))


def _unit(value: Any, unit: Any) -> str:
    if value is None:
        return "—"
    unit = str(unit or "")
    body = f"{float(value):,.1f}" if unit == "%" else f"{float(value):,.0f}"
    return f"{body}{unit}"


def _diff(item: Mapping[str, Any]) -> str:
    points = item.get("gap_points")
    if points is not None:
        return f"{float(points):+.1f}ポイント"
    percent = item.get("gap_pct")
    return f"{float(percent):+.1f}%" if percent is not None else "差があります"


def _position(fraction: Any) -> str:
    """percentile を、そのまま日本語にする。

    **「上位52%」は位置を表す言葉として働きません。** 上位と言われた読み手は
    高いほうを思い浮かべますが、52% は真ん中です。真ん中は真ん中と書きます。
    """
    if fraction is None:
        return "—"
    top = (1 - float(fraction)) * 100
    if top <= 40:
        return f"上位{top:.0f}%"
    if top >= 60:
        return f"下位{100 - top:.0f}%"
    return f"真ん中あたり（上位{top:.0f}%）"


def _round(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:,.1f}" if abs(number) < 100 else f"{number:,.0f}"


def _source(item: Mapping[str, Any]) -> str:
    source = str(item.get("source") or "")
    year = item.get("data_year")
    return f"{source}／{year}年" if year else source
