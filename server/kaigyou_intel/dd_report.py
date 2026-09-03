"""プレDD レポート（10 章）を Markdown にする。

**数字は事実の束から、文章は LLM から。** 混ぜません。表と件数は
`kaigyou_core.dd` が DB のデータだけで確定させたもので、LLM は「その事実が
何を意味するか」の文だけを書きます。

これまでのレポートは、仮説を立てて検証する筋書きが本文になっていました。
問いと仮説の一覧が前に出て、**読み手が知りたい「この商圏はどうなのか」が
どこにも無い**、という形でした。章立てを固定したのはそのためです。

読み手は 2 通りいます。

    これから開業する人   …… ここで開業して患者が来るか
    既存医院を買う人     …… 承継後も患者が残るか、のれん代の前提は何か

同じ事実を見ますが、読み方が違います。だから総合評価は分けて書きます。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from kaigyou_intel.report import _sources_block, _num, _pct, _table

SEVERITY_LABEL = {"high": "高", "medium": "中", "low": "低"}


def to_markdown(report: Mapping[str, Any], pack: Mapping[str, Any],
                sources: Sequence[Mapping[str, Any]] = (),
                disclaimer: str = "") -> str:
    """レポート 1 本ぶん。章の順番は設定（dd.yaml の chapters）が決めます。"""
    place = pack.get("location") or {}
    takeaway = {t.get("chapter"): t.get("takeaway")
                for t in report.get("takeaways") or []}

    name = place.get("name") or " ".join(
        x for x in (place.get("prefecture"), place.get("municipality")) if x) \
        or f"{place.get('lat')},{place.get('lng')}"
    heading = str(report.get("title") or "").strip() or f"{name} 事前調査レポート"

    lines = [f"# {heading}", ""]
    lines += [f"{name}　地点 {place.get('lat')}, {place.get('lng')}"
              f" / 半径 {_m(place.get('radius_m'))}", ""]
    lines += ["この文書は、**これから開業する人の事前調査**と、**既存医院を買う人の"
              "初期デューデリジェンス**の両方に使えるように書いてあります。"
              "第10章の総合評価は、その 2 つを分けて記しています。", ""]

    body = {
        "summary": lambda: _summary(report),
        "trade_area": lambda: _trade_area(pack),
        "competition": lambda: _competition(pack),
        "location": lambda: _location(pack),
        "demand": lambda: _demand(pack),
        "outlook": lambda: _outlook(pack),
        "risks": lambda: _risks(pack),
        "growth": lambda: _growth(pack, report),
        "further_dd": lambda: _further_dd(pack),
        "verdict": lambda: _verdict(report),
    }
    for index, chapter in enumerate(pack.get("chapters") or [], start=1):
        key = chapter.get("key")
        render = body.get(key)
        if render is None:
            continue
        lines += [f"## {index}. {chapter.get('title')}", ""]
        # 章の読みどころ。**事実より先に置きます**——読み手はまずここを見ます。
        if key not in ("summary", "verdict") and takeaway.get(key):
            lines += [f"> {takeaway[key]}", ""]
        lines += render()

    lines += _sources_block(sources)
    if disclaimer:
        lines += ["## 免責", "", disclaimer, ""]
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------------ 各章
def _summary(report: Mapping[str, Any]) -> list[str]:
    return [str(report.get("summary") or "—"), ""]


def _trade_area(pack: Mapping[str, Any]) -> list[str]:
    t = pack.get("trade_area") or {}
    lines = [f"- 商圏の取り方：{t.get('description') or t.get('kind') or '—'}",
             f"- 面積：{_num(t.get('area_km2'), ' km²')}"]
    if t.get("meshes"):
        lines.append(f"- 500m メッシュ：{t['meshes']} 面"
                     + (f"（うち居住者なし {t['meshes_with_no_residents']} 面）"
                        if t.get("meshes_with_no_residents") else ""))
    lines.append("")

    rings = [r for r in (t.get("rings") or []) if r.get("population") is not None]
    if rings:
        lines += _table(["半径", "常住人口", "世帯"],
                        [[_m(r["radius_m"]), _num(r.get("population"), " 人"),
                          _num(r.get("households"), " 世帯")] for r in rings])

    share = t.get("largest_mesh_share")
    if share is not None:
        lines += [
            f"商圏人口の **{float(share) * 100:.0f}%** が 1 つの 500m メッシュに"
            f"入っています（{_num(t.get('population_largest_mesh'), ' 人')}）。",
            "",
            "**合計人口だけでは商圏の形は分かりません。** 一様な住宅地なのか、"
            "集合住宅 1 棟に乗っているのかで、来院の見込みも将来の振れ幅も"
            "変わります。", ""]
    conc = t.get("concentration")
    if isinstance(conc, Mapping) and conc.get("index") is not None:
        lines += [f"足元への集中度：**{conc['index']:.2f}**"
                  f"（1.00 なら商圏内に一様。{conc.get('note') or ''}）", ""]
    return lines


def _competition(pack: Mapping[str, Any]) -> list[str]:
    c = pack.get("competition") or {}
    lines: list[str] = []
    by_radius = c.get("by_radius") or {}
    if by_radius:
        rows = []
        for radius in sorted(by_radius, key=lambda r: int(r)):
            row = by_radius[radius] or {}
            rows.append([_m(int(radius)), _num(row.get("dental_clinics"), " 件"),
                         _num(row.get("population_per_clinic"), " 人")])
        lines += _table(["半径", "歯科医院", "1 医院あたり人口"], rows)

    ladder = (c.get("proximity") or {}).get("nth_nearest_distance_m") or {}
    if ladder:
        rows = [[f"{n} 番目", _m(ladder[n])] for n in sorted(ladder, key=int)]
        lines += ["**近さの階段**（件数が同じでも、短ければ密集、長ければ分散）", ""]
        lines += _table(["近い順", "直線距離"], rows)

    vintage = c.get("vintage") or {}
    if vintage.get("available"):
        lines += [
            f"- 開設年が取れた医院：{vintage.get('with_opening_date')} / "
            f"{vintage.get('total_clinics')} 件",
            f"- 開設年の中央値：**{vintage.get('median_opening_year')} 年**",
            f"- 直近 {vintage.get('opened_within_years')} 年の新規開設："
            f"**{vintage.get('opened_recently')} 件**",
            f"- {vintage.get('opened_over_years_ago')} 年より前の開設："
            f"**{vintage.get('opened_long_ago')} 件**", ""]
        if vintage.get("note"):
            lines += [str(vintage["note"]), ""]

    # 中身まで調べた医院。**調べていなければ、そう書きます。**
    surveyed = c.get("surveyed") or []
    if surveyed:
        lines += [f"### 中身まで調べた医院（{len(surveyed)} 件）", ""]
        for one in surveyed:
            lines += _one_clinic(one)
    else:
        lines += ["### 各医院の中身", "",
                  "**この分析では調べていません。** 件数と距離は施設データベース"
                  "から出していますが、各医院が何を掲げ、いくらで、いつ開けて"
                  "いるかは Web を見ないと分かりません。"
                  "「周辺の競合を分析する」を実行すると、この節が埋まります。", ""]
    if c.get("not_surveyed"):
        lines += [f"半径内 {c.get('total_in_radius')} 件のうち "
                  f"**{c['not_surveyed']} 件は未調査**です"
                  "（近い順に上限で切っています）。", ""]
    return lines


def _one_clinic(one: Mapping[str, Any]) -> list[str]:
    lines = [f"**{one.get('name')}**"
             + (f"（{_m(one.get('distance_m'))}）" if one.get("distance_m") else "")]
    for key, label in (("products", "扱っている領域"),
                       ("positioning", "掲げている強み"),
                       ("target", "訴求している層")):
        values = [str(v) for v in (one.get(key) or []) if str(v).strip()]
        if values:
            lines.append(f"- {label}：{'、'.join(values)}")
    for key, label in (("price_note", "価格"), ("place_note", "立地・診療時間"),
                       ("promotion_note", "Web での訴求")):
        if str(one.get(key) or "").strip():
            lines.append(f"- {label}：{one[key]}")
    read = list(one.get("read") or [])
    if read:
        lines.append("- 開いた頁：" + " / ".join(
            str(r.get("url")) for r in read if r.get("url")))
    else:
        lines.append("- **開いて読んだ頁はありません**（検索結果の抜粋のみ）")
    return lines + [""]


def _location(pack: Mapping[str, Any]) -> list[str]:
    q = pack.get("location_quality") or {}
    lines: list[str] = []
    station = q.get("station")
    if station:
        lines += [f"- 最寄り駅：**{station.get('name')}**"
                  f"　{_m(station.get('distance_m'))}"
                  f"（徒歩 {station.get('walk_minutes')} 分）",
                  f"- 駅との関係：**{station.get('band')}**"
                  + (f" — {station.get('band_note')}" if station.get("band_note") else "")]
        if station.get("daily_passengers"):
            lines.append(f"- 乗降客数：{_num(station['daily_passengers'], ' 人/日')}")
        if station.get("direction"):
            lines.append(f"- 方位：{station['direction']}")
        side = station.get("population_side") or {}
        if side.get("share_toward_station") is not None:
            lines.append(
                f"- 商圏人口の駅側への寄り：**"
                f"{float(side['share_toward_station']) * 100:.0f}%**"
                f" — {side.get('reading') or ''}")
        lines.append("")
        lines += ["**駅は説明変数の一つです。** この地点の商圏は駅の商圏では"
                  "ありません。上の「駅との関係」が弱いなら、乗降客数を来院"
                  "動線として読むことはできません。", ""]
    else:
        lines += ["- 最寄り駅：取得できていません", ""]

    plan = (q.get("regulation") or {}).get("city_plan")
    if plan:
        lines += [f"- 用途地域：{plan.get('zone_label') or '—'}"
                  f"（建蔽率 {plan.get('building_coverage_pct') or '—'}% / "
                  f"容積率 {plan.get('floor_area_ratio_pct') or '—'}%）", ""]
    else:
        lines += ["- 用途地域：この地点の判定が取れていません"
                  "（**建てられるかどうかは別途確認が必要です**）", ""]

    cost = q.get("cost") or {}
    if cost.get("land_price_yen_per_sqm"):
        lines += [f"- 地価（公示）：{_num(cost['land_price_yen_per_sqm'], ' 円/m²')}"]
        if cost.get("rent_estimate"):
            lines.append(f"- 想定賃料：{cost['rent_estimate']}"
                         "（**想定利回りによる換算です。実勢賃料ではありません**）")
        lines.append("")
    else:
        lines += ["- 地価：この地点の近傍に標準地がありません", ""]
    if q.get("resolution_note"):
        lines += [str(q["resolution_note"]), ""]
    return lines


def _demand(pack: Mapping[str, Any]) -> list[str]:
    d = pack.get("demand") or {}
    lines: list[str] = []
    residents = d.get("residents") or {}
    daytime = d.get("daytime") or {}
    if residents.get("age_bands"):
        rows = [[b.get("label"), _num(b.get("population"), " 人"),
                 _pct(b.get("share")) if b.get("share") is not None else "—"]
                for b in residents["age_bands"]]
        lines += ["### 年齢構成", ""]
        lines += _table(["年齢", "人口", "構成比"], rows)
    if daytime.get("workers") is not None:
        lines += [f"- 昼間の従業者：{_num(daytime.get('workers'), ' 人')}",
                  f"- 事業所：{_num(daytime.get('establishments'), ' 所')}", ""]
    metrics = [m for m in (d.get("insight_metrics") or []) if m.get("value") is not None]
    if metrics:
        lines += ["### この商圏の需要側の指標", ""]
        lines += _table(["指標", "値", "意味"],
                        [[m.get("label"), _num(m.get("value"), m.get("unit") or ""),
                          m.get("means") or m.get("note") or "—"] for m in metrics])
    if not lines:
        lines = ["人口の内訳が取得できていません。", ""]
    return lines


def _outlook(pack: Mapping[str, Any]) -> list[str]:
    o = pack.get("outlook") or {}
    lines: list[str] = []
    if not o.get("projection_available"):
        # **ここを先頭に置きます。** 実績を将来の話として読まれるのが、この章の
        # いちばん危ない誤読です。
        lines += ["**将来推計人口は取り込まれていません。**", "",
                  str(o.get("projection_note") or ""),
                  "", "以下は 2015→2020 の**実績**です。将来の予測ではありません。", ""]
    observed = o.get("observed_growth") or {}
    if observed.get("value") is not None:
        lines += [f"- 人口増減率（実績）：**{float(observed['value']):+.1f}%**"
                  f"　{observed.get('note') or ''}", ""]
    years = o.get("years") or []
    if years:
        rows = [[y.get("year"), _num(y.get("population"), " 人"),
                 _pct(y.get("change_from_base"))
                 if y.get("change_from_base") is not None else "—"]
                for y in years]
        lines += _table(["年", "推計人口", "基準年比"], rows)

    lines += ["### 競合側の入れ替わり", "",
              "将来性は人口だけでは決まりません。**周辺医院が増えるか減るか**も、"
              "同じだけ効きます。", ""]
    if o.get("median_opening_year"):
        lines += [f"- 開設年の中央値：{o['median_opening_year']} 年",
                  f"- 直近 {o.get('opened_within_years')} 年の新規開設："
                  f"**{o.get('opened_recently')} 件**",
                  f"- {o.get('opened_over_years_ago')} 年より前の開設："
                  f"**{o.get('opened_long_ago')} 件**"
                  "（今後 10 年で世代交代が起きうる医院）", ""]
        if o.get("vintage_coverage") is not None:
            lines += [f"開設年が取れたのは全体の "
                      f"{float(o['vintage_coverage']) * 100:.0f}% です。", ""]
    return lines


def _risks(pack: Mapping[str, Any]) -> list[str]:
    found = pack.get("risks") or []
    if not found:
        return ["設定した基準に**該当したリスクはありません。**"
                "基準そのものは config/<業態>/dd.yaml にあります。", ""]
    lines = ["**該当したものだけ**を並べています。該当しなかった項目は"
             "出していません——並べると、調べた量が多く見えるだけです。", ""]
    lines += _table(["重大度", "リスク", "観測値", "なぜ効くか", "確かめ方"],
                    [[SEVERITY_LABEL.get(r.get("severity"), r.get("severity")),
                      r.get("label"), r.get("observed") or "—",
                      r.get("why") or "—", r.get("verify") or "—"]
                     for r in found])
    return lines


def _growth(pack: Mapping[str, Any], report: Mapping[str, Any]) -> list[str]:
    g = pack.get("growth") or {}
    lines: list[str] = []
    axes = g.get("axes") or []
    if axes:
        lines += ["### 周囲と比べた位置（GIS が計算した値）", ""]
        lines += _table(["観点", "偏差値", "評価", "意味"],
                        [[a.get("label"), a.get("score"), a.get("assessment"),
                          a.get("means") or "—"] for a in axes])
    for gap in g.get("gaps") or []:
        if gap.get("statement"):
            lines += [f"- {gap['statement']}"]
    if g.get("gaps"):
        lines.append("")

    thin = g.get("thin_areas") or []
    if thin:
        lines += [f"### 調べた {g.get('surveyed_count')} 院に無かった領域", ""]
        lines += [f"- {t.get('label')}" for t in thin]
        lines += ["", "**空いている＝機会ではありません。** まだ誰もやって"
                  "いないのか、やってみて成立しなかったのかは、この分析では"
                  "区別できません。次の仮説はそれを踏まえたものです。", ""]

    hypotheses = report.get("growth_hypotheses") or []
    if hypotheses:
        lines += ["### 成長余地の仮説", ""]
        for h in hypotheses:
            lines += [f"**{h.get('position')}**", "",
                      f"- そう言える根拠：{h.get('why')}",
                      f"- **外れるとしたら**：{h.get('caveat')}", ""]

    frame = g.get("ksf_frame") or []
    if frame:
        lines += ["### KSF — この立地で成否を分ける論点", "",
                  "**答えではなく問いです。** 事業計画を作るときに、この 5 つに"
                  "答えられるかどうかで詰まり方が変わります。", ""]
        lines += _table(["論点", "問い"],
                        [[f.get("label"), f.get("question")] for f in frame])
    return lines


def _further_dd(pack: Mapping[str, Any]) -> list[str]:
    items = pack.get("further_dd") or []
    if not items:
        return ["特にありません。", ""]
    reason_label = {
        "public_data_gap": "公開情報に無い",
        "not_loaded": "データ未取込",
        "budget": "今回調べていない",
        "not_on_web": "Web に載っていない",
    }
    lines = ["この分析で**確かめられなかったこと**です。"
             "「公開情報に無い」ものは次に走らせても出てきません。"
             "それ以外は、取り込みや設定で埋まります。", ""]
    lines += _table(["区分", "項目", "なぜ確かめられないか", "どう確かめるか"],
                    [[reason_label.get(i.get("reason"), i.get("reason")),
                      i.get("label"), i.get("why") or "—", i.get("how") or "—"]
                     for i in items])
    return lines


def _verdict(report: Mapping[str, Any]) -> list[str]:
    v = report.get("verdict") or {}
    if not v:
        return ["—", ""]
    lines = [str(v.get("statement") or "—"), ""]
    lines += ["### これから開業する人にとって", "",
              str(v.get("for_opening") or "—"), ""]
    lines += ["### 既存医院を買う人にとって", "",
              str(v.get("for_acquisition") or "—"), ""]
    if v.get("counterpoint"):
        lines += ["### この評価が外れるとしたら", "",
                  str(v["counterpoint"]), ""]
    return lines


def _m(value: Any) -> str:
    if value is None:
        return "—"
    return f"{int(float(value)):,}m"
