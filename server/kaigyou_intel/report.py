"""レポートを人が読む形にして保存する（要件 §18・§29）。

Markdown はここで作ります。LLM に書かせません。免責・出典・データ時点は
**必ず載る**ものなので、書き忘れの起きうる場所に置かないためです。

出典の一覧も同じ理由でここで組み立てます。STEP2 が analysis_sources に
残した行から作るので、レポート本文に URL を書き写す手間も、写し間違いも
ありません。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

import psycopg
from psycopg.types.json import Json

from kaigyou_intel.schemas import Step4Output, TraceProblem

#: 出典の並び順。要件 §9 の優先順位そのまま。
_SOURCE_ORDER = ("government", "statistics", "prefecture", "municipality",
                 "public_body", "transit", "academic", "company", "news", "other")
_SOURCE_LABEL = {
    "government": "国・省庁", "statistics": "政府統計", "prefecture": "都道府県",
    "municipality": "市区町村", "public_body": "公的機関", "transit": "交通事業者",
    "academic": "大学・研究機関", "company": "企業", "news": "ニュース",
    "other": "その他",
}


def to_markdown(output: Mapping[str, Any], dataset: Mapping[str, Any],
                sources: Sequence[Mapping[str, Any]] = (),
                judgement: Mapping[str, Any] | None = None) -> str:
    """レポート 1 本ぶんの Markdown。

    ``judgement`` は STEP3 の出力（``decision`` と ``actions``）です。散文は
    最終段が書きますが、開業方針は**欄のまま**載せます。散文に溶かすと
    「誰と競争しないか」が抜けても気づけません。表なら空欄が見えます。
    """
    location = dataset.get("location") or {}
    query = dataset.get("query") or {}
    place = " ".join(x for x in (location.get("prefecture_name"),
                                 location.get("municipality_name")) if x)
    name = location.get("name") or place or \
        f"{location.get('lat')},{location.get('lng')}"
    # STEP5 が付けた表題を優先します。顧客に渡す文書なので、こちらが決めた
    # 定型より、その商圏について書かれた見出しのほうが読み手に向いています。
    heading = str(output.get("title") or "").strip() or f"商圏分析レポート：{name}"
    lines: list[str] = [f"# {heading}", ""]

    where = f"{location.get('lat')}, {location.get('lng')}"
    if query.get("radius_m"):
        lines += [f"地点 {where} / 半径 {query['radius_m']:,}m"
                  f" / プロファイル {query.get('active_profile', '-')}", ""]

    # STEP5（顧客提出用）があればそちらを本文にします。STEP4 の形は根拠を
    # 辿るためのもので、人が読む文書ではありません。
    lines += (_client_body(output, judgement) if _is_client_report(output)
              else _working_body(output))

    lines += _legend_block(output)
    lines += _figures_block(dataset)
    lines += _sources_block(sources)
    lines += _provenance_block(dataset)
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------- 付録：基礎数値
#
# 本文の数字は LLM が選んで書きます。ここはデータセットからそのまま並べます。
# 分けているのは、レポートを読んだ人が「その数字はどこから来たのか」を
# 同じ文書の中で確かめられるようにするためです。本文に全部書かせると、
# 選ばれなかった数字がどこにも残りません。
#
# LLM を通さないので、桁の取り違えも書き落としも起きません。トークンも
# 使いません。

def _daytime_block(daytime: Mapping[str, Any] | None) -> list[str]:
    """昼間人口（従業地・通学地）。取れていないときは、取れていないと書く。

    実測：早稲田駅前のレポートは、経済センサスの従業者数 52,688 人を昼間の
    人の代理として使い、**大学生に一言も触れませんでした**。学生は従業者では
    ないので、経済センサスには 1 人も現れません。黙って省くと、読み手には
    「そこに学生はいない」と読めます。
    """
    if not daytime:
        return []
    if not daytime.get("available"):
        return ["#### 昼間人口（従業地・通学地）", "",
                f"**取得できていません。** {daytime.get('note', '')}", ""]
    rows = [
        ["昼間人口（従業地・通学地による人口）", _num(daytime.get("population"))],
        ["　うち この場所で働いている人", _num(daytime.get("workers_here"))],
        ["　うち この場所に通学している人", _num(daytime.get("students_here"))],
        ["　うち それ以外＊", _num(daytime.get("other_here"))],
        ["（参考）同じ調査の常住人口", _num(daytime.get("night_population"))],
    ]
    lines = ["#### 昼間人口（従業地・通学地）", ""]
    lines += _table(["区分", "人数"], rows)
    lines += ["＊ 引き算で出した残りです（在宅の常住者など）。", ""]
    if daytime.get("meshes_without_students"):
        lines += [f"通学者が取得できなかったメッシュが "
                  f"{daytime['meshes_without_students']} 件あります。"
                  "その分は合計に入っていません（0 として数えてはいません）。", ""]
    if daytime.get("definition"):
        lines += [str(daytime["definition"]), ""]
    return lines


def _industry_block(mix: Mapping[str, Any] | None) -> list[str]:
    """産業別を、足し合わせられる形で出す。

    実測：「第3次産業 49,203」「教育・学習支援 13,245」「第2次産業 3,474」…
    と同じ字下げで並べていました。1行目に3行目以降が含まれているので、
    読み手が合計を取ると二重計上になります。

    親子を字下げで示し、名前の付いた内訳を引いた残りも行として出します。
    残差を出さないと「4つで全部」と読まれます。
    """
    if not mix or not mix.get("divisions"):
        return []
    total = mix.get("total") or {}
    rows: list[list[Any]] = []
    if total.get("workers") is not None:
        rows.append(["**全産業**", _num(total.get("workers")),
                     _num(total.get("establishments"))])
    for item in mix["divisions"]:
        # 全角スペースで段を作ります。Markdown の表はセル先頭の半角空白を
        # 落とすので、半角では字下げが消えます。
        indent = "　" if item.get("parent") is None else "　　"
        label = f"{indent}{item.get('label')}"
        if item.get("derived"):
            label += "＊"
        rows.append([label, _num(item.get("workers")),
                     _num(item.get("establishments"))])
    lines = ["#### 産業別（商圏内）", ""]
    lines += _table(["産業", "従業者数", "事業所数"], rows)
    if any(item.get("derived") for item in mix["divisions"]):
        lines += ["＊ 印は測った値ではなく、親から名前の付いた内訳を引いた残りです。", ""]
    if mix.get("note"):
        lines += [str(mix["note"]), ""]
    return lines


def _table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    if not rows:
        return []
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join("---" for _ in header) + "|"]
    for row in rows:
        out.append("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
    return out + [""]


def _num(value: Any, unit: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.1f}{unit}"
    return f"{int(value):,}{unit}"


def _pct(value: Any) -> str:
    return "—" if value is None else f"{float(value) * 100:+.2f}%"


def _figures_block(dataset: Mapping[str, Any]) -> list[str]:
    demand = dataset.get("demand") or {}
    competition = dataset.get("competition") or {}
    radii = ["500", "1000", "2000"]
    head = ["指標", "500m", "1km", "2km"]
    lines = ["## 付録：商圏の基礎数値", "",
             "本文が引用しなかったものも含め、分析に使った数値です。"
             "文章は生成されたものですが、この表はデータからそのまま出しています。", ""]

    residents = (demand.get("residents") or {}).get("by_radius") or {}
    if residents:
        lines += ["### 常住人口", ""]
        lines += _table(head, [
            [label] + [_num((residents.get(r) or {}).get(key)) for r in radii]
            for label, key in (("総人口", "population"), ("0〜14歳", "age_0_14"),
                               ("15〜64歳", "age_15_64"), ("65歳以上", "age_65_plus"),
                               ("世帯数", "households"))
        ] + [["人口増減率（2015→2020）"]
             + [_pct((residents.get(r) or {}).get("population_growth")) for r in radii]])

    daytime = (demand.get("daytime") or {}).get("by_radius") or {}
    if daytime:
        lines += ["### 昼間（従業者・事業所）", ""]
        lines += _table(head, [
            [label] + [_num((daytime.get(r) or {}).get(key)) for r in radii]
            for label, key in (("従業者数", "workers"), ("事業所数", "establishments"))
        ])
        lines += _daytime_block((demand.get("daytime") or {}).get("census_daytime"))
        lines += _industry_block((demand.get("daytime") or {}).get("industry_mix"))

    by_radius = competition.get("by_radius") or {}
    if by_radius:
        lines += ["### 競合（歯科医院）", ""]
        lines += _table(head, [
            [label] + [_num((by_radius.get(r) or {}).get(key)) for r in radii]
            for label, key in (("医院数", "dental_clinics"),
                               ("1院あたり常住人口", "population_per_clinic"),
                               ("1院あたり従業者数", "workers_per_clinic"))
        ])

    specialty = competition.get("by_specialty") or {}
    if specialty.get("detail"):
        lines += ["#### 標榜診療科目（商圏内）", ""]
        lines += _table(["科目", "医院数", "標榜率", "1院あたり人口"], [
            [item.get("label"), _num(item.get("count")),
             "—" if item.get("share_of_clinics_with_data") is None
             else f"{item['share_of_clinics_with_data'] * 100:.1f}%",
             _num(item.get("population_per_clinic"))]
            for item in specialty["detail"]])

    hours = competition.get("hours") or {}
    if hours.get("counts"):
        lines += ["#### 診療時間（商圏内）", ""]
        lines += _table(["区分", "医院数", "割合"], [
            [item.get("label"), _num(item.get("count")),
             "—" if item.get("share") is None else f"{item['share'] * 100:.1f}%"]
            for item in hours["counts"]])
        if hours.get("weekly_hours_median") is not None:
            lines += [f"週間診療時間の中央値: {hours['weekly_hours_median']} 時間", ""]

    vintage = competition.get("vintage") or {}
    if vintage.get("available"):
        lines += ["#### 開設年（商圏内）", ""]
        lines += _table(["区分", "値"], [
            ["開設年の中央値", vintage.get("median_opening_year")],
            ["いちばん古い開設年", vintage.get("oldest_opening_year")],
            ["いちばん新しい開設年", vintage.get("newest_opening_year")],
            [f"開設から{vintage.get('opened_over_years_ago')}年以上",
             _num(vintage.get("opened_long_ago"), "院")],
            [f"直近{vintage.get('opened_within_years')}年の開設",
             _num(vintage.get("opened_recently"), "院")],
            ["開設年が分かる医院",
             f"{vintage.get('with_opening_date')} / "
             f"{vintage.get('total_clinics')} 院"],
        ])
        lines += [str(vintage.get("note", "")), ""]
    elif vintage.get("reason") == "no_opening_dates":
        # 「古い医院は0件」ではありません。1件も分からなかったのです。
        # 黙って省くと、調べたうえで該当なしと読まれます。
        lines += ["#### 開設年（商圏内）", "", str(vintage.get("note", "")), ""]

    clinics = (competition.get("clinics_in_radius") or {}).get("items") or []
    if clinics:
        lines += ["#### 商圏内の歯科医院", ""]
        lines += _table(["距離", "名称", "標榜科目", "週間診療時間"], [
            [_num(c.get("distance_m"), "m"), c.get("name"),
             "・".join(s.get("label", "") for s in (c.get("specialties") or [])) or "—",
             _num(((c.get("hours") or {}) or {}).get("weekly_hours"), "h")]
            for c in clinics])

    lines += _outlook_figures(dataset)
    lines += _access_figures(dataset)
    lines += _cost_figures(dataset)
    return lines




#: レポートの表に載せる年の間隔と、どこまで先を見るか。
#:
#: 公表データは 2070 年まで 5 年刻みで 11 点あります。全部並べると 11 列に
#: なり、読み物として成立しません（読みづらいという指摘を実際に受けました）。
#: それに 40代で開業する人にとって 2070 年は引退のはるか先で、意思決定に
#: 効きません。**10 年刻みで 30 年先まで**に絞ります。
#:
#: 絞るのは表示だけです。データセットには全年次が入っているので、LLM も
#: JSON 出力も全部を見られます。
_OUTLOOK_STEP_YEARS = 10
_OUTLOOK_HORIZON_YEARS = 30


def _readable_years(years: list[Mapping[str, Any]],
                    base_year: Any) -> list[Mapping[str, Any]]:
    if len(years) <= 4:
        return list(years)
    base = int(base_year) if base_year else int(years[0]["year"])
    limit = base + _OUTLOOK_HORIZON_YEARS
    picked = [y for y in years
              if int(y["year"]) <= limit
              and (int(y["year"]) - base) % _OUTLOOK_STEP_YEARS == 0]
    # 基準年しか残らないなら、絞らずに元のまま出します。刻みが合わない
    # 版が来たときに、表が消えるより並びすぎるほうがましです。
    return picked if len(picked) >= 2 else list(years)


def _outlook_figures(dataset: Mapping[str, Any]) -> list[str]:
    """将来推計人口。取れていないときは、取れていないと書く。

    無言で省くと、読み手は「この商圏は将来が明るいから触れていない」とも
    「調べていない」とも読めます。売り物の文書で、その曖昧さは残せません。
    """
    outlook = ((dataset.get("demand") or {}).get("outlook")) or {}
    if not outlook:
        return []
    if not outlook.get("available"):
        return ["### 将来推計人口", "",
                f"**取得できていません。** {outlook.get('note', '')}", ""]

    years = _readable_years(outlook.get("years") or [], outlook.get("base_year"))
    if not years:
        return []

    # 基準年の実績（国勢調査）を先頭の列に置きます。将来推計の公表値は基準年に
    # ついて総人口しか持たないので、そのままだと基準年の列が「—」だらけに
    # なり、読み手には「分からない」に見えます。**別の数え方なので列を分け、
    # 混ぜません。**
    actual = outlook.get("actual") or {}
    columns: list[tuple[str, Mapping[str, Any]]] = []
    if actual.get("population") is not None:
        columns.append((f"{actual.get('year')}（実績）", actual))
    columns += [(str(y["year"]) + ("（推計基準年）" if y["year"] == outlook.get("base_year")
                                   else ""), y) for y in years]

    def pct(entry: Mapping[str, Any], key: str) -> str:
        value = entry.get(key)
        return "—" if value is None else f"{value * 100:.1f}%"

    head = ["年"] + [name for name, _ in columns]
    rows = [
        ["総人口"] + [_num(e.get("population")) for _n, e in columns],
        ["0〜14歳"] + [_num(e.get("age_0_14")) for _n, e in columns],
        ["15〜64歳"] + [_num(e.get("age_15_64")) for _n, e in columns],
        ["65歳以上"] + [_num(e.get("age_65_plus")) for _n, e in columns],
        ["65歳以上の割合"] + [pct(e, "elderly_share") for _n, e in columns],
        ["75歳以上"] + [_num(e.get("age_75_plus")) for _n, e in columns],
        ["75歳以上の割合"] + [pct(e, "late_elderly_share") for _n, e in columns],
        [f"{outlook.get('base_year')}年=100"]
        + ["—" if e.get("index_vs_base") is None
           else f"{e['index_vs_base'] * 100:.0f}" for _n, e in columns],
    ]
    lines = ["### 人口の実績と将来推計", "",
             f"出典: 実績は{actual.get('source') or '国勢調査'}、"
             f"推計は{outlook.get('estimate_label') or '将来推計人口'}"
             f"（基準年 {outlook.get('base_year')}）。"
             "推計であって予測ではなく、出生・死亡・移動の仮定の上に成り立ちます。"
             + (f"（公表は{years[0]['year']}〜{outlook['years'][-1]['year']}年の5年刻み。"
                "表は10年刻みで抜粋）" if len(outlook.get("years") or []) > len(years) else ""),
             ""]
    lines += _table(head, rows)
    if actual.get("note"):
        lines += [str(actual["note"]), ""]
    return lines


def _access_figures(dataset: Mapping[str, Any]) -> list[str]:
    access = dataset.get("access") or {}
    stations = (access.get("stations_in_radius") or {}).get("items") or []
    if not stations:
        return []
    return ["### 交通アクセス", ""] + _table(
        ["距離", "駅", "事業者", "乗降客数/日"],
        [[_num(s.get("distance_m"), "m"), s.get("name"), s.get("operator"),
          _num(s.get("daily_passengers"), "人")] for s in stations[:10]])


def _cost_figures(dataset: Mapping[str, Any]) -> list[str]:
    cost = dataset.get("cost") or {}
    divisions = cost.get("by_use_division") or []
    if not divisions:
        return []
    lines = ["### 地価（公示地価）", ""]
    lines += _table(["用途", "地点数", "中央値", "最小", "最大", "前年比", "調査年"], [
        [d.get("use_category"), _num(d.get("points")),
         _num(d.get("median_yen_per_sqm"), "円/m²"),
         _num(d.get("min_yen_per_sqm"), "円/m²"),
         _num(d.get("max_yen_per_sqm"), "円/m²"),
         "—" if d.get("mean_change_pct") is None else f"{d['mean_change_pct']:+.1f}%",
         d.get("survey_year")] for d in divisions])
    if cost.get("note"):
        lines += [str(cost["note"]), ""]

    rent = cost.get("rent_estimate")
    if rent:
        lines += ["#### 賃料の目安（地価からの換算）", ""]
        lines += _table(["", "月額（円/坪）", "月額（円/m²）"], [
            [f"想定利回り {rent['assumed_yield_low']:.0%}",
             _num(rent["monthly_yen_per_tsubo_low"]),
             _num(rent["monthly_yen_per_sqm_low"])],
            [f"想定利回り {rent['assumed_yield_high']:.0%}",
             _num(rent["monthly_yen_per_tsubo_high"]),
             _num(rent["monthly_yen_per_sqm_high"])],
        ])
        lines += [f"`{rent['formula']}`", "", str(rent["note"]), ""]
    return lines


def _is_client_report(output: Mapping[str, Any]) -> bool:
    return "verdict" in output and "support_needed" in output


def _refs(item: Mapping[str, Any]) -> str:
    """根拠の id。散文の中では小さく添えます。

    §25 の追跡はここを通ります。読み物としては邪魔ですが、消すと
    「その話はどこから来たのか」を辿れなくなります。行末に置いて、
    読み飛ばせる形にしてあります。"""
    ids = item.get("evidence") or item.get("basis") or []
    return f"  〔{', '.join(ids)}〕" if ids else ""


def _client_body(output: Mapping[str, Any],
                 judgement: Mapping[str, Any] | None = None) -> list[str]:
    """顧客に渡す本文。散文で、結論から。"""
    verdict = output.get("verdict") or {}
    lines = ["## この立地について", "", output.get("summary", ""), ""]

    if verdict:
        lines += [f"### 評価：{verdict.get('label', '')}", "",
                  str(verdict.get("statement", "")) + _refs(verdict), ""]
        if verdict.get("counterpoint"):
            lines += ["**この判断が外れるとしたら**　"
                      + str(verdict["counterpoint"]), ""]

    if output.get("why_here"):
        lines += ["## なぜこの立地か", "", output["why_here"], ""]

    # 開業方針。要件 §17 の答えを、欄のまま。以前はこれを作った段の出力ごと
    # 捨てていて（散文に書き直したあと参照されませんでした）、「誰とは競争
    # しないか」がレポートのどこにも残りませんでした。
    if judgement and judgement.get("decision"):
        lines += _decision_block(judgement["decision"])

    for section in output.get("sections") or []:
        lines += [f"## {section.get('heading')}", ""]
        if section.get("takeaway"):
            lines += [f"> {section['takeaway']}", ""]
        lines += [str(section.get("body", "")) + _refs(section), ""]

    support = output.get("support_needed") or []
    if support:
        lines += ["## この立地で開業するために必要なこと", ""]
        by_category: dict[str, list[Mapping[str, Any]]] = {}
        for item in support:
            by_category.setdefault(str(item.get("category") or "その他"), []).append(item)
        for category, items in by_category.items():
            lines += [f"### {category}", ""]
            for item in items:
                lines += [f"**{item.get('item')}**", "",
                          str(item.get("why", "")) + _refs(item), ""]

    # 古い形（questions_for_the_client）で保存されたレポートも読めるように。
    # レポートは DB に何か月も残り、その間にスキーマは変わります。
    research = output.get("further_research") or []
    if not research:
        research = [{"topic": q} for q in
                    (output.get("questions_for_the_client") or [])]
    if research:
        lines += ["## さらに深掘りすべき調査", "",
                  "本レポートは公的統計から読み取れる範囲です。"
                  "ここから先は現地・一次情報の領域で、"
                  "調査の方向としては次が考えられます。", ""]
        for item in research:
            lines += [f"**{item.get('topic')}**", ""]
            lines += [f"{item.get('why', '')}", ""]
            if item.get("how"):
                lines += [f"調べ方: {item['how']}", ""]

    actions = (judgement or {}).get("actions") or []
    if actions:
        lines += ["## 次に取るべき行動", ""]
        for action in actions:
            lines.append(f"- {action.get('statement')}" + _refs(action))
        lines.append("")

    if output.get("judgement_note"):
        lines += ["## このレポートにおける評価の位置づけ", "",
                  str(output["judgement_note"]), ""]
    return lines


def _working_body(output: Mapping[str, Any]) -> list[str]:
    """STEP4 の形（タグ付き）。STEP5 が無いときの控えです。"""
    lines = ["## 結論", "", output.get("executive_summary", ""), ""]
    lines += _decision_block(output.get("decision") or {})

    for section in sorted(output.get("sections") or [],
                          key=lambda s: s.get("number", 0)):
        lines += [f"## {section.get('number')}. {section.get('title')}", ""]
        for block in section.get("blocks") or []:
            lines.append(f"**[{block.get('tag')}]** {block.get('text')}"
                         + _refs(block))
            lines.append("")

    if output.get("actions"):
        lines += ["## 次に取るべき行動", ""]
        for action in output["actions"]:
            lines.append(f"- {action.get('statement')}" + _refs(action))
        lines.append("")
    return lines


def _decision_block(decision: Mapping[str, Any]) -> list[str]:
    """要件 §17 の答え。レポートの先頭に、欄のまま置きます。

    散文に溶かすと「誰と競争しないか」が抜けても気づけません。表なら空欄が
    見えます。
    """
    fields = (("主要患者", "primary_patients"),
              ("主要に置かない層", "secondary_patients"),
              ("競争しない領域", "avoid_competing_on"),
              ("患者獲得エリア", "acquisition_area"),
              ("来院理由", "reason_to_visit"),
              ("医院モデル", "clinic_model"))
    lines = ["### 開業方針", "", "| | |", "|---|---|"]
    for label, key in fields:
        item = decision.get(key) or {}
        refs = item.get("evidence") or []
        tail = f"  〔{', '.join(refs)}〕" if refs else ""
        text = str(item.get("statement", "")).replace("|", "\\|")
        lines.append(f"| **{label}** | {text}{tail} |")
    lines.append("")
    for label, key in (("開業上のメリット", "advantages"), ("リスク", "risks")):
        entries = decision.get(key) or []
        if not entries:
            continue
        lines += [f"### {label}", ""]
        for item in entries:
            refs = item.get("evidence") or []
            tail = f"  〔{', '.join(refs)}〕" if refs else ""
            lines.append(f"- {item.get('statement')}{tail}")
        lines.append("")
    if decision.get("confidence"):
        lines += [f"判断の確度: {decision['confidence']}", ""]
    return lines


#: 出典の表題の上限。e-Stat の表題は「国勢調査 …（27区分）による人口，就業者数
#: 及び通学者数(流出人口，流入人口，昼夜間人口比率－特掲) 全国，都道府県，
#: 市区町村 | 統計表・グラフ表示 | 政府統計の総合窓口」で 481 文字ありました。
_TITLE_LIMIT = 90


def _short_title(title: str, url: str) -> str:
    text = (title or url or "").strip()
    # 「… | サイト名 | 政府統計の総合窓口」の後半はどれも同じなので落とします。
    head = text.split(" | ", 1)[0].strip() or text
    if len(head) <= _TITLE_LIMIT:
        return head
    return head[:_TITLE_LIMIT - 1].rstrip() + "…"


def _sources_block(sources: Sequence[Mapping[str, Any]]) -> list[str]:
    """レポートに載せるのは、**本文が引用した**出典だけ。

    最初の実装は取得した URL を全部並べていて、銀座のレポートで 230 件に
    なりました。同じ URL が最大 6 回、医院のホームページや情報サイトも
    混ざっていて、出典一覧として使えません。

    引用されなかったぶんは analysis_sources に残っています。調べた記録としては
    そちらが正しい置き場所で、レポートの末尾ではありません。件数だけ添えます。
    """
    cited = [s for s in sources if s.get("pattern_id")]
    others = len(sources) - len(cited)
    if not cited:
        # 引用が 1 件も無いのは、外部情報を使えなかったということ。黙らない。
        return (["## 出典（外部情報）", "",
                 f"本文が引用した外部資料はありません（{others} 件を参照）。", ""]
                if sources else [])

    seen: set[str] = set()
    unique: list[Mapping[str, Any]] = []
    for source in cited:
        key = (source.get("url") or "").rstrip("/").lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(source)
    others += len(cited) - len(unique)

    lines = ["## 出典（外部情報）", ""]
    ordered = sorted(unique, key=lambda s: (
        _SOURCE_ORDER.index(s.get("source_type") or "other")
        if (s.get("source_type") or "other") in _SOURCE_ORDER else len(_SOURCE_ORDER),
        s.get("url") or ""))
    for source in ordered:
        label = _SOURCE_LABEL.get(source.get("source_type") or "other", "その他")
        retrieved = source.get("retrieved_at")
        when = f"（取得 {retrieved:%Y-%m-%d}）" if hasattr(retrieved, "year") else ""
        lines.append(f"- [{label}] "
                     f"{_short_title(source.get('title'), source.get('url'))}{when}  "
                     f"\n  {source.get('url')}")
    if others > 0:
        lines += ["", f"このほか {others} 件の資料を参照しましたが、"
                      "本文の根拠としては引用していません。"]
    lines.append("")
    return lines


#: 本文に添えた id が何なのか。読み手が説明なしに分かる記号ではありません。
#: 実測：「〔F011, F012, F013, F017, P001〕といった数字は何の数字だろうか？」と
#: 訊かれました。記号を出すなら、同じ文書の中に読み方を置きます。
_EVIDENCE_LEGEND = (
    ("F", "基礎データから読み取った事実（国勢調査・経済センサス・医療機能情報提供制度など）"),
    ("P", "複数の事実を組み合わせて見えた、この商圏の特徴"),
    ("C", "外部資料で確認した事実。出典は「出典（外部情報）」に載っています"),
    ("H", "その背景についての仮説と、外部情報による判定"),
    ("S", "推定した患者層"),
    ("M", "需要が生まれる筋道"),
    ("I", "複数の筋道を横断して見えたこと"),
)


def _legend_block(output: Mapping[str, Any]) -> list[str]:
    """本文の〔F001〕の読み方。

    根拠を辿れることがこのレポートの売りなので、記号は消しません。ただし
    説明の無い記号は、読み手にとっては模様と同じです。
    """
    text = json.dumps(output, ensure_ascii=False)
    used = [(prefix, label) for prefix, label in _EVIDENCE_LEGEND
            if re.search(rf'"{prefix}\d{{3}}"', text)]
    if not used:
        return []
    return ["## 本文中の〔F001〕などについて", "",
            "文末の〔　〕は、その記述の根拠にした項目の番号です。"
            "どの記述がどのデータに基づくかを後から辿れるように付けています。", ""] + [
        f"- **{prefix}001** … {label}" for prefix, label in used] + [""]


def _provenance_block(dataset: Mapping[str, Any]) -> list[str]:
    """データ時点・注意事項・免責。省略できません（プロジェクトの前提）。

    出典と年次は measures の各指標に付いています。レポートに書き写させると
    写し間違いが起きるので、こちらで集めて並べます。

    caveats も落としません。「標榜診療科目は届出値であって診療内容ではない」
    のような但し書きは、レポートの読み方そのものを変えます。
    """
    lines = ["## データの出典と時点", ""]
    seen: list[tuple[str, str]] = []
    for measure in (dataset.get("measures") or {}).get("items") or []:
        source = measure.get("source")
        if not source:
            continue
        pair = (str(source), str(measure.get("data_year") or "-"))
        if pair not in seen:
            seen.append(pair)
    for source, year in seen:
        lines.append(f"- {source}（{year}）")

    quality = dataset.get("data_quality") or {}
    for gap in quality.get("unavailable_datasets") or []:
        label = gap.get("label") or gap.get("key") if isinstance(gap, Mapping) else gap
        lines.append(f"- 未取得: {label}")
    lines.append("")

    notes = list(quality.get("notes") or []) + list(quality.get("benchmark_notes") or [])
    if notes:
        lines += ["## データについての注記", ""]
        lines += [f"- {note}" for note in notes] + [""]

    if quality.get("caveats"):
        lines += ["## 読むときの注意", ""]
        lines += [f"- {caveat}" for caveat in quality["caveats"]] + [""]

    lines += ["## 免責", ""]
    for key in ("disclaimer", "score_disclaimer"):
        text = dataset.get(key)
        if text:
            lines += [str(text), ""]
    return lines


def save(conn: psycopg.Connection, job_id: str, output: Mapping[str, Any],
         dataset: Mapping[str, Any], problems: Sequence[TraceProblem] = ()) -> str:
    """レポートを保存する。§25 の検算結果も一緒に。

    問題が残っていても保存するのは、「いつから壊れていたか」を後から追うため
    です。ただし ``trace_ok`` は false になり、そのことは読む人にも見えます。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT url, title, source_type, retrieved_at, pattern_id
            FROM analysis_sources
            WHERE job_id = %s ORDER BY created_at
            """, (job_id,))
        sources = [dict(r) for r in cur.fetchall()]
        # 地点名は Job のほうにあります（「銀座4丁目」のように人が付けた名前）。
        # 座標より、その名前で呼ばれたほうが読む人には分かります。
        cur.execute("SELECT location_name FROM analysis_jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
        # 開業方針は STEP3 が欄のまま出しています。最終段は散文だけを書くので、
        # 表にする材料はここで取りに行きます。
        cur.execute("SELECT output_json FROM analysis_steps WHERE job_id = %s "
                    "AND step_number = 3 AND status = 'completed'", (job_id,))
        step3 = cur.fetchone()
        judgement = (step3 or {}).get("output_json") or None

    named = dict(dataset)
    if row and row["location_name"]:
        named["location"] = {**(dataset.get("location") or {}),
                             "name": row["location_name"]}

    with conn.cursor() as cur:
        markdown = to_markdown(output, named, sources, judgement)
        cur.execute(
            """
            INSERT INTO analysis_reports (job_id, report_json, report_markdown,
                                          trace_ok, trace_problems)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (job_id) DO UPDATE SET
                report_json = EXCLUDED.report_json,
                report_markdown = EXCLUDED.report_markdown,
                trace_ok = EXCLUDED.trace_ok,
                trace_problems = EXCLUDED.trace_problems,
                created_at = now()
            RETURNING id
            """,
            (job_id, Json(dict(output)), markdown, not problems,
             Json([p.model_dump() for p in problems]) if problems else None))
        report_id = str(cur.fetchone()["id"])
    conn.commit()
    return report_id


#: レポートを書き出す既定の場所。設定で変えられます（config/analysis.yaml）。
DEFAULT_OUTPUT_DIR = "reports"


def write_file(conn: psycopg.Connection, job_id: str,
               directory: str | None = None) -> Path | None:
    """レポートをファイルとして書き出す。

    STEP4 が終わったら黙って保存します。追加のコマンドを打たないと現物が
    手に入らないのでは、「レポートを作る道具」として不完全です。DB の中に
    あることと、手元にファイルがあることは違います。

    同じジョブを何度やり直しても同じ名前に上書きします。日付ごとに増やすと、
    どれが最新か分からなくなります。

    **書けなければ黙って諦めます。** ホスティングされた関数のファイル
    システムは読み取り専用です（実測：Vercel で
    ``OSError: [Errno 30] Read-only file system: 'reports'``）。
    レポート本体は DB に入っていて、画面からも ``/report.md`` からも
    取り出せます。ここで書き出すのは手元で回すときの便宜であって、
    成果物そのものではありません。おまけの失敗で、$1.29 かけて仕上がった
    レポートを失うのは筋が通りません。
    """
    from kaigyou_core import config as cfg

    markdown = markdown_for(conn, job_id)
    if markdown is None:
        return None
    settings = (cfg.analysis_config().get("report") or {})
    target = Path(directory or settings.get("output_dir") or DEFAULT_OUTPUT_DIR)

    with conn.cursor() as cur:
        cur.execute("SELECT location_name, latitude, longitude, radius_m, created_at "
                    "FROM analysis_jobs WHERE id = %s", (job_id,))
        job = cur.fetchone()
    try:
        target.mkdir(parents=True, exist_ok=True)
        path = target / _file_name(job_id, job)
        path.write_text(markdown, encoding="utf-8")
    except OSError:
        return None
    return path


def _file_name(job_id: str, job: Mapping[str, Any] | None) -> str:
    """人が見て分かる名前に。地点名が無ければ座標で。

    ファイル名に使えない文字は落とします（Windows で `/` や `:` が入ると
    保存できません）。
    """
    if not job:
        return f"{job_id[:8]}.md"
    name = (job.get("location_name") or "").strip()
    if not name:
        name = f"{job['latitude']:.5f}_{job['longitude']:.5f}"
    for bad in '\\/:*?"<>|\n\t':
        name = name.replace(bad, "_")
    when = job.get("created_at")
    stamp = f"{when:%Y%m%d}" if hasattr(when, "year") else ""
    return "_".join(x for x in ("商圏分析", name[:40], stamp, job_id[:8]) if x) + ".md"


def markdown_for(conn: psycopg.Connection, job_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT report_markdown FROM analysis_reports WHERE job_id = %s",
                    (job_id,))
        row = cur.fetchone()
    return row["report_markdown"] if row else None
