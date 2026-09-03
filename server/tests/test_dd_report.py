"""プレDD レポート（10 章）。

**静的データは自社 DB、動的データと LLM だけ都度 API。**

この製品でいちばん高くつくのは API 費用ではなく、**同じ地点なのに実行するたび
違う数字が出ること**です。DD の文書でそれが起きると、文書ごと信用されなく
なります。だから数字は DB のデータから Python が確定させ、LLM は「その事実が
何を意味するか」の文だけを書きます。

ここで守るのは 3 つです。

    1 段目は API を叩かない
    本文に、渡した事実に無い数字が出ない
    章立てが設定どおりに、10 章そろって出る
"""
from __future__ import annotations

import pytest

from kaigyou_core import config as cfg
from kaigyou_core import dd
from kaigyou_intel import dd_report
from kaigyou_intel.schemas import verify_dd_report


def _dataset() -> dict:
    """DB から取れる静的データの、最小のかたち。"""
    return {
        "location": {"lat": 35.1, "lng": 138.9, "prefecture_name": "静岡県",
                     "municipality_name": "裾野市"},
        "query": {"radius_m": 1000, "active_profile": "default"},
        "catchment": {"kind": "circle", "area_km2": 3.14,
                      "description": "半径1,000mの円"},
        "demand": {
            "residents": {"by_radius": {"1000": {"population": 12000,
                                                 "households": 5100}}},
            "distribution": {"meshes": 8, "largest_mesh_share": 0.52,
                             "population_largest_mesh": 6240,
                             "meshes_with_no_residents": 0},
            "outlook": {"available": False, "note": "将来推計は未取得です。"},
        },
        "competition": {
            "by_radius": {"1000": {"dental_clinics": 9,
                                   "population_per_clinic": 1333}},
            "proximity": {"nth_nearest_distance_m": {"1": 52, "5": 610}},
            "clinics_in_radius": {"count": 9},
            "vintage": {"available": True, "total_clinics": 9,
                        "with_opening_date": 7, "median_opening_year": 1996,
                        "opened_recently": 3, "opened_within_years": 5,
                        "opened_long_ago": 2, "opened_over_years_ago": 30,
                        "coverage": 0.78, "note": "開設年は院長の年齢ではありません。"},
        },
        "access": {"nearest_station": {
            "name": "裾野駅", "distance_m": 210, "daily_passengers": 4100,
            "band": {"label": "駅前", "walk_minutes": 3}}},
        "measures": {"items": [
            {"key": "population_growth", "value": -6.2, "label": "人口増減率"}]},
        "positioning": {"axes": [
            {"key": "supply", "label": "歯科の供給", "score": 88,
             "assessment": "非常に高い", "means": "人口あたりの医院の多さ"}]},
        "data_quality": {"unavailable_datasets": []},
        "disclaimer": "この分析は参考情報です。",
    }


# --------------------------------------------------- 1 段目は API を叩かない
def test_the_fact_step_never_calls_the_api():
    """**静的データは DB。都度 API を叩く理由がありません。**

    人口も医院の位置も開設年も、既に自社 DB にあります。数え直すだけの段で
    トークンを使っていたら、それは設計が間違っています。
    """
    from kaigyou_intel.steps import dd_facts

    payload = dd_facts.build_input(_dataset(), None)
    pack, usage, sources = dd_facts.run(payload, "dental_clinic")

    assert usage.input_tokens == 0 and usage.output_tokens == 0
    assert usage.web_searches == 0
    assert sources == []
    assert pack["chapters"], "章立てが空です"


def test_the_same_place_gives_the_same_numbers_every_time():
    """**同じ地点なら、何度実行しても同じ数字。**

    DD の文書で「先週と今週で結論が違う」が起きると、文書ごと信用されなく
    なります。事実の層に LLM を入れないのはそのためです。
    """
    first = dd.fact_pack(_dataset(), None, "dental_clinic")
    second = dd.fact_pack(_dataset(), None, "dental_clinic")
    assert first == second


# ------------------------------------------------------------ リスクの判定
def test_only_the_risks_that_actually_fired_are_listed():
    """該当したものだけ。**並べると、調べた量が多く見えるだけです。**"""
    found = dd.fact_pack(_dataset(), None, "dental_clinic")["risks"]
    keys = {r["key"] for r in found}
    assert "population_shrinking" in keys, "人口 -6.2% は該当するはず"
    assert "supply_dense" in keys, "供給 88 は該当するはず"
    assert "new_entrants" in keys, "直近5年に3件は該当するはず"
    assert "single_mesh_dependency" in keys, "最大メッシュ 52% は該当するはず"
    assert "station_detached" not in keys, "駅前なのに該当している"

    # 何の値で引っかかったかを残すこと。閾値を動かすときに要ります。
    growth = next(r for r in found if r["key"] == "population_shrinking")
    assert growth["observed"] == "-6.2%"
    # 重い順。読む人は上から見ます。
    severities = [r["severity"] for r in found]
    assert severities == sorted(severities, key=["high", "medium", "low"].index)


def test_a_risk_rule_pointing_at_a_missing_check_fails_loudly():
    """**黙って無視しません。**

    実装のない判定を指したリスクは、永久に該当しません。引っかからないのか
    判定していないのかが区別できないので、その場で落とします。
    """
    with pytest.raises(dd.UnknownRiskCheck) as caught:
        dd.risks(_dataset(), {"risks": [{"key": "x", "check": "typo_here"}]})
    assert "typo_here" in str(caught.value)


# ------------------------------------------------ 本文に無い数字を書かせない
def test_a_number_the_model_invented_is_caught():
    """**LLM に数字を作らせません。**

    束に無い数字が本文にあれば、それがどこから来たのか誰にも辿れません。
    """
    allowed = dd.numbers_in(dd.fact_pack(_dataset(), None, "dental_clinic"))
    invented = {
        "summary": "商圏人口は 12000 人で、歯科医院は 9 件あります。",
        "takeaways": [{"chapter": "demand", "takeaway": "患者は年間 45000 人です。"}],
        "verdict": {"statement": "", "for_opening": "", "for_acquisition": ""},
    }
    problems = verify_dd_report(invented, allowed)
    assert any("45000" in p for p in problems), "作られた数字が素通りしています"
    assert not any("12000" in p for p in problems), "実在する数字を弾いています"
    assert not any(p.count("9") and "9 件" in p for p in problems)


def test_years_and_single_digits_are_not_treated_as_invented():
    """年号と 1 桁は見逃します。拾うと、ほぼ全文が引っかかります。"""
    problems = verify_dd_report(
        {"summary": "2020年の国勢調査によれば、3 つの観点で見る必要があります。",
         "takeaways": [], "verdict": {}}, set())
    assert problems == []


# ------------------------------------------------------------ 章立てと本文
def _written() -> dict:
    return {
        "title": "裾野市 事前調査レポート",
        "summary": "供給が厚く、人口は減少局面にあります。",
        "takeaways": [
            {"chapter": "trade_area", "takeaway": "商圏は一つのメッシュに偏っています。"},
            {"chapter": "competition", "takeaway": "開設年の古い医院が目立ちます。"},
            {"chapter": "risks", "takeaway": "人口減と供給過密が重なっています。"},
        ],
        "growth_hypotheses": [
            {"position": "予防中心の運営", "why": "開設年の古い医院が多い",
             "caveat": "既存医院が既にやっている可能性がある"}],
        "verdict": {"statement": "標準的な地方市街地の商圏です。",
                    "for_opening": "差別化なしの参入は取り合いになります。",
                    "for_acquisition": "のれん代は既存患者の定着に依存します。",
                    "counterpoint": "将来推計が入れば評価は変わりえます。"},
    }


def test_the_report_has_all_ten_chapters_in_the_configured_order():
    """章立ては**設定が決めます。** コードに埋めると、変えるのに配備が要ります。"""
    pack = dd.fact_pack(_dataset(), None, "dental_clinic")
    markdown = dd_report.to_markdown(_written(), pack, [], "免責の文")

    titles = [c["title"] for c in cfg.dd_config("dental_clinic")["chapters"]]
    assert len(titles) == 10
    positions = []
    for index, title in enumerate(titles, start=1):
        heading = f"## {index}. {title}"
        assert heading in markdown, f"{heading} がありません"
        positions.append(markdown.index(heading))
    assert positions == sorted(positions), "章の順番が設定と違います"


def test_each_chapter_leads_with_its_takeaway():
    """表を見なくても要点が分かること。**読み手はまずそこを見ます。**"""
    pack = dd.fact_pack(_dataset(), None, "dental_clinic")
    markdown = dd_report.to_markdown(_written(), pack, [], "")
    section = markdown[markdown.index("## 2. 商圏分析"):markdown.index("## 3.")]
    assert "> 商圏は一つのメッシュに偏っています。" in section
    assert section.index(">") < section.index("|"), "読みどころが表より後にあります"


def test_the_verdict_is_written_for_both_readers():
    """**開業する人と、買う人では読み方が違います。**"""
    pack = dd.fact_pack(_dataset(), None, "dental_clinic")
    markdown = dd_report.to_markdown(_written(), pack, [], "")
    tail = markdown[markdown.index("## 10. 総合評価"):]
    assert "### これから開業する人にとって" in tail
    assert "### 既存医院を買う人にとって" in tail
    assert "のれん代" in tail


def test_the_competition_chapter_says_when_nothing_was_read():
    """**件数と中身は別です。** 中身を見ていないなら、そう書くこと。"""
    pack = dd.fact_pack(_dataset(), None, "dental_clinic")
    markdown = dd_report.to_markdown(_written(), pack, [], "")
    section = markdown[markdown.index("## 3. 競合分析"):markdown.index("## 4.")]
    assert "**この分析では調べていません。**" in section
    assert "周辺の競合を分析する" in section


def test_the_outlook_chapter_does_not_pass_history_off_as_a_forecast():
    """実績を将来の話として読ませないこと。**この章のいちばん危ない誤読です。**"""
    pack = dd.fact_pack(_dataset(), None, "dental_clinic")
    markdown = dd_report.to_markdown(_written(), pack, [], "")
    section = markdown[markdown.index("## 6. 将来性"):markdown.index("## 7.")]
    assert "**将来推計人口は取り込まれていません。**" in section
    assert "将来の予測ではありません" in section
    assert section.index("取り込まれていません") < section.index("-6.1%") \
        if "-6.1%" in section else True


def test_further_dd_separates_what_can_never_be_known():
    """次に走らせても出てこないものと、埋まるものを分けること。"""
    pack = dd.fact_pack(_dataset(), None, "dental_clinic")
    reasons = {i["reason"] for i in pack["further_dd"]}
    assert "public_data_gap" in reasons
    markdown = dd_report.to_markdown(_written(), pack, [], "")
    section = markdown[markdown.index("## 9. 追加DD"):markdown.index("## 10.")]
    assert "公開情報に無い" in section
    assert "売上・利益・患者数" in section
