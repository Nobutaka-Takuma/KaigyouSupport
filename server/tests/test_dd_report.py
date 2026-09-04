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
            # **人数だけ。** 割合は fact_pack が計算します——渡さないと
            # LLM が割り算し、検算に弾かれて段が止まりました（実測 3 回）。
            "residents": {"by_radius": {"1000": {
                "population": 12000, "households": 5100,
                "age_0_14": 1572, "age_15_64": 7596, "age_65_plus": 2832}}},
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


# ===================================================== 第II部（開業提言）
#
# **第I部と第II部は目的が違います。**
#
#     第I部  この商圏は「どうなっているか」   …… 事実。推論しない
#     第II部 ここで開業するなら「どうするか」 …… 推論する。仮説と明示する
#
# 分けているのは、混ぜると第I部が信用を失うからです。事実の文書に戦略提言が
# 混ざると、読み手はどこまでが確定でどこからが提案なのか分からなくなります。

def _advice() -> dict:
    return {
        "title": "開業提言",
        "reasoning": [
            {"tag": "FACT", "statement": "商圏人口は厚い。", "source": "国勢調査2020"},
            {"tag": "PATTERN", "statement": "人口集積と供給過密が同時に立つ。",
             "source": ""},
            {"tag": "WHY", "statement": "旧市街の住宅地形成による可能性。",
             "source": "https://city.example/"},
            {"tag": "INSIGHT", "statement": "量ではなく質で選ばれる商圏。", "source": ""},
            {"tag": "ACTION", "statement": "予防とリコールの仕組みを開業時から。",
             "source": ""},
        ],
        "segments": [
            {"role": "primary", "label": "子育て世帯",
             "basis": "年少人口比＋世帯あたり人員", "why": "住宅地形成",
             "caution": "開発が一巡していれば減る"},
            {"role": "avoid", "label": "自費矯正の専門層",
             "basis": "周辺に専門医院＋価格競争", "why": "既存医院が確立",
             "caution": "実際の症例数は未確認"},
        ],
        "catchments": [{"rank": "primary", "extent": "徒歩圏 500m と生活動線",
                        "basis": "メッシュ人口の偏り", "expectation": "かかりつけ"}],
        "reason_to_visit": "待ち時間の短さ。",
        "clinic_model": "ユニット4台、土曜診療。",
        "differentiation": "予防中心のリコール設計。",
        "opening_risks": ["供給過密による新患獲得の遅れ"],
        "before_opening": ["各院のユニット数と待ち時間"],
        "information_gaps": "周辺医院の中身は 1 件も調べていません。",
    }


def test_one_run_produces_both_reports():
    """**ボタン 1 つで 2 部。** 章立てはどちらも設定が決めます。"""
    pack = dd.fact_pack(_dataset(), None, "dental_clinic")
    markdown = dd_report.to_markdown(_written(), pack, [], "免責", advice=_advice())

    assert "# 第I部　商圏プレDD" in markdown
    assert "# 第II部　開業提言" in markdown
    assert markdown.index("第I部") < markdown.index("第II部")

    titles = [c["title"] for c in cfg.dd_config("dental_clinic")["advice_chapters"]]
    assert len(titles) == 10
    tail = markdown[markdown.index("# 第II部"):]
    for index, title in enumerate(titles, start=1):
        assert f"## {index}. {title}" in tail, title


def test_the_reader_is_told_which_part_reasons():
    """**どこまでが事実で、どこからが提案か。** 混ぜると第I部が信用を失います。"""
    pack = dd.fact_pack(_dataset(), None, "dental_clinic")
    markdown = dd_report.to_markdown(_written(), pack, [], "", advice=_advice())
    assert "推論" in markdown[:markdown.index("# 第I部")]
    assert "**ここからは推論です。**" in markdown[markdown.index("# 第II部"):]


def test_the_reasoning_chain_is_shown_with_its_tags():
    """FACT → … → ACTION を、印つきで残すこと。"""
    pack = dd.fact_pack(_dataset(), None, "dental_clinic")
    markdown = dd_report.to_markdown(_written(), pack, [], "", advice=_advice())
    appendix = markdown[markdown.index("## 付録：この提言に至った筋道"):]
    for tag in ("FACT", "PATTERN", "WHY", "INSIGHT", "ACTION"):
        assert f"`{tag}`" in appendix, tag
    assert "https://city.example/" in appendix, "WHY の出典が消えています"


def test_a_layer_with_no_basis_says_so_instead_of_inventing_one():
    """挙げられなかった層は、**空欄ではなく「挙げられていない」と書く。**"""
    pack = dd.fact_pack(_dataset(), None, "dental_clinic")
    thin = {**_advice(), "segments": [s for s in _advice()["segments"]
                                      if s["role"] == "primary"]}
    markdown = dd_report.to_markdown(_written(), pack, [], "", advice=thin)
    section = markdown[markdown.index("## 2. 準主要患者"):markdown.index("## 3. 積極的")]
    assert "挙げられていません" in section


# ------------------------------------------------- 情報不足を機会と読み替えない
def _payload(surveyed: int) -> dict:
    return {"facts": {"competition": {"surveyed_count": surveyed}}}


def test_calling_the_competition_weak_without_looking_is_rejected():
    """**いちばん起きやすい壊れ方です。**

    競合の中身を 1 件も見ていないのに「競合が少ない」と書くと、情報不足が
    そのまま機会に化けます。指示書が名指しで禁じている失敗です。
    """
    from kaigyou_intel.steps.advice_write import _verify

    report = {**_advice(),
              "differentiation": "周辺は競合が少ないため、予防で差別化できる。"}
    problems = _verify(report, _payload(0))
    assert any("競合が少な" in p for p in problems), problems

    # 中身を調べたうえでの記述なら通ること。
    assert not any("競合が少な" in p for p in _verify(report, _payload(3)))


def test_not_looking_at_all_must_be_stated_as_a_gap():
    """調べていないなら、**調べていないと書くこと。**"""
    from kaigyou_intel.steps.advice_write import _verify

    silent = {**_advice(), "information_gaps": ""}
    assert any("information_gaps" in p for p in _verify(silent, _payload(0)))
    assert not [p for p in _verify(_advice(), _payload(0))
                if "information_gaps" in p]


def test_the_advice_must_reach_an_action():
    """**示唆で終わらせない。** 開業者が何をするかまで書くのが提言です。"""
    from kaigyou_intel.steps.advice_write import _verify

    stops_short = {**_advice(),
                   "reasoning": [s for s in _advice()["reasoning"]
                                 if s["tag"] != "ACTION"]}
    assert any("ACTION" in p for p in _verify(stops_short, _payload(3)))


def test_the_segments_and_the_first_catchment_are_required():
    """主要患者・競争しない層・第1商圏は、提言の骨格です。"""
    from kaigyou_intel.steps.advice_write import _verify

    assert _verify(_advice(), _payload(3)) == []
    no_avoid = {**_advice(),
                "segments": [s for s in _advice()["segments"]
                             if s["role"] != "avoid"]}
    assert any("競争すべきでない層" in p for p in _verify(no_avoid, _payload(3)))
    no_ring = {**_advice(), "catchments": []}
    assert any("第1商圏" in p for p in _verify(no_ring, _payload(3)))


def test_the_advice_schema_stays_simple_enough_to_compile():
    """スキーマは平らに保つこと。**実測で `Schema is too complex.` を踏んでいます。**"""
    import json

    from kaigyou_intel.schemas import AdviceReport

    def shape(node, depth=0):
        arrays = unions = 0
        deepest = depth
        if isinstance(node, dict):
            if node.get("type") == "array":
                arrays += 1
            if "anyOf" in node or "oneOf" in node:
                unions += 1
            children = node.values()
        elif isinstance(node, list):
            children = node
        else:
            return arrays, unions, deepest
        for child in children:
            a, u, d = shape(child, depth + 1)
            arrays += a
            unions += u
            deepest = max(deepest, d)
        return arrays, unions, deepest

    schema = AdviceReport.model_json_schema()
    arrays, unions, depth = shape(schema)
    # 落ちたときの CompetitorSurvey は 配列 8・union 2・深さ 7 でした。
    assert arrays < 8 and unions == 0 and depth <= 7, \
        f"配列 {arrays} / union {unions} / 深さ {depth}"
    assert len(json.dumps(schema, ensure_ascii=False)) < 5_200


# ================================= 検算が厳しすぎて段が止まった（実測の回帰）
#
# 実測：STEP2 が
#
#     本文の数値 77.4 は、渡した事実の中にありません
#     本文の数値 13.6 は、渡した事実の中にありません  …
#
# で止まりました。0.774 も 0.136 も**束にある値**です。割合を 0〜1 で持ち、
# 画面とレポートでは % で出しているので、同じ事実が 2 通りの数字で書かれます。
# **自分のレンダラがやっている変換を、自分の検算が禁じていました。**

def test_a_share_written_as_a_percentage_is_accepted():
    """0.337 と 33.7% と 34% は同じ事実。**書き方の違いで止めない。**"""
    allowed = dd.numbers_in({"share": 0.337, "coverage": 0.643})
    for written in ("0.337", "33.7", "34", "33.70", "64.3", "0.64"):
        assert not verify_dd_report(
            {"summary": f"値は {written} です。", "takeaways": [], "verdict": {}},
            allowed), written


def test_rounding_is_allowed():
    """「約6.94km²」で止めない。**丸めても値は変わっていません。**"""
    allowed = dd.numbers_in({"area_km2": 6.9366})
    for written in ("6.9366", "6.937", "6.94", "6.9", "7"):
        assert not verify_dd_report(
            {"summary": f"面積は {written} km² です。", "takeaways": [],
             "verdict": {}}, allowed), written


def test_a_round_invented_number_is_still_caught():
    """**いちばん通してはいけない抜け方。**

    「45000」の末尾の 0 を削ると「45」になります。整数から 0 を削っていた
    ため、**丸い数ほど何かに当たって素通り**していました。作られた数字は
    丸いことが多いので、この抜け方は致命的です。
    """
    allowed = dd.numbers_in({"clinics": 45, "meshes": 12, "share": 0.34})
    for invented in ("45000", "1200000", "3400", "120"):
        problems = verify_dd_report(
            {"summary": f"患者は {invented} 人です。", "takeaways": [],
             "verdict": {}}, allowed)
        assert problems, f"{invented} が素通りしています"


def test_the_scaling_does_not_open_a_hole_for_big_numbers():
    """×100 は**割合にだけ**掛けること。

    何にでも掛けると、束の 450 が 45,000 を許します。人口や距離が割合として
    書かれることはないので、掛ける必要もありません。
    """
    allowed = dd.numbers_in({"population": 450, "workers": 12000})
    assert verify_dd_report(
        {"summary": "45000 人です。", "takeaways": [], "verdict": {}}, allowed)
    # 元の値そのものは通ること。
    assert not verify_dd_report(
        {"summary": "450 人、12000 人です。", "takeaways": [], "verdict": {}},
        allowed)


# ============================ 検算で段を止めない（実測：3 回止まった）
#
# 実測のエラー：
#
#     本文の数値 77.4 は、渡した事実の中にありません   （0.774 が束にある）
#     本文の数値 15.3 は、渡した事実の中にありません   （人数から割った割合）
#     本文の数値 13.1 は、渡した事実の中にありません   （同上）
#
# **照合は網であって、証明ではありません。** 人数から割り算して出した割合は
# コンサルタントが書いて当然の数字で、それを理由に、料金を払って書かせた文書を
# 破棄するほうが間違っています。止めずに、隠さない。

def test_the_shares_the_model_would_otherwise_divide_for_are_provided():
    """**割らせないための正しい直し方は、禁じることではなく渡すこと。**

    束は人数（age_0_14: 4643）しか持っておらず、読み手に要る「年少人口比
    13.1%」がありませんでした。渡さなければ LLM は割り算するしかありません。
    """
    pack = dd.fact_pack(_dataset(), None, "dental_clinic")
    row = (pack["demand"]["residents"]["by_radius"] or {}).get("1000") or {}
    assert row.get("share_0_14_pct") is not None
    assert row.get("share_15_64_pct") is not None
    assert row.get("share_65_plus_pct") is not None
    # 計算済みなので、そのまま書けば照合を通ること。
    allowed = dd.numbers_in(pack)
    for key in ("share_0_14_pct", "share_65_plus_pct"):
        written = row[key]
        assert not verify_dd_report(
            {"summary": f"構成比は {written}% です。", "takeaways": [],
             "verdict": {}}, allowed), key


def test_the_demand_chapter_actually_receives_the_measures():
    """**LLM が数字を作る理由を、こちらで作らない。**

    layer で絞っていたら 1 件も残らず、この章に数字が渡っていませんでした。
    """
    pack = dd.fact_pack(_dataset(), None, "dental_clinic")
    assert pack["demand"]["measures"], "第5章に指標が 1 件も渡っていません"


def test_an_untraceable_number_is_disclosed_not_fatal():
    """**止めずに、隠さない。**

    段を止めれば料金を払って書かせた文書が消えます。黙って通せば読み手が
    根拠のない数字を信じます。どちらでもなく、文書に明記します。
    """
    from kaigyou_intel.steps.dd_write import _numbers_only

    pack = dd.fact_pack(_dataset(), None, "dental_clinic")
    allowed = dd.numbers_in(pack)
    written = {**_written(),
               "summary": "患者は年間 45000 人と見込まれます。"}
    untraceable = _numbers_only(verify_dd_report(written, allowed))
    assert "45000" in untraceable

    markdown = dd_report.to_markdown(
        {**written, "unverified_numbers": untraceable}, pack, [], "免責")
    assert "## 本文の数値について" in markdown
    assert "45000" in markdown[markdown.index("## 本文の数値について"):]
    # レポートは出ていること（止まっていない）。
    assert "## 1. Executive Summary" in markdown


def test_nothing_is_said_when_every_number_checks_out():
    """辿れた文書に、余計な注記を足さないこと。"""
    pack = dd.fact_pack(_dataset(), None, "dental_clinic")
    markdown = dd_report.to_markdown(_written(), pack, [], "免責")
    assert "## 本文の数値について" not in markdown


# ================================== 計算式まで出させて、こちらで計算し直す
#
# 割り算を禁じても守られず、黙って許すと根拠のない数字が残り、弾くと文書ごと
# 消えました（実測 3 回）。第 4 の道が、式を出させてこちらで計算することです。
#
#     LLM   「年少人口比 13.1%」  式: 1572 / 12000 * 100
#     こちら 1572 と 12000 が束にあるか確かめ、計算して 13.1 と一致するか見る
#
# これで派生値が**検証可能な事実**になり、読み手も式で追えます。

def test_only_arithmetic_gets_through_the_evaluator():
    """**`eval` に文字列を渡すのとは別物です。** 名前も呼び出しも通しません。"""
    from kaigyou_core import arithmetic

    assert arithmetic.evaluate("1572 / 12000 * 100") == pytest.approx(13.1)
    assert arithmetic.evaluate("(10 + 2) * 3") == 36
    assert arithmetic.evaluate("-5 + 3") == -2

    for hostile in ("__import__('os').system('ls')", "population * 2",
                    "open('/etc/passwd')", "2 ** 100", "[1,2][0]",
                    "1 if True else 2", "lambda: 1"):
        with pytest.raises(arithmetic.BadExpression):
            arithmetic.evaluate(hostile)


def test_a_formula_built_from_the_facts_becomes_a_usable_number():
    """式が通れば、その答えは**本文で使ってよい数**になる。"""
    from kaigyou_core import arithmetic

    pack = dd.fact_pack(_dataset(), None, "dental_clinic")
    numbers = dd.numbers_in(pack)
    row = pack["demand"]["residents"]["by_radius"]["1000"]
    young, total = row["age_0_14"], row["population"]

    checked = arithmetic.check(
        [{"label": "年少人口比", "expression": f"{young} / {total} * 100",
          "value": round(young / total * 100, 1), "unit": "%"}], numbers)
    assert checked[0]["ok"], checked[0]["problem"]

    allowed = numbers | arithmetic.verified_values(checked)
    stated = round(young / total * 100, 1)
    assert not verify_dd_report(
        {"summary": f"年少人口比は {stated}% です。", "takeaways": [],
         "verdict": {}}, allowed)


def test_a_formula_using_a_number_nobody_supplied_is_rejected():
    """**式の形をした作り話**を通さない。"""
    from kaigyou_core import arithmetic

    numbers = dd.numbers_in({"population": 12000})
    checked = arithmetic.check(
        [{"label": "作り話", "expression": "45000 / 3", "value": 15000}], numbers)
    assert not checked[0]["ok"]
    assert "45000" in checked[0]["problem"]


def test_a_formula_whose_answer_does_not_match_is_rejected():
    """式と答えが食い違っていたら、**式のほうを信じない。**"""
    from kaigyou_core import arithmetic

    numbers = dd.numbers_in({"a": 1572, "b": 12000})
    checked = arithmetic.check(
        [{"label": "ずれ", "expression": "1572 / 12000 * 100", "value": 99.9}],
        numbers)
    assert not checked[0]["ok"]
    assert "99.9" in checked[0]["problem"]


def test_rounding_the_answer_is_not_a_mismatch():
    """「13.1」は小数第1位に丸めた値。**丸めたことを間違いにしない。**"""
    from kaigyou_core import arithmetic

    numbers = dd.numbers_in({"a": 1572, "b": 12000})
    for stated in (13.1, 13, 13.10):
        checked = arithmetic.check(
            [{"label": "比", "expression": "1572 / 12000 * 100",
              "value": stated}], numbers)
        assert checked[0]["ok"], f"{stated}: {checked[0]['problem']}"


def test_the_conversion_constants_do_not_need_to_be_in_the_facts():
    """`% にするための 100` まで束に要求しない。"""
    from kaigyou_core import arithmetic

    checked = arithmetic.check(
        [{"label": "比", "expression": "1572 / 12000 * 100", "value": 13.1}],
        dd.numbers_in({"a": 1572, "b": 12000}))
    assert checked[0]["ok"], checked[0]["problem"]


def test_the_formulas_are_shown_to_the_reader_including_the_failed_ones():
    """**黙って消さない。** 消すと、数字が本文に残ったまま根拠だけが消えます。"""
    pack = dd.fact_pack(_dataset(), None, "dental_clinic")
    written = {**_written(), "derived": [
        {"label": "年少人口比", "expression": "1572 / 12000 * 100",
         "value": 13.1, "unit": "%", "ok": True, "computed": 13.1, "problem": ""},
        {"label": "作り話", "expression": "45000 / 3", "value": 15000,
         "unit": "人", "ok": False, "computed": 15000.0,
         "problem": "式の 45000 が、確定した事実の中にありません"},
    ]}
    markdown = dd_report.to_markdown(written, pack, [], "免責")
    section = markdown[markdown.index("## 本文で使った計算"):]
    assert "`1572 / 12000 * 100`" in section
    assert "✓" in section and "✗" in section
    assert "式の 45000 が、確定した事実の中にありません" in section


def test_the_advice_report_shows_its_formulas_too():
    """提言（第II部）でも同じ。"""
    pack = dd.fact_pack(_dataset(), None, "dental_clinic")
    advice = {**_advice(), "derived": [
        {"label": "高齢化率", "expression": "2832 / 12000 * 100", "value": 23.6,
         "unit": "%", "ok": True, "computed": 23.6, "problem": ""}]}
    markdown = dd_report.to_markdown(_written(), pack, [], "", advice=advice)
    tail = markdown[markdown.index("# 第II部"):]
    assert "## 提言で使った計算" in tail
    assert "`2832 / 12000 * 100`" in tail


def test_a_runaway_expression_is_refused():
    """長い式は、たいてい説明ではなく辻褄合わせです。"""
    from kaigyou_core import arithmetic

    with pytest.raises(arithmetic.BadExpression):
        arithmetic.evaluate(" + ".join(["1"] * 100))
    with pytest.raises(arithmetic.BadExpression):
        arithmetic.evaluate("1 / 0")
