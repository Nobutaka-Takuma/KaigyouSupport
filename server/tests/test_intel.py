"""商圏インテリジェンス・エンジン（Phase 1〜2）。

ここで守りたいのは、要件定義書がいちばん強く禁じていることです。

**入力に無い数字を出さない**（§3 原則2）。LLM にパーセンタイルを作らせない
仕組みになっているか、出力の数字が入力に実在するかを見ます。

**根拠が辿れる**（§25）。FACT が実在の指標を、PATTERN が実在の FACT を
指しているか。スキーマは形しか保証しないので、参照はこちらで確かめます。

**ステップが独立している**（§32）。Step2 が落ちても Step1 は残り、Step2 から
やり直せるか。

LLM は呼びません（API キーが要るうえ、呼ぶたびに結果が変わるとテストに
なりません）。呼び出しの境界を差し替えて、その前後を検証します。
"""
from __future__ import annotations

import json
import pathlib
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest

from kaigyou_core import config as cfg
from kaigyou_intel import client as llm
from kaigyou_intel.projection import (
    allowed_numbers,
    base_data_hash,
    for_step1,
    for_step2,
    for_step4,
    to_jsonable,
)
from kaigyou_intel.schemas import Fact, Pattern, Step1Output, verify_step1

REPO_ROOT = Path(__file__).resolve().parents[2]


# ------------------------------------------------------------------ 設定
def test_every_step_is_configured_with_its_prompt():
    """実装済みのステップには、プロンプトの実体があること。

    未実装のぶんは名前だけ先に置いてあります。実装した番号を RUNNERS に足すと、
    このテストがその番号のプロンプトを要求します。
    """
    from kaigyou_intel.jobs import STEP_NAMES
    from kaigyou_intel.worker import RUNNERS

    config = cfg.analysis_config()
    assert set(config["steps"]) == set(STEP_NAMES)
    for number, step in config["steps"].items():
        assert step["prompt_version"], f"step{number} に prompt_version がありません"
        if number not in RUNNERS:
            continue
        assert (cfg.business_dir() / "prompts" / step["prompt"]).is_file(), (
            f"step{number} のプロンプトが見つかりません: {step['prompt']}")


def test_the_searching_step_has_a_second_prompt_for_writing_it_down():
    """Web検索と構造化出力は同じ呼び出しでは併用しないので、STEP2 は 2 本必要。"""
    step = cfg.analysis_config()["steps"][2]
    assert (cfg.business_dir() / "prompts" / step["prompt_structure"]).is_file()
    assert llm.step_settings(2)["prompt_structure"] == step["prompt_structure"]


def test_only_step2_may_search_the_web():
    """要件 §38：外部コンテクスト調査を STEP2 に限定する。

    STEP1 で外部情報が混ざると、FACT と EXTERNAL FACT の区別が最初の段階で
    壊れます。STEP4 で足せると、§16 の「新しい外部事実を追加しない」が破れます。
    """
    steps = cfg.analysis_config()["steps"]
    assert steps[2]["web_search"] is True
    for number in (1, 3, 4):
        assert steps[number].get("web_search") is False, (
            f"STEP{number} で Web 検索が有効になっています")


def test_the_search_source_priority_matches_the_requirement():
    """要件 §9 の優先順位が、設定として存在すること。"""
    types = [s["type"] for s in cfg.analysis_config()["search"]["source_types"]]
    assert types[:5] == ["government", "statistics", "prefecture",
                         "municipality", "public_body"]


def test_source_type_is_decided_by_the_url_not_by_the_model():
    """機械的に決まることを LLM に判定させない。"""
    from kaigyou_intel.jobs import classify_source

    assert classify_source("https://www.mlit.go.jp/a") == "government"
    assert classify_source("https://www.pref.shizuoka.jp/b") == "prefecture"
    assert classify_source("https://www.city.susono.shizuoka.jp/c") == "municipality"
    assert classify_source("https://www.u-tokyo.ac.jp/d") == "academic"
    assert classify_source("https://example.com/e") == "other"


# ------------------------------------------------------------------ 射影
@pytest.fixture(scope="module")
def dataset():
    psycopg = pytest.importorskip("psycopg")
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app
    from kaigyou_core.db import connect

    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM mesh_scores")
            if cur.fetchone()["n"] == 0:
                pytest.skip("mesh scores not computed here")
    except psycopg.OperationalError as exc:
        pytest.skip(f"database unavailable: {exc}")

    response = TestClient(app).get("/api/dataset", params={
        "lat": 35.6717, "lng": 139.7650, "radius": 1000,
        "profile": "pediatric", "max_clinics": 0})
    assert response.status_code == 200
    return response.json()


def test_step1_is_given_the_benchmarks_rather_than_asked_to_compute_them(dataset):
    """§3 原則2 を仕組みで守る。

    パーセンタイル・順位・position_label を入力に含めます。含めなければ LLM は
    自分で作るしかなく、作ったものは入力に無い数字です。
    """
    payload = for_step1(dataset)
    measures = {m["key"]: m for m in payload["measures"]}
    placed = [m for m in measures.values() if m.get("value") is not None]
    assert placed
    for measure in placed:
        assert "unit" in measure and "source" in measure
        if measure.get("rank") is not None:
            assert measure.get("position_label"), f"{measure['key']} に position_label が無い"
            assert measure.get("benchmark_type")


def test_step1_is_not_given_the_raw_percentile(dataset):
    """端で逆の意味に読める数字は渡さない。

    銀座の人口増減率は中央区内で最下位で、percentile は 0.0 です。それを
    渡したところ、モデルは position_label（「下位0.4%」）ではなく percentile から
    文を作り、「下位0%」と書きました。0% の集合には誰も入れません。

    プロンプトには「percentile から作るな」と既に書いてありました。書いてある
    ことより、無いことのほうが強い。
    """
    payload = for_step1(dataset, {"all_benchmarks": True})
    for measure in payload["measures"]:
        assert "percentile" not in measure
        assert "top_share_pct" not in measure
        for benchmark in measure.get("benchmarks") or []:
            assert "percentile" not in benchmark, measure["key"]
            assert "top_share_pct" not in benchmark, measure["key"]
        # 位置そのものは落としません。落とすと今度は何も言えなくなります。
        if measure.get("rank") is not None:
            assert measure.get("position_label") and measure.get("of")


def test_step1_keeps_every_reference_class(dataset):
    """母集団6つの比較は削らない。

    最初の実装は「大きいものを削る」で代表1つに間引いていました。ですがそこは
    いちばん削ってはいけない場所でした。銀座の 0〜14歳人口は、県内では
    下位24.3%（typical）なのに、近隣・同一区・同規模の商圏と比べると
    下位2.4〜4.5%（very_low）です。**食い違いこそがパターン**で、代表1つでは
    見えません。

    削る基準は「大きいもの」ではなく「発見に寄与しないもの」です。
    """
    payload = for_step1(dataset, {"all_benchmarks": True})
    placed = [m for m in payload["measures"]
              if m.get("value") is not None and m.get("rank") is not None]
    assert placed
    for measure in placed:
        assert measure.get("benchmarks"), f"{measure['key']} の比較が落ちています"
    # 母集団が複数あることが、この設計の要点。
    assert max(len(m["benchmarks"]) for m in placed) >= 3


def test_step1_still_drops_what_does_not_help(dataset):
    """要件 §34：元JSONを毎回丸ごと渡さない。

    医院20件の名前と住所は14KBありますが、パターン発見には効きません
    （効くのは by_specialty と hours の集計のほう）。
    """
    whole = len(json.dumps(to_jsonable(dataset), ensure_ascii=False).encode())
    projected = len(json.dumps(
        for_step1(dataset, {"all_benchmarks": True}), ensure_ascii=False).encode())
    assert projected < whole, "何も削れていません"
    assert "clinics" not in for_step1(dataset, {})["competition"]


def test_the_whole_dataset_can_be_sent_when_asked(dataset):
    """比較実験のための逃げ道。既定ではありません。"""
    payload = for_step1(dataset, {"full_dataset": True})
    assert set(payload) == set(to_jsonable(dataset))


def test_turning_off_all_benchmarks_actually_turns_them_off(dataset):
    """設定の欄が読まれずに落ちていないこと。

    all_benchmarks は読み取ってはいたものの、_measure に渡し忘れていました。
    既定が True なので出力は正しく、設定だけが黙って効かない状態でした。
    """
    trimmed = for_step1(dataset, {"all_benchmarks": False})
    assert all("benchmarks" not in m for m in trimmed["measures"])
    # 代表1つ（平坦な benchmark_*）は残ります。
    assert any(m.get("position_label") for m in trimmed["measures"])
    full = for_step1(dataset, {"all_benchmarks": True})
    assert any(m.get("benchmarks") for m in full["measures"])


def test_what_step1_sees_is_configuration_not_code():
    """何を渡すかは config/analysis.yaml で変えられること。"""
    projection = cfg.analysis_config().get("projection") or {}
    assert projection.get("all_benchmarks") is True
    assert projection.get("clinic_list") is False
    assert projection.get("full_dataset") is False


def test_the_clinic_list_is_not_sent_to_step1(dataset):
    """50件の医院名と住所は 34KB あって、パターン発見には寄与しません。"""
    payload = for_step1(dataset, {})
    assert "clinics" not in payload["competition"]
    assert payload["competition"]["by_specialty"] is not None
    assert payload["competition"]["hours"] is not None


def test_step1_keeps_the_reasons_a_comparison_was_withheld(dataset):
    """significance が無い理由を落とさない。

    落とすと「平凡だった」と読まれ、裾野のような地域で誤った断定が出ます。
    """
    payload = for_step1(dataset)
    for measure in payload["measures"]:
        if measure.get("value") is not None and measure.get("rank") is None:
            assert measure.get("benchmark_unavailable_reason")


def test_step1_keeps_the_gaps(dataset):
    """gaps は「確認できていないこと」。ここが唯一「未確認」と言える場所。"""
    payload = for_step1(dataset)
    for insight in payload["insight_metrics"]:
        assert "gaps" in insight and "complete" in insight
        # 成分の値は measures にあるので再掲しない。
        assert "components" not in insight
        assert "component_keys" in insight


def test_the_clinic_opening_years_reach_the_analysis(dataset):
    """20年後の競合の数は、いまの数ではなく「いまの院長があと何年やるか」で
    決まります。年齢は公表されていませんが、開設年は届出にあり、既に
    取り込んであります。それを出していませんでした。

    **開設年は院長の年齢ではありません。** 承継で代替わりした医院も、法人の
    分院も、開設は新しいままです。だから代理指標であって結論ではない、と
    データ自身に書かせます。
    """
    vintage = dataset["competition"].get("vintage")
    assert vintage is not None, "商圏に医院が1件でもあれば、この欄は出ます"
    # 取れたか取れなかったかを、必ずどちらかで名乗ること。
    assert "available" in vintage
    assert vintage["total_clinics"] >= 0
    if vintage["available"]:
        assert "院長の年齢ではありません" in vintage["note"]
        # 分母を必ず並べる。3件しか分からない商圏で「中央値1998年」と書くと
        # 10件ぶんの話に読めます。
        assert vintage["with_opening_date"] <= vintage["total_clinics"]
    else:
        assert vintage["reason"] == "no_opening_dates"
        assert vintage["with_opening_date"] == 0


def test_the_report_says_when_no_opening_date_could_be_read(dataset):
    """「古い医院は0件」と「1件も分からなかった」は別のことです。

    黙って省くと、調べたうえで該当なしと読まれます。
    """
    from kaigyou_intel.report import to_markdown

    data = to_jsonable(dataset)
    data["competition"] = dict(data["competition"], vintage={
        "available": False, "reason": "no_opening_dates",
        "total_clinics": 10, "with_opening_date": 0,
        "note": "商圏内の医院について開設年月日が1件も取得できていません。"})
    markdown = to_markdown(_report_output().model_dump(), data)
    assert "#### 開設年（商圏内）" in markdown
    assert "1件も取得できていません" in markdown


def test_the_hours_and_specialties_can_be_cited_as_evidence(dataset):
    """実測：レポートは「日曜診療は2院」と本文に書きながら、根拠には別の
    指標の id を添えていました。§25 の追跡がそこで切れています。

    診療時間も標榜科目も measures ではありません（比較用の分布が
    mesh_scores に無いので指標にできない）。**引けないものは、掛け合わせ
    ようもありません。**
    """
    from kaigyou_intel.projection import citable_keys

    payload = for_step1(dataset)
    keys = citable_keys(payload)
    assert "clinic_hours.sunday" in keys
    assert keys["clinic_hours.sunday"] == "competition_offer"
    # 数の層と、提供体制の層は別。医院数と1院あたり人口を掛けても、それは
    # 同じ数の割り算でしかありません。
    assert keys["dental_clinics"] == "competition"
    assert keys["workers"] == "economy"
    assert len({keys[k] for k in keys}) >= 4, "掛け合わせる相手が要ります"

    # 値はデータセットから写すだけ。ここで計算しません。
    hours = {c["key"]: c for c in payload["citable"]}
    counts = {e["key"]: e["count"]
              for e in dataset["competition"]["hours"]["counts"]}
    assert hours["clinic_hours.sunday"]["value"] == counts["sunday"]


def test_step2_is_not_given_the_base_data(dataset):
    """渡さなかったものについては何も言えない。

    base_data を渡すと、外部情報を調べずに手元の数字を言い換えたものが
    「外部事実」として返ってきます。
    """
    step1 = {"patterns": [{"id": "P001", "title": "t", "evidence_summary": "s",
                           "importance": "high", "research_questions": ["q"]}]}
    payload = for_step2(step1, dataset, {"max_patterns": 5})
    assert "measures" not in payload
    assert "competition" not in payload
    assert payload["patterns"][0]["id"] == "P001"


def test_step2_is_given_the_clinic_names_it_cannot_research_without():
    """インプラント・審美・訪問診療は標榜診療科目ではありません。

    届出の自由記載欄にしかなく、記載率が低い（東京都で1%台）。つまり
    「この商圏でインプラントを扱う医院が何院あるか」は手元のデータからは
    **原理的に分かりません**。固有名詞を渡さないかぎり、外部でも調べようが
    ない。だから医院の名前だけは例外として渡します。
    """
    step1 = {"patterns": []}
    data = {"location": {"municipality_name": "裾野市"},
            "competition": {"clinics_in_radius": {"items": [
                {"name": f"歯科医院{i}", "distance_m": i * 50,
                 "homepage": f"https://example.invalid/{i}",
                 "specialties": [{"key": "general", "label": "一般歯科"}],
                 "address": "渡さない", "lat": 1.0, "lng": 2.0}
                for i in range(1, 11)]}}}
    payload = for_step2(step1, data, {"clinics_to_research": 3})
    names = payload["nearby_clinics"]
    assert [c["name"] for c in names] == ["歯科医院1", "歯科医院2", "歯科医院3"]
    assert names[0]["specialties"] == ["一般歯科"]
    assert names[0]["homepage"] == "https://example.invalid/1"
    # 住所と座標は渡しません。調べるのに要らないうえ、量が増えます。
    assert "address" not in names[0] and "lat" not in names[0]


def test_step2_respects_the_pattern_limit(dataset):
    """要件 §34：PATTERN の上限。"""
    step1 = {"patterns": [{"id": f"P{i:03d}", "title": "t", "evidence_summary": "s",
                           "importance": "high", "research_questions": ["q"]}
                          for i in range(1, 12)]}
    payload = for_step2(step1, dataset, {"max_patterns": 5})
    assert len(payload["patterns"]) == 5


def test_step4_gets_conclusions_not_raw_material(dataset):
    """要件 §16：STEP4 で新しい外部事実を足さない。

    足せないようにするには、足せるだけの材料を渡さないのが確実です。
    """
    payload = for_step4({"facts": []}, {"external_facts": []}, {"insights": []}, dataset)
    assert "items" not in payload["competition"]
    assert payload["step2"]["external_facts"] == []
    # 母集団6つの比較は渡しません。位置づけは STEP1 が FACT として既に選んで
    # いて、ここでもう一度選ばせると段ごとに答えがぶれます。
    assert all("benchmarks" not in m for m in payload["measures"])


def test_the_hash_ignores_the_timestamp(dataset):
    """同一地点・同一データの再実行を見分けるため（§34 Cache）。

    generated_at を含めると毎回別物になり、キャッシュが永久に当たりません。
    """
    a = dict(dataset, generated_at="2026-01-01T00:00:00Z")
    b = dict(dataset, generated_at="2026-12-31T23:59:59Z")
    assert base_data_hash(a) == base_data_hash(b)
    c = dict(dataset)
    c["location"] = dict(c["location"], lat=99.0)
    assert base_data_hash(c) != base_data_hash(a)


def test_numbers_in_the_input_can_be_enumerated(dataset):
    """出力の検算に使う集合。入力に無い数字を見つけるための土台。"""
    numbers = allowed_numbers(for_step1(dataset))
    assert len(numbers) > 50
    population = next(m for m in for_step1(dataset)["measures"]
                      if m["key"] == "population")
    from kaigyou_intel.projection import _canonical
    assert _canonical(population["value"]) in numbers


# ------------------------------------------------- スキーマと根拠の追跡
def _output(**kwargs) -> Step1Output:
    base = dict(
        facts=[Fact(id="F001", statement="0〜14歳人口は1,088人", value=1088.6,
                    unit="人", measure_key="child_population",
                    position_label="下位24.3%", benchmark_type="prefecture"),
               Fact(id="F002", statement="構成比は8.2%", measure_key="child_share")],
        patterns=[Pattern(id="P001", title="絶対数も構成比も周辺並み",
                          evidence=["F001", "F002"], evidence_summary="…",
                          importance="medium", research_questions=["なぜか"])])
    base.update(kwargs)
    return Step1Output(**base)


#: 層を跨いだ出力。人口動態 × 競合の提供体制。
def _crossing_output(**kwargs) -> Step1Output:
    facts = [
        Fact(id="F001", statement="65歳以上の割合は31.2%", measure_key="elderly_share"),
        Fact(id="F002", statement="日曜に開けている医院は2院",
             measure_key="clinic_hours.sunday"),
        Fact(id="F003", statement="従業者数は4,035人", measure_key="workers"),
    ]
    patterns = [
        Pattern(id=f"P00{i}", title="高齢化は進むが日曜の受け皿が薄い",
                evidence=["F001", "F002"], evidence_summary="…",
                importance="high", research_questions=["日曜に人は採れるか"])
        for i in (1, 2, 3)
    ]
    base = dict(facts=facts, patterns=patterns)
    base.update(kwargs)
    return Step1Output(**base)


#: 指標 -> 層。実データでは citable_keys が返すもの。
_LAYERS = {"child_population": "residents", "child_share": "residents",
           "elderly_share": "residents", "workers": "economy",
           "clinic_hours.sunday": "competition_offer"}


def test_a_pattern_must_rest_on_at_least_two_facts():
    """単一の事実の言い換えは PATTERN ではありません（要件 §6）。"""
    with pytest.raises(Exception):
        Pattern(id="P001", title="t", evidence=["F001"], evidence_summary="s",
                importance="high", research_questions=["q"])


def test_a_fact_pointing_at_a_measure_that_does_not_exist_is_caught():
    """LLM が指標名を作ってしまった場合。スキーマは通ってしまう。"""
    output = _output(facts=[Fact(id="F001", statement="…",
                                 measure_key="median_household_income")])
    output.patterns[0].evidence = ["F001", "F001"]
    problems = verify_step1(output, {"child_population", "child_share"})
    assert any("median_household_income" in p.problem for p in problems)


def test_a_pattern_pointing_at_a_fact_that_does_not_exist_is_caught():
    """§25 の追跡は、参照が全部解決して初めて成立します。"""
    output = _output()
    output.patterns[0].evidence = ["F001", "F999"]
    problems = verify_step1(output, {"child_population", "child_share"})
    assert any("F999" in p.problem for p in problems)


def test_a_clean_output_has_no_problems():
    assert verify_step1(_output(), {"child_population", "child_share"}) == []


def test_a_pattern_that_stays_inside_one_layer_is_not_a_pattern():
    """実測：「人口が市内2位」「歯科医院数も市内2位」を並べたレポートが
    出ました。どちらも同じ商圏の大きさを別の言葉で言っているだけで、
    掛けても何も出てきません。読み手は「だから何なのか」を自分で考える
    ことになります。

    `importance` が high は「競合戦略・患者層・医院モデルに影響する」と
    いう宣言です。1つの層の中で完結する観察がそれに当たることはありません。
    """
    from kaigyou_intel.schemas import Pattern

    inside = _output(patterns=[Pattern(
        id="P001", title="絶対数も構成比も低い", evidence=["F001", "F002"],
        evidence_summary="…", importance="high", research_questions=["q"])])
    problems = verify_step1(inside, set(_LAYERS), _LAYERS)
    assert any("閉じています" in p.problem for p in problems)

    # importance を下げれば通ります。単一の層の観察が無価値なのではなく、
    # 「これが判断を変える」と名乗るのが違うだけです。
    inside.patterns[0].importance = "medium"
    assert verify_step1(inside, set(_LAYERS), _LAYERS) == []


def test_the_layers_are_counted_from_the_data_not_from_the_model():
    """層をモデルに申告させません。

    自己申告にすると「人口 × 競合」と書きながら人口の指標を2つ引く、と
    いう形だけ整った出力が通ります。引かれた指標から数えます。
    """
    assert verify_step1(_crossing_output(), set(_LAYERS), _LAYERS,
                        min_cross_layer=3) == []

    # 同じ形でも、引いている指標が同じ層なら跨いでいません。
    same_layer = _crossing_output()
    for pattern in same_layer.patterns:
        pattern.evidence = ["F001", "F002"]
    same_layer.facts[1].measure_key = "child_share"   # residents に移す
    problems = verify_step1(same_layer, set(_LAYERS) | {"child_share"},
                            {**_LAYERS, "child_share": "residents"},
                            min_cross_layer=3)
    assert any("層を跨いだ PATTERN が 0 件" in p.problem for p in problems)


def test_the_crossing_requirement_is_off_unless_it_is_configured():
    """層の情報が無い呼び出し（古いテスト・別の用途）まで落とさない。"""
    assert verify_step1(_output(), {"child_population", "child_share"}) == []


def test_the_prompt_and_the_check_read_the_same_threshold():
    """「3件以上」と書いておきながら2件で通る、の逆も含めて防ぐ。

    別々に読むと、書いていない条件で落ちます。落ちるとその段はやり直しで、
    費用も倍かかります。
    """
    from kaigyou_intel.steps.step1_features import min_cross_layer

    configured = (cfg.hypotheses_config()["crossing"]["min_cross_layer_patterns"])
    assert min_cross_layer() == configured
    # 上げすぎると無い矛盾を作り始めます。PATTERN の上限を超えないこと。
    assert configured <= cfg.analysis_config()["limits"]["max_patterns"]


def test_the_step1_schema_does_not_ask_for_benchmarks():
    """要件 §7 からの変更点。ここが変わったら気づけるように。

    パーセンタイルは /api/dataset が算出済みで、FACT は measure_key で参照
    します。スキーマに benchmarks を戻すと、LLM が数字を作り始めます。
    """
    assert set(Step1Output.model_fields) == {
        "facts", "patterns", "not_determinable", "surroundings", "questions",
        # 問いはここから生まれます（指示書 §53）。数値を伴う欄ではありません。
        "assumptions"}
    assert "measure_key" in Fact.model_fields
    # surroundings は外部情報の置き場所で、FACT の材料ではありません。
    # 数字を伴う欄（percentile / rank / benchmark）を足さないこと。
    from kaigyou_intel.schemas import NearbyFacility

    assert set(NearbyFacility.model_fields) == {
        "name", "category", "where", "scale", "why_it_matters", "source_url"}
    assert NearbyFacility.model_fields["scale"].annotation == (str | None), \
        "規模は文字列。数値欄にすると、統計の数字と同じ確かさに見えます"


# ------------------------------------------------------------------ Job
@pytest.fixture
def conn():
    psycopg = pytest.importorskip("psycopg")
    from kaigyou_core.db import connect

    try:
        with connect() as c:
            with c.cursor() as cur:
                cur.execute("SELECT to_regclass('public.analysis_jobs') AS t")
                if cur.fetchone()["t"] is None:
                    pytest.skip("017_analysis_jobs.sql not applied")
            yield c
            c.rollback()
    except psycopg.OperationalError as exc:
        pytest.skip(f"database unavailable: {exc}")


def test_a_new_job_has_every_step_pending(conn, dataset):
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    steps = jobs.get_steps(conn, job_id)
    assert [s["step_number"] for s in steps] == sorted(jobs.STEP_NAMES)
    assert all(s["status"] == "pending" for s in steps)
    assert jobs.next_step(conn, job_id) == 1
    conn.rollback()


def test_a_failed_step_does_not_erase_the_completed_ones(conn, dataset):
    """要件 §32：Step2 から再実行できること。

    最初からやり直させると、Web検索の費用も待ち時間ももう一度かかります。
    """
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    jobs.start_step(conn, job_id, 1, {"in": 1}, {"prompt_version": "v", "model": "m"})
    jobs.finish_step(conn, job_id, 1, {"facts": []},
                     {"input_tokens": 10, "output_tokens": 5, "web_searches": 0})
    jobs.fail_step(conn, job_id, 2, "検索に失敗しました")

    steps = {s["step_number"]: s for s in jobs.get_steps(conn, job_id)}
    assert steps[1]["status"] == "completed"
    assert steps[1]["output_json"] == {"facts": []}
    assert steps[2]["status"] == "failed"
    assert jobs.next_step(conn, job_id) == 2
    conn.rollback()


def test_an_unimplemented_step_is_left_pending_not_marked_failed(
        conn, dataset, monkeypatch):
    """未実装は失敗ではない。

    最初の実装は STEP2 を failed で記録していました。画面には赤い「失敗」と
    エラー本文が出るので、実行して壊れたように見えます。実際には一度も
    呼ばれていません。pending のままにしておけば、RUNNERS に足した次の
    `analyze --once` がそのまま続きから拾います。
    """
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    unimplemented = _pretend_the_last_step_is_unimplemented(monkeypatch)
    _complete_the_steps_before(conn, job_id, unimplemented)

    with pytest.raises(worker.StepNotImplemented):
        worker.run_step(conn, job_id, unimplemented)

    steps = {s["step_number"]: s for s in jobs.get_steps(conn, job_id)}
    assert steps[unimplemented]["status"] == "pending"
    assert not steps[unimplemented]["error_message"]
    assert jobs.next_step(conn, job_id) == unimplemented
    conn.rollback()


def test_retrying_a_step_also_clears_the_steps_after_it(conn, dataset):
    """後続だけ残すと、古い前提の上に新しい結論が乗ります。"""
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    for number in (1, 2, 3):
        jobs.start_step(conn, job_id, number, {}, {"prompt_version": "v", "model": "m"})
        jobs.finish_step(conn, job_id, number, {"n": number}, {})

    jobs.reset_step(conn, job_id, 2)
    steps = {s["step_number"]: s for s in jobs.get_steps(conn, job_id)}
    assert steps[1]["status"] == "completed", "STEP1 まで消してはいけない"
    assert steps[2]["status"] == "pending"
    assert steps[3]["status"] == "pending" and steps[3]["output_json"] is None
    assert jobs.next_step(conn, job_id) == 2
    conn.rollback()


def test_two_workers_do_not_claim_the_same_job(conn, dataset):
    """同じジョブを2回分析すると、費用が2倍で結果は1つしか残りません。"""
    from kaigyou_core.db import connect
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="claimtest")
    conn.commit()
    try:
        with connect() as a, connect() as b:
            first = jobs.claim_job(a)
            second = jobs.claim_job(b)
            assert first is not None
            assert second != first
    finally:
        with connect() as cleanup, cleanup.cursor() as cur:
            cur.execute("DELETE FROM analysis_jobs WHERE id = %s", (job_id,))
            cleanup.commit()


# ------------------------------------------------------ LLM を差し替えて通す
def test_step1_runs_end_to_end_with_a_stubbed_model(dataset, monkeypatch):
    """API キー無しで、呼び出しの前後を通す。

    プロンプトの組み立て・射影・構造化出力の検証・参照の追跡まで、LLM 以外の
    全部がここを通ります。
    """
    from kaigyou_intel.steps import step1_features

    calls: list[dict[str, object]] = []

    def fake_ask(*, step_number, system, user, schema=None, tools=None,
                 web_search=None, effort=None, max_uses=None):
        calls.append({"step_number": step_number, "system": system, "user": user,
                      "schema": schema, "tools": tools, "web_search": web_search,
                      "effort": effort, "max_uses": max_uses})
        if schema is None:   # 1 回目：周辺施設スキャン
            return llm.Result(
                parsed=None,
                text="〇〇モール（約120店舗）に隣接。徒歩5分に〇〇大学。",
                sources=[{"url": "https://example.go.jp/mall",
                          "title": "商業施設の概要"}],
                usage=llm.Usage(input_tokens=300, output_tokens=100),
                model="claude-opus-5")
        return llm.Result(parsed=_crossing_output(), text="",
                          usage=llm.Usage(input_tokens=1000, output_tokens=200),
                          model="claude-opus-5")

    monkeypatch.setattr(llm, "ask", fake_ask)
    output, usage, sources = step1_features.run(step1_features.build_input(dataset))

    scan, main = calls
    assert [c["step_number"] for c in calls] == [1, 1]
    # 1 回目は検索する呼び出し。**先に場所を調べてから統計を読みます。**
    assert scan["web_search"] is True
    assert scan["schema"] is None, "検索と構造化出力は同じ呼び出しで併用しない"
    assert scan["max_uses"] == cfg.analysis_config()["limits"]["surroundings_searches"]
    assert "{max_searches}" not in scan["system"]
    # 統計は渡しません。渡すと、検索せずに手元の数字を言い換えたものが
    # 「周辺にはこういう施設がある」として返ってきます。
    assert "measures" not in scan["user"]

    # 2 回目は要件 §6 のまま。道具を渡さず、構造化出力で受けます。
    assert main["tools"] is None, "STEP1 の本体に道具を渡してはいけない"
    assert main["schema"] is Step1Output
    # スキャンの結果と、取得した URL の一覧が届いていること。
    assert "〇〇モール" in main["user"]
    assert "https://example.go.jp/mall" in main["user"]
    # プロンプトに上限が埋め込まれていること。
    limit = cfg.analysis_config()["limits"]["max_patterns"]
    assert f"最大 {limit} 個" in main["system"]
    assert "{max_patterns}" not in main["system"]
    # 定性要因の枠と、層の掛け合わせの指示が入っていること。
    assert "デンタルIQ" in main["system"]
    assert "{qualitative_factors}" not in main["system"]
    assert "{crossing_examples}" not in main["system"]
    # 使用量は 2 回ぶんの合計。片方だけだと費用が半分に見えます。
    assert usage.input_tokens == 1300
    assert usage.output_tokens == 300
    assert output["facts"][0]["measure_key"] == "elderly_share"
    # measures に無いキーも FACT の根拠として引けること。診療時間は
    # measures ではなく citable にあります。
    assert output["facts"][1]["measure_key"] == "clinic_hours.sunday"
    # スキャンで取得した URL が出典として返ること。
    assert [s["url"] for s in sources] == ["https://example.go.jp/mall"]


def _scan_result(url: str = "https://example.go.jp/mall"):
    return llm.Result(parsed=None,
                      text="〇〇モール（約120店舗）に隣接。",
                      sources=[{"url": url, "title": "商業施設の概要"}],
                      usage=llm.Usage(input_tokens=300, output_tokens=100),
                      model="m")


def _with_surroundings(url: str = "https://example.go.jp/mall"):
    from kaigyou_intel.schemas import NearbyFacility, Surroundings

    output = _crossing_output()
    output.surroundings = Surroundings(
        setting="商業施設内・隣接",
        setting_reason="〇〇モールに隣接する区画です。",
        facilities=[NearbyFacility(
            name="〇〇モール", category="大型商業施設", where="商圏内",
            scale="約120店舗",
            why_it_matters="商圏が徒歩圏ではなく施設の集客圏になります。",
            source_url=url)])
    return output


def _stub_step1(monkeypatch, scan, main):
    def fake_ask(*, step_number, system, user, schema=None, tools=None,
                 web_search=None, effort=None, max_uses=None):
        if schema is None:
            if isinstance(scan, Exception):
                raise scan
            return scan
        return main

    monkeypatch.setattr(llm, "ask", fake_ask)


def test_step1_keeps_going_when_the_surroundings_scan_fails(dataset, monkeypatch):
    """スキャンは付随物です。**落ちても段ごと捨てません。**

    FACT 十数件と PATTERN の生成をやり直すと、時間も費用も倍かかります
    （実測で 1 回 $1 前後）。落ちたことは not_determinable に残します。
    """
    from kaigyou_intel.steps import step1_features

    _stub_step1(monkeypatch, RuntimeError("rate limited"),
                llm.Result(parsed=_with_surroundings(), usage=llm.Usage(), model="m"))
    output, _usage, sources = step1_features.run(step1_features.build_input(dataset))

    assert output["facts"], "スキャンが落ちても本題は出ること"
    # 検索が動いていないのに施設が並んでいる＝モデルの記憶です。落とします。
    assert output["surroundings"] is None
    assert any("スキャンは完了していません" in x
               for x in output["not_determinable"]), \
        "黙って消すと「周辺に施設は無い」と読まれます"
    assert sources == []


def test_step1_drops_a_facility_whose_url_was_never_retrieved(dataset, monkeypatch):
    """モデルは実在しそうな URL を書けます。取得した集合に無ければ出典ではない。

    ただし**段は落としません**（STEP2 の外部事実と同じ扱い）。落としたことは
    note に残します。
    """
    from kaigyou_intel.steps import step1_features

    _stub_step1(monkeypatch, _scan_result(),
                llm.Result(parsed=_with_surroundings("https://invented.example.com/x"),
                           usage=llm.Usage(), model="m"))
    output, _usage, sources = step1_features.run(step1_features.build_input(dataset))

    assert output["surroundings"]["facilities"] == []
    assert "〇〇モール" in output["surroundings"]["note"]
    assert "出典を確かめられなかった" in output["surroundings"]["note"]
    # 引用されなかった URL も記録には残ります。ただし印は付きません。
    assert [s["pattern_id"] for s in sources] == [None]


def test_step1_marks_the_urls_a_facility_actually_cited(dataset, monkeypatch):
    """出典一覧に載るのは印の付いたものだけ。付けないと、本文が施設名を
    書いているのに出典がどこにも出ません。"""
    from kaigyou_intel.steps import step1_features

    _stub_step1(monkeypatch, _scan_result(),
                llm.Result(parsed=_with_surroundings(), usage=llm.Usage(), model="m"))
    output, _usage, sources = step1_features.run(step1_features.build_input(dataset))

    assert output["surroundings"]["setting"] == "商業施設内・隣接"
    assert [s["pattern_id"] for s in sources] == ["周辺施設"]


def test_the_surroundings_scan_can_be_switched_off(dataset, monkeypatch):
    """唯一の「毎回必ず走る検索」なので、設定 1 行で切れること。

    レート制限に当たったときや、外部に出られない環境で試すときに、切る口が
    無いと段ごと落ちます。
    """
    from kaigyou_intel.steps import step1_features

    assert step1_features.surroundings_searches({"surroundings_searches": 0}) == 0
    scan = step1_features.scan_surroundings({}, {"surroundings_searches": 0})
    assert not scan.usable
    assert "0" in (scan.error or "")


def test_the_surroundings_scan_is_not_given_the_statistics(dataset):
    """統計を渡すと、検索せずに手元の数字を言い換えたものが「周辺にはこういう
    施設がある」として返ってきます（STEP2 で実際にそうなりました）。

    駅名は渡します。緯度経度だけでは検索の取っかかりがありません。
    """
    from kaigyou_intel.steps import step1_features

    payload = step1_features.build_input(dataset)
    asked = step1_features.scan_input(payload)
    assert set(asked) == {"prefecture", "municipality", "address", "nearby_address",
                          "lat", "lng", "radius_m", "nearest_station",
                          "stations_in_radius"}
    assert "measures" not in asked and "demand" not in asked


def test_the_scan_is_told_which_side_of_the_station_the_pin_is_on(dataset):
    """実測（沼津駅前）のレポートは「候補地が南口側・北口側のどちらに位置する
    のかは基礎データからは特定できていない」と書きました。ピンは明確に南側に
    ありました。**特定できていなかったのは、計算していなかったからです。**

    駅の座標は S12 にあり、候補地の座標は利用者が置いたピンそのものです。
    """
    from kaigyou_intel.steps import step1_features

    payload = step1_features.build_input(dataset)
    heading = (payload.get("access") or {}).get("direction_from_station")
    if not heading:
        # 駅が取り込まれていない環境。方角が無いことは黙って通します。
        assert (payload.get("access") or {}).get("nearest_station", {}).get("name") \
            in (None, "")
        return
    assert heading["compass"] in _ALL_COMPASS
    assert 0 <= heading["bearing_deg"] < 360
    # **出口の名前まで断定させません。** 「南口」が西側にある駅は実在します。
    assert "出口の名前は別の話" in heading["note"]
    assert step1_features.scan_input(payload)["nearest_station"]["direction"] == heading


_ALL_COMPASS = ("北", "北北東", "北東", "東北東", "東", "東南東", "南東", "南南東",
                "南", "南南西", "南西", "西南西", "西", "西北西", "北西", "北北西")


def test_the_bearing_is_measured_from_the_station_to_the_pin():
    """向きを取り違えると、南の候補地が「駅の北」になります。"""
    from kaigyou_core.dataset import _COMPASS, _bearing

    def compass(a, b, c, d):
        return _COMPASS[int((_bearing(a, b, c, d) + 11.25) % 360 // 22.5)]

    # 駅(35.10, 138.86) から見て、南にある候補地は「南」。
    assert compass(35.10, 138.86, 35.09, 138.86) == "南"
    assert compass(35.10, 138.86, 35.11, 138.86) == "北"
    assert compass(35.10, 138.86, 35.10, 138.87) == "東"
    assert compass(35.10, 138.86, 35.10, 138.85) == "西"
    # 実測（沼津駅前）。ピンは駅の南側にあります。
    assert compass(35.1036, 138.8615, 35.101942, 138.861033).startswith("南")


def test_the_scan_gets_a_place_name_it_can_actually_search(dataset):
    """緯度経度では検索が当たりません。候補地そのものの住所は手元に無い
    （利用者が置いたのは座標）ので、いちばん近い地価公示の標準地の住所を
    渡します。**候補地の住所ではないことを、距離と注記で示します。**
    """
    from kaigyou_intel.steps import step1_features

    payload = step1_features.build_input(dataset)
    nearby = step1_features.scan_input(payload)["nearby_address"]
    if nearby is None:
        assert not (payload.get("cost") or {}).get("nearest_points")
        return
    assert nearby["address"]
    assert nearby["distance_m"] is not None, "距離が無いと候補地の住所に見えます"
    assert "候補地の住所ではありません" in nearby["note"]


def test_the_surroundings_reach_every_later_step(dataset):
    """立地類型は数字の読み方を決める前提です。**後段に届かないと意味が
    ありません。** STEP2 に届かなければ同じ施設を調べ直し、STEP3 に届かなければ
    商業施設の商圏を徒歩圏として判断します。
    """
    from kaigyou_intel.projection import for_step2, for_step3, for_step4

    step1 = _with_surroundings().model_dump()
    limits = cfg.analysis_config()["limits"]
    assert for_step2(step1, dataset, limits)["surroundings"]["setting"] \
        == "商業施設内・隣接"
    step2 = {"external_facts": [], "hypotheses": [], "unanswered": []}
    assert for_step3(step1, step2, dataset)["step1"]["surroundings"]
    assert for_step4(step1, step2, {}, dataset)["step1"]["surroundings"]


def test_the_report_shows_the_setting_and_what_it_changes():
    """商業施設のテナントで、半径1km の人口をそのまま商圏として読むのが
    いちばん大きな読み違いです。表の隣で1回言います。"""
    from kaigyou_intel.report import _surroundings_block

    text = "\n".join(_surroundings_block(_with_surroundings().model_dump()["surroundings"]))
    assert "商業施設内・隣接" in text
    assert "半径で測った円ではありません" in text
    assert "〇〇モール" in text and "約120店舗" in text
    # 規模が空のときに 0 に見えないこと。
    blank = _with_surroundings().model_dump()["surroundings"]
    blank["facilities"][0]["scale"] = None
    assert "0 ではなく" in "\n".join(_surroundings_block(blank))
    # スキャンが動かなかったときは節ごと出しません。空の表は「周辺に施設が
    # 無い」に見えます。
    assert _surroundings_block(None) == []
    assert _surroundings_block({"setting": "", "facilities": []}) == []


def test_step1_refuses_an_output_whose_references_do_not_resolve(dataset, monkeypatch):
    """参照が切れた出力は保存しない。レポートの末尾まで残ると追跡が切れます。"""
    from kaigyou_intel.steps import step1_features

    broken = _output()
    broken.patterns[0].evidence = ["F001", "F404"]
    monkeypatch.setattr(llm, "ask", lambda **kw: llm.Result(
        parsed=broken, usage=llm.Usage(), model="m"))

    with pytest.raises(step1_features.StepFailed, match="F404"):
        step1_features.run(step1_features.build_input(dataset))


# ----------------------------------------------- PowerShell から使えること
def test_a_job_can_be_created_without_an_http_client():
    """ジョブ作成が CLI にもあること。

    PowerShell の `curl` は Invoke-WebRequest の別名で -X を受け付けません。
    このプロジェクトの操作は全部 `python -m kaigyou_etl` で済むので、
    ジョブ作成だけ HTTP クライアントを要求すると、そこが最初の障害になります。
    """
    import argparse

    from kaigyou_etl.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["new-analysis", "--lat", "35.6717", "--lng", "139.765",
                              "--name", "銀座4丁目", "--profile", "pediatric"])
    assert isinstance(args, argparse.Namespace)
    assert (args.lat, args.lng, args.radius) == (35.6717, 139.765, 1000)
    assert args.func.__name__ == "cmd_new_analysis"


def test_the_dry_run_can_target_one_job():
    """作った直後のジョブを見たいことがある。worker の順番とは別。"""
    from kaigyou_etl.cli import build_parser

    args = build_parser().parse_args(
        ["analyze", "--dry-run", "step1.txt", "--job", "abc-123"])
    assert args.dry_run == "step1.txt" and args.job == "abc-123"


# ------------------------------------------------------------ モデルの契約
#
# Sonnet 5 / Opus 5 系では、以前よく書かれていた指定のいくつかが**削除**され、
# 送ると 400 で落ちます。「前はこう書いた」で戻ってこないように固定します。

def test_the_configured_model_is_a_current_one():
    """モデルIDは実在するものだけ。日付サフィックスは付けない。"""
    known = {"claude-sonnet-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
             "claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5",
             "claude-fable-5"}
    config = cfg.analysis_config()
    assert config["model"]["id"] in known, f"未知のモデル: {config['model']['id']}"
    for number, step in config["steps"].items():
        if step.get("model"):
            assert step["model"] in known, f"step{number} のモデルが未知です"


def test_effort_is_one_of_the_documented_levels():
    config = cfg.analysis_config()
    levels = {"low", "medium", "high", "xhigh", "max"}
    assert config["model"]["effort"] in levels


def test_we_never_send_the_parameters_that_were_removed():
    """budget_tokens / temperature などは 400 になります。

    思考の深さは adaptive thinking と effort で決めます。固定のトークン予算と
    いう考え方はもうありません。
    """
    from kaigyou_intel import client as llm

    source = Path(llm.__file__).read_text(encoding="utf-8")
    request_block = source[source.index('request: dict[str, Any] = {'):
                            source.index('declared = list(tools or [])')]
    for parameter in llm.REMOVED_PARAMETERS:
        assert f'"{parameter}"' not in request_block, (
            f"{parameter} を送っています。このモデルでは 400 になります")


def test_thinking_is_adaptive():
    """このモデル系での唯一の有効な指定。"""
    from kaigyou_intel import client as llm

    source = Path(llm.__file__).read_text(encoding="utf-8")
    assert '"thinking": {"type": "adaptive"}' in source


def test_a_refusal_is_reported_rather_than_read_as_an_empty_answer(monkeypatch):
    """拒否は例外ではなく HTTP 200 で返る。

    content を読む前に stop_reason を確かめないと、空の応答を
    「モデルが何も見つけなかった」と取り違えます。
    """
    from kaigyou_intel import client as llm

    class _Details:
        category, explanation = "cyber", "declined"

    class _Message:
        stop_reason, stop_details, content = "refusal", _Details(), []
        usage = type("U", (), {"input_tokens": 10, "output_tokens": 0})()
        model = "claude-sonnet-5"

    message = _Message()
    message.parsed_output = None
    monkeypatch.setattr(llm, "_client", lambda: _stub_client(message))
    with pytest.raises(llm.Refused, match="cyber"):
        llm.ask(step_number=1, system="s", user="u", schema=Step1Output)


# --------------------------------------------- 送信する本体そのものを検算する
#
# 最初の実装は Pydantic の**クラス**を output_config.format に入れていて、
# 送信の瞬間に `Object of type ModelMetaclass is not JSON serializable` で
# 落ちました。呼び出しごとスタブしたテストでは、その形を一度も見ていません
# でした。以下は API キー無しで、本体の形だけを確かめます。

def test_the_request_body_can_actually_be_serialised():
    """これが通らなければ、キーがあっても送信で落ちます。"""
    from kaigyou_intel.client import build_request

    for step_number in (1, 2, 3, 4):
        body = build_request(step_number, "system", "user")
        json.dumps(body, ensure_ascii=False)   # 落ちたらそこが原因


def test_the_schema_never_goes_into_output_config():
    """スキーマは output_format に渡し、SDK が output_config へマージします。

    自分で output_config.format に入れると、型がそのまま送信されます。
    """
    from kaigyou_intel.client import build_request

    body = build_request(1, "system", "user")
    assert "format" not in body["output_config"]
    assert body["output_config"]["effort"] in {"low", "medium", "high", "xhigh", "max"}


def test_output_format_must_be_a_type_not_a_dict():
    """SDK の契約。ここを取り違えたのが今回の原因なので、明示的に置きます。"""
    anthropic = pytest.importorskip("anthropic")
    import inspect

    source = inspect.getsource(anthropic.resources.messages.messages.Messages.parse)
    assert "`output_format` must be a type" in source, (
        "SDK の契約が変わった可能性があります。client.ask の呼び方を確認してください")


def test_step1_sends_no_tools_and_step2_sends_web_search():
    """要件 §38：外部コンテクスト調査を STEP2 に限定する。

    設定だけでなく、組み立てた本体でも確かめます。設定が正しくても
    組み立てが無視していたら意味がありません。
    """
    from kaigyou_intel.client import build_request

    assert "tools" not in build_request(1, "s", "u")
    assert [t["type"] for t in build_request(2, "s", "u")["tools"]] == \
        ["web_search_20260209"]
    for step_number in (3, 4):
        assert "tools" not in build_request(step_number, "s", "u")


def _stub_client(message, seen: dict | None = None):
    """SDK の呼び出し口だけを模した最小のクライアント。

    ストリームの context manager までは真似ます。ここを省いて ``ask`` ごと
    差し替えたせいで、送信する本体を一度も見ないまま
    ``ModelMetaclass is not JSON serializable`` を出荷しました。
    """
    import contextlib

    class _Stream:
        @staticmethod
        def get_final_message():
            return message

    class _Client:
        class messages:
            @staticmethod
            @contextlib.contextmanager
            def stream(**kwargs):
                if seen is not None:
                    seen.update(kwargs)
                yield _Stream()

    return _Client()


def test_the_structured_call_passes_the_schema_as_output_format(monkeypatch):
    """呼び方を固定する。

    スキーマは型のまま ``output_format`` へ。``output_config.format`` に
    自分で入れると、型がそのまま送信されて落ちます。
    """
    from kaigyou_intel import client as llm

    seen: dict[str, object] = {}
    message = type("M", (), {})()
    message.parsed_output = _output()
    message.content, message.stop_reason = [], "end_turn"
    message.usage = type("U", (), {"input_tokens": 1, "output_tokens": 1})()
    message.model = "claude-sonnet-5"

    monkeypatch.setattr(llm, "_client", lambda: _stub_client(message, seen))
    llm.ask(step_number=1, system="s", user="u", schema=Step1Output)

    assert seen["output_format"] is Step1Output
    assert "format" not in seen["output_config"]
    json.dumps({k: v for k, v in seen.items() if k != "output_format"},
               ensure_ascii=False)


def test_every_call_is_streamed(monkeypatch):
    """SDK は「10 分を超えうる操作」を非ストリームで呼ぶと送信前に落とします。

    その境目は max_tokens で決まります。実測：16,000 → 24,000 に上げた途端、
    ValueError: Streaming is required for operations that may take longer than
    10 minutes。ストリームなら上限を気にせず上げられます。
    """
    from kaigyou_intel import client as llm

    message = type("M", (), {})()
    message.parsed_output = None
    message.content, message.stop_reason = [], "end_turn"
    message.usage = type("U", (), {"input_tokens": 1, "output_tokens": 1})()
    message.model = "claude-sonnet-5"

    client = _stub_client(message)
    assert not hasattr(client.messages, "parse"), (
        "parse() を使うと max_tokens しだいで送信前に落ちます")
    monkeypatch.setattr(llm, "_client", lambda: client)
    # スキーマ有り・無しのどちらもストリームで通ること。
    llm.ask(step_number=1, system="s", user="u", schema=Step1Output)
    llm.ask(step_number=2, system="s", user="u")


def test_the_step_output_can_be_read_without_an_http_client():
    """STEP の出力を読む手段があること。

    プロンプトを直すかどうかの判断はここを読んでするので、JSON を
    そのまま出すのでは足りません。
    """
    from kaigyou_etl.cli import build_parser

    args = build_parser().parse_args(["analyze", "--show"])
    assert args.show == ""          # 省略時は最新のジョブ
    args = build_parser().parse_args(["analyze", "--show", "abc-123"])
    assert args.show == "abc-123"


def test_the_cost_is_computed_from_the_recorded_usage():
    """使用量は保存してあるので、後から数えられること（要件 §34）。"""
    from kaigyou_etl.cli import _step_cost

    cost = _step_cost({"model": "claude-sonnet-5",
                       "input_tokens": 43102, "output_tokens": 3201})
    assert cost == pytest.approx(43102 / 1e6 * 2 + 3201 / 1e6 * 10)
    # 知らないモデルなら黙って 0 を返さない。
    assert _step_cost({"model": "unknown", "input_tokens": 1}) is None


# ------------------------------------------------------------------ STEP2
def _step1_for_step2() -> dict:
    return {"patterns": [
        {"id": "P001", "title": "子ども人口は薄いが小児歯科は多い",
         "evidence_summary": "0〜14歳は下位3.1%、小児歯科標榜は上位9%",
         "importance": "high",
         "research_questions": ["この地区で2015年以降に大規模な住宅供給があったか"]},
    ]}


def _step2_output(**overrides) -> "Step2Output":
    from kaigyou_intel.schemas import ExternalFact, Hypothesis, Step2Output

    data = {
        "external_facts": [ExternalFact(
            id="C001", pattern_id="P001",
            statement="中央区の年少人口は2015年から2020年に増加した",
            source_url="https://www.city.chuo.lg.jp/toukei/jinkou.html",
            source_title="中央区 人口統計", confidence="high")],
        "hypotheses": [Hypothesis(
            id="H001", pattern_id="P001",
            statement="タワーマンション供給が子育て世帯を呼び込んだ",
            status="SUPPORTED", evidence=["C001"],
            reasoning="区の統計で年少人口の増加が確認できる", confidence="medium",
            changes=["患者層", "診療コンセプト"],
            decision_impact="成人単独ではなく親子を一組として獲得する設計にする。")],
        "unanswered": ["小児歯科の開設年は確認できなかった"],
    }
    data.update(overrides)
    return Step2Output(**data)


def test_a_fabricated_source_url_is_caught():
    """モデルは実在しそうな URL を書けます。実在するかは検索結果で決まります。

    ここが STEP2 でいちばん重要な検算です。出典が実在しないレポートは、
    出典が無いレポートより悪い。読む人が確かめたつもりになるからです。
    """
    from kaigyou_intel.schemas import verify_step2

    output = _step2_output()
    problems = verify_step2(output, {"P001"},
                            {"https://www.city.chuo.lg.jp/kurashi/betsu.html"})
    assert any("検索結果に無い URL" in p.problem for p in problems)


def test_a_real_source_url_survives_the_usual_url_differences():
    """末尾のスラッシュや www の有無で本物の出典を落とさない。"""
    from kaigyou_intel.schemas import verify_step2

    output = _step2_output()
    assert verify_step2(output, {"P001"},
                        {"http://city.chuo.lg.jp/toukei/jinkou.html/"}) == []


def test_step2_references_must_resolve():
    from kaigyou_intel.schemas import Hypothesis, verify_step2

    urls = {"https://www.city.chuo.lg.jp/toukei/jinkou.html"}
    stray = _step2_output(hypotheses=[Hypothesis(
        id="H001", pattern_id="P404", statement="s", status="UNSUPPORTED",
        evidence=["C404"], reasoning="r", confidence="low",
        changes=["診療時間"],
        decision_impact="日曜ではなく平日夜間に診療枠を寄せる。")])
    problems = verify_step2(stray, {"P001"}, urls)
    assert any("P404" in p.problem for p in problems)
    assert any("C404" in p.problem for p in problems)


def test_a_hypothesis_that_changes_nothing_is_refused():
    """「正しくても次の一手が動かない仮説」を落とす。

    実測：「裾野駅西地区は区画整理により計画的に形成された市街地である」と
    いう仮説が出ました。正しくても、診療コンセプトも設備も診療時間も
    変わりません。知識であって仮説ではありません。
    """
    from pydantic import ValidationError

    from kaigyou_intel.schemas import Hypothesis

    # 変わるものを1つも挙げられない仮説は、スキーマが通しません。
    with pytest.raises(ValidationError):
        Hypothesis(id="H001", pattern_id="P001", statement="s",
                   status="SUPPORTED", evidence=["C001"], reasoning="r",
                   confidence="high", changes=[],
                   decision_impact="主要患者を居住者ではなく勤務者に置く。")


def test_a_decision_impact_that_restates_the_hypothesis_is_caught():
    """「AではなくBにする」の形で書かせる。

    「計画的に形成された市街地である」に対して「計画的に形成された市街地で
    あることが分かる」と書かれても、次の一手は1ミリも動きません。
    """
    from kaigyou_intel.schemas import Hypothesis, verify_step2

    statement = "裾野駅西地区は区画整理により計画的に形成された市街地である"
    echoed = _step2_output(hypotheses=[Hypothesis(
        id="H001", pattern_id="P001", statement=statement, status="SUPPORTED",
        evidence=["C001"], reasoning="r", confidence="high",
        changes=["立地判断"], decision_impact=statement)])
    problems = verify_step2(echoed, {"P001"},
                            {"https://www.city.chuo.lg.jp/toukei/jinkou.html"})
    assert any("仮説文と同じ" in p.problem for p in problems)


def test_a_decision_impact_that_is_barely_written_is_caught():
    """1行に満たない言い切りで済ませる、がいちばん多い抜け方でした。"""
    from kaigyou_intel.schemas import Hypothesis, verify_step2

    thin = _step2_output(hypotheses=[Hypothesis(
        id="H001", pattern_id="P001", statement="s", status="SUPPORTED",
        evidence=["C001"], reasoning="r", confidence="high",
        changes=["診療時間"], decision_impact="時間を変える")])
    problems = verify_step2(thin, {"P001"},
                            {"https://www.city.chuo.lg.jp/toukei/jinkou.html"})
    assert any("変わる先まで" in p.problem for p in problems)


def test_the_levers_in_the_config_and_the_schema_agree():
    """設定に書いてあるのにスキーマが受け付けない選択肢を作らない。"""
    from typing import get_args

    from kaigyou_intel.schemas import DecisionLever

    configured = cfg.hypotheses_config()["screening"]["levers"]
    assert list(get_args(DecisionLever)) == configured


def test_a_proxy_that_this_location_cannot_produce_is_not_offered():
    """**設定に書いた代理指標が、その地点では取れていないことがあります。**

    実例：開設年月日は医療機能情報提供制度の配布ファイルに列が無く、
    `clinic_vintage.*` はどの地点でも作られません。フィルタしないと、
    存在しないキーを使えと指示することになり、それを引いた FACT は検算で
    落ちて段ごとやり直しになります（1回 $1 前後）。

    ただし黙って消しません。消すと「そもそも見ていない」と「調べたが
    無かった」が区別できなくなります。
    """
    from kaigyou_intel.steps.step1_features import _factor_frame

    frame = cfg.hypotheses_config()
    text = _factor_frame(frame, {"population", "workers"})
    assert "手元に代理指標はありません" in text
    assert "clinic_vintage.median_year" in text, "消さずに、取れていないと書く"
    assert "使わないでください" in text
    # 外部で調べる道は残っていること。手元に無い＝諦める、ではありません。
    assert "歯科医師数・年齢構成" in text

    # 取れているものは、これまでどおり代理指標として出す。
    with_proxies = _factor_frame(frame, {"clinic_hours.sunday", "station_distance_m"})
    assert "`clinic_hours.sunday`" in with_proxies


def test_the_two_steps_see_the_same_picture_of_what_is_available(dataset):
    """片方だけ「代理指標あり」と書くと、STEP1 が立てた問いを STEP2 が
    別の前提で読むことになります。"""
    from kaigyou_intel.projection import citable_keys, for_step1, for_step2

    payload = for_step2({"patterns": []}, dataset, {"max_patterns": 4})
    assert set(payload["available_keys"]) == set(citable_keys(for_step1(dataset)))


def test_a_missing_opening_date_is_reported_as_a_gap_in_the_source(dataset):
    """「古い医院は0件」と「1件も分からなかった」は別のことです。

    そして、取り込みに失敗したのか、元のファイルに列が無いのかも別です。
    前者なら直せますが、後者は直せません。読む人が取り違えると、
    無い列を探して時間を使います。
    """
    vintage = dataset["competition"].get("vintage") or {}
    if vintage.get("available"):
        pytest.skip("この環境では開設年月日が取れています")
    assert vintage["reason"] == "no_opening_dates"
    assert "取り込みの失敗ではありません" in vintage["note"]


def test_the_timing_view_separates_running_from_waiting():
    """「実行 4分」と「所要 12分」が両方本当のことがあります。

    どちらが効いているかで、打つ手がまったく違います。段が遅いのか、
    段の間で cron を待っているのか。
    """
    from datetime import datetime, timedelta, timezone

    from kaigyou_etl.cli import _print_timing_summary

    start = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    steps = [
        {"started_at": start, "completed_at": start + timedelta(seconds=60)},
        # 4 分空いてから次の段（cron 待ち）。
        {"started_at": start + timedelta(seconds=300),
         "completed_at": start + timedelta(seconds=360)},
    ]
    import io
    import contextlib

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        _print_timing_summary(steps)
    text = buffer.getvalue()
    assert "所要 6分0秒" in text
    assert "実行 2分0秒" in text and "待ち 4分0秒" in text
    assert "cron を待っています" in text, "待ちのほうが長いときは、そう言う"


def test_a_report_that_never_says_what_to_build_is_refused():
    """**商圏の説明で終わらせないための最低線です。**

    実測：沼津駅前のレポートは、通勤者と前期高齢者という需要の読み分けまでは
    到達していましたが、ユニットを何台置くのか・床面積はいくら要るのか・
    衛生士は何人要るのかには触れていませんでした。それは商圏の話ではなく
    医院の話なので、商圏データだけを見ていると永久に出てきません。
    """
    from kaigyou_intel.schemas import SupportItem, verify_step4
    from kaigyou_intel.steps.step4_report import required_categories

    needed = required_categories()
    assert needed, "設定が空だと、この検査は効きません"

    only_staff = _report_output(support_needed=[SupportItem(
        item="平日夜間まで回せる人員体制",
        why="勤務者を主に据えると、受診は勤務前後に寄ります。",
        evidence=["M001"], category="人員")])
    problems = verify_step4(only_staff, _REPORT_IDS, _REPORT_NUMBERS, needed)
    assert any("物件" in p.problem for p in problems)
    assert any("設備" in p.problem for p in problems)
    # 埋まっている分類は指摘しない。
    assert not any("「人員」について" in p.problem for p in problems)


def test_a_report_that_covers_the_dental_requirements_passes():
    from kaigyou_intel.schemas import SupportItem, verify_step4
    from kaigyou_intel.steps.step4_report import required_categories

    complete = _report_output(support_needed=[
        SupportItem(item="ユニット4台を置ける診療室",
                    why="かかりつけ中心ならリコール枠が要ります。",
                    evidence=["M001"], category="設備"),
        SupportItem(item="1階かエレベーターのある物件",
                    why="65歳以上の比率が高く、階段は来院の障壁になります。",
                    evidence=["F001"], category="物件"),
        SupportItem(item="夜間まで回せる歯科衛生士の確保",
                    why="日曜・夜間を軸にするなら、そこで働く人が前提です。",
                    evidence=["M001"], category="人員"),
    ])
    assert verify_step4(complete, _REPORT_IDS, _REPORT_NUMBERS,
                        required_categories()) == []


def test_the_dental_checklist_reaches_both_the_judgement_and_the_report():
    """判断する段と書く段の両方に同じ枠を渡します。

    片方だけに書くと、STEP3 が決めていないことを STEP4 が書こうとして、
    そこで新しい判断が生まれます。最終段は書き直す段であって、判断し直す
    段ではありません。
    """
    from kaigyou_intel.steps.step1_features import requirement_frame

    frame = cfg.hypotheses_config()
    ids = [r["id"] for r in frame["requirements"]]
    assert {"chairs", "floor_area", "parking", "hygienists"} <= set(ids)

    text = requirement_frame(frame)
    assert "ユニット" in text and "歯科衛生士" in text and "駐車場" in text
    # 分類は support_needed の語と一致していること。一致しないと、埋めても
    # 検査が「空だ」と言い続けます。
    from typing import get_args

    from kaigyou_intel.schemas import SupportItem

    allowed = set(get_args(SupportItem.model_fields["category"].annotation))
    assert {r["category"] for r in frame["requirements"]} <= allowed
    assert set(frame["required_support_categories"]) <= allowed

    for name in ("step3_demand.md", "step4_client_report.md"):
        assert "{dental_requirements}" in cfg.prompt_text(name), name


def test_the_checklist_says_what_the_data_cannot_settle():
    """無いものを無いと書かせる。**書かずに省くのがいちばん悪い。**

    省くと、調べたうえで不要と判断した、と読まれます。来院手段も駐車場の
    相場も取り込んでいません。
    """
    frame = cfg.hypotheses_config()
    parking = next(r for r in frame["requirements"] if r["id"] == "parking")
    assert "データはありません" in parking["note"]
    chairs = next(r for r in frame["requirements"] if r["id"] == "chairs")
    assert "断定しないこと" in chairs["note"]


def test_the_qualitative_factors_are_a_frame_not_an_answer():
    """統計にも外部資料にも無いことを、事実として書かせない。

    「この地域はデンタルIQが高い」はどこにも書いていません。枠として
    渡すのは、**調べる対象**を示すためであって、答えを渡すためでは
    ありません。
    """
    from kaigyou_intel.steps.step1_features import _factor_frame

    frame = cfg.hypotheses_config()
    ids = [f["id"] for f in frame["factors"]]
    assert {"dental_iq", "succession", "staffing"} <= set(ids)

    text = _factor_frame(frame)
    assert "強い根拠として使わないでください" in text
    # 代理指標は、引けるキーで書かれていること。引けないキーを勧めると、
    # そのキーを書いた FACT は検算で落ちます。
    from kaigyou_core.measures import LAYERS, MEASURE_SPECS

    for factor in frame["factors"]:
        for proxy in factor.get("proxies") or []:
            key = proxy["key"]
            assert key in MEASURE_SPECS or "." in key, key
    assert set(LAYERS) >= {"competition_offer", "future", "economy"}


def test_a_contradicted_hypothesis_is_kept():
    """要件 §11：否定された仮説も保存する。

    「調べたが違った」は、調べていないのとは別の情報です。落とすと STEP3 が
    同じ筋を追い直します。
    """
    from kaigyou_intel.schemas import EvidenceLink, Hypothesis, verify_step2

    output = _step2_output(hypotheses=[Hypothesis(
        id="H001", pattern_id="P001", statement="再開発が原因である",
        status="CONTRADICTED",
        evidence=["C001"],
        evidence_links=[EvidenceLink(
            fact_id="C001", stance="contradicts",
            note="区の資料では当該期間の大規模開発は確認できなかった")],
        reasoning="区の資料では当該期間の大規模開発は確認できなかった",
        confidence="medium", changes=["立地判断"],
        decision_impact="再開発による流入を前提にせず、現在の居住者だけで"
                        "成り立つ規模で計画する。")])
    assert verify_step2(output, {"P001"},
                        {"https://www.city.chuo.lg.jp/toukei/jinkou.html"}) == []
    assert output.model_dump()["hypotheses"][0]["status"] == "CONTRADICTED"


def test_the_old_verdict_still_loads_but_is_not_accepted_from_the_model():
    """**古い保存済みレポートは読めること。ただし新しい出力では使わせない。**

    `UNSUPPORTED` は「調べたが支持が見つからなかった」と「調べたら違うと
    分かった」を兼ねていました。経営判断にとってこの 2 つは正反対です
    （前者は現地で確かめる、後者は別の筋を追う）。語彙は分けましたが、
    すでに保存されたレポートを読めなくするわけにはいきません。
    """
    from kaigyou_intel.schemas import Hypothesis, Step2Output, verify_step2

    # 読み出しは通ること（古いジョブの再表示）。
    old = Step2Output.model_validate({
        "hypotheses": [{
            "id": "H001", "pattern_id": "P001", "statement": "s",
            "status": "UNSUPPORTED", "evidence": ["C001"], "reasoning": "r",
            "confidence": "low", "changes": ["立地判断"],
            "decision_impact": "AではなくBにする"}]})
    assert old.hypotheses[0].status == "UNSUPPORTED"

    # 新しい出力としては通らないこと。
    problems = verify_step2(_step2_output(hypotheses=[Hypothesis(
        id="H001", pattern_id="P001", statement="s", status="UNSUPPORTED",
        evidence=["C001"], reasoning="r", confidence="low",
        changes=["立地判断"], decision_impact="AではなくBにする")]),
        {"P001"}, {"https://www.city.chuo.lg.jp/toukei/jinkou.html"})
    assert any("使わなくなった判定" in p.problem for p in problems), problems


def test_step2_searches_once_and_then_writes_it_down(monkeypatch):
    """検索する呼び出しと、JSON に写す呼び出しを分ける。

    Web検索（サーバ側ツール）と構造化出力は同じ呼び出しでは併用しません。
    2 回目に検索を残すと、1 回目に無かった事実が増えて、出典の照合が
    意味を失います。
    """
    from kaigyou_intel.steps import step2_research

    calls: list[dict] = []

    def fake_ask(*, step_number, system, user, schema=None, tools=None,
                 web_search=None, effort=None, max_uses=None):
        calls.append({"system": system, "user": user, "schema": schema,
                      "web_search": web_search})
        if schema is None:
            return llm.Result(
                parsed=None, text="中央区の年少人口は増加していました。",
                usage=llm.Usage(input_tokens=500, output_tokens=800, web_searches=3),
                model="claude-sonnet-5",
                sources=[{"url": "https://www.city.chuo.lg.jp/toukei/jinkou.html",
                          "title": "中央区 人口統計", "page_age": None},
                         {"url": "https://www.e-stat.go.jp/x", "title": "e-Stat",
                          "page_age": None}])
        return llm.Result(parsed=_step2_output(),
                          usage=llm.Usage(input_tokens=700, output_tokens=400),
                          model="claude-sonnet-5")

    monkeypatch.setattr(llm, "ask", fake_ask)
    payload = step2_research.build_input(_step1_for_step2(), {"location": {"name": "銀座"}})
    output, usage, sources = step2_research.run(payload)

    assert len(calls) == 2
    assert calls[0]["schema"] is None and calls[0]["web_search"] is None
    assert calls[1]["schema"] is not None
    assert calls[1]["web_search"] is False, "2 回目で検索を切っていない"
    # 取得した URL の一覧を 2 回目に見せること。無いものを書かせないため。
    assert "https://www.e-stat.go.jp/x" in calls[1]["user"]
    # 上限がプロンプトに埋まっていること。
    assert "{searches_per_pattern}" not in calls[0]["system"]

    assert usage.input_tokens == 1200 and usage.output_tokens == 1200
    assert usage.web_searches == 3
    assert output["hypotheses"][0]["status"] == "SUPPORTED"

    # 出典は「どの PATTERN を調べていて出てきたか」まで残す（§25）。
    cited = {s["url"]: s["pattern_id"] for s in sources}
    assert cited["https://www.city.chuo.lg.jp/toukei/jinkou.html"] == "P001"
    assert cited["https://www.e-stat.go.jp/x"] is None, "引用されなかった出典も残す"


def _many_patterns(count: int) -> dict:
    return {"patterns": [
        {"id": f"P{i:03d}", "title": "t", "evidence_summary": "s",
         "importance": "high", "research_questions": ["q"]}
        for i in range(1, count + 1)]}


def test_step2_researches_each_pattern_in_its_own_call(monkeypatch):
    """**1 本にまとめると、検索ループが 1 本の中で直列に回ります。**

    サーバ側の検索は、増えていく文脈を毎回読み直します。実測（沼津・4検索）
    で入力 794,572 トークン・5分36秒。レポート1本 11分のうち、この段だけで
    半分を使っていました。

    調べる中身は PATTERN ごとに独立しているので、分けても答えは変わりません。
    変わるのは待ち時間で、直列の合計からいちばん遅い1本になります。
    """
    from kaigyou_intel.steps import step2_research

    seen: list[str] = []

    def fake_ask(*, step_number, system, user, schema=None, tools=None,
                 web_search=None, effort=None, max_uses=None):
        if schema is None:
            # 1 本に渡されたのは 1 つの PATTERN だけであること。
            assert user.count('"id"') == 1, "PATTERN をまとめて渡していないこと"
            seen.append(user)
            pattern_id = json.loads(
                user.split("```json", 1)[1].rsplit("```", 1)[0])["pattern"]["id"]
            return llm.Result(
                parsed=None, text=f"{pattern_id} を調べました。",
                usage=llm.Usage(input_tokens=100, output_tokens=50, web_searches=2),
                model="m",
                sources=[{"url": "https://www.city.chuo.lg.jp/toukei/jinkou.html",
                          "title": "中央区 人口統計"}])
        # 4 本ぶんの本文が、入力どおりの順で 1 つにまとまっていること。
        body = user.split("## 今回の検索", 1)[0]
        assert body.index("P001") < body.index("P002") < body.index("P003")
        return llm.Result(parsed=_step2_output(), usage=llm.Usage(), model="m")

    monkeypatch.setattr(llm, "ask", fake_ask)
    payload = step2_research.build_input(_many_patterns(3), {"location": {}})
    _output, usage, _sources = step2_research.run(payload)

    assert len(seen) == 3, "PATTERN ごとに 1 本"
    # 使用量は全部の合計。分けたぶんを数え落とすと、費用が実際より安く見えます。
    assert usage.web_searches == 6 and usage.output_tokens == 150


def test_step2_gives_each_call_only_its_own_share_of_the_searches(monkeypatch):
    """全体の上限をそのまま渡すと、1 本が全部使い切れてしまいます。"""
    from kaigyou_intel.steps import step2_research

    budgets: list[int | None] = []

    def fake_ask(*, step_number, system, user, schema=None, tools=None,
                 web_search=None, effort=None, max_uses=None):
        if schema is None:
            budgets.append(max_uses)
            return llm.Result(parsed=None, text="調べました。",
                              usage=llm.Usage(), model="m",
                              sources=[{"url": "https://www.city.chuo.lg.jp/toukei/jinkou.html"}])
        return llm.Result(parsed=_step2_output(), usage=llm.Usage(), model="m")

    monkeypatch.setattr(llm, "ask", fake_ask)
    step2_research.run(step2_research.build_input(_many_patterns(2), {"location": {}}))
    per_pattern = cfg.analysis_config()["limits"]["searches_per_pattern"]
    assert budgets == [per_pattern, per_pattern]


def test_step2_does_not_throw_away_three_good_calls_because_one_failed(monkeypatch):
    """4 本のうち 1 本が落ちただけで、通った 3 本ぶんの検索と時間を捨てるのは
    割に合いません。**ただし黙って捨てません。**"""
    from kaigyou_intel.steps import step2_research

    def fake_ask(*, step_number, system, user, schema=None, tools=None,
                 web_search=None, effort=None, max_uses=None):
        if schema is None:
            if "P002" in user:
                raise RuntimeError("upstream hiccup")
            return llm.Result(parsed=None, text="調べました。",
                              usage=llm.Usage(), model="m",
                              sources=[{"url": "https://www.city.chuo.lg.jp/toukei/jinkou.html"}])
        return llm.Result(parsed=_step2_output(), usage=llm.Usage(), model="m")

    monkeypatch.setattr(llm, "ask", fake_ask)
    output, _usage, _sources = step2_research.run(
        step2_research.build_input(_many_patterns(3), {"location": {}}))
    unanswered = " ".join(output["unanswered"])
    assert "P002" in unanswered and "upstream hiccup" in unanswered


def test_step2_stops_when_every_call_failed(monkeypatch):
    """全部落ちたのを「外部情報が見つからなかった」と記録しない。

    取り違えると、次に読む人が調査済みだと思います。
    """
    from kaigyou_intel.steps import step2_research

    def fake_ask(**kwargs):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(llm, "ask", fake_ask)
    with pytest.raises(step2_research.StepFailed, match="upstream down"):
        step2_research.run(
            step2_research.build_input(_many_patterns(2), {"location": {}}))


def test_step2_fails_only_when_nothing_survives_verification(monkeypatch):
    """出典を確かめられた外部事実がひとつも残らないなら、それは調査ではありません。

    1件だけなら落として続けます（上の _drop_unverifiable）。全部なら止めます。
    """
    from kaigyou_intel.steps import step2_research

    def fake_ask(*, step_number, system, user, schema=None, tools=None,
                 web_search=None, effort=None, max_uses=None):
        if schema is None:
            return llm.Result(parsed=None, text="調べました。", usage=llm.Usage(),
                              model="m", sources=[{"url": "https://example.com/a"}])
        return llm.Result(parsed=_step2_output(), usage=llm.Usage(), model="m")

    monkeypatch.setattr(llm, "ask", fake_ask)
    payload = step2_research.build_input(_step1_for_step2(), {"location": {}})
    with pytest.raises(step2_research.StepFailed, match="ひとつも残りませんでした"):
        step2_research.run(payload)


def test_the_search_call_declares_the_web_search_tool_and_the_write_up_does_not():
    """設定で web_search を切り替えられること。送信する本体で確かめます。"""
    tooled = llm.build_request(2, "s", "u")
    assert tooled["tools"][0]["type"] == llm.WEB_SEARCH_TOOL_TYPE
    assert tooled["tools"][0]["max_uses"] == cfg.analysis_config()["limits"][
        "max_searches_total"]
    assert "tools" not in llm.build_request(2, "s", "u", web_search=False)
    # 本体はそのまま JSON にできること（ModelMetaclass 事件の再発防止）。
    json.dumps(tooled)


def test_step2_input_is_built_from_the_stored_step1_output(conn, dataset):
    """worker は記憶を持たない。前段の出力は DB から読む。"""
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    jobs.start_step(conn, job_id, 1, {}, {"prompt_version": "v", "model": "m"})
    jobs.finish_step(conn, job_id, 1, _step1_for_step2(), {})

    job = jobs.get_job(conn, job_id, include_base_data=True)
    payload = worker.build_input(conn, job, 2)
    assert payload["patterns"][0]["id"] == "P001"
    assert "measures" not in payload, "STEP2 に基礎データを渡してはいけない"
    conn.rollback()


def test_step2_will_not_start_before_step1_has_produced_patterns(conn, dataset):
    """PATTERN が無い状態で検索させると、地域紹介が返ってきます。"""
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    job = jobs.get_job(conn, job_id, include_base_data=True)
    with pytest.raises(worker.StepNotImplemented, match="STEP1 の出力"):
        worker.build_input(conn, job, 2)
    conn.rollback()


def test_a_search_that_could_not_run_is_not_reported_as_nothing_found(monkeypatch):
    """検索が動かなかったことと、調べて見つからなかったことは別。

    サーバ側ツールのエラーは例外ではなく content の中身として HTTP 200 で
    返ります。空の結果として扱うと、「調査済み・該当なし」がレポートに残ります。
    """
    from kaigyou_intel.steps import step2_research

    monkeypatch.setattr(llm, "ask", lambda **kw: llm.Result(
        parsed=None, text="検索できませんでした。", usage=llm.Usage(), model="m",
        sources=[{"error": "max_uses_exceeded"}]))
    payload = step2_research.build_input(_step1_for_step2(), {"location": {}})
    with pytest.raises(step2_research.StepFailed, match="Web検索が実行できません"):
        step2_research.run(payload)


# --------------------------------------------------- 止まったジョブを再開できる
def test_a_job_that_stops_does_not_stay_running_forever(conn, dataset, monkeypatch):
    """実測：銀座のジョブが running のまま残り、0 件が出続けました。

    claim_job は queued しか見ません。走り終わった Job の状態を戻していないと、
    二度と拾われないまま「待っているジョブはありません」になります。
    """
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    unimplemented = _pretend_the_last_step_is_unimplemented(monkeypatch)
    _complete_the_steps_before(conn, job_id, unimplemented)
    assert jobs.claim_specific(conn, job_id) == job_id  # ここで running になる

    try:
        assert worker.run_job(conn, job_id) == "blocked"
        # running のままにしない。ただし queued にも戻しません。戻すと worker が
        # 同じ Job を拾っては同じところで止まる、を繰り返します。
        # claim_job は queued しか見ないので、blocked は拾われません。
        assert jobs.get_job(conn, job_id)["status"] == "blocked"

        # 止まっていたステップを実装した体で、自動的に待ち行列へ戻ること。
        blocked_at = jobs.next_step(conn, job_id)
        assert jobs.requeue_unblocked(conn, set(worker.RUNNERS)) == 0, "まだ実装していない"
        assert jobs.requeue_unblocked(conn, {blocked_at}) >= 1
        assert jobs.get_job(conn, job_id)["status"] == "queued"
    finally:
        _drop_job(job_id)


def test_a_finished_job_is_marked_completed(conn, dataset, monkeypatch):
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    for number in sorted(jobs.STEP_NAMES):
        jobs.start_step(conn, job_id, number, {}, {"prompt_version": "v", "model": "m"})
        jobs.finish_step(conn, job_id, number, {"n": number}, {})
    jobs.claim_specific(conn, job_id)

    try:
        assert worker.run_job(conn, job_id) == "completed"
        job = jobs.get_job(conn, job_id)
        assert job["status"] == "completed" and job["completed_at"] is not None
    finally:
        _drop_job(job_id)


def test_a_failed_job_is_not_picked_up_again_on_its_own(conn, dataset, monkeypatch):
    """壊れたまま自動で拾い直すと、同じ失敗に何度も課金されます。"""
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    jobs.claim_specific(conn, job_id)
    monkeypatch.setattr(worker, "RUNNERS", {1: _boom})

    try:
        assert worker.run_job(conn, job_id) == "failed"
        # failed は claim_job の対象外。壊れたまま自動で拾い直すと、同じ失敗に
        # 何度も課金されます。
        assert jobs.get_job(conn, job_id)["status"] == "failed"
        # 人が名指しすれば再開できること。ここが無いと詰みます。
        assert jobs.claim_specific(conn, job_id) == job_id
    finally:
        _drop_job(job_id)


def _boom(_payload, _category=None):
    raise RuntimeError("模擬的な失敗")


def _pretend_the_last_step_is_unimplemented(monkeypatch) -> int:
    """最後のステップを未実装ということにして、その番号を返す。

    4 段とも実装済みになったので、未実装の扱いは実物では作れません。ですが
    「未実装で止まった Job をどうするか」は仕組みの話で、次に段を足すときにも
    要ります。RUNNERS を差し替えて、その状況だけを作ります。
    """
    from kaigyou_intel import worker

    number = max(worker.RUNNERS)
    monkeypatch.setattr(
        worker, "RUNNERS", {k: v for k, v in worker.RUNNERS.items() if k != number})
    return number


def _complete_the_steps_before(conn, job_id: str, number: int) -> None:
    from kaigyou_intel import jobs

    for step in range(1, number):
        jobs.start_step(conn, job_id, step, {},
                        {"prompt_version": "v", "model": "m"})
        jobs.finish_step(conn, job_id, step, _step1_for_step2(), {})


def _drop_job(job_id: str) -> None:
    """このテストが commit した行を片づける。

    jobs の関数は自分で commit します（worker が落ちても状態が残るように）。
    そのぶん、テスト側は rollback では消せません。
    """
    from kaigyou_core.db import connect

    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM analysis_jobs WHERE id = %s", (job_id,))
        conn.commit()


def test_retrying_from_a_step_is_available_from_the_command_line():
    """PowerShell から止まったジョブを再開できること。"""
    from kaigyou_etl.cli import build_parser

    args = build_parser().parse_args(
        ["analyze", "--job", "abc", "--retry", "1"])
    assert (args.job, args.retry) == ("abc", 1)


# ------------------------------------- 空振りを「事実」として記録させない
def test_the_prompts_say_where_a_missing_source_belongs():
    """実測：EXTERNAL FACT 12件のうち6件が「その資料には載っていない」でした。

    調べた記録としては要りますが、商圏についての事実ではありません。混ぜると
    後続が「外部情報で12件確認できた」と読み、半分が空振りだったことが
    見えなくなります。
    """
    text = cfg.prompt_text("step2_structure.md")
    assert "unanswered" in text
    assert "EXTERNAL FACT ではありません" in text
    # 「何も見つからなかった」と「違うと分かった」を分けてあること。
    # 前者は現地で確かめる項目、後者は消えた筋で、経営判断では正反対です。
    assert "UNCERTAIN" in text and "CONTRADICTED" in text
    assert "`UNSUPPORTED` は使わないでください" in text


def test_step1_is_told_which_questions_have_public_answers():
    """答えの無い質問を作らせない。

    医院の経営方針・患者の居住地別内訳・自由診療比率は、どこにも公表されて
    いません。それを尋ねると、STEP2 の検索上限と費用が空振りに消えます。

    以前はプロンプトに禁止事項として並べていました。**それでは足りません。**
    「市内の歯科衛生士・歯科医師の年齢構成」は禁止の語に当たらず、公的統計に
    ありそうな顔をしていて、実際には市区町村単位では公表されていません。
    いまは実測でできた台帳（config/dead_ends.yaml）を渡します。
    """
    from kaigyou_intel.steps.step1_features import _dead_end_block

    text = cfg.prompt_text("step1_features.md")
    # 検索するかどうかを決める欄が、プロンプトで説明されていること。
    assert "researchability" in text
    assert "`low` の問いは検索しません" in text
    # 台帳が本文に差し込まれること。
    assert "{dead_ends}" in text
    rendered = _dead_end_block()
    assert "自由診療比率" in rendered
    assert "歯科医師・歯科衛生士の年齢構成" in rendered
    # **代わりに何をすればよいか**まで渡すこと。塞ぐだけでは、次に何を
    # 問えばよいかが分かりません。
    assert "代わりに" in rendered


def test_the_dead_end_ledger_says_where_it_looked():
    """「無いから無い」では、あとから直せません。

    公表され始めたものに気づけるのは、どこを探して無かったのかが書いて
    あるときだけです。
    """
    entries = cfg.dead_ends()
    assert entries, "台帳が空です"
    for entry in entries:
        assert entry.get("topic"), entry
        assert entry.get("why"), f"{entry.get('topic')} に why がありません"
        assert entry.get("instead"), (
            f"{entry.get('topic')} に instead がありません。"
            "塞ぐだけでは、次に何を問えばよいかが分かりません")


def test_the_cache_breakpoint_sits_on_the_part_that_does_not_change():
    """**キャッシュは区切りより前が毎回同じでないと、一度も読まれません。**

    以前はトップレベルに `cache_control` を置いていました（自動キャッシュ）。
    自動キャッシュは区切りを「最後のキャッシュ可能なブロック」に置きますが、
    このアプリではそれが地点ごとに中身の違う user メッセージです。区切りより
    前のハッシュが毎回変わるので、**毎回書き込んで一度も読まない**という、
    いちばん損な形になっていました。

    システムプロンプトは同じ段・同じプロンプト版なら 1 バイトも変わりません。
    """
    body = llm.build_request(2, "s", "u")
    assert "cache_control" not in body, "自動キャッシュは可変ブロックに当たる"
    assert body["system"] == [
        {"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}]
    # user 側には置きません。ここは地点ごとに変わります。
    assert body["messages"] == [{"role": "user", "content": "u"}]
    json.dumps(body)


def test_the_cache_lifetime_can_be_lengthened_without_a_code_change(monkeypatch):
    """5分は**応答の開始から**数えます。レポート1本が12分かかるなら、次の
    地点を分析するころには消えています。

    何地点か続けて見るなら 1h が当たりますが、書き込みの単価が 1.25 倍から
    2 倍に上がるので、当たらなければ損です。既定は 5 分のまま。
    """
    monkeypatch.setenv(llm.CACHE_TTL_ENV, "1h")
    body = llm.build_request(2, "s", "u")
    assert body["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    monkeypatch.setenv(llm.CACHE_TTL_ENV, "")
    assert llm.build_request(2, "s", "u")["system"][0]["cache_control"] == {
        "type": "ephemeral"}


def test_cache_tokens_are_recorded_and_priced(conn, dataset):
    """要件 §34：1レポートいくらかかったかを後から数えられること。

    単価が違うので、入力トークンとまとめてしまうと概算が合いません。
    """
    from kaigyou_etl.cli import _step_cost
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    jobs.start_step(conn, job_id, 1, {}, {"prompt_version": "v",
                                          "model": "claude-sonnet-5"})
    jobs.finish_step(conn, job_id, 1, {"facts": []}, {
        "input_tokens": 1_000_000, "output_tokens": 0, "web_searches": 0,
        "cache_read_tokens": 1_000_000, "cache_write_tokens": 1_000_000})

    step = {s["step_number"]: s for s in jobs.get_steps(conn, job_id)}[1]
    assert step["cache_read_tokens"] == 1_000_000
    # 入力 $2 + 読み出し $0.2 + 書き込み $2.5
    assert _step_cost(step) == pytest.approx(4.7)
    conn.rollback()


# ------------------------------------------------------------------ STEP3
def _evidenced(text="s", refs=("F001",)):
    from kaigyou_intel.schemas import Evidenced

    return Evidenced(statement=text, evidence=list(refs))


def _decision(**overrides):
    from kaigyou_intel.schemas import BusinessDecision

    data = {
        "primary_patients": _evidenced("周辺勤務者", ["S001"]),
        "secondary_patients": _evidenced("居住小児は主要に置かない", ["F001"]),
        "avoid_competing_on": _evidenced("小児歯科の標榜数では競わない", ["F001"]),
        "acquisition_area": _evidenced("駅から徒歩圏の通勤動線", ["M001"]),
        "reason_to_visit": _evidenced("勤務時間の前後に通える診療時間", ["F001"]),
        "clinic_model": _evidenced("平日夜間まで開ける成人中心の医院", ["M001"]),
        "advantages": [_evidenced("昼間人口が常住人口を大きく上回る", ["F001"])],
        "risks": [_evidenced("地価が高い", ["F001"])],
        "confidence": "medium",
    }
    data.update(overrides)
    return BusinessDecision(**data)


def _step3_output(**overrides) -> "Step3Output":
    from kaigyou_intel.schemas import (
        DemandInsight, DemandMechanism, PatientSegment, Step3Output)

    data = {
        "demand_mechanisms": [DemandMechanism(
            id="M001", title="昼間人口が常住人口を大きく上回る",
            chain=["昼間従業者数が常住人口を大きく上回る",
                   "就業者は平日日中をこの地区で過ごす",
                   "受診も勤務地周辺で行われる",
                   "平日日中の歯科受診需要が常住人口の規模を超えて発生する"],
            evidence=["F001", "C002"], confidence="medium")],
        "patient_segments": [PatientSegment(
            id="S001", name="周辺勤務者", evidence=["F001"], mechanism_id="M001",
            importance="high", confidence="medium", note=None)],
        "insights": [DemandInsight(
            id="I001", statement="常住人口の規模で需要を測ると過小評価になる",
            evidence=["M001", "S001"])],
        "not_supported": ["広域流入患者：来院範囲を示すデータが無い"],
        "decision": _decision(),
        "actions": [_evidenced("平日夜間の診療体制を決める", ["M001"])],
    }
    data.update(overrides)
    return Step3Output(**data)


def test_a_segment_without_a_mechanism_is_rejected():
    """要件 §14：患者属性を並べる作業ではありません。

    どの筋道でも説明できない層は、観察ではなく思いつきです。
    """
    from kaigyou_intel.schemas import PatientSegment, verify_step3

    output = _step3_output(patient_segments=[PatientSegment(
        id="S001", name="高齢者", evidence=["F001"], mechanism_id="M404",
        importance="high", confidence="low")])
    problems = verify_step3(output, {"F001"}, {"C002"})
    assert any("M404" in p.problem for p in problems)


def test_step3_evidence_must_come_from_the_earlier_steps():
    from kaigyou_intel.schemas import verify_step3

    output = _step3_output()
    problems = verify_step3(output, {"F001"}, set())
    assert any("C002" in p.problem for p in problems)
    assert verify_step3(output, {"F001"}, {"C002"}) == []


def test_a_mechanism_must_be_a_chain_not_a_restatement():
    """「駅前だから患者が来る」を書けなくする。

    段を 3 つ以上必須にしているので、一足飛びの説明はスキーマで落ちます。
    空文字で段を埋めるほうも塞ぎます。
    """
    from pydantic import ValidationError

    from kaigyou_intel.schemas import DemandMechanism, verify_step3

    with pytest.raises(ValidationError):
        DemandMechanism(id="M001", title="駅前だから患者が来る",
                        chain=["駅前である", "患者が来る"],
                        evidence=["F001", "F002"], confidence="high")

    hollow = _step3_output(demand_mechanisms=[DemandMechanism(
        id="M001", title="t", chain=["駅前である", "  ", "患者が来る"],
        evidence=["F001", "C002"], confidence="high")])
    problems = verify_step3(hollow, {"F001"}, {"C002"})
    assert any("空の段" in p.problem for p in problems)


def test_step3_runs_end_to_end_with_a_stubbed_model(dataset, monkeypatch):
    from kaigyou_intel.steps import step3_demand

    captured: dict[str, object] = {}

    def fake_ask(*, step_number, system, user, schema=None, tools=None,
                 web_search=None, effort=None, max_uses=None):
        captured.update(step_number=step_number, schema=schema, tools=tools,
                        system=system)
        return llm.Result(parsed=_step3_output(),
                          usage=llm.Usage(input_tokens=9, output_tokens=9), model="m")

    monkeypatch.setattr(llm, "ask", fake_ask)
    payload = step3_demand.build_input(
        {"facts": [{"id": "F001"}], "patterns": []},
        {"external_facts": [{"id": "C002"}], "hypotheses": []},
        dataset)
    output, usage, sources = step3_demand.run(payload)

    assert captured["tools"] is None, "STEP3 で外部検索をさせてはいけない（要件 §38）"
    assert output["patient_segments"][0]["mechanism_id"] == "M001"
    assert sources == []


def test_step3_refuses_evidence_it_was_never_given(dataset, monkeypatch):
    from kaigyou_intel.steps import step3_demand

    monkeypatch.setattr(llm, "ask", lambda **kw: llm.Result(
        parsed=_step3_output(), usage=llm.Usage(), model="m"))
    payload = step3_demand.build_input(
        {"facts": [{"id": "F001"}], "patterns": []},
        {"external_facts": [], "hypotheses": []}, dataset)
    with pytest.raises(step3_demand.StepFailed, match="C002"):
        step3_demand.run(payload)


def test_step3_input_needs_both_earlier_steps(conn, dataset):
    """STEP2 の結論が無いまま需要分析をさせると、外部事実のない推論になります。"""
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    jobs.start_step(conn, job_id, 1, {}, {"prompt_version": "v", "model": "m"})
    jobs.finish_step(conn, job_id, 1, _step1_for_step2(), {})

    job = jobs.get_job(conn, job_id, include_base_data=True)
    with pytest.raises(worker.StepNotImplemented, match="STEP1 と STEP2"):
        worker.build_input(conn, job, 3)
    conn.rollback()


def test_step3_is_told_what_to_do_with_a_refuted_hypothesis():
    """反証を無視するのと、反証を採用するのは別です。

    「日曜に開ければ空白を取れる」が反証されたなら、日曜開院を軸に据えない
    という判断になります。前の段が調べた結果が判断に届いていないと、
    調べた意味がありません。
    """
    text = cfg.prompt_text("step3_demand.md")
    assert "decision_impact" in text and "changes" in text
    assert "反証されたほうを採用する" in text


def test_step3_is_not_asked_to_predict():
    """禁止事項。売上・患者数・成功確率の予測はこのシステムの目的外です。"""
    text = cfg.prompt_text("step3_demand.md")
    assert "売上・患者数・成功確率の予測" in text
    assert "UNSUPPORTED" in text, "反証された仮説を根拠に使わせない指示が要ります"


# --------------------------------------------- STEP3 の経営判断（要件 §16〜§17）
#
# 判断は以前 STEP4 という独立した段にあり、その段はタグ付きの10章レポートも
# 書いていました。最終段がそれを散文に書き直していたので、**同じ内容を2回**
# 生成していたことになります。読み手に届くのは散文だけです。タグ付きの章立てを
# 守らせる検査（§18・§19）は、その形ごと無くなりました。

def test_the_judgement_travels_with_the_analysis_that_produced_it():
    """判断は、同じ段で作った患者層・筋道を根拠にできること。

    段を分けていた頃は、判断のために同じことをもう一度書かせていました。
    """
    from kaigyou_intel.schemas import verify_step3

    assert verify_step3(_step3_output(), {"F001"}, {"C002"}) == []


def test_the_judgement_cannot_cite_an_id_no_step_produced():
    """§25：判断から根拠まで辿れること。"""
    from kaigyou_intel.schemas import verify_step3

    output = _step3_output(decision=_decision(
        avoid_competing_on=_evidenced("何かと競わない", ["Z999"])))
    assert any("Z999" in p.problem
               for p in verify_step3(output, {"F001"}, {"C002"}))


def test_an_action_cannot_cite_an_id_no_step_produced():
    from kaigyou_intel.schemas import verify_step3

    output = _step3_output(actions=[_evidenced("何かする", ["Z999"])])
    assert any("Z999" in p.problem
               for p in verify_step3(output, {"F001"}, {"C002"}))


@pytest.mark.parametrize("phrase", ["年商", "成功確率", "投資回収", "売上予測"])
def test_a_judgement_that_predicts_revenue_is_refused(phrase):
    """開業成功確率・売上・患者数の予測は、このシステムの目的外です。

    プロンプトで禁じたうえで、出力でも落とします。お願いだけで守られることに
    賭けない。判断の段が変わっても、この検査は付いていきます。
    """
    from kaigyou_intel.schemas import verify_step3

    output = _step3_output(decision=_decision(
        clinic_model=_evidenced(f"この立地の{phrase}は良好と見込まれる", ["M001"])))
    assert any(phrase in p.problem
               for p in verify_step3(output, {"F001"}, {"C002"}))


def test_the_decision_has_a_place_for_who_not_to_compete_with():
    """要件 §17。散文に溶かすと、抜けても気づけません。

    欄として持つので、埋まっていなければスキーマが通しません。
    """
    from pydantic import ValidationError

    from kaigyou_intel.schemas import BusinessDecision

    with pytest.raises(ValidationError):
        BusinessDecision(
            primary_patients=_evidenced(), secondary_patients=_evidenced(),
            acquisition_area=_evidenced(), reason_to_visit=_evidenced(),
            clinic_model=_evidenced(), advantages=[_evidenced()],
            risks=[_evidenced()], confidence="low")  # avoid_competing_on 欠落


def test_the_judgement_reaches_the_report_as_a_table(dataset):
    """§17 の答えは、散文ではなく欄のまま載ること。

    以前は判断を作った段の出力ごと捨てていて（散文に書き直したあと参照されま
    せんでした）、「誰とは競争しないか」がレポートのどこにも残りませんでした。
    """
    from kaigyou_intel.report import to_markdown

    markdown = to_markdown(_report_output().model_dump(), to_jsonable(dataset),
                           (), _step3_output().model_dump())
    assert "競争しない領域" in markdown and "主要に置かない層" in markdown
    assert "## 次に取るべき行動" in markdown


# ------------------------------------------------------------------ STEP4
def test_the_report_markdown_always_carries_the_disclaimer_and_provenance(dataset):
    """免責・出典・データ時点は LLM に書かせません。書き忘れの起きる場所に
    置かないためです。"""
    from kaigyou_intel.report import to_markdown

    markdown = to_markdown(_report_output().model_dump(), to_jsonable(dataset))
    assert "## 免責" in markdown
    assert "## データの出典と時点" in markdown
    assert dataset["disclaimer"][:20] in markdown


def test_the_report_lists_its_external_sources_in_priority_order(dataset):
    """要件 §9 の優先順位で並べる。読む人が上から見て一次資料に当たれるように。"""
    from kaigyou_intel.report import to_markdown

    markdown = to_markdown(_report_output().model_dump(), to_jsonable(dataset), [
        {"url": "https://example.com/a", "title": "企業サイト",
         "source_type": "company", "pattern_id": "P001"},
        {"url": "https://www.mhlw.go.jp/b", "title": "厚労省",
         "source_type": "government", "pattern_id": "P001"},
    ])
    assert markdown.index("厚労省") < markdown.index("企業サイト")


def test_the_source_list_is_what_the_report_cited_not_what_it_opened(dataset):
    """実測：銀座のレポートの出典が 230 件になりました。

    同じ URL が最大6回、医院のホームページや情報サイトも混ざっていて、
    出典一覧として使えません。調べた記録は analysis_sources に残っているので、
    レポートに載せるのは本文が引用したものだけにします。
    """
    from kaigyou_intel.report import to_markdown

    markdown = to_markdown(_report_output().model_dump(), to_jsonable(dataset), [
        {"url": "https://www.city.chuo.lg.jp/x", "title": "中央区 人口統計",
         "source_type": "municipality", "pattern_id": "P001"},
        {"url": "https://www.city.chuo.lg.jp/x/", "title": "中央区 人口統計",
         "source_type": "municipality", "pattern_id": "P002"},
        {"url": "https://example-clinic.com/", "title": "銀座◯◯歯科",
         "source_type": "company", "pattern_id": None},
    ])
    block = markdown.split("## 出典（外部情報）", 1)[1].split("\n## ", 1)[0]
    assert block.count("city.chuo.lg.jp") == 1, "同じ URL を重ねて出しています"
    assert "銀座◯◯歯科" not in block, "引用していない資料を出典に載せています"
    assert "このほか 2 件" in block


def test_a_very_long_source_title_is_trimmed(dataset):
    """e-Stat の表題は 481 文字ありました。"""
    from kaigyou_intel.report import to_markdown

    long_title = ("国勢調査 平成27年国勢調査 従業地・通学地による集計" + "あ" * 200
                  + " | 統計表・グラフ表示 | 政府統計の総合窓口")
    markdown = to_markdown(_report_output().model_dump(), to_jsonable(dataset), [
        {"url": "https://www.e-stat.go.jp/x", "title": long_title,
         "source_type": "statistics", "pattern_id": "P001"}])
    line = [ln for ln in markdown.splitlines() if ln.startswith("- [政府統計]")][0]
    assert len(line) < 130
    assert "政府統計の総合窓口" not in line, "どの出典にも付く後半は落とす"


def test_a_report_with_no_cited_source_says_so(dataset):
    """外部情報を使えなかったことを黙らない。"""
    from kaigyou_intel.report import to_markdown

    markdown = to_markdown(_report_output().model_dump(), to_jsonable(dataset), [
        {"url": "https://example.com/a", "title": "t", "source_type": "other",
         "pattern_id": None}])
    assert "本文が引用した外部資料はありません（1 件を参照）" in markdown


def test_step4_needs_all_three_earlier_steps(conn, dataset):
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    jobs.start_step(conn, job_id, 1, {}, {"prompt_version": "v", "model": "m"})
    jobs.finish_step(conn, job_id, 1, _step1_for_step2(), {})

    job = jobs.get_job(conn, job_id, include_base_data=True)
    with pytest.raises(worker.StepNotImplemented, match="STEP2・STEP3"):
        worker.build_input(conn, job, 4)
    conn.rollback()


def test_the_report_is_saved_and_readable(conn, dataset):
    from kaigyou_intel import jobs, report

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    try:
        report.save(conn, job_id, _report_output().model_dump(), to_jsonable(dataset))
        markdown = report.markdown_for(conn, job_id)
        assert markdown and "商圏分析レポート" in markdown
        # 二度目は上書き（やり直しても行が増えない）。
        report.save(conn, job_id, _report_output().model_dump(), to_jsonable(dataset))
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM analysis_reports WHERE job_id = %s",
                        (job_id,))
            assert cur.fetchone()["n"] == 1
    finally:
        _drop_job(job_id)


def test_the_report_can_be_read_from_the_command_line():
    from kaigyou_etl.cli import build_parser

    args = build_parser().parse_args(["analyze", "--report", "--out", "report.md"])
    assert args.report == "" and args.out == "report.md"


def test_the_report_carries_the_caveats_that_change_how_it_reads(dataset):
    """「標榜診療科目は届出値であって診療内容ではない」は、落とすと誤読になります。

    競合の数え方の但し書きが本文から消えると、標榜数をそのまま診療実態として
    読まれます。データ側が持っている注意書きは、レポートに必ず出します。
    """
    from kaigyou_intel.report import to_markdown

    markdown = to_markdown(_report_output().model_dump(), to_jsonable(dataset))
    assert "## 読むときの注意" in markdown
    assert "標榜" in markdown
    # 出典と年次が指標から拾えていること（空欄のまま出さない）。
    assert "## データの出典と時点" in markdown
    body = markdown.split("## データの出典と時点", 1)[1].split("##", 1)[0]
    assert body.strip(), "出典が空のまま出しています"


def test_the_cost_line_does_not_look_like_it_lost_the_input(capsys, conn, dataset):
    """実測：STEP3 が「入力 2 tok」と出ました。

    キャッシュに入ったぶんは input_tokens から抜けます。そこを足さずに出すと、
    数え損ねたように見えます（費用の計算は合っているのに）。
    """
    from kaigyou_etl.cli import _print_step_cost_line

    _print_step_cost_line({"input_tokens": 2, "output_tokens": 5093,
                           "cache_read_tokens": 0, "cache_write_tokens": 42000,
                           "model": "claude-sonnet-5"})
    printed = capsys.readouterr().out
    assert "入力 42,002 tok" in printed
    assert "キャッシュ書 42,000" in printed


# ------------------------------------------------------------ API と UI
def test_starting_an_analysis_is_refused_on_a_public_host_without_a_secret(monkeypatch):
    """分析1件でLLMの課金が発生します。公開URLに認証なしで置けません。

    警告を出して通すのでは、気づいたときには請求が来ています。
    """
    from fastapi import HTTPException

    from kaigyou_api.routers.intel import _authorise

    monkeypatch.delenv("KAIGYOU_ANALYSIS_TOKEN", raising=False)
    monkeypatch.setenv("VERCEL", "1")
    with pytest.raises(HTTPException) as caught:
        _authorise(None)
    assert caught.value.status_code == 503

    monkeypatch.setenv("KAIGYOU_ANALYSIS_TOKEN", "s3cret")
    with pytest.raises(HTTPException) as caught:
        _authorise("wrong")
    assert caught.value.status_code == 401
    _authorise("s3cret")  # 一致すれば通る


def test_the_local_machine_does_not_need_a_secret(monkeypatch):
    """手元では設定なしで動かせること。開発のたびに秘密を作らせない。"""
    from kaigyou_api.routers.intel import _authorise

    for var in ("VERCEL", "AWS_LAMBDA_FUNCTION_NAME", "K_SERVICE",
                "KAIGYOU_ANALYSIS_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    _authorise(None)


def test_the_progress_endpoint_reports_what_it_cost(conn, dataset):
    """要件 §34。画面に出す金額と端末に出す金額を同じ表から出します。"""
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    try:
        jobs.start_step(conn, job_id, 1, {}, {"prompt_version": "v",
                                              "model": "claude-sonnet-5"})
        jobs.finish_step(conn, job_id, 1, {"facts": []}, {
            "input_tokens": 2, "output_tokens": 1_000_000, "web_searches": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 1_000_000})

        body = TestClient(app).get(f"/api/analysis/{job_id}").json()
        # キャッシュに入ったぶんを引いたまま出すと「入力 2」になります。
        assert body["usage"]["input_tokens"] == 1_000_002
        assert body["usage"]["cache_write_tokens"] == 1_000_000
        # 入力 $2.5（書き込み1.25倍）+ 出力 $10
        assert body["usage"]["estimated_cost_usd"] == pytest.approx(12.5, abs=0.01)
        assert body["status_note"], "待っている人に、何を待っているのかを言う"
    finally:
        _drop_job(job_id)


def test_the_cost_estimate_is_not_understated_when_a_model_is_unknown():
    """一部だけの合計を総額として見せない。実際より安いと思われます。"""
    from kaigyou_intel.pricing import total_cost

    assert total_cost([]) == 0.0
    assert total_cost([{"model": "claude-sonnet-5", "input_tokens": 1_000_000}]) == 2.0
    assert total_cost([
        {"model": "claude-sonnet-5", "input_tokens": 1_000_000},
        {"model": "未知のモデル", "input_tokens": 1_000_000},
    ]) is None


def test_retrying_restarts_the_clock(conn, dataset):
    """やり直した直後の画面に「経過 25秒」と出さない。

    数えているのが前回の開始からだと、待っている人が見たいものと違います。
    """
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    jobs.claim_specific(conn, job_id)
    assert jobs.get_job(conn, job_id)["started_at"] is not None
    jobs.reset_step(conn, job_id, 1)
    assert jobs.get_job(conn, job_id)["started_at"] is None
    conn.rollback()


def test_a_billing_failure_tells_the_user_what_to_do():
    """「失敗しました」だけでは動けません。

    実測：残高不足で止まったとき、画面には400のJSONとスタックトレースだけが
    出ていました。読んでも次に何をすればいいのか分かりません。
    """
    from pathlib import Path

    panel = (Path(__file__).resolve().parents[2] / "web" / "src" / "components"
             / "AnalysisPanel.tsx").read_text(encoding="utf-8")
    assert "credit balance is too low" in panel
    assert "Plans & Billing" in panel
    # やり直しは新しいジョブを作るのではなく、失敗したステップから再開する。
    assert "retryFrom" in panel and "からやり直す" in panel


def test_the_report_is_written_to_a_file_without_another_command(conn, dataset, tmp_path):
    """DB の中にあることと、手元にファイルがあることは違います。

    追加のコマンドを打たないと現物が手に入らないのでは、「レポートを作る道具」
    として不完全です。
    """
    from kaigyou_intel import jobs, report

    job_id = jobs.create_job(conn, lat=35.6717, lng=139.765, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x",
                             location_name="銀座4丁目")
    try:
        report.save(conn, job_id, _report_output().model_dump(), to_jsonable(dataset))
        path = report.write_file(conn, job_id, directory=str(tmp_path))
        assert path is not None and path.exists()
        assert "銀座4丁目" in path.name, "人が見て分かる名前にする"
        assert path.read_text(encoding="utf-8").startswith("# 銀座4丁目 商圏分析レポート")

        # やり直しても同じ名前に上書きする。日付ごとに増やすと最新が分からない。
        again = report.write_file(conn, job_id, directory=str(tmp_path))
        assert again == path
        assert len(list(tmp_path.glob("*.md"))) == 1
    finally:
        _drop_job(job_id)


def test_a_file_name_survives_windows(conn, dataset, tmp_path):
    """`/` や `:` が入ると Windows では保存できません。"""
    from kaigyou_intel import jobs, report

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x",
                             location_name='A/B:C*D?"E<F>G|H')
    try:
        report.save(conn, job_id,
                    _report_output(title='X/Y:Z*?"<>|').model_dump(),
                    to_jsonable(dataset))
        path = report.write_file(conn, job_id, directory=str(tmp_path))
        assert path is not None and path.exists()
        assert not set(path.name) & set('\\/:*?"<>|')
    finally:
        _drop_job(job_id)


def test_the_saved_file_is_named_after_the_report_title(conn, dataset, tmp_path):
    """**入口が違っても同じ名前にします。**

    以前は 2 通りありました。マイレポートからの保存は
    ``商圏分析_35.76542_139.85036_20260828_ff4ce176.md``、地図の画面からの
    保存は ``商圏分析レポート.md``（ブラウザ側で Blob を組み立てていて名前が
    固定）。同じ文書が別の名前で 2 つ手元に残ります。

    一覧に出ている表題と揃えるのは、あとから探せるようにするためです。
    座標と16進数のファイルは、フォルダの中で見分けが付きません。
    """
    from kaigyou_intel import jobs, report

    title = "商圏分析レポート：亀有駅前（葛飾区）候補地の開業診断"
    job_id = jobs.create_job(conn, lat=35.76542, lng=139.85036, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    try:
        report.save(conn, job_id, _report_output(title=title).model_dump(),
                    to_jsonable(dataset))
        # 画面（/report.md）と、手元に書き出すファイル。同じ関数を通ります。
        assert report.file_name_for(conn, job_id) == f"{title}.md"
        path = report.write_file(conn, job_id, directory=str(tmp_path))
        assert path is not None and path.name == f"{title}.md"
        # 全角の「：」は Windows でも使えます。落とすと表題が変わります。
        assert "：" in path.name
    finally:
        _drop_job(job_id)


def test_a_report_without_a_title_still_gets_the_same_shape_of_name(conn, dataset):
    """表題が無いのは、最終段まで走っていないジョブです。**形は変えません。**

    入口によって名前の形が変わるのを直したので、ここで別の形に落とすと
    同じことが起きます。
    """
    from kaigyou_intel import jobs, report

    job_id = jobs.create_job(conn, lat=35.76542, lng=139.85036, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x",
                             location_name="亀有駅前")
    try:
        assert report.file_name_for(conn, job_id) == "商圏分析レポート：亀有駅前.md"
    finally:
        _drop_job(job_id)

    # 地点名も無ければ座標。空の名前にはしません。
    assert report.file_name("abcd1234", {
        "title": None, "location_name": None,
        "latitude": 35.76542, "longitude": 139.85036,
    }) == "商圏分析レポート：35.76542,139.85036.md"
    # 消せる文字しか無かったときの逃げ道。名前が ".md" だけになりません。
    assert report.file_name("abcd1234", {"title": " . ", "location_name": None,
                                         "latitude": None}) == "abcd1234.md"
    assert report.file_name("abcd1234", None) == "abcd1234.md"


def test_the_report_carries_the_numbers_it_did_not_quote(dataset):
    """本文が引用しなかった数値も、同じ文書の中で確かめられること。

    本文の数字は LLM が選びます。選ばれなかった数字がどこにも残らないと、
    読んだ人は「その数字はどこから来たのか」を別の画面で探すことになります。
    付録はデータセットからそのまま出すので、桁の取り違えも起きません。
    """
    from kaigyou_intel.report import to_markdown

    markdown = to_markdown(_report_output().model_dump(), to_jsonable(dataset))
    assert "## 付録：商圏の基礎数値" in markdown
    for heading in ("### 常住人口", "### 昼間（従業者・事業所）",
                    "### 競合（歯科医院）", "### 交通アクセス", "### 地価（公示地価）"):
        assert heading in markdown, heading
    # 半径3段の比較が並ぶこと（1kmだけでは商圏の広がりが分からない）。
    assert "| 指標 | 500m | 1km | 2km |" in markdown
    # 標榜科目と診療時間は、競合の読み方を変える情報なので落とさない。
    assert "#### 標榜診療科目（商圏内）" in markdown
    assert "#### 診療時間（商圏内）" in markdown


def test_a_truncated_answer_is_not_reported_as_broken_json(monkeypatch):
    """実測：STEP4 が

      Invalid JSON: EOF while parsing a string at line 1 column 18741

    で落ちました。モデルが間違った JSON を書いたのではなく、**書き終わる前に
    止められた**という意味です。この 2 つは直し方がまったく違います
    （前者はプロンプト、後者は max_tokens）。

    SDK は content_block_stop の時点で本文を解析するので、stop_reason が
    max_tokens だと分かる前に例外が出ます。だから壊れ方のほうを見ます。
    """
    from pydantic_core import ValidationError as CoreValidationError

    from kaigyou_intel import client as llm

    boom = Exception(
        "1 validation error for Step4Output\n  Invalid JSON: EOF while parsing "
        "a string at line 1 column 18741 [type=json_invalid, input_value='{...']")
    assert llm._looks_truncated(boom)
    # 本物の書き間違いは別扱い（やり直しても同じところで落ちるとは限らない）。
    assert not llm._looks_truncated(Exception("Field required [type=missing]"))
    assert CoreValidationError is not None  # import できることの確認


def test_the_truncation_message_says_what_to_change(monkeypatch):
    import contextlib

    from kaigyou_intel import client as llm

    class _Stream:
        @staticmethod
        def get_final_message():
            raise Exception("Invalid JSON: EOF while parsing a string "
                            "[type=json_invalid, input_value='{']")

    class _Client:
        class messages:
            @staticmethod
            @contextlib.contextmanager
            def stream(**kwargs):
                yield _Stream()

    monkeypatch.setattr(llm, "_client", lambda: _Client())
    with pytest.raises(llm.Truncated, match="max_tokens"):
        llm.ask(step_number=4, system="s", user="u", schema=Step1Output)


def test_the_output_ceiling_leaves_room_for_the_whole_report():
    """レポート1本の JSON に、思考のぶんを足しても収まる上限にしておく。

    24,000 では書き終わる前に切れました。全部ストリームで受けているので
    HTTP タイムアウトの心配はなく、上限は余裕を持たせられます。払うのは
    実際に出た分だけなので、上げても高くなりません。
    """
    assert cfg.analysis_config()["model"]["max_tokens"] >= 48000


# ------------------------------------------------------------ STEP4（最終段）
def _report_output(**overrides) -> "Step4Output":
    from kaigyou_intel.schemas import (
        Judgement, NarrativeSection, ResearchDirection, Step4Output, SupportItem)

    data = {
        "title": "銀座4丁目 商圏分析レポート",
        "summary": "常住人口13,268人に対し歯科医院は186院あり、"
                   "住民だけを相手にする立地ではありません。",
        "verdict": Judgement(
            label="条件付きで有望",
            statement="昼間人口が常住人口を大きく上回るため、勤務者を主に据えれば"
                      "条件は揃っています。",
            basis=["M001", "F010"],
            counterpoint="勤務者の受診が勤務地周辺で行われていない場合、"
                         "この前提が崩れます。"),
        "why_here": "約49万人が昼間この地区で働いており、駅までは101mです。",
        "sections": [
            NarrativeSection(heading="住民と就業者", body="本文。", takeaway="要点",
                             evidence=["F001"]),
            NarrativeSection(heading="競合の厚み", body="本文。", evidence=["F010"]),
            NarrativeSection(heading="立地の条件", body="本文。"),
        ],
        "support_needed": [SupportItem(
            item="平日夜間まで回せる人員体制",
            why="勤務者を主に据えると、受診は勤務前後に寄ります。",
            evidence=["M001"], category="人員")],
        "further_research": [ResearchDirection(
            topic="既存の小児歯科標榜3院の受入れ余力",
            why="小児の供給ギャップは届出上の標榜数からの推定にすぎず、"
                "この判断のいちばん弱いところです",
            how="初診予約の空き状況を電話で確認する")],
        "judgement_note": "数値は公的統計です。「条件が揃っている」という評価は"
                          "本レポートの判断であり、開業の成否を示すものではありません。",
    }
    data.update(overrides)
    return Step4Output(**data)


_REPORT_IDS = {"F001", "F010", "M001"}
_REPORT_NUMBERS = {"13268", "186", "494517", "101"}


def test_a_readable_report_passes():
    from kaigyou_intel.schemas import verify_step4

    assert verify_step4(_report_output(), _REPORT_IDS, _REPORT_NUMBERS) == []


def test_rounding_for_readability_is_allowed():
    """「494,517人」と書けとは言えません。読み物として渡す文書です。

    「約49万人」は正しい書き方で、落としてはいけない。
    """
    from kaigyou_intel.schemas import invented_numbers

    known = {"494517", "13268", "0.279", "71.43"}
    assert invented_numbers("約49万人が働き、住民は約1.3万人です。", known) == []
    assert invented_numbers("構成比は27.9%、1院あたり71.4人。", known) == []
    assert invented_numbers("2020年から2025年、3つの理由で", known) == []


def test_a_number_that_was_never_in_the_data_is_caught():
    """散文にすると、数字はいくらでも滑らかに増やせます。

    「約5万人」は、元が13,268でも494,517でも文としては通ります。だから
    照合します。
    """
    from kaigyou_intel.schemas import invented_numbers

    known = {"494517", "13268"}
    assert invented_numbers("約5万人が働いています。", known) == ["5万"]
    assert invented_numbers("年間3,200人の来院が見込めます。", known) == ["3,200"]


def test_the_report_refuses_a_number_it_was_not_given():
    from kaigyou_intel.schemas import verify_step4

    output = _report_output(why_here="約5万人がこの地区で働いています。")
    problems = verify_step4(output, _REPORT_IDS, _REPORT_NUMBERS)
    assert any("5万" in p.problem for p in problems)


def test_the_report_still_may_not_predict():
    """評価（「条件が揃っている」）と予測（「儲かる」）は別のものです。

    価値判断は書けますが、売上・患者数・成功確率は書けません。
    """
    from kaigyou_intel.schemas import verify_step4

    output = _report_output(summary="この立地の年商は良好と見込まれます。")
    assert any("年商" in p.problem
               for p in verify_step4(output, _REPORT_IDS, _REPORT_NUMBERS))


def test_the_judgement_is_marked_as_a_judgement():
    """価値判断を許すなら、どこからが判断かを読み手に見せる必要があります。"""
    from pydantic import ValidationError

    from kaigyou_intel.schemas import Judgement

    # counterpoint が無い判断は通しません。書けないなら根拠が薄いということです。
    with pytest.raises(ValidationError):
        Judgement(label="有望", statement="良い立地です", basis=["F001"])
    # judgement_note も必須。
    with pytest.raises(ValidationError):
        _report_output(judgement_note=None)


def test_the_client_report_reads_as_prose_not_as_tagged_facts(dataset):
    """[FACT] が20個並んだ文書は、読み手に「自分で要約してください」と
    言っているのと同じです。"""
    from kaigyou_intel.report import to_markdown

    markdown = to_markdown(_report_output().model_dump(), to_jsonable(dataset))
    assert "**[FACT]**" not in markdown
    assert "## なぜこの立地か" in markdown
    assert "## この立地で開業するために必要なこと" in markdown
    assert "### 人員" in markdown, "支援の要件は分類して出す"
    assert "## さらに深掘りすべき調査" in markdown
    assert "## このレポートにおける評価の位置づけ" in markdown
    # 根拠の id は残す。読み飛ばせる形で本文の末尾に。
    assert "〔M001, F010〕" in markdown
    # 付録と免責はこれまでどおり。
    assert "## 付録：商圏の基礎数値" in markdown and "## 免責" in markdown


def test_the_working_format_is_still_rendered_for_reports_saved_before_the_merge(dataset):
    """タグ付きの形で保存された古いレポートも読めること。

    レポートは DB に何か月も残り、その間に段の構成は変わります。読めなく
    なったら、それは記録を失ったのと同じです。
    """
    from kaigyou_intel.report import to_markdown

    legacy = {
        "executive_summary": "常住人口では説明できない供給を、勤務者需要が支えている。",
        "decision": _decision().model_dump(),
        "sections": [{"number": 1, "title": "エグゼクティブサマリー",
                      "blocks": [{"tag": "FACT", "text": "昼間人口が多い",
                                  "evidence": ["F001"]}]}],
        "actions": [{"statement": "平日夜間の診療体制を決める", "evidence": ["M001"]}],
    }
    markdown = to_markdown(legacy, to_jsonable(dataset))
    assert "**[FACT]**" in markdown
    assert "### 開業方針" in markdown


def test_the_report_needs_the_steps_before_it(conn, dataset):
    """材料が揃っていないまま書かせない。空の入力で「分析しました」と
    言われるほうが、止まっているより悪い。"""
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    job = jobs.get_job(conn, job_id, include_base_data=True)
    with pytest.raises(worker.StepNotImplemented, match="STEP1・STEP2・STEP3"):
        worker.build_input(conn, job, 4)
    conn.rollback()


def test_an_existing_job_gains_a_step_that_was_added(conn, dataset):
    """段を増やしたとき、既にある Job には行がありません。

    行が無いと next_step は「全部終わった」と読み、増やした段が黙って
    飛ばされます。作り直さずに続きから流せること。
    """
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM analysis_steps WHERE job_id = %s AND step_number = 4",
                    (job_id,))
    for number in (1, 2, 3):
        jobs.start_step(conn, job_id, number, {}, {"prompt_version": "v", "model": "m"})
        jobs.finish_step(conn, job_id, number, {"n": number}, {})
    # 3 段だったころに完走した Job の状態。
    jobs.release_job(conn, job_id, "completed")
    try:
        assert jobs.next_step(conn, job_id) is None, "行が無いので終わったように見える"
        assert jobs.ensure_steps(conn, job_id) == 1
        assert jobs.next_step(conn, job_id) == 4
        assert jobs.get_job(conn, job_id)["status"] == "queued"
    finally:
        _drop_job(job_id)


def test_a_job_from_before_a_step_was_removed_does_not_stall(conn, dataset):
    """段を**減らした**ときも、同じだけ困ります。

    無くなった段の行が pending のまま残ると、next_step はその番号を返し、
    走らせる実装が無いのでジョブはそこで永久に止まります。画面には
    「順番待ち」とだけ出ます。実際にそうなりました。
    """
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_steps (job_id, step_number, step_name, status) "
            "VALUES (%s, 5, '無くなった段', 'pending')", (job_id,))
    for number in (1, 2, 3, 4):
        jobs.start_step(conn, job_id, number, {}, {"prompt_version": "v", "model": "m"})
        jobs.finish_step(conn, job_id, number, {"n": number}, {})
    try:
        assert jobs.next_step(conn, job_id) is None, "存在しない段は飛ばす"
        assert jobs.ensure_steps(conn, job_id) == 1, "その行は落とす"
        assert [s["step_number"] for s in jobs.get_steps(conn, job_id)] == [1, 2, 3, 4]
    finally:
        _drop_job(job_id)


def test_the_client_report_keeps_its_own_title(dataset):
    """顧客に渡す文書なので、こちらが決めた定型より、その商圏について
    書かれた見出しのほうが読み手に向いています。"""
    from kaigyou_intel.report import to_markdown

    markdown = to_markdown(_report_output().model_dump(), to_jsonable(dataset))
    assert markdown.startswith("# 銀座4丁目 商圏分析レポート")
    # 表題が無いときは定型に戻ること。
    plain = to_markdown(_report_output(title="").model_dump(), to_jsonable(dataset))
    assert plain.startswith("# 商圏分析レポート：")


def test_a_number_the_earlier_step_wrote_in_prose_can_be_quoted(dataset):
    """実測：「下位30.1%」「409件中1位」を引用したら捏造として落ちました。

    数値欄しか見ていなかったためです。前の段が文章で書いた数字は、次の段が
    引用してよいものです。
    """
    from kaigyou_intel.projection import allowed_numbers, for_step4
    from kaigyou_intel.schemas import invented_numbers

    payload = for_step4({"facts": [{"statement": "409件中1位で、下位30.1%"}]},
                        {"external_facts": []}, {"demand_mechanisms": []},
                        to_jsonable(dataset))
    known = allowed_numbers(payload)
    assert invented_numbers("409件中1位、都内では下位30.1%です。", known) == []


def test_rounding_to_the_hundreds_is_allowed():
    """「約7,400人」は 7,431 を丸めた書き方です。4桁で照合すると落ちます。"""
    from kaigyou_intel.schemas import invented_numbers

    assert invented_numbers("約7,400人が居住しています。", {"7431"}) == []
    assert invented_numbers("約7,400人が居住しています。", {"9999"}) == ["7,400"]


def test_the_report_may_say_it_is_not_a_prediction():
    """「開業の成功確率を示すものではありません」は免責であって予測ではありません。

    語の有無だけを見ると、書いてほしい一文で落ちます。
    """
    from kaigyou_intel.schemas import verify_step4

    ok = _report_output(
        judgement_note="この評価は本レポートの判断であり、開業の成功確率を"
                       "示すものではありません。")
    assert verify_step4(ok, _REPORT_IDS, _REPORT_NUMBERS) == []

    inline = _report_output(
        sections=_report_output().sections,
        why_here="売上予測を示すものではありませんが、条件は揃っています。")
    assert verify_step4(inline, _REPORT_IDS, _REPORT_NUMBERS) == []


def test_the_prompt_says_which_ids_are_real():
    """実測：'competition.proximity' を根拠として書かれ、解決できずに落ちました。

    どれが id なのかを書いていなかったので、入力の場所を書かれました。
    """
    text = cfg.prompt_text("step4_client_report.md")
    assert "データの項目名は id ではありません" in text
    assert "competition.proximity" in text


def test_an_idle_worker_says_why_it_is_idle(conn, dataset):
    """実測：失敗したジョブが3件あるのに worker が黙って待ち、

      「レポートが生成されない」

    となりました。何も言わずに待ち続けると、動いているのか壊れているのかが
    分かりません。worker は queued しか拾わないので、拾えないものがあるなら
    そう言います。
    """
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    try:
        jobs.claim_specific(conn, job_id)
        jobs.release_job(conn, job_id, "failed", "模擬的な失敗")
        lines = "\n".join(worker.idle_reason(conn))
        assert "失敗したまま" in lines
        assert f"--job {job_id}" in lines
    finally:
        _drop_job(job_id)


def test_the_research_section_is_addressed_to_the_consultant_not_the_dentist():
    """このレポートを配るのは開業支援の事業者です。

    「〜をご存知ですか」と本人に尋ねる欄ではなく、担当者が自分で手配できる
    次の調査を示す欄です。
    """
    text = cfg.prompt_text("step4_client_report.md")
    assert "本人に尋ねる欄ではありません" in text
    assert "how" in text and "現地確認" in text


def test_the_urban_scope_compares_town_with_town(dataset):
    """実測：静岡で「歯科医院が実在する商圏」の医院数の中央値が2院でした。

    山あいの集落に1院あるだけの商圏も、その母集団に入るためです。
    「9院は中央値の4.5倍」は、市街地を農村と比べた結果でした。閾値は
    決め打ちではなく、その県で開業が成立している商圏人口の実測下限を使います。
    """
    scopes = {s["benchmark_type"]: s
              for s in dataset["measures"]["benchmark_scopes"]}
    assert "urban" in scopes, "市街地どうしの比較が要ります"
    assert "人口が生活圏規模に達する商圏" in scopes["urban"]["label"]

    preference = cfg.insights_config()["benchmarks"]["preference"]
    assert preference.index("urban") < preference.index("with_clinics"), (
        "with_clinics より前で試すこと。農村の1院商圏が母集団に残ります")


def test_every_step_declares_how_hard_it_should_think():
    """考える深さは段ごとに違います。**一律に深くすると、ただ遅くなります。**

    実測：レポート1本に32分かかったとき、すべての段が effort: high でした。
    STEP1 は算出済みの順位から FACT を選ぶ段で、思考だけで10,094トークン
    出ていました。判断の段（STEP3）と書く段（STEP4）は下げません。
    """
    from kaigyou_intel.client import step_settings

    for number in sorted(cfg.analysis_config()["steps"]):
        assert step_settings(number)["effort"], f"STEP{number} の effort が空です"
    assert step_settings(3)["effort"] == "high", "判断の段は削らない"
    assert step_settings(4)["effort"] == "high", "書く段は削らない"


def test_the_transcription_call_thinks_less_than_the_search_call():
    """STEP2 の 2 回目は、調べた本文を JSON に写すだけの呼び出しです。

    ここで考えさせると、調べていないことを補い始めます（そして出典の
    検算で落ちます）。深さは段ではなく**呼び出しごと**に決まります。
    """
    from kaigyou_intel.client import build_request, step_settings

    settings = step_settings(2)
    depths = ["low", "medium", "high", "xhigh", "max"]
    assert depths.index(settings["effort_structure"]) <= depths.index(settings["effort"])

    request = build_request(2, "s", "u", web_search=False,
                            effort=settings["effort_structure"])
    assert request["output_config"]["effort"] == settings["effort_structure"]


def test_the_search_budget_is_small_enough_to_finish():
    """検索回数はそのまま実行時間です。

    実測（裾野・半径1km）：36件の資料を取得して、本文が根拠に引いたのは
    7件でした。29件は読むためだけに時間と金を使っています。
    """
    limits = cfg.analysis_config()["limits"]
    assert limits["max_searches_total"] <= 10
    assert limits["searches_per_pattern"] * limits["max_patterns"] >= \
        limits["max_searches_total"], (
        "PATTERN あたりの上限が全体の上限より厳しいと、全体の上限が効きません")


def test_the_reserve_leaves_room_for_more_than_one_step_per_call():
    """1 呼び出しで 1 段しか進めないと、段の間に cron の間隔がまるごと空きます。

    実測：予算800秒に対して見込み420秒だったので、380秒使った時点で次に
    進めなくなり、段の数だけ最大60秒の待ちが入っていました。
    """
    worker_config = cfg.analysis_config()["worker"]
    budget = worker_config["invocation_seconds"]
    reserve = worker_config["reserve_seconds"]
    assert reserve * 2 < budget, "1 呼び出しで少なくとも 2 段は進めること"


def test_the_status_shows_where_the_time_went():
    """どこに時間が溶けているかが分からないと、縮めようがありません。

    実測でレポート1本32分かかったとき、段ごとの開始と完了は記録されて
    いたのに、どこにも表示されていませんでした。
    """
    from datetime import datetime, timedelta, timezone

    from kaigyou_etl.cli import _format_seconds, _step_seconds

    start = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    assert _step_seconds({"started_at": start,
                          "completed_at": start + timedelta(seconds=95)}) == 95
    # 走っている途中の段は、まだ言えることがありません。
    assert _step_seconds({"started_at": start, "completed_at": None}) is None
    assert _format_seconds(95) == "1分35秒"
    assert _format_seconds(42) == "42秒"


def test_the_neighbourhood_scope_names_the_towns_it_includes():
    """市区町村の中だけの順位は、その市の大きさで意味が変わります。

    実測：裾野市の商圏について「裾野市内157商圏中2位」というレポートが
    出ました。小さい市なら市街地はどこでも上位に入るので、これは
    「そこが市街地である」と言っているのとほぼ同じです。開業地を探して
    いる人が実際に並べているのは、市の境界の内側ではなく通える範囲
    ——三島市・長泉町・御殿場市です。

    「近隣」とだけ書かずに自治体名を並べるのは、どこまでを近隣と言って
    いるのかが読み手に分からないと、順位を読みようがないからです。
    """
    from kaigyou_core.measures import benchmark_scopes

    scopes = {s.type: s for s in benchmark_scopes(
        prefecture_code="22", prefecture_label="静岡県", municipality="裾野市",
        population=13378, radius_m=1000, lat=35.17, lng=138.90, config={},
        neighbours=["三島市", "長泉町", "御殿場市", "沼津市"])}

    assert "neighbourhood" in scopes
    label = scopes["neighbourhood"].label
    for name in ("裾野市", "三島市", "長泉町", "御殿場市"):
        assert name in label, "どこと比べたのかが読み手に見えること"

    preference = cfg.insights_config()["benchmarks"]["preference"]
    assert preference.index("neighbourhood") < preference.index("municipality"), (
        "市の内側だけの母集団より先に試すこと")


def test_a_town_with_no_neighbours_gets_no_neighbourhood_scope():
    """島や、境界データが無い場合。**同じ母集団を2つ作らない。**

    隣が1つも無ければ、neighbourhood は municipality と同じ集合です。同じ
    ものを2回並べると、読み手には別々の比較に見えます。
    """
    from kaigyou_core.measures import benchmark_scopes

    scopes = {s.type for s in benchmark_scopes(
        prefecture_code="22", prefecture_label="静岡県", municipality="裾野市",
        population=13378, radius_m=1000, lat=35.17, lng=138.90, config={},
        neighbours=[])}
    assert "municipality" in scopes and "neighbourhood" not in scopes


def test_the_neighbours_come_from_the_boundaries_not_from_a_radius(conn):
    """半径ではなく隣接で決めます。読み手が地図を持たなくても、自治体の
    名前は知っています。

    境界データの頂点は完全には一致しないので、厳密な ST_Touches では
    隣どうしが落ちます。少しの隙間は隣とみなします。
    """
    from kaigyou_core.dataset import _municipality, _neighbour_municipalities

    here = _municipality(conn, 35.6717, 139.7650)
    if here is None:
        pytest.skip("境界データが読み込まれていません")
    names = _neighbour_municipalities(conn, here["municipality_code"])
    assert names, "隣接する市区町村が1つも出ないのは、判定が効いていない"
    assert here["name"] not in names, "自分自身は隣ではありません"


def test_the_research_step_must_look_up_the_things_the_data_cannot_hold():
    """インプラント・審美・訪問診療は標榜診療科目ではありません。

    届出の自由記載欄にしかなく、記載率が低い（東京都で1%台）。ここを
    調べないと、レポートは「一般歯科9院・矯正6院なので差別化は難しい」
    という、看板の数え上げで止まります。実測でそうなりました。

    区画整理も同じで、名前を見つけて終わりにすると「計画的な街です」
    しか書けません。30年前に完了した事業と、いま保留地が売れ残っている
    事業では、これから人口が動くかどうかが逆になります。
    """
    text = cfg.prompt_text("step2_research.md")
    assert "インプラント" in text and "自由記載欄" in text
    assert "nearby_clinics" in text
    assert "扱っていないことは断定しないでください" in text, (
        "サイトに書いていないことと、やっていないことは違います")
    assert "施行面積" in text and "保留地" in text


def test_the_rent_estimate_is_a_range_with_its_assumption_visible():
    """賃料の「予測」はしません。想定利回りで次元を置き換えるだけです。

    1つの数字に決め打ちすると、出てきた数字が一人歩きします。式と仮定を
    出力に載せるので、読み手は自分の利回り観で引き直せます。
    """
    from kaigyou_core.dataset import rent_estimate

    out = rent_estimate(782_000, {"yield_range": [0.06, 0.10]})
    assert out is not None
    # 782,000 × 3.305785 × 0.06 ÷ 12
    assert out["monthly_yen_per_tsubo_low"] == 12926
    assert out["monthly_yen_per_tsubo_high"] == 21543
    assert out["monthly_yen_per_tsubo_low"] < out["monthly_yen_per_tsubo_high"]
    assert "想定利回り" in out["note"] and "募集賃料ではありません" in out["note"]
    assert "3.305785" in out["formula"]
    # 地価が無い地点では黙って 0 を出さない。
    assert rent_estimate(None, {}) is None
    assert rent_estimate(0, {}) is None


def test_the_rent_estimate_reaches_the_report(dataset):
    from kaigyou_intel.report import to_markdown

    markdown = to_markdown(_report_output().model_dump(), to_jsonable(dataset))
    if (dataset.get("cost") or {}).get("rent_estimate"):
        assert "#### 賃料の目安（地価からの換算）" in markdown
        assert "月額（円/坪）" in markdown


# ------------------------------------------------ 関数で回す（PCを常駐させない）
def test_one_tick_runs_one_step_and_puts_the_job_back(conn, dataset, monkeypatch):
    """5段を1回の関数呼び出しには収められません。

    Vercel の実行時間上限は Hobby で300秒、Pro で800秒。分析は通しで10〜20分です。
    1呼び出し=1ステップにして、続きは次の呼び出しに任せます。
    """
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    monkeypatch.setattr(worker, "RUNNERS", {
        n: (lambda _p, _c=None, n=n: ({"n": n}, llm.Usage(), []))
        for n in jobs.STEP_NAMES})
    monkeypatch.setattr(worker, "build_input", lambda conn, job, number: {})
    try:
        # claim_job は「いちばん古い queued」を拾うので、順番を当てにせず
        # この Job を名指しして進めます。見たいのは 1 呼び出しの粒度です。
        jobs.claim_specific(conn, job_id)
        first = worker.advance(conn, job_id)
        assert first["step"] == 1 and first["status"] == "queued"
        # 走り終わって queued に戻っているので、次の呼び出しが続きを拾える。
        assert jobs.get_job(conn, job_id)["status"] == "queued"
        assert jobs.next_step(conn, job_id) == 2

        for _ in range(len(jobs.STEP_NAMES) - 1):
            jobs.claim_specific(conn, job_id)
            worker.advance(conn, job_id)
        assert jobs.get_job(conn, job_id)["status"] == "completed"
        assert worker.advance(conn, job_id)["status"] == "completed"
    finally:
        _drop_job(job_id)


def test_a_tick_with_nothing_queued_says_it_is_idle(conn, monkeypatch):
    """待っているものが無いときに、何かを掴んだふりをしない。

    待ち行列そのものは他のテストと共有なので、掴む口だけ差し替えます。
    """
    from kaigyou_intel import jobs, worker

    monkeypatch.setattr(jobs, "claim_job", lambda _conn: None)
    monkeypatch.setattr(jobs, "recover_stale", lambda _conn, _m: [])
    assert worker.tick(conn) == {"claimed": None, "recovered": [], "status": "idle"}


def test_a_step_that_died_mid_flight_goes_back_to_the_queue(conn, dataset):
    """関数がタイムアウトすると、Job も ステップも running のまま残ります。

    手元の worker では滅多に起きませんが、関数で回すなら必ず起きます。
    済んだステップは触りません（要件 §32）。
    """
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    try:
        jobs.start_step(conn, job_id, 1, {}, {"prompt_version": "v", "model": "m"})
        jobs.finish_step(conn, job_id, 1, {"n": 1}, {})
        jobs.start_step(conn, job_id, 2, {}, {"prompt_version": "v", "model": "m"})
        with conn.cursor() as cur:  # 30分前に始まったことにする
            cur.execute("UPDATE analysis_steps SET started_at = now() - interval '30 min' "
                        "WHERE job_id = %s AND step_number = 2", (job_id,))
            cur.execute("UPDATE analysis_jobs SET status = 'running', "
                        "started_at = now() - interval '30 min' WHERE id = %s", (job_id,))
            conn.commit()

        assert job_id in jobs.recover_stale(conn, minutes=20)
        steps = {s["step_number"]: s for s in jobs.get_steps(conn, job_id)}
        assert steps[1]["status"] == "completed", "済んだステップは触らない"
        assert steps[2]["status"] == "pending"
        assert jobs.get_job(conn, job_id)["status"] == "queued"
        assert jobs.recover_stale(conn, minutes=20) == [], "戻したものを何度も戻さない"
    finally:
        _drop_job(job_id)


def test_a_fresh_step_is_not_reclaimed(conn, dataset):
    """走っている最中のものを横取りしない。"""
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    try:
        jobs.claim_specific(conn, job_id)
        jobs.start_step(conn, job_id, 1, {}, {"prompt_version": "v", "model": "m"})
        assert jobs.recover_stale(conn, minutes=20) == []
    finally:
        _drop_job(job_id)


def test_the_worker_endpoint_needs_its_own_secret(monkeypatch):
    """分析トークンとは別の鍵にします。片方が漏れても、もう片方は閉じたまま。"""
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app

    for var in ("KAIGYOU_WORKER_TOKEN", "CRON_SECRET"):
        monkeypatch.delenv(var, raising=False)
    client = TestClient(app)
    assert client.post("/api/worker/tick").status_code == 503

    monkeypatch.setenv("KAIGYOU_WORKER_TOKEN", "w0rker")
    assert client.post("/api/worker/tick").status_code == 401
    assert client.post("/api/worker/tick",
                       headers={"X-Worker-Token": "wrong"}).status_code == 401
    # Vercel Cron は Authorization: Bearer で来るので、そちらも受ける。
    assert client.post("/api/worker/tick",
                       headers={"Authorization": "Bearer w0rker"}).status_code == 200


def test_the_search_budget_can_be_set_per_deployment(monkeypatch):
    """ホスティング先で関数の実行時間の上限が違います。

    Vercel は Hobby で300秒、Pro で800秒。STEP2 は検索のたびに文脈を読み直すので、
    回数がそのまま実行時間になります。コードにも設定にも「本番はこう」と
    書かずに、環境で決めます。
    """
    monkeypatch.delenv(llm.MAX_SEARCHES_ENV, raising=False)
    assert llm.build_request(2, "s", "u")["tools"][0]["max_uses"] == \
        cfg.analysis_config()["limits"]["max_searches_total"]

    monkeypatch.setenv(llm.MAX_SEARCHES_ENV, "6")
    assert llm.build_request(2, "s", "u")["tools"][0]["max_uses"] == 6
    # 数字でない値で 0 回にしない（検索なしの STEP2 は空振りしかしません）。
    monkeypatch.setenv(llm.MAX_SEARCHES_ENV, "many")
    assert llm.build_request(2, "s", "u")["tools"][0]["max_uses"] >= 1


def test_the_report_explains_its_own_notation(dataset):
    """実測：「〔F011, F012, F013, F017, P001〕といった数字は何の数字だろうか？」

    根拠を辿れることがこのレポートの売りなので記号は消しません。ですが
    説明の無い記号は、読み手にとっては模様と同じです。
    """
    from kaigyou_intel.report import to_markdown

    markdown = to_markdown(_report_output().model_dump(), to_jsonable(dataset))
    assert "## 本文中の〔F001〕などについて" in markdown
    # 本文で使った記号だけを説明する（使っていない S001 の説明は要らない）。
    assert "**F001**" in markdown and "**M001**" in markdown
    assert "**H001**" not in markdown


def test_a_report_saved_before_a_rename_still_renders(dataset):
    """レポートは DB に何か月も残り、その間にスキーマは変わります。

    実測：questions_for_the_client を further_research に直したあと、前の形で
    保存されたレポートを開いた画面が真っ白になりました
    （Cannot read properties of undefined (reading 'length')）。
    """
    from kaigyou_intel.report import to_markdown

    old = _report_output().model_dump()
    old["questions_for_the_client"] = ["古い形の設問"]
    del old["further_research"]
    markdown = to_markdown(old, to_jsonable(dataset))
    assert "古い形の設問" in markdown
    assert "## さらに深掘りすべき調査" in markdown


# ------------------------------------------------ 止まらずに進む（自動やり直し）
def test_a_model_slip_is_retried_instead_of_stopping(conn, dataset, monkeypatch):
    """実測：出典を1つ書き間違えただけで STEP2 が止まり、人がボタンを押しに
    行くことになりました。

    モデルの言い間違いは一定の確率で起きます。数回は黙って直させます。
    """
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    calls = {"n": 0}

    def flaky(_payload, _category=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("参照が解決しませんでした: 検索結果に無い URL です")
        return {"ok": True}, llm.Usage(), []

    monkeypatch.setattr(worker, "RUNNERS", {1: flaky})
    monkeypatch.setattr(worker, "build_input", lambda conn, job, number: {})
    try:
        jobs.claim_specific(conn, job_id)
        first = worker.advance(conn, job_id)
        assert first["status"] == "retrying" and first["attempt"] == 1
        # 失敗にしない。queued のまま、次の呼び出しが同じステップを拾い直す。
        assert jobs.get_job(conn, job_id)["status"] == "queued"
        assert jobs.next_step(conn, job_id) == 1

        jobs.claim_specific(conn, job_id)
        second = worker.advance(conn, job_id)
        assert second["status"] == "queued" and second["step"] == 1
        steps = {s["step_number"]: s for s in jobs.get_steps(conn, job_id)}
        assert steps[1]["status"] == "completed"
        assert steps[1]["attempts"] == 1, "やり直した回数が残ること"
    finally:
        _drop_job(job_id)


def test_a_failure_that_will_never_pass_is_not_retried(conn, dataset, monkeypatch):
    """残高不足を3回繰り返しても、増えるのは費用だけです。"""
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    calls = {"n": 0}

    def broke(_payload, _category=None):
        calls["n"] += 1
        raise RuntimeError("Your credit balance is too low to access the Anthropic API")

    monkeypatch.setattr(worker, "RUNNERS", {1: broke})
    monkeypatch.setattr(worker, "build_input", lambda conn, job, number: {})
    try:
        jobs.claim_specific(conn, job_id)
        outcome = worker.advance(conn, job_id)
        assert outcome["status"] == "failed"
        assert calls["n"] == 1, "1回で止めること"
        assert jobs.get_job(conn, job_id)["status"] == "failed"
    finally:
        _drop_job(job_id)


def test_retries_stop_at_the_limit(conn, dataset, monkeypatch):
    """いつまでも繰り返さない。直らないものに払い続けないため。"""
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")

    def always(_payload, _category=None):
        raise RuntimeError("構造化出力を受け取れませんでした")

    monkeypatch.setattr(worker, "RUNNERS", {1: always})
    monkeypatch.setattr(worker, "build_input", lambda conn, job, number: {})
    limit = worker._retry_limit()
    try:
        outcomes = []
        for _ in range(limit + 2):
            if jobs.get_job(conn, job_id)["status"] not in ("queued", "running"):
                break
            jobs.claim_specific(conn, job_id)
            outcomes.append(worker.advance(conn, job_id)["status"])
        assert outcomes[-1] == "failed"
        assert outcomes.count("retrying") == limit - 1
    finally:
        _drop_job(job_id)


def test_an_unverifiable_source_costs_one_fact_not_the_whole_step():
    """12件中1件の URL のために、通った11件と $1 の実行を捨てない。

    出典が実在しないレポートは出典が無いレポートより悪い、という判断は
    変えていません。落としたことは unanswered に書き残します。
    """
    from kaigyou_intel.schemas import ExternalFact, Hypothesis
    from kaigyou_intel.steps.step2_research import _drop_unverifiable

    good = "https://www.city.chuo.lg.jp/toukei/jinkou.html"
    output = _step2_output(
        external_facts=[
            ExternalFact(id="C001", pattern_id="P001", statement="s",
                         source_url=good, source_title="t", confidence="high"),
            ExternalFact(id="C002", pattern_id="P001", statement="s",
                         source_url="https://www.e-stat.go.jp/stat-search/database?page=1",
                         source_title="t", confidence="low"),
        ],
        hypotheses=[
            Hypothesis(id="H001", pattern_id="P001", statement="s",
                       status="SUPPORTED", evidence=["C001"], reasoning="r",
                       confidence="high", changes=["患者層"],
                       decision_impact="主要患者を居住者ではなく勤務者に置く。"),
            Hypothesis(id="H002", pattern_id="P001", statement="s",
                       status="SUPPORTED", evidence=["C002"], reasoning="r",
                       confidence="low", changes=["設備投資"],
                       decision_impact="ユニットを増やさず個室に振る。"),
        ])

    dropped = _drop_unverifiable(output, {good})
    assert dropped == ["C002"]
    assert [f.id for f in output.external_facts] == ["C001"]
    # 根拠が全部消えた仮説も落とす。残すと根拠の無い判定になります。
    assert [h.id for h in output.hypotheses] == ["H001"]
    # 黙って落とさない。件数と id を残します。
    assert any("C002" in entry for entry in output.unanswered)


def test_failures_are_sorted_by_whether_retrying_helps():
    from kaigyou_intel import failures

    assert failures.is_retryable("検索結果に無い URL です")
    assert failures.is_retryable("Invalid JSON: EOF while parsing")
    assert failures.is_retryable("overloaded_error")
    assert not failures.is_retryable("credit balance is too low")
    assert not failures.is_retryable("モデルが応答を拒否しました")
    # 分からないものはやり直しません。繰り返すのは費用だけが増える失敗の仕方です。
    assert not failures.is_retryable("なにか未知の例外")


def test_a_number_the_previous_step_wrote_is_not_called_invented():
    """前の段が「1.5万」と書いたら、次の段はそう引用してよい。

    数値を集める側と検査する側で読み方がずれていた。集める側は「万」を
    読まずに 1.5 だけを許可し、検査する側は 15000 を探した。前の段が書いた
    文章を次の段がそのまま引用しただけで捏造として落ちる。しかも決定的なので、
    やり直しても同じところで落ちる。実測：STEP5 が2回やり直して2回とも失敗。
    """
    from kaigyou_intel.projection import allowed_numbers
    from kaigyou_intel.schemas import invented_numbers

    known = allowed_numbers({"insight": "商圏人口はおよそ1.5万人です。"})
    assert invented_numbers("商圏人口はおよそ1.5万人です。", known) == []
    assert invented_numbers("市場は1.2億円", allowed_numbers({"n": "1.2億円の市場"})) == []
    # 締めるところは締めたまま。書き換えは捕まえる。
    assert invented_numbers("商圏人口は9.9万人です。", known) == ["9.9万"]
    assert invented_numbers("48,000人", allowed_numbers({"population": 15234})) == ["48,000"]


def test_a_retry_is_told_why_the_last_one_failed(conn, dataset, monkeypatch):
    """同じプロンプトを投げ直すだけでは、決定的な失敗は永久に直らない。"""
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    seen: list[dict] = []

    def fussy(payload, _category=None):
        seen.append(dict(payload))
        raise RuntimeError("構造化出力を受け取れませんでした")

    monkeypatch.setattr(worker, "RUNNERS", {1: fussy})
    monkeypatch.setattr(worker, "build_input", lambda conn, job, number: {"a": 1})
    try:
        jobs.claim_specific(conn, job_id)
        worker.advance(conn, job_id)
        jobs.claim_specific(conn, job_id)
        worker.advance(conn, job_id)

        assert "_前回の失敗" not in seen[0], "1回目は前回が無い"
        assert "構造化出力" in seen[1]["_前回の失敗"]["内容"], "2回目は理由を渡す"
    finally:
        _drop_job(job_id)


def test_a_run_that_never_came_back_counts_as_an_attempt(conn, dataset):
    """関数が強制終了されると例外が飛ばない。数えないと永久に回り続ける。"""
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    try:
        jobs.start_step(conn, job_id, 1, {}, {"prompt_version": "v", "model": "m"})
        with conn.cursor() as cur:   # 上限に当たって消えた実行を作る
            cur.execute("UPDATE analysis_steps SET started_at = now() - interval '2 hours' "
                        "WHERE job_id = %s AND step_number = 1", (job_id,))
        conn.commit()

        assert jobs.attempts_for(conn, job_id, 1) == 0
        jobs.recover_stale(conn, 20)
        assert jobs.attempts_for(conn, job_id, 1) == 1, "消えた実行も1回と数える"
        assert "上限" in (jobs.last_error(conn, job_id, 1) or "")
    finally:
        _drop_job(job_id)


def test_a_step_that_keeps_dying_is_given_up_on(conn, dataset, monkeypatch):
    """例外を経ずに消え続けるステップも、いつか打ち切る。"""
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    try:
        for _ in range(worker._retry_limit()):
            jobs.start_step(conn, job_id, 1, {}, {"prompt_version": "v", "model": "m"})
            with conn.cursor() as cur:
                cur.execute("UPDATE analysis_steps SET started_at = now() - interval '2 hours' "
                            "WHERE job_id = %s AND step_number = 1", (job_id,))
            conn.commit()
            jobs.recover_stale(conn, 20)

        jobs.claim_specific(conn, job_id)
        # RUNNERS を触らないので、走ってしまえば本物を呼ぼうとする。
        # 走らせずに打ち切ることを確かめる。
        outcome = worker.advance(conn, job_id)
        assert outcome["status"] == "failed"
        assert jobs.get_job(conn, job_id)["status"] == "failed"
    finally:
        _drop_job(job_id)


def test_the_wait_for_a_lost_run_matches_the_environment(monkeypatch):
    """関数の上限より長く待つのは、ただの空白。

    手元の worker 用の 20 分をホスティング環境でも使っていたので、5 分で
    死んだステップに気づくまで 15 分かかっていた。画面には経過時間だけが
    増え続け、動いているように見える。実測：STEP4 が 15分7秒。
    """
    from kaigyou_intel import worker

    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    local = worker.stale_after()

    monkeypatch.setenv("VERCEL", "1")
    hosted = worker.stale_after()

    assert hosted < local, "関数のほうが短く待つ"
    # 具体的な秒数は vercel.json と突き合わせて test_deployment 側で見ます。
    # ここで見るのは「環境によって変わること」だけ。
    assert hosted > 0


def test_one_call_runs_as_many_steps_as_the_time_allows(conn, dataset, monkeypatch):
    """ステップの間に cron の間隔をまるごと空けない。

    1 呼び出し 1 ステップだと、5 段で最大5分（平均2分半）をただ待つことに
    なる。関数の上限が 800秒 になったので、残り時間が足りるうちは続ける。
    """
    from kaigyou_intel import client as llm
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    monkeypatch.setattr(worker, "build_input", lambda conn, job, number: {})
    monkeypatch.setattr(worker, "RUNNERS", {
        n: (lambda _p, _c=None: ({"ok": True},
                                 llm.Usage(input_tokens=10, output_tokens=5), []))
        for n in sorted(jobs.STEP_NAMES)})
    monkeypatch.setattr(worker.report, "save", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(worker, "time_budget", lambda: (800.0, 420.0))
    # tick は「いちばん古い queued」を拾います。他のテストが残した Job を
    # 掴まないよう、この Job を指すよう固定します。
    monkeypatch.setattr(jobs, "claim_job", lambda _conn: job_id)
    try:
        outcome = worker.tick(conn)
        assert outcome["status"] == "completed"
        assert outcome["steps_completed"] == sorted(jobs.STEP_NAMES), \
            "時間が足りるなら1回で最後まで"
    finally:
        _drop_job(job_id)


def test_a_call_stops_before_it_would_be_killed(conn, dataset, monkeypatch):
    """足りるか分からないなら進まない。途中で殺されると丸ごとやり直しになる。"""
    from kaigyou_intel import client as llm
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    monkeypatch.setattr(worker, "build_input", lambda conn, job, number: {})
    monkeypatch.setattr(worker, "RUNNERS", {
        n: (lambda _p, _c=None: ({"ok": True},
                                 llm.Usage(input_tokens=10, output_tokens=5), []))
        for n in sorted(jobs.STEP_NAMES)})
    # 残り時間が最初から足りない設定。1 段だけ進んで手を引くこと。
    monkeypatch.setattr(worker, "time_budget", lambda: (100.0, 420.0))
    monkeypatch.setattr(jobs, "claim_job", lambda _conn: job_id)
    try:
        outcome = worker.tick(conn)
        assert outcome["steps_completed"] == [1], "1段で止める"
        assert jobs.get_job(conn, job_id)["status"] == "queued", "続きは次の呼び出しへ"
    finally:
        _drop_job(job_id)


def test_the_local_worker_has_no_time_limit(monkeypatch):
    """手元の worker は上限が無いので、時間を理由に手を引かない。"""
    from kaigyou_intel import worker

    for var in ("VERCEL", "AWS_LAMBDA_FUNCTION_NAME", "K_SERVICE"):
        monkeypatch.delenv(var, raising=False)
    budget, reserve = worker.time_budget()
    assert budget == float("inf") and reserve == 0.0

    monkeypatch.setenv("VERCEL", "1")
    budget, reserve = worker.time_budget()
    assert budget > reserve > 0, "関数側は有限で、余裕を残す"


def test_either_configured_secret_wakes_the_worker(monkeypatch):
    """移行期は両方が設定されている。片方だけを正解にすると cron が黙って落ちる。

    以前は `KAIGYOU_WORKER_TOKEN or CRON_SECRET` で先に見つかったほうだけを
    正解にしていた。両方を別々の値で設定すると、Vercel Cron が送る
    Bearer <CRON_SECRET> が KAIGYOU_WORKER_TOKEN と比べられて 401 になる。
    cron の失敗は画面に出ないので、Job が「順番待ち」のまま止まるだけに見えた。
    """
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app

    monkeypatch.setenv("KAIGYOU_WORKER_TOKEN", "pg-cron-no-kagi")
    monkeypatch.setenv("CRON_SECRET", "vercel-cron-no-kagi")
    client = TestClient(app)

    # Supabase の pg_cron はヘッダで送る。
    assert client.post("/api/worker/tick",
                       headers={"X-Worker-Token": "pg-cron-no-kagi"}).status_code != 401
    # Vercel Cron は Bearer で送る。
    assert client.post("/api/worker/tick",
                       headers={"Authorization": "Bearer vercel-cron-no-kagi"}
                       ).status_code != 401
    # 知らない鍵は通さない。
    assert client.post("/api/worker/tick",
                       headers={"X-Worker-Token": "shiranai"}).status_code == 401
    assert client.post("/api/worker/tick").status_code == 401


def test_the_worker_says_so_when_no_secret_is_configured(monkeypatch):
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app

    monkeypatch.delenv("KAIGYOU_WORKER_TOKEN", raising=False)
    monkeypatch.delenv("CRON_SECRET", raising=False)
    res = TestClient(app).post("/api/worker/tick")
    assert res.status_code == 503
    assert "CRON_SECRET" in res.json()["detail"]


def test_the_worker_answers_a_get_because_that_is_what_cron_sends(monkeypatch):
    """Vercel Cron は GET で叩く。POST だけにしていたので 404 を返していた。

    実測（Vercel の Logs）：毎分きっちり呼ばれていて、毎回 404。
    「cron が動いていない」ように見えるが、動いていて断られていた。
    Supabase の pg_net は POST なので、両方受ける。
    """
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app

    monkeypatch.setenv("CRON_SECRET", "kagi")
    client = TestClient(app)
    headers = {"Authorization": "Bearer kagi"}

    for method in ("get", "post"):
        res = getattr(client, method)("/api/worker/tick", headers=headers)
        assert res.status_code != 404, f"{method.upper()} が 404 では cron が空振りする"
        assert res.status_code != 405, f"{method.upper()} が 405 でも同じこと"

    # 鍵が無ければ、どちらの方法でも断る。
    assert client.get("/api/worker/tick").status_code == 401
    assert client.post("/api/worker/tick").status_code == 401


def test_a_read_only_disk_does_not_throw_away_the_report(conn, dataset, monkeypatch, tmp_path):
    """おまけの書き出しの失敗で、$1.29 のレポートを捨てない。

    ホスティングされた関数のファイルシステムは読み取り専用。実測：
    OSError: [Errno 30] Read-only file system: 'reports'。
    レポート本体は DB に入っているので、ファイルは便宜でしかない。
    """
    from kaigyou_intel import jobs, report

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    try:
        jobs.start_step(conn, job_id, 5, {}, {"prompt_version": "v", "model": "m"})
        monkeypatch.setattr(report, "markdown_for", lambda *a, **k: "# レポート\n")

        def read_only(*_a, **_k):
            raise OSError(30, "Read-only file system", "reports")

        monkeypatch.setattr(pathlib.Path, "mkdir", read_only)
        assert report.write_file(conn, job_id, str(tmp_path)) is None, "例外を上げない"
    finally:
        _drop_job(job_id)


def test_the_report_file_is_still_written_where_it_can_be(conn, dataset, monkeypatch, tmp_path):
    """手元で回すときは、これまでどおりファイルが手に入る。"""
    from kaigyou_intel import jobs, report

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    try:
        monkeypatch.setattr(report, "markdown_for", lambda *a, **k: "# レポート\n")
        path = report.write_file(conn, job_id, str(tmp_path))
        assert path is not None and path.read_text(encoding="utf-8") == "# レポート\n"
    finally:
        _drop_job(job_id)


def test_the_outlook_says_when_it_was_not_obtained():
    """無いものを黙って省かない。読み手が「明るいから触れていない」と読める。"""
    from kaigyou_intel.report import to_markdown

    md = to_markdown({"title": "t", "sections": []},
                     {"demand": {"outlook": {"available": False,
                                             "note": "この地域の将来推計人口は取り込まれていません。"}}})
    assert "将来推計人口" in md
    assert "取得できていません" in md


def test_the_outlook_is_shown_as_published_with_its_basis():
    """基準年と推計の名前を落とさない。「2040年の人口」だけでは出典を辿れない。"""
    from kaigyou_intel.report import to_markdown

    md = to_markdown({"title": "t", "sections": []}, {"demand": {"outlook": {
        "available": True,
        "base_year": 2020,
        "estimate_label": "平成30年国政局推計",
        "years": [
            {"year": 2020, "population": 16167, "age_0_14": 2307,
             "age_65_plus": 4200, "elderly_share": 0.26, "index_vs_base": 1.0},
            {"year": 2040, "population": 12610, "age_0_14": 1420,
             "age_65_plus": 5300, "elderly_share": 0.42, "index_vs_base": 0.78},
        ]}}})
    assert "平成30年国政局推計" in md and "基準年 2020" in md
    assert "推計であって予測ではなく" in md
    assert "12,610" in md and "42.0%" in md
    assert "78" in md, "基準年比の指数"


def _fake_projection_shapefile(tmp_path, fields, rows):
    """本物と同じ形の 500m メッシュ shapefile を作る（zip 入り）。"""
    import shapefile
    import zipfile

    base = tmp_path / "500m_mesh_suikei"
    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYGON)
    for name in fields:
        writer.field(name, "C", size=32)
    for row in rows:
        writer.poly([[[138.86, 35.10], [138.87, 35.10],
                      [138.87, 35.11], [138.86, 35.11], [138.86, 35.10]]])
        writer.record(*[row.get(f, "") for f in fields])
    writer.close()

    archive = tmp_path / "mesh.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for suffix in (".shp", ".shx", ".dbf"):
            zf.write(str(base) + suffix, "500m_mesh_suikei" + suffix)
    return archive


def _projection_adapter(spec_overrides=None):
    from kaigyou_core import config as cfg
    from kaigyou_etl.adapters import AdapterContext
    from kaigyou_etl.adapters.mlit_future_population import MLITFuturePopulationAdapter

    spec = dict(cfg.sources_config()["sources"]["mlit_future_population"])
    spec.update(spec_overrides or {})
    return MLITFuturePopulationAdapter(
        AdapterContext(source_id="mlit_future_population", spec=spec,
                       defaults={}, raw_dir=pathlib.Path("."),
                       prefecture_override="22"))


def test_future_population_is_read_year_by_year(tmp_path):
    """年を行にしてあるので、手に入った年だけが入る。"""
    fields = ["MESH_ID", "PTN_2020", "PTA_2020", "PTC_2020", "PTN_2040", "PTC_2040"]
    archive = _fake_projection_shapefile(tmp_path, fields, [
        {"MESH_ID": "52386278", "PTN_2020": "1200.5", "PTA_2020": "180",
         "PTC_2020": "300", "PTN_2040": "930.２".replace("２", "2"), "PTC_2040": "410"},
    ])
    adapter = _projection_adapter({"years": [2020, 2040, 2050]})

    facts = adapter.validate(archive)
    assert facts["years_loaded"] == [2020, 2040]
    assert facts["years_configured_but_missing"] == [2050], "無い年は無いと言う"
    # round(1200.5) は 1200（偶数丸め）。集計の要約なので実害は無い。
    # 保存されるのは 1200.5 のまま（下の transform で確かめる）。
    assert facts["population_total_by_year"] == {2020: 1200, 2040: 930}

    rows = list(adapter.transform(archive))
    assert {r["projection_year"] for r in rows} == {2020, 2040}
    got = {r["projection_year"]: r for r in rows}
    assert got[2040]["population"] == 930.2
    assert got[2040]["age_65_plus"] == 410.0
    assert got[2040]["age_0_14"] is None, "その年に無い列は空のまま。0 で埋めない"
    assert got[2020]["population"] == 1200.5, "推計値は按分の結果。丸めない"
    assert got[2020]["base_year"] == 2020 and got[2020]["mesh_size_m"] == 500


def test_a_changed_release_fails_loudly_instead_of_loading_nothing(tmp_path):
    """属性名が合わないとき、0 件で終わらせない。

    「0 件」だと、人が住んでいないのか列名が違うのかが区別できない。
    実際の属性名を並べて落とす。
    """
    from kaigyou_etl.acquisition import AcquisitionError

    archive = _fake_projection_shapefile(
        tmp_path, ["MESH_ID", "POP2020", "POP2040"],
        [{"MESH_ID": "52386278", "POP2020": "1200", "POP2040": "930"}])
    adapter = _projection_adapter()

    with pytest.raises(AcquisitionError) as caught:
        adapter.validate(archive)
    assert "POP2020" in str(caught.value), "実際の属性名を見せる"
    assert "sources.yaml" in str(caught.value), "直す場所を示す"


def test_the_outlook_table_stays_readable():
    """公表は 5年刻みで 11 点。全部並べると 11 列になり、読み物にならない。

    40代で開業する人にとって 2070 年は引退のはるか先で、意思決定に効かない。
    10年刻みで 30年先まで。絞ったことは黙らずに書く。
    """
    from kaigyou_intel.report import to_markdown

    years = [{"year": y, "population": 1000 - (y - 2020),
              "age_65_plus": 300, "elderly_share": 0.3,
              "index_vs_base": 1.0} for y in range(2020, 2075, 5)]
    md = to_markdown({"title": "t", "sections": []}, {"demand": {"outlook": {
        "available": True, "base_year": 2020,
        "estimate_label": "令和6年国政局推計", "years": years}}})

    table = [ln for ln in md.splitlines() if ln.startswith("| 年 |")][0]
    shown = [c.strip() for c in table.split("|")[2:-1]]
    assert shown == ["2020（推計基準年）", "2030", "2040", "2050"], f"出た年: {shown}"
    assert "2070" not in table, "引退のはるか先まで並べない"
    assert "10年刻みで抜粋" in md, "絞ったことを黙らない"


def test_every_headcount_says_how_it_was_counted():
    """**ここを混ぜると、足してはいけないものを足します。**

    実測（早稲田駅・半径1km）で、同じ「働く人」が経済センサス 52,688 人
    （従業地基準）に対して国勢調査 22,322 人（常住地基準）。2.4 倍の差は
    誤差ではなく、前者が昼間そこにいる人、後者が夜そこにいる人だからです。
    「同じ国勢調査だから」という理由で後者に揃えると、昼間の人が半分以下に
    なります。
    """
    from kaigyou_core.dataset import MEASUREMENT_BASES

    assert set(MEASUREMENT_BASES) == {"workplace", "residence",
                                      "workplace_or_school"}
    text = cfg.prompt_text("step1_features.md")
    assert "数え方が3通り" in text
    assert "52,688" in text and "22,322" in text, "差を数字で見せる"
    # 足し算の禁止を、具体例で書いていること。
    assert "書けない：「昼間人口は 56,490 人" in text
    assert "この商圏に学生はいない" in text, "数えていないことと居ないことは別"


def test_the_basis_note_appears_next_to_the_tables(dataset):
    """表の並びの中で1回だけ言います。離れた場所の注記は読まれません。"""
    from kaigyou_intel.report import _basis_note

    text = "\n".join(_basis_note(dataset))
    assert "従業地基準" in text and "常住地基準" in text
    assert "基準の違う数字を足さないでください" in text
    assert "52,688" in text and "22,322" in text
    # 昼間人口が未取得なら、そう名乗ること。
    census = ((dataset.get("demand") or {}).get("daytime")
              or {}).get("census_daytime") or {}
    if not census.get("available"):
        assert "未取得" in text and "通学者は数えられていません" in text


def test_the_student_count_says_it_is_residents_not_arrivals(dataset):
    """在学者は既に取り込んであります。ただし**そこに住んでいる学生**です。

    早稲田駅前の商圏に住む大学・大学院生は 3,802 人。早稲田大学に通ってくる
    数万人は、この数字に1人も含まれていません。従業者数に足しても昼間人口には
    なりません。
    """
    from kaigyou_intel.projection import for_step1

    entries = {c["key"]: c for c in for_step1(dataset)["citable"]}
    key = "schooling.university"
    if key not in entries:
        pytest.skip("この環境には就業状態等基本集計が取り込まれていません")
    note = entries[key]["note"]
    assert "通ってくる学生ではありません" in note
    assert "足しても昼間人口にはなりません" in note
    # 常住地基準の就業者にも、同じ区別が付いていること。
    assert "別のものを数えています" in entries["schooling.workers_living_here"]["note"]


def test_the_resident_profile_is_not_mistaken_for_daytime_population(dataset):
    """就業状態等基本集計は**常住地基準**です。昼間人口ではありません。

    実測：大学・大学院在学者がいちばん多いメッシュでも 835 人、早稲田駅の
    メッシュで 393 人。通学地基準ならキャンパスのメッシュに数万人が出る
    はずで、出ていません。「そこに住んでいる学生」であって「そこに通って
    くる学生」ではない、と data 自身に書かせます。
    """
    profile = ((dataset.get("demand") or {}).get("residents") or {}).get("profile")
    if profile is None:
        pytest.skip("この版のデータセットには居住者プロファイルがありません")
    if not profile["available"]:
        assert profile["reason"] in ("not_loaded", "not_migrated")
        return
    assert "常住地基準" in profile["definition"]
    assert "昼間人口では" in profile["definition"]
    assert "通ってくる在学者ではありません" in profile["schooling"]["note"]


def test_the_commute_modes_are_shares_not_a_headcount():
    """1人が複数の手段を使うので、合計は人数と一致しません。**比率で読むもの。**"""
    from kaigyou_intel.report import _resident_profile_block

    text = "\n".join(_resident_profile_block({
        "available": True,
        "commute_modes": [
            {"key": "commute_rail", "label": "鉄道・電車", "people": 13452,
             "share": 0.593},
            {"key": "commute_car", "label": "自家用車", "people": 644,
             "share": 0.028}],
        "car_share": 0.028,
        "residence": {"under_1_year": 3424, "twenty_years_plus": 10305,
                      "note": "リコールの回り方が違います。"},
        "schooling": {"university": 3802,
                      "note": "そこに通ってくる在学者ではありません。"},
        "definition": "常住地基準であり、昼間人口ではありません。"}))
    assert "常住地基準" in text
    assert "59.3%" in text and "2.8%" in text
    assert "合計は人数と一致しません" in text
    assert "来院手段そのものではありません" in text


def test_the_parking_question_finally_has_something_behind_it():
    """「来院手段のデータは手元にありません」としか書けませんでした。

    通勤・通学の交通手段は来院手段そのものではありませんが、その地域で車が
    使われるかどうかの手がかりにはなります。**手がかりであって答えではない**
    ことを、枠に書いておきます。
    """
    frame = cfg.hypotheses_config()
    parking = next(r for r in frame["requirements"] if r["id"] == "parking")
    assert any("commute.car_share" in x for x in parking["decided_by"])
    assert "来院手段では" in parking["note"]
    assert "現地で確かめる" in parking["note"]
    # 居住期間という新しい論点も入っていること。
    assert any(r["id"] == "recall_base" for r in frame["requirements"])


def test_the_resident_profile_loader_refuses_a_daytime_table():
    """通学地基準の表を間違えて入れると、大学生が1メッシュに数万人という形で
    現れます。常住なら千人の桁です（実測：東京都で最大 835 人）。"""
    from kaigyou_core import config as _cfg
    from kaigyou_etl.adapters import ADAPTERS

    spec = _cfg.sources_config()["sources"]["estat_resident_profile"]
    assert spec["adapter"] in ADAPTERS
    # 交通手段と居住期間が取り込み対象に入っていること。これが無いなら、
    # この取り込みは足す価値がありません。
    assert "commute_car" in spec["columns"]
    assert "resident_20y_plus" in spec["columns"]


def test_the_municipality_daytime_is_never_offered_as_a_catchment_figure(dataset):
    """新宿区の昼間人口 79万人のうち何人が早稲田駅前の半径1km にいるかは、
    市区町村の表からは分かりません。歌舞伎町にも西新宿にもいます。

    **面積で按分するのは完全に間違いです。** 取り違えを防ぐ手立ては、
    キーとラベルと注記に「市区町村全体」と書くことだけです。
    """
    from kaigyou_intel.projection import for_step1

    town = (dataset.get("location") or {}).get("daytime") or {}
    if not town.get("available"):
        assert town.get("reason") in ("not_loaded", "not_migrated", "no_municipality")
        return
    assert "この商圏の数字ではありません" in town["definition"]

    entries = {c["key"]: c for c in for_step1(dataset)["citable"]}
    key = "municipality_daytime.population"
    assert key in entries, "引けるなら、引けるキーとして出す"
    assert "全体" in entries[key]["label"], "ラベルで商圏と区別する"
    assert "この商圏の数字ではありません" in entries[key]["note"]
    # 商圏の昼間人口とは別のキーであること。同じキーだと混ざります。
    assert key != "daytime.population"


def test_the_municipality_block_is_labelled_so_it_cannot_be_misread():
    """同じページに商圏の数字と並ぶので、区別が付かないと取り違えます。"""
    from kaigyou_intel.report import _municipality_daytime_block

    text = "\n".join(_municipality_daytime_block({
        "available": True, "municipality_name": "新宿区",
        "night_population": 349385, "daytime_population": 793528,
        "daytime_over_night": 2.271,
        "by_age": [{"age_band": "03_20～24歳", "night_population": 21906,
                    "daytime_population": 80136, "daytime_over_night": 3.658}],
        "young_inflow": {"note": "学生数ではありません。"},
        "definition": "市区町村全体の数字であって、この商圏の数字ではありません。"}))
    assert "新宿区全体" in text
    assert "この商圏の数字ではありません" in text
    assert "3.66倍" in text, "年齢別の膨らみ方が、学生流入の手がかり"
    assert "学生数ではありません" in text
    # 取り込んでいなければ、節そのものを出しません（商圏の欄と違い、
    # これは無くても分析が成立するので）。
    assert _municipality_daytime_block({"available": False}) == []
    assert _municipality_daytime_block(None) == []


def test_the_municipality_loader_rejects_a_wrong_area_filter():
    """全国の昼間人口は夜間人口と一致します（国内で移動するだけなので）。

    実測：地域識別コードを "0"（政令市の区＋特別区）だけに絞っていて、
    都市部に偏ったため全国合計の比が 1.090 になりました。**この検算が
    思い込みを捕まえました。** 正しくは {0, 2, 3}（区・市・町村）です。
    """
    from kaigyou_etl.adapters.estat_daytime_municipality import _MUNICIPALITY_LEVELS

    assert _MUNICIPALITY_LEVELS == {"0", "2", "3"}
    # "1"（政令市計）を入れると、その区と二重計上になります。
    assert "1" not in _MUNICIPALITY_LEVELS
    assert "a" not in _MUNICIPALITY_LEVELS


def test_the_daytime_population_counts_the_people_who_are_not_workers(dataset):
    """実測：早稲田駅前のレポートは、経済センサスの従業者数 52,688 人を
    昼間の人の代理として使い、**大学生に一言も触れませんでした**。

    学生は従業者ではないので、経済センサスには1人も現れません。歯科医院に
    とって、20代前半の数万人がそこにいるかどうかは診療内容も診療時間も
    変える情報です。
    """
    daytime = (dataset["demand"]["daytime"]).get("census_daytime")
    assert daytime is not None, "取れていないなら、取れていないと言う欄が要ります"
    if not daytime["available"]:
        # 取り込んでいない環境。**黙って0にせず、理由を名乗ること。**
        assert daytime["reason"] in ("not_loaded", "not_migrated")
        assert "通学者" in daytime["note"]
        return
    assert daytime["students_here"] is not None
    # 内訳が総数を超えないこと。就業者も通学者も昼間人口の内数です。
    assert (daytime["workers_here"] + daytime["students_here"]
            <= daytime["population"])
    assert daytime["other_here"] >= 0


def test_the_daytime_block_says_so_when_it_is_not_loaded():
    """黙って省くと、読み手には「そこに学生はいない」と読めます。"""
    from kaigyou_intel.report import _daytime_block

    text = "\n".join(_daytime_block(
        {"available": False, "reason": "not_loaded",
         "note": "取り込まれていません。通学者（大学生など）は含まれていません。"}))
    assert "取得できていません" in text and "通学者" in text
    # 欄そのものが無いときは、見出しも出しません（そういう版のレポート）。
    assert _daytime_block(None) == []


def test_the_two_daytime_figures_are_not_added_together(dataset):
    """経済センサスの従業者数と、国勢調査の従業地・通学地による就業者数は、
    調査も定義も違います。足すと二重計上です。

    別のキーで持ち、ラベルにもそう書きます。
    """
    from kaigyou_intel.projection import citable_keys, for_step1

    keys = citable_keys(for_step1(dataset))
    if "daytime.population" not in keys:
        pytest.skip("この環境には昼間人口が取り込まれていません")
    payload = for_step1(dataset)
    entries = {c["key"]: c for c in payload["citable"]}
    assert "足さないこと" in entries["daytime.population"]["note"]
    assert "経済センサスには現れない" in entries["daytime.students_here"]["note"]


def test_the_caveats_name_what_the_worker_count_misses():
    """「従業者数は昼間人口ではありません」だけでは、何が抜けているのかが
    読み手に伝わりません。**通学者**と名指しします。"""
    from kaigyou_core.dataset import _dataset_caveats

    text = " ".join(_dataset_caveats())
    assert "通学者が" in text and "大学" in text
    assert "来街者" in text, "買い物・観光の来街はどちらの調査にも入りません"


def test_the_daytime_adapter_is_registered():
    """設定に書いてもアダプタが無ければ、`run` はそこで止まります。"""
    from kaigyou_core import config as _cfg
    from kaigyou_etl.adapters import ADAPTERS

    spec = _cfg.sources_config()["sources"]["estat_daytime_mesh"]
    assert spec["adapter"] in ADAPTERS
    # 必須は 2 つだけ。内訳は取れなければ NULL のまま（0 で埋めない）。
    assert {"mesh_code", "daytime_population"} <= set(spec["columns"])


def test_the_industry_table_adds_up():
    """実測：「第3次産業 49,203」「教育・学習支援 13,245」「第2次産業 3,474」
    「医療・福祉 5,978」…と同じ字下げで並べていました。

    1行目に3行目以降が含まれているのに、見た目が同じです。読み手が合計を
    取ると二重計上になります。親子を字下げで示し、名前の付いた内訳を引いた
    残りも行として出します。
    """
    from kaigyou_core.dataset import _industry_tree

    mix = _industry_tree(
        {"secondary": {"workers": 3474, "establishments": 407},
         "tertiary": {"workers": 49203, "establishments": 2583},
         "wholesale_retail": {"workers": 5180, "establishments": 534},
         "accommodation_food": {"workers": 3418, "establishments": 351},
         "education": {"workers": 13245, "establishments": 119},
         "health_welfare": {"workers": 5978, "establishments": 264}},
        {"workers": 52688, "establishments": 2992})

    rows = {r["key"]: r for r in mix["divisions"]}
    # 第3次の内訳が、名前つき4つ＋残りで親に一致すること。
    children = ["wholesale_retail", "accommodation_food", "education",
                "health_welfare", "tertiary_other"]
    assert sum(rows[k]["workers"] for k in children) == rows["tertiary"]["workers"]
    # 上位の3分類＋差分が全産業に一致すること。
    assert (rows["secondary"]["workers"] + rows["tertiary"]["workers"]
            + rows["unclassified"]["workers"]) == 52688
    # 引き算で出したものは、そう名乗ること。
    assert rows["tertiary_other"]["derived"] and rows["unclassified"]["derived"]
    assert not rows["education"]["derived"]
    # 残りは内訳のいちばん最後。前に出すと何の残りか読み取れません。
    order = [r["key"] for r in mix["divisions"]]
    assert order.index("tertiary_other") == order.index("health_welfare") + 1


def test_the_industry_table_shows_the_nesting():
    from kaigyou_core.dataset import _industry_tree
    from kaigyou_intel.report import _industry_block

    mix = _industry_tree(
        {"tertiary": {"workers": 100, "establishments": 10},
         "education": {"workers": 40, "establishments": 4}},
        {"workers": 120, "establishments": 12})
    text = "\n".join(_industry_block(mix))
    assert "| **全産業** | 120 | 12 |" in text
    # 全角スペースで段を作ります。Markdown は半角の字下げを落とします。
    assert "| 　第3次産業 |" in text
    assert "| 　　教育・学習支援 |" in text
    assert "| 　　その他の第3次産業＊ | 60 | 6 |" in text
    assert "測った値ではなく" in text


def test_a_rounding_overshoot_does_not_produce_a_negative_row():
    """按分の丸めで内訳の合計が親を超えることがあります。**負の従業者数は
    出しません。** 止めたことは残します。"""
    from kaigyou_core.dataset import _industry_tree

    mix = _industry_tree(
        {"tertiary": {"workers": 100, "establishments": 10},
         "education": {"workers": 103, "establishments": 11}},
        {"workers": 100, "establishments": 10})
    rest = next(r for r in mix["divisions"] if r["key"] == "tertiary_other")
    assert rest["workers"] == 0 and rest["establishments"] == 0
    assert "0 にしました" in rest["note"]


def test_the_industry_keys_stay_citable_with_their_nesting(dataset):
    """「教育・学習支援の従業者数」と「第3次産業の従業者数」を、同じ段の
    数字として足されると困ります。ラベルに親を書きます。"""
    from kaigyou_intel.projection import for_step1

    citable = {c["key"]: c for c in for_step1(dataset)["citable"]}
    nested = [c for k, c in citable.items() if k.startswith("industry.")]
    if not nested:
        pytest.skip("この環境には産業別のデータがありません")
    child = citable.get("industry.education.workers")
    if child:
        assert "内訳" in child["label"]


def test_the_base_year_is_filled_in_from_the_census():
    """将来推計の公表値は、基準年について総人口しか持ちません。

    そのままだと基準年の列が「—」だらけになり、読み手には「分からない」に
    見えます。ところが 2020 年の年齢内訳は国勢調査にあり、この商圏について
    既に集計済みです。

    **ただし合わせて1つの数にはしません。** 実測でも総人口が 66,965（推計の
    基準年）と 66,817（国勢調査）のように少し違います。別の集合を別の方法で
    数えたものなので、列を分けて、違う理由を書きます。
    """
    from kaigyou_core.dataset import _outlook_with_actual
    from kaigyou_intel.report import to_markdown

    outlook = _outlook_with_actual(
        {"available": True, "base_year": 2020, "estimate_label": "令和6年国政局推計",
         "years": [{"year": 2020, "population": 66965, "index_vs_base": 1.0},
                   {"year": 2030, "population": 71096, "age_65_plus": 13647,
                    "elderly_share": 0.192, "index_vs_base": 1.06}]},
        {"population": 66817, "age_0_14": 5709, "age_15_64": 42450,
         "age_65_plus": 12981, "households": 41574})
    md = to_markdown({"title": "t", "sections": []},
                     {"demand": {"outlook": outlook}})

    table = [ln for ln in md.splitlines() if ln.startswith("| 年 |")][0]
    shown = [c.strip() for c in table.split("|")[2:-1]]
    assert shown[0] == "2020（実績）" and shown[1] == "2020（推計基準年）"
    # 実績の年齢内訳が入っていること。
    assert "| 0〜14歳 | 5,709 |" in md
    assert "| 65歳以上の割合 | 19.4% |" in md
    # 混ぜていないこと。推計の基準年の総人口はそのまま。
    assert "| 総人口 | 66,817 | 66,965 |" in md
    assert "別の数え方" in md
    # 75歳以上は国勢調査メッシュに無い。**0 と書かない。**
    row = [ln for ln in md.splitlines() if ln.startswith("| 75歳以上 |")][0]
    assert row.split("|")[2].strip() == "—"


def test_the_base_year_column_is_dropped_when_there_is_no_census_to_show():
    """実績が無ければ、列も出しません。空の列は「取れなかった」ではなく
    「そういう年がある」に見えます。"""
    from kaigyou_core.dataset import _outlook_with_actual

    outlook = {"available": True, "base_year": 2020, "years": [{"year": 2020}]}
    assert "actual" not in _outlook_with_actual(outlook, {})
    assert "actual" not in _outlook_with_actual({"available": False}, {"population": 1})


def test_a_short_series_is_shown_in_full():
    """点が少ないなら絞らない。表が消えるより並ぶほうがまし。"""
    from kaigyou_intel.report import to_markdown

    years = [{"year": y, "population": 1000, "index_vs_base": 1.0}
             for y in (2020, 2040)]
    md = to_markdown({"title": "t", "sections": []}, {"demand": {"outlook": {
        "available": True, "base_year": 2020, "estimate_label": "x", "years": years}}})
    assert "2040" in md and "抜粋" not in md


def test_loading_one_prefecture_does_not_erase_another(conn, tmp_path, monkeypatch):
    """東京を入れたら静岡が消えた、を繰り返さない。

    source_id だけで置換すると、1ファイル=1都道府県で配布されるデータでは
    後から入れたほうしか残らない。実測：静岡 127,985 行を入れたあとに東京を
    入れて、59,917 行になった。load は成功と報告する。
    """
    from kaigyou_etl.adapters import AdapterContext
    from kaigyou_etl.adapters.mlit_future_population import MLITFuturePopulationAdapter
    from kaigyou_core import config as cfg

    spec = dict(cfg.sources_config()["sources"]["mlit_future_population"])
    with conn.cursor() as cur:
        cur.execute("INSERT INTO data_sources (id, name, publisher, dataset_kind) "
                    "VALUES ('mlit_future_population','x','x','official') "
                    "ON CONFLICT (id) DO NOTHING")
    conn.commit()

    def load(prefecture, mesh_code):
        adapter = MLITFuturePopulationAdapter(AdapterContext(
            source_id="mlit_future_population", spec=spec, defaults={},
            raw_dir=tmp_path, prefecture_override=prefecture))
        return adapter.load(conn, [{
            "source_id": "mlit_future_population", "mesh_code": mesh_code,
            "mesh_size_m": 500, "prefecture_code": prefecture,
            "projection_year": 2040, "population": 100.0, "age_0_14": None,
            "age_15_64": None, "age_65_plus": None, "age_75_plus": None,
            "base_year": 2020, "estimate_label": "x", "source_date": None}])

    try:
        load("22", "52386278")
        load("13", "53393292")
        with conn.cursor() as cur:
            cur.execute("SELECT prefecture_code, count(*) n "
                        "FROM mesh_population_projection GROUP BY 1 ORDER BY 1")
            got = {r["prefecture_code"]: r["n"] for r in cur.fetchall()}
        assert got == {"13": 1, "22": 1}, f"両方残ること: {got}"
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM mesh_population_projection "
                        "WHERE source_id = 'mlit_future_population'")
            cur.execute("DELETE FROM data_sources WHERE id = 'mlit_future_population'")
        conn.commit()


def test_a_year_without_an_age_breakdown_is_not_reported_as_zero(conn, dataset, monkeypatch):
    """「分からない」を「いない」と言い換えない。

    公表データの基準年（2020）は総人口のみで年齢内訳が無い。`or 0` と
    書いていたので、画面に「65歳以上 0.0%」と出た。実際に指摘を受けた。
    """
    from kaigyou_core import dataset as ds

    rows = [{"year": 2020, "base_year": 2020, "estimate_label": "x",
             "source_date": None, "population": 20720.0, "age_0_14": None,
             "age_15_64": None, "age_65_plus": None, "age_75_plus": None,
             "mesh_count": 21},
            {"year": 2040, "base_year": 2020, "estimate_label": "x",
             "source_date": None, "population": 18731.0, "age_0_14": 2000.0,
             "age_15_64": 10000.0, "age_65_plus": 6000.0, "age_75_plus": 3800.0,
             "mesh_count": 21}]

    class FakeCursor:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchall(self): return rows

    class FakeConn:
        def cursor(self, *a, **k): return FakeCursor()

    monkeypatch.setattr(ds, "table_exists", lambda *a, **k: True, raising=False)
    import kaigyou_core.db as db
    monkeypatch.setattr(db, "table_exists", lambda *a, **k: True)

    out = ds.population_outlook(FakeConn(), 35.0, 139.0, 1000, 500)
    by_year = {y["year"]: y for y in out["years"]}
    assert by_year[2020]["elderly_share"] is None, "内訳が無い年は None のまま"
    assert by_year[2020]["late_elderly_share"] is None
    assert by_year[2040]["elderly_share"] == round(6000.0 / 18731.0, 3)


def test_the_growth_metric_comes_from_the_projection(conn):
    """商圏の 2020→2050 が、面積按分で正しく出ること。

    分子と分母が同じメッシュ集合なので、按分の重みは比では打ち消し合う。
    それでも同じ重み付けを使うのは、現在人口と並べたときに「差が推計の差か
    数え方の差か」を読み手が悩まないようにするため。
    """
    from kaigyou_core.analysis import projected_change

    with conn.cursor() as cur:
        cur.execute("INSERT INTO data_sources (id, name, publisher, dataset_kind) "
                    "VALUES ('proj_t','t','t','official') ON CONFLICT (id) DO NOTHING")
        # 商圏の中心をすっぽり覆う1メッシュ。按分は 1.0 になる。
        cur.execute("""
            INSERT INTO population_mesh (source_id, mesh_code, mesh_size_m,
                prefecture_code, geom, centroid, population)
            VALUES ('proj_t','TESTMESH1',500,'99',
                    ST_MakeEnvelope(139.00, 35.00, 139.02, 35.02, 4326),
                    ST_SetSRID(ST_MakePoint(139.01, 35.01), 4326), 1000)""")
        for year, pop in ((2020, 1000.0), (2050, 780.0)):
            cur.execute("""
                INSERT INTO mesh_population_projection (source_id, mesh_code,
                    mesh_size_m, prefecture_code, projection_year, population)
                VALUES ('proj_t','TESTMESH1',500,'99',%s,%s)""", (year, pop))
    conn.commit()
    try:
        change = projected_change(conn, 35.01, 139.01, 500, 500, 2020, 2050)
        assert change is not None
        assert abs(change - (-0.22)) < 1e-6, f"780/1000-1 = -0.22 のはず: {change}"

        # 推計の無い年を頼まれたら、0 ではなく None。
        assert projected_change(conn, 35.01, 139.01, 500, 500, 2020, 2099) is None
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM data_sources WHERE id = 'proj_t'")
        conn.commit()


def test_the_demand_input_follows_the_configured_growth_metric():
    """成長の指標を変えたとき、demand 側が取り残されないこと。

    demand も同じ目盛り（growth.low/high）を共用している。名前を直書きして
    いたので、片方だけ将来推計にすると**過去の実績を将来の目盛りで採点する**
    ことになっていた。
    """
    from kaigyou_core.scoring import ScoringModel
    from kaigyou_core import config as cfg

    profiles = cfg.scoring_config()["profiles"]
    for name, profile in profiles.items():
        metric = (profile.get("growth") or {}).get("metric")
        weights = profile.get("demand_weights") or {}
        rate_keys = [k for k in weights if "growth" in k or "change" in k]
        for key in rate_keys:
            assert key == metric, (
                f"{name}: demand の率指標 {key!r} が growth.metric {metric!r} と"
                "違う。目盛りを共用しているので、揃っていないと別物を同じ物差しで測る")


def test_every_profile_scores_growth_on_the_same_scale():
    """目盛りだけ取り残されると、静かに全部 0 点になる。

    実測：default の目盛りだけ将来推計向け（-40%〜+5%）に直し、他の4つを
    過去の実績向け（-8%〜+12%）のままにしていた。静岡の中央値は -24.7% なので、
    その4つは全メッシュが下限に張り付いて成長スコアが 0 になる。点数は出るので
    気づけない。
    """
    from kaigyou_core import config as cfg

    profiles = cfg.scoring_config()["profiles"]
    scales = {name: ((p.get("growth") or {}).get("low"),
                     (p.get("growth") or {}).get("high"))
              for name, p in profiles.items()}
    assert len(set(scales.values())) == 1, f"目盛りが揃っていない: {scales}"

    # 実測の中央値が、目盛りの内側に入っていること。端に張り付く目盛りは
    # 点数を出しても地点を選ぶ手掛かりにならない。
    low, high = next(iter(scales.values()))
    assert low < -0.247 < high, "静岡の中央値 -24.7% が目盛りの外にある"


def test_the_growth_horizon_lives_in_one_place():
    """年次はデータ全体の設定。プロファイルごとの好みではない。"""
    from kaigyou_core import config as cfg
    from kaigyou_core.analysis import growth_years

    scoring = cfg.scoring_config()
    assert scoring.get("growth_horizon", {}).get("to_year") == 2050
    for name, profile in scoring["profiles"].items():
        growth = profile.get("growth") or {}
        assert "to_year" not in growth, f"{name} に年次が重複している"
    assert growth_years() == {"from_year": 2020, "to_year": 2050}


def test_the_markdown_uses_only_the_syntax_the_web_view_can_draw(dataset):
    """**画面がそのまま読めることを、こちら側で保証します。**

    レポートは画面の「提出用の文書」タブで描かれます。描くのは自分たちの
    文書だけなので、汎用のパーサではなく、使っている記法だけを扱う小さな
    描画（``web/src/lib/markdown.ts``）にしてあります——扱う記法は見出し・
    強調・箇条書き・表・引用・区切り線・裸の URL の 7 つです。

    report.py がそれ以外を使い始めると、**画面には記法が文字のまま出ます。**
    黙って消えるよりましですが、気づくのは顧客に見せたあとです。だから
    ここで見張ります。

    増やしたくなったときは、まず描画側に足してから、この一覧を広げること。
    """
    import re

    from kaigyou_intel.report import to_markdown

    markdown = to_markdown(_report_output().model_dump(), to_jsonable(dataset),
                           (), _step3_output().model_dump())

    unsupported = {
        "コードブロック（```）": re.compile(r"^\s*```"),
        "画像（![...](...)）": re.compile(r"!\[[^\]]*\]\("),
        "リンク（[題名](URL)）": re.compile(r"(?<!!)\[[^\]]+\]\(\s*\S+\s*\)"),
        "番号つき箇条書き": re.compile(r"^\s*\d+\.\s"),
        "見出しの下線（===）": re.compile(r"^=+\s*$"),
    }
    found: dict[str, str] = {}
    for line in markdown.splitlines():
        for label, pattern in unsupported.items():
            if label not in found and pattern.search(line):
                found[label] = line.strip()[:60]

    assert not found, (
        "画面が描けない記法が使われています。web/src/lib/markdown.ts に"
        f"対応を足してから、この試験を広げてください: {found}")


def test_the_markdown_tables_are_shaped_the_way_the_web_view_expects(dataset):
    """表は「| で始まる行」＋「次の行が |---|」の形であること。

    描画側はこの 2 行組で表を見つけます。区切り行が無い表は、ただの段落に
    なります（罫線が文字のまま出ます）。
    """
    import re

    from kaigyou_intel.report import to_markdown

    lines = to_markdown(_report_output().model_dump(), to_jsonable(dataset),
                        (), _step3_output().model_dump()).splitlines()
    rule = re.compile(r"^\s*\|[\s:|-]+\|\s*$")

    tables = 0
    for i, line in enumerate(lines):
        if not line.strip().startswith("|") or rule.match(line):
            continue
        # 表の中の行か、表の見出し行か。見出し行なら次が区切り行のはず。
        previous_is_table = i > 0 and lines[i - 1].strip().startswith("|")
        if previous_is_table:
            continue
        assert i + 1 < len(lines) and rule.match(lines[i + 1]), (
            f"{i + 1} 行目の表に区切り行がありません: {line.strip()[:60]}")
        tables += 1
    assert tables > 0, "表が 1 つも無いなら、この試験は何も見張っていません"


# ============================================================ 調査の連鎖（指示書 §8-§12）
def test_a_question_carries_how_to_answer_it():
    """**問いを立てた段で、調べ方まで決めさせる。**

    指示書 §16「調査すべきことを AI 自身に決めさせる」の実体がここです。
    次の段に丸投げすると「〇〇駅について検索してください」になり、§23 が
    禁じている「AI検索サービス」になります。
    """
    from kaigyou_intel.schemas import Question

    assert set(Question.model_fields) == {
        "id", "pattern_id", "question", "why_it_matters", "what_would_answer_it",
        # どこから生まれた問いか（§57）。問いだけを保存してはいけません。
        "assumption_id", "trigger",
        # 検索するかどうかを決める欄（§55）。**researchability だけが
        # 検索の可否を決めます。** 残りは順序づけと、あとから
        # 「どんな問いが効いたか」を数えるためのものです。
        "researchability", "researchability_reason", "decision_levers",
        "importance", "already_in_data"}


def test_not_knowing_and_being_wrong_are_different_verdicts():
    """**「調べたが分からなかった」と「調べたら違った」を分ける。**

    経営判断にとってこの 2 つは正反対です。前者は開業前に現地で確かめる
    項目になり、後者はその筋が消えたので別を追うことになります。同じ札に
    入れると、レポートを読んだ歯科医師はどちらなのか判断できません。
    """
    from kaigyou_intel.schemas import (
        CURRENT_HYPOTHESIS_STATUSES, STATUSES_WITHOUT_EVIDENCE)

    assert {"UNCERTAIN", "CONTRADICTED"} <= CURRENT_HYPOTHESIS_STATUSES
    assert "UNSUPPORTED" not in CURRENT_HYPOTHESIS_STATUSES
    # 「分からなかった」だけが根拠なしでよい。調べて何も出なかったことを、
    # そのまま書けるようにするため。
    assert STATUSES_WITHOUT_EVIDENCE == {"UNCERTAIN"}


def test_a_verdict_must_match_the_direction_of_its_evidence():
    """**「支持されている」と言うなら、支持する根拠が要る。**

    文章の説得力の話ではなく、機械で確かめられる形の話です。SUPPORTED なのに
    supports の根拠が 1 つも無い出力は、読んだ人には「調べて確かめた」ように
    見えて、実際には何も確かめていません。
    """
    from kaigyou_intel.schemas import EvidenceLink, Hypothesis, verify_step2

    def check(status, stances):
        output = _step2_output(hypotheses=[Hypothesis(
            id="H001", pattern_id="P001", statement="s", status=status,
            evidence_links=[EvidenceLink(fact_id="C001", stance=st, note="n")
                            for st in stances],
            reasoning="r", confidence="medium", changes=["立地判断"],
            decision_impact="平日夕方を主戦場にするのではなく、土曜の終日診療に人員を寄せる。")])
        return verify_step2(output, {"P001"},
                            {"https://www.city.chuo.lg.jp/toukei/jinkou.html"})

    assert check("SUPPORTED", ["supports"]) == []
    assert check("CONTRADICTED", ["contradicts"]) == []
    assert check("UNCERTAIN", []) == []

    # context だけでは支持になりません。
    assert any("支持する根拠が 1 つも" in p.problem
               for p in check("SUPPORTED", ["context"]))
    # 否定する根拠があるのに SUPPORTED は通しません。
    assert any("否定する根拠があるのに" in p.problem
               for p in check("SUPPORTED", ["supports", "contradicts"]))
    # CONTRADICTED なのに否定の根拠が無いのも通しません。
    assert any("否定する根拠が 1 つも" in p.problem
               for p in check("CONTRADICTED", ["supports"]))


def test_an_old_hypothesis_without_directions_is_not_flagged():
    """**古い形の保存済みレポートを読み直すたびに問題を並べない。**

    歯科版は商談で使われています。向きの無い（古い）出力に新しい検算を
    掛けると、再表示のたびに「根拠がありません」が並びます。
    """
    from kaigyou_intel.schemas import Hypothesis, verify_step2

    output = _step2_output(hypotheses=[Hypothesis(
        id="H001", pattern_id="P001", statement="s", status="SUPPORTED",
        evidence=["C001"], reasoning="r", confidence="high",
        changes=["立地判断"], decision_impact="平日夕方を主戦場にするのではなく、土曜の終日診療に人員を寄せる。")])
    assert verify_step2(output, {"P001"},
                        {"https://www.city.chuo.lg.jp/toukei/jinkou.html"}) == []


def test_the_questions_reach_the_step_that_answers_them():
    """問いが STEP2 に id つきで渡ること。

    渡らないと、どの問いにどの仮説が答えたかを機械で追えません。
    """
    from kaigyou_intel.projection import for_step2

    payload = for_step2(
        {"patterns": [{"id": "P001", "title": "t", "importance": "high"}],
         "questions": [
             {"id": "Q001", "pattern_id": "P001", "question": "なぜか",
              "why_it_matters": "何が変わるか", "what_would_answer_it": "市の資料"},
             # 渡した PATTERN に紐づかない問いは落とすこと。
             {"id": "Q009", "pattern_id": "P099", "question": "x",
              "why_it_matters": "y", "what_would_answer_it": "z"}]},
        {"location": {}, "competition": {}},
        {"max_patterns": 5})
    assert [q["id"] for q in payload["questions"]] == ["Q001"]
    assert payload["questions"][0]["what_would_answer_it"] == "市の資料"


def test_the_report_shows_what_was_asked_and_how_it_was_settled(dataset):
    """**この節がこの文書と、生成 AI が書いた地域紹介との違いです。**

    結論だけを読ませると、読み手は「本当に調べたのか」「調べて分からなかった
    のか」を確かめられません。
    """
    from kaigyou_intel.report import to_markdown

    inquiry = {
        "questions": [
            {"id": "Q001", "pattern_id": "P001",
             "question": "なぜ若年人口が突出しているのか",
             "why_it_matters": "夜間需要を住民数から見積もれるかが変わる",
             "what_would_answer_it": "大学の公式サイトの学部所在地一覧"},
            {"id": "Q002", "pattern_id": "P002",
             "question": "なぜ昼間人口が多いのか",
             "why_it_matters": "平日夕方の診療時間の判断が変わる",
             "what_would_answer_it": "経済センサスの事業所立地"},
        ],
        "external_facts": [
            {"id": "C001", "statement": "1km圏に大学の校地がある",
             "source_url": "https://example.ac.jp/campus"},
        ],
        "hypotheses": [
            {"id": "H001", "pattern_id": "P001", "question_id": "Q001",
             "statement": "大学の学生が通ってきている", "status": "SUPPORTED",
             "evidence_links": [{"fact_id": "C001", "stance": "supports",
                                 "note": "校地が商圏内にある"}],
             "reasoning": "校地の所在が確認できた", "confidence": "high",
             "changes": ["診療時間"], "decision_impact": "平日夕方を主戦場にするのではなく、土曜の終日診療に人員を寄せる。"},
        ],
        "open_questions": [
            {"question_id": "Q002", "why": "公表資料に該当がなかった",
             "what_would_settle_it": "平日12時台に現地で歩行者数を数える"},
        ],
    }
    markdown = to_markdown(_report_output().model_dump(), to_jsonable(dataset),
                           (), None, None, inquiry)

    assert "## 調査の記録" in markdown
    # 立てた問いと、答えの出た数が出ること（指示書 §25）。
    assert "2 件の問い" in markdown and "1 件" in markdown
    # 何を調べれば答えが出るのかが読み手に見えること。
    assert "大学の公式サイトの学部所在地一覧" in markdown
    # 根拠に向きが付くこと。
    assert "支持" in markdown and "C001" in markdown
    # 答えの出なかった問いが「現地で確かめること」として残ること。
    assert "開業前に確かめること" in markdown
    assert "平日12時台に現地で歩行者数を数える" in markdown


def test_the_report_stays_quiet_when_there_was_no_inquiry_recorded(dataset):
    """古い形のジョブでは節ごと出さない。

    空の見出しは「調べていない」ではなく「この版では記録していない」なので、
    出すと誤読させます。
    """
    from kaigyou_intel.report import to_markdown

    markdown = to_markdown(_report_output().model_dump(), to_jsonable(dataset))
    assert "## 調査の記録" not in markdown


# ================================================ 冒頭の調査カバレッジ
def test_the_report_says_up_front_what_it_could_not_confirm(dataset):
    """**結論より先に「何を確かめて、何が未確認か」を出す。**

    ここが競合の資料に無いものです。ディーラーの無料商圏調査もコンサルの
    提案書も、結論だけを載せます——載せない理由があるからで、「調べたが
    分からなかった」と書くと、勧めている案件が弱く見えます。

    読み手にとっては逆で、どこまで確かめられているかが分からないまま
    数千万円の判断をすることになります。
    """
    from kaigyou_intel.report import to_markdown

    inquiry = {
        "questions": [
            {"id": "Q001", "pattern_id": "P001", "question": "なぜ若年が多いのか",
             "why_it_matters": "夜間需要の見積もりが変わる",
             "what_would_answer_it": "大学の公式サイト"},
            {"id": "Q002", "pattern_id": "P002", "question": "昼間人口の中身は何か",
             "why_it_matters": "診療時間の判断が変わる",
             "what_would_answer_it": "経済センサス"},
        ],
        "hypotheses": [
            {"id": "H001", "pattern_id": "P001", "question_id": "Q001",
             "statement": "s", "status": "SUPPORTED", "reasoning": "r",
             "evidence_links": [{"fact_id": "C001", "stance": "supports", "note": "n"}],
             "confidence": "high", "changes": ["診療時間"],
             "decision_impact": "平日夕方ではなく土曜に人員を寄せる。"},
            {"id": "H002", "pattern_id": "P002", "question_id": "Q002",
             "statement": "s2", "status": "CONTRADICTED", "reasoning": "r",
             "evidence_links": [{"fact_id": "C002", "stance": "contradicts", "note": "n"}],
             "confidence": "medium", "changes": ["患者層"],
             "decision_impact": "勤務者ではなく居住者を主に据える。"},
        ],
        "external_facts": [{"id": "C001", "statement": "f1"},
                           {"id": "C002", "statement": "f2"}],
        "open_questions": [
            {"question_id": "Q002", "why": "資料が無かった",
             "what_would_settle_it": "平日12時台に現地で歩行者数を数える"}],
    }
    sources = [
        {"url": "https://www.mhlw.go.jp/a", "title": "t", "source_type": "government",
         "pattern_id": "P001"},
        {"url": "https://example.com/b", "title": "t", "source_type": "company",
         "pattern_id": "P001"},
        # 本文が引用しなかったもの（pattern_id 無し）は数に入れない。
        {"url": "https://example.com/c", "title": "t", "source_type": "news",
         "pattern_id": None},
    ]
    markdown = to_markdown(_report_output().model_dump(), to_jsonable(dataset),
                           sources, None, None, inquiry)

    assert "## この分析で確かめたこと" in markdown
    # 本文（評価・判断）より前にあること。**あとに置くと順番として遅すぎます。**
    # 判断そのものである「この立地について」を基準にします。免責は末尾なので、
    # そこと比べても順番を確かめたことになりません。
    assert markdown.index("## この分析で確かめたこと") < markdown.index("## この立地について")

    assert "問い 2 件" in markdown and "答えが出た **1 件**" in markdown
    # 「違うと分かった」が数に出ること。埋もれさせない。
    assert "調べたら違うと分かった 1" in markdown
    # 引用した資料だけを数え、一次資料の内訳を出すこと。
    assert "外部資料 2 件" in markdown and "一次資料 1 件" in markdown
    # 未確認は、そのまま現地確認の項目として出る。
    assert "確かめられなかったこと" in markdown
    assert "平日12時台に現地で歩行者数を数える" in markdown


def test_the_coverage_summary_is_absent_when_nothing_was_recorded(dataset):
    """古い形のジョブでは節ごと出さない。空の見出しは誤読させます。"""
    from kaigyou_intel.report import to_markdown

    assert "## この分析で確かめたこと" not in to_markdown(
        _report_output().model_dump(), to_jsonable(dataset))


# ================================================ やってはいけない読み方
def test_the_misreadings_are_configuration_not_code():
    """**注意書きの寄せ集めではなく、この製品の中身です。**

    公表データはどれも正しい値で、間違えるのは読む側です。多くのツールは
    それを黙って通します——だから誰も「不便だ」と言わず、気づかないまま
    自信を持ちます。

    実測：国勢調査の「在学者数」メッシュで西早稲田キャンパスを引くと 169 人
    でした。実際にそのキャンパスに通う学生は 3,000 人規模です。
    """
    items = cfg.misreadings("dental_clinic")
    assert len(items) >= 10

    by_id = {i["id"]: i for i in items}
    # 実測から入れた項目が残っていること。
    assert "students_are_residents" in by_id
    assert "常住地基準" in by_id["students_are_residents"]["why"]

    for item in items:
        # 誤読・理由・正しい読み方の 3 つが揃っていること。「気をつけて
        # ください」だけでは、読んだ人は何をすればよいか分かりません。
        assert item["trap"] and item["why"] and item["instead"], item["id"]
        assert item["severity"] in ("high", "medium"), item["id"]


def test_a_business_specific_misreading_does_not_leak_to_another_business():
    """歯科の標榜科目の話を、内科の画面に出さない。そこだけ嘘になります。"""
    dental = {i["id"] for i in cfg.misreadings("dental_clinic")}
    other = {i["id"] for i in cfg.misreadings("hospital")}

    assert "free_text_as_count" in dental
    assert "free_text_as_count" not in other
    # 業態に依らないものは両方に出ること。
    assert "mixing_population_bases" in dental & other


def test_the_screen_and_the_report_use_the_same_words(dataset):
    """画面とレポートで違うことを言わない。

    地図で見た注意とレポートの注意が食い違うと、どちらが正しいのか読み手には
    分かりません。だから画面は自分で文を書かず、設定から来たものを出します。
    """
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app
    from kaigyou_intel.report import to_markdown

    served = TestClient(app, raise_server_exceptions=False) \
        .get("/api/misreadings").json()
    assert served["items"] == cfg.misreadings("dental_clinic")

    # レポートは判断がひっくり返るものだけを載せます。**長い注意書きは
    # 読まれません。**
    markdown = to_markdown(_report_output().model_dump(), to_jsonable(dataset))
    assert "## この数字で、やってはいけない読み方" in markdown
    for item in served["high"]:
        assert item["trap"] in markdown, item["id"]
    medium = [i for i in served["items"] if i["severity"] == "medium"]
    assert medium and all(i["trap"] not in markdown for i in medium)


# --------------------------------------------------------------- §25 段階表示
#
# 待っている数分に「STEP2 実行中 3分12秒」しか出ていませんでした。動いている
# ことは分かっても、何かを見つけているのかは分かりません。

def test_the_progress_shows_what_has_been_found_not_just_that_it_is_running(
        conn, dataset):
    """STEP1 が済んだ時点で、パターンと問いの件数が出ること。

    その数字はもう DB にあります。レポートが書き上がるまで見せていなかった
    だけでした。
    """
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    try:
        jobs.start_step(conn, job_id, 1, {}, {})
        jobs.finish_step(conn, job_id, 1, {
            "facts": [{"id": "F001"}, {"id": "F002"}],
            "patterns": [{"id": "P001"}, {"id": "P002"}, {"id": "P003"}],
            "questions": [{"id": "Q001", "question": "なぜ若年層が多いのか"},
                          {"id": "Q002", "question": "昼間人口の中身は何か"}],
        }, {})

        progress = TestClient(app).get(f"/api/analysis/{job_id}").json()["progress"]
        assert progress["patterns"] == 3 and progress["questions"] == 2
        assert progress["facts"] == 2
        assert progress["through_step"] == 1
        # **まだ調べていないことを、0 件という結果にしない。**
        assert progress["researched"] is False
    finally:
        _drop_job(job_id)


def test_the_progress_does_not_call_an_unfinished_step_a_zero_result(conn, dataset):
    """STEP2 が走っている最中に「答えが出た 0 件」と出さないこと。

    「まだ」と「0 件だった」は違います。0 と NULL は違う、と同じ話です。
    """
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    try:
        jobs.start_step(conn, job_id, 1, {}, {})
        jobs.finish_step(conn, job_id, 1, {"patterns": [{"id": "P001"}],
                                           "questions": [{"id": "Q001"}]}, {})
        jobs.start_step(conn, job_id, 2, {}, {})  # 走っている最中
        body = TestClient(app).get(f"/api/analysis/{job_id}").json()
        assert body["progress"]["researched"] is False, (
            "STEP2 の結果をまだ数えてはいけない")
    finally:
        _drop_job(job_id)


def test_the_progress_counts_the_same_things_the_report_does(conn, dataset):
    """画面の件数とレポート冒頭の件数が食い違わないこと。

    **これがこの節の要です。** 別々に数えると、待っている間に見た「答えが出た
    5 件」と出来上がった文書の「4 件」が食い違い、どちらが正しいのかを読み手が
    確かめる術はありません。確かめられない食い違いは、数字そのものへの信用を
    落とします。
    """
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app
    from kaigyou_intel import jobs
    from kaigyou_intel.report import to_markdown

    step1 = {
        "facts": [{"id": "F001"}],
        "patterns": [{"id": "P001"}],
        "questions": [{"id": "Q001", "question": "なぜ若年層が多いのか"},
                      {"id": "Q002", "question": "昼間人口の中身は何か"}],
    }
    step2 = {
        "hypotheses": [
            {"id": "H001", "question_id": "Q001", "status": "SUPPORTED"},
            {"id": "H002", "question_id": "Q002", "status": "CONTRADICTED"},
        ],
        "external_facts": [{"id": "C001"}],
        # Q002 は仮説が付いたが、決着していない。**仮説が付いた＝答えが出た、
        # ではありません。**
        "open_questions": [{"question_id": "Q002", "why": "資料が無かった",
                            "what_would_settle_it": "平日12時台に現地で数える"}],
    }
    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    try:
        for number, output in ((1, step1), (2, step2)):
            jobs.start_step(conn, job_id, number, {}, {})
            jobs.finish_step(conn, job_id, number, output, {})
        jobs.save_sources(conn, job_id, 2, "P001", [
            {"url": "https://www.mhlw.go.jp/a", "title": "t"},
            {"url": "https://example.com/b", "title": "t"}])

        progress = TestClient(app).get(f"/api/analysis/{job_id}").json()["progress"]
        assert progress["questions"] == 2 and progress["answered"] == 1
        assert progress["researched"] is True

        markdown = to_markdown(
            _report_output().model_dump(), to_jsonable(dataset), [], None, None,
            {"questions": step1["questions"], "hypotheses": step2["hypotheses"],
             "external_facts": step2["external_facts"],
             "open_questions": step2["open_questions"]})
        assert f"問い {progress['questions']} 件" in markdown
        assert f"答えが出た **{progress['answered']} 件**" in markdown
    finally:
        _drop_job(job_id)


def test_the_progress_stays_quiet_for_a_job_from_the_old_shape(conn, dataset):
    """問いを記録していない頃のジョブに「問い 0 件」と出さないこと。

    それは「調べていない」に見えます。実際は「この版では記録していない」です。
    """
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    try:
        jobs.start_step(conn, job_id, 1, {}, {})
        jobs.finish_step(conn, job_id, 1, {"facts": [{"id": "F001"}]}, {})
        body = TestClient(app).get(f"/api/analysis/{job_id}").json()
        assert body["progress"] is None
    finally:
        _drop_job(job_id)


def test_the_status_response_does_not_ship_step_outputs(conn, dataset):
    """4 秒ごとの問い合わせで、誰も読まない段の全文を往復させないこと。

    画面が使うのは件数だけで、全文は `analyze --show` が DB から直接読みます。
    """
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    try:
        jobs.start_step(conn, job_id, 1, {}, {})
        jobs.finish_step(conn, job_id, 1, {"patterns": [{"id": "P001"}],
                                           "questions": [{"id": "Q001"}]}, {})
        body = TestClient(app).get(f"/api/analysis/{job_id}").json()
        assert all("output_json" not in s for s in body["steps"])
        # それでも段の状態は分かること。進捗表示はこちらを見ています。
        assert body["steps"][0]["status"] == "completed"
        # DB からは今までどおり読めること（CLI がここを見ます）。
        assert jobs.step_output(conn, job_id, 1)["patterns"] == [{"id": "P001"}]
    finally:
        _drop_job(job_id)


def test_a_verdict_that_did_not_occur_is_not_shown_as_zero(dataset):
    """出なかった判定を「0 件」として並べないこと。

    「調べたら違うと分かった 0」を並べると、読み手はそれを結果として読みます。
    0 件は「その判定が出なかった」であって「0 という結果が出た」ではありません。
    """
    from kaigyou_intel import coverage

    counts = coverage.tally({
        "questions": [{"id": "Q001"}],
        "hypotheses": [{"id": "H001", "question_id": "Q001",
                        "status": "SUPPORTED"}],
    })
    keys = [v["key"] for v in counts["verdicts"]]
    assert keys == ["SUPPORTED"], keys


# ------------------------------------------------------- RESEARCH の反復（2周目）
#
# 1 周目は STEP1 が立てた問いをそのまま検索に持っていきます。実際の調査は
# そうではありません。1 回目に何が出てこなかったかを見て、次に何を引くかが
# 決まります。

def _step2_with_a_dead_end() -> "Step2Output":
    """1 周目で説明が消え、問いだけが残った状態。"""
    from kaigyou_intel.schemas import (
        ExternalFact, Hypothesis, Step2Output, UnansweredQuestion)

    return Step2Output(
        external_facts=[ExternalFact(
            id="C001", pattern_id="P001", statement="外部事実1",
            source_url="https://www.city.chuo.lg.jp/toukei/jinkou.html",
            source_title="t", confidence="high")],
        hypotheses=[Hypothesis(
            id="H001", pattern_id="P001", question_id="Q001",
            statement="タワーマンション供給が子育て世帯を呼び込んだ",
            status="CONTRADICTED", evidence=["C001"],
            evidence_links=[{"fact_id": "C001", "stance": "contradicts",
                             "note": "供給時期が合わない"}],
            reasoning="供給は2010年代前半で、増加はそれ以降", confidence="medium",
            changes=["患者層"],
            decision_impact="子育て世帯ではなく単身勤務者を主に据える。")],
        open_questions=[UnansweredQuestion(
            question_id="Q002", why="公表資料に無かった",
            what_would_settle_it="市の住宅着工統計を年次で確認する")])


def _followup_output() -> "Step2Output":
    """2 周目の出力。**id は 1 周目と同じ C001 / H001 から始まります。**"""
    from kaigyou_intel.schemas import ExternalFact, Hypothesis, Step2Output

    return Step2Output(
        external_facts=[ExternalFact(
            id="C001", pattern_id="P001",
            statement="市の住宅着工統計では2018年以降に賃貸共同住宅が増えている",
            source_url="https://www.city.chuo.lg.jp/kenchiku.html",
            source_title="建築着工統計", confidence="high")],
        hypotheses=[Hypothesis(
            id="H001", pattern_id="P001", question_id="Q002",
            statement="分譲ではなく賃貸の供給が単身勤務者を呼び込んだ",
            status="SUPPORTED", evidence=["C001"],
            evidence_links=[{"fact_id": "C001", "stance": "supports",
                             "note": "着工統計が賃貸の増加を示す"}],
            reasoning="着工統計で賃貸共同住宅の増加が確認できる", confidence="high",
            changes=["患者層"],
            decision_impact="親子ではなく単身勤務者を一組として獲得する設計にする。")])


def _round_two_asks(followup: "Step2Output | None" = None, calls: list | None = None):
    """1 周目で行き止まり、2 周目で答えが出る、を模したモデル。"""
    calls = calls if calls is not None else []

    def fake_ask(*, step_number, system, user, schema=None, tools=None,
                 web_search=None, effort=None, max_uses=None):
        calls.append({"system": system, "user": user, "schema": schema,
                      "max_uses": max_uses})
        research = len([c for c in calls if c["schema"] is None])
        if schema is None:
            url = ("https://www.city.chuo.lg.jp/toukei/jinkou.html" if research == 1
                   else "https://www.city.chuo.lg.jp/kenchiku.html")
            return llm.Result(
                parsed=None, text=f"{research}周目の調査結果。",
                usage=llm.Usage(input_tokens=100, output_tokens=100, web_searches=2),
                model="claude-sonnet-5",
                sources=[{"url": url, "title": "t", "page_age": None}])
        parsed = (_step2_with_a_dead_end()
                  if len([c for c in calls if c["schema"] is not None]) == 1
                  else (followup if followup is not None else _followup_output()))
        return llm.Result(parsed=parsed,
                          usage=llm.Usage(input_tokens=50, output_tokens=50),
                          model="claude-sonnet-5")

    return fake_ask, calls


def test_a_dead_end_is_researched_again_from_a_different_angle(monkeypatch):
    """「調べたら違うと分かった」で止めないこと。

    **問いはまだ生きています。** 人がやるなら「では何が本当の理由なのか」と
    続けるところで、1 周目で止まるとそこで終わりになります。
    """
    from kaigyou_intel.steps import step2_research

    fake_ask, calls = _round_two_asks()
    monkeypatch.setattr(llm, "ask", fake_ask)
    payload = step2_research.build_input(
        {**_step1_for_step2(),
         "questions": [{"id": "Q001", "question": "なぜ年少人口が増えたのか"},
                       {"id": "Q002", "question": "供給されたのは分譲か賃貸か"}]},
        {"location": {"name": "銀座"}})
    output, usage, sources = step2_research.run(payload)

    assert len(calls) == 4, "調べる×2・写す×2 で 4 回"
    # 2 周目には、1 周目に消えた説明と残った問いが渡ること。
    followup = calls[2]["user"]
    assert "Q002" in followup and "タワーマンション供給" in followup
    # 1 周目の本文は渡しません（入力が倍になるだけ）。
    assert "1周目の調査結果。" not in followup

    assert usage.web_searches == 4, "2 周ぶんの検索が数えられていない"
    assert len(output["external_facts"]) == 2
    assert len(output["hypotheses"]) == 2


def test_the_second_round_does_not_overwrite_the_first_rounds_ids(monkeypatch):
    """**どちらの周も C001 から採番します。**

    そのまま足すと、2 周目の C001 が 1 周目の C001 を指しているように読め、
    根拠の追跡が静かに壊れます。見た目には何も起きません——レポートには
    「C001 による」と出て、別の事実が引かれているだけです。
    """
    from kaigyou_intel.steps import step2_research

    fake_ask, _ = _round_two_asks()
    monkeypatch.setattr(llm, "ask", fake_ask)
    payload = step2_research.build_input(
        {**_step1_for_step2(),
         "questions": [{"id": "Q001", "question": "q1"},
                       {"id": "Q002", "question": "q2"}]},
        {"location": {"name": "銀座"}})
    output, _, _ = step2_research.run(payload)

    ids = [f["id"] for f in output["external_facts"]]
    assert ids == ["C001", "C002"], ids
    assert [h["id"] for h in output["hypotheses"]] == ["H001", "H002"]
    # 2 周目の仮説が指す先が、振り直したあとの id になっていること。
    second = output["hypotheses"][1]
    assert second["evidence"] == ["C002"]
    assert second["evidence_links"][0]["fact_id"] == "C002"
    # そして、その C002 は 2 周目の事実であること（1 周目のを指していない）。
    assert "着工統計" in output["external_facts"][1]["statement"]


def test_what_came_back_in_the_second_round_says_so(monkeypatch):
    """調べ直して出たことと、最初から出ていたことを、同じ顔で並べない。

    **調べ直したという事実そのものが記録です。**
    """
    from kaigyou_intel.report import to_markdown
    from kaigyou_intel.steps import step2_research

    fake_ask, _ = _round_two_asks()
    monkeypatch.setattr(llm, "ask", fake_ask)
    payload = step2_research.build_input(
        {**_step1_for_step2(),
         "questions": [{"id": "Q001", "question": "q1"},
                       {"id": "Q002", "question": "q2"}]},
        {"location": {"name": "銀座"}})
    output, _, _ = step2_research.run(payload)

    assert output["external_facts"][0]["round"] == 1
    assert output["external_facts"][1]["round"] == 2
    assert output["hypotheses"][1]["round"] == 2


def test_a_question_answered_in_the_second_round_leaves_the_unconfirmed_list(
        monkeypatch):
    """2 周目で答えが出た問いを「開業前に現地で確かめること」に残さない。

    残すと、答えが出ているのに現地確認の項目として並び続けます。
    """
    from kaigyou_intel.steps import step2_research

    fake_ask, _ = _round_two_asks()
    monkeypatch.setattr(llm, "ask", fake_ask)
    payload = step2_research.build_input(
        {**_step1_for_step2(),
         "questions": [{"id": "Q001", "question": "q1"},
                       {"id": "Q002", "question": "q2"}]},
        {"location": {"name": "銀座"}})
    output, _, _ = step2_research.run(payload)

    assert [q["question_id"] for q in output["open_questions"]] == [], (
        "Q002 は 2 周目で答えが出たのに未確認のまま残っている")


def test_the_second_round_does_not_run_when_the_first_settled_everything(
        monkeypatch):
    """残っているものが無ければ走らせない。**足すものがありません。**

    1 件あたりの費用と時間だけが増えます。
    """
    from kaigyou_intel.steps import step2_research

    calls: list[dict] = []

    def fake_ask(*, step_number, system, user, schema=None, **kw):
        calls.append({"schema": schema})
        if schema is None:
            return llm.Result(
                parsed=None, text="調査結果。", usage=llm.Usage(web_searches=1),
                model="m",
                sources=[{"url": "https://www.city.chuo.lg.jp/toukei/jinkou.html",
                          "title": "t", "page_age": None}])
        return llm.Result(parsed=_step2_output(), usage=llm.Usage(), model="m")

    monkeypatch.setattr(llm, "ask", fake_ask)
    payload = step2_research.build_input(_step1_for_step2(),
                                         {"location": {"name": "銀座"}})
    step2_research.run(payload)
    assert len(calls) == 2, "答えが出ているのに 2 周目を走らせている"


def test_the_second_round_can_be_switched_off(monkeypatch):
    """設定 1 行で 1 周に戻せること。

    費用と時間が惜しいとき、外部に出られない環境で試すとき、挙動を 1 周目
    だけに固定して比べたいときに要ります。
    """
    from kaigyou_core import config as cfg
    from kaigyou_intel.steps import step2_research

    base = cfg.analysis_config()
    tuned = {**base, "limits": {**(base.get("limits") or {}), "research_rounds": 1}}
    monkeypatch.setattr(cfg, "analysis_config", lambda: tuned)

    fake_ask, calls = _round_two_asks()
    monkeypatch.setattr(llm, "ask", fake_ask)
    payload = step2_research.build_input(
        {**_step1_for_step2(), "questions": [{"id": "Q001", "question": "q"}]},
        {"location": {"name": "銀座"}})
    step2_research.run(payload)
    assert len(calls) == 2, "research_rounds: 1 でも 2 周目が走っている"


def test_a_failed_second_round_does_not_throw_away_the_first(monkeypatch):
    """**1 周目は既に払った検索と時間の上に立っています。**

    2 周目がしくじったからといって、それを捨てるのは割に合いません。
    """
    from kaigyou_intel.steps import step2_research

    calls: list[dict] = []

    def fake_ask(*, step_number, system, user, schema=None, **kw):
        calls.append({"schema": schema})
        research = len([c for c in calls if c["schema"] is None])
        if schema is None:
            if research == 2:
                raise RuntimeError("overloaded")
            return llm.Result(
                parsed=None, text="1周目。", usage=llm.Usage(web_searches=2),
                model="m",
                sources=[{"url": "https://www.city.chuo.lg.jp/toukei/jinkou.html",
                          "title": "t", "page_age": None}])
        return llm.Result(parsed=_step2_with_a_dead_end(), usage=llm.Usage(),
                          model="m")

    monkeypatch.setattr(llm, "ask", fake_ask)
    payload = step2_research.build_input(
        {**_step1_for_step2(), "questions": [{"id": "Q001", "question": "q"},
                                             {"id": "Q002", "question": "q2"}]},
        {"location": {"name": "銀座"}})
    output, _, _ = step2_research.run(payload)

    assert len(output["external_facts"]) == 1, "1 周目まで消えている"
    # 黙って落とさない。何が起きたかを残します。
    assert any("2 周目" in u for u in output["unanswered"])


def test_the_second_round_is_held_to_the_same_verification(monkeypatch):
    """検算を 2 周目だけ緩めないこと。

    緩めると「支持された」と書いてあるのに支持の根拠が無い仮説が混ざります。
    かといって落として例外を上げると、既に払った 1 周目まで消えます。
    **2 周目のほうを捨てます。**
    """
    from kaigyou_intel.schemas import ExternalFact, Hypothesis, Step2Output
    from kaigyou_intel.steps import step2_research

    # 支持されたと言いながら、根拠の向きが反証しかない。
    bad = Step2Output(
        external_facts=[ExternalFact(
            id="C001", pattern_id="P001", statement="f",
            source_url="https://www.city.chuo.lg.jp/kenchiku.html",
            source_title="t", confidence="high")],
        hypotheses=[Hypothesis(
            id="H001", pattern_id="P001", question_id="Q002", statement="s",
            status="SUPPORTED", evidence=["C001"],
            evidence_links=[{"fact_id": "C001", "stance": "contradicts",
                             "note": "n"}],
            reasoning="r", confidence="high", changes=["患者層"],
            decision_impact="親子ではなく単身勤務者を主に据える。")])
    fake_ask, _ = _round_two_asks(followup=bad)
    monkeypatch.setattr(llm, "ask", fake_ask)
    payload = step2_research.build_input(
        {**_step1_for_step2(), "questions": [{"id": "Q001", "question": "q"},
                                             {"id": "Q002", "question": "q2"}]},
        {"location": {"name": "銀座"}})
    output, _, _ = step2_research.run(payload)

    assert len(output["hypotheses"]) == 1, "検算に落ちた 2 周目を採用している"
    assert len(output["external_facts"]) == 1
    assert any("2 周目" in u for u in output["unanswered"])


def test_the_report_says_when_it_went_back_and_looked_again(dataset):
    """調べ直したことを書かないと、1 周で出たように読めます。

    **調べ直したという事実そのものが記録です。**
    """
    from kaigyou_intel.report import to_markdown

    inquiry = {
        "questions": [{"id": "Q001", "question": "q1"},
                      {"id": "Q002", "question": "q2"}],
        "hypotheses": [
            {"id": "H001", "question_id": "Q001", "statement": "s1",
             "status": "CONTRADICTED", "round": 1,
             "evidence_links": [{"fact_id": "C001", "stance": "contradicts",
                                 "note": "n"}],
             "reasoning": "r", "confidence": "medium", "changes": ["患者層"],
             "decision_impact": "子育て世帯ではなく単身勤務者を主に据える。"},
            {"id": "H002", "question_id": "Q002", "statement": "s2",
             "status": "SUPPORTED", "round": 2,
             "evidence_links": [{"fact_id": "C002", "stance": "supports",
                                 "note": "n"}],
             "reasoning": "r", "confidence": "high", "changes": ["患者層"],
             "decision_impact": "親子ではなく単身勤務者を一組として獲得する。"},
        ],
        "external_facts": [{"id": "C001", "statement": "f1", "round": 1},
                           {"id": "C002", "statement": "f2", "round": 2}],
        "open_questions": [],
    }
    markdown = to_markdown(_report_output().model_dump(), to_jsonable(dataset),
                           [], None, None, inquiry)
    assert "角度を変えて調べ直しました" in markdown
    assert "2 周目で立てた仮説 1 件" in markdown
    # 調査の記録でも、1 周目のものと同じ顔で並べない。
    assert "（2 周目に調べ直して）" in markdown


def test_a_one_round_job_does_not_claim_it_looked_again(dataset):
    """1 周しか回っていないジョブに「調べ直しました」と書かない。"""
    from kaigyou_intel.report import to_markdown

    inquiry = {
        "questions": [{"id": "Q001", "question": "q1"}],
        "hypotheses": [{"id": "H001", "question_id": "Q001", "statement": "s",
                        "status": "SUPPORTED", "reasoning": "r",
                        "evidence_links": [{"fact_id": "C001",
                                            "stance": "supports", "note": "n"}],
                        "confidence": "high", "changes": ["患者層"],
                        "decision_impact": "AではなくBに寄せる。"}],
        "external_facts": [{"id": "C001", "statement": "f"}],
        "open_questions": [],
    }
    markdown = to_markdown(_report_output().model_dump(), to_jsonable(dataset),
                           [], None, None, inquiry)
    assert "調べ直しました" not in markdown
    assert "周目に調べ直して" not in markdown


# ------------------------------------------------- 問いの品質（指示書 §8-3）
#
# LLM に採点させません。「この問いは良い問いですか」と聞けばそれらしい点数が
# 返りますが、それは問いを出したのと同じモデルの意見です。

def test_a_question_that_was_answered_but_changed_nothing_is_not_counted_as_good():
    """**答えが出ることと、判断が動くことは違います。**

    実測で出た例：「区画整理により計画的に形成された市街地である」——正しくても
    診療コンセプトも設備も診療時間も変わりません。答えが出た件数だけを数えると、
    こういう問いが良い問いとして数えられます。
    """
    from kaigyou_intel import question_quality as q

    rows = q.outcomes(
        {"questions": [{"id": "Q001", "question": "区画整理で形成された市街地か"},
                       {"id": "Q002", "question": "若年層が多いのはなぜか"}]},
        {"hypotheses": [
            {"id": "H001", "question_id": "Q001", "status": "SUPPORTED",
             "changes": []},
            {"id": "H002", "question_id": "Q002", "status": "SUPPORTED",
             "changes": ["患者層"]}]})
    by_id = {r["question_id"]: r for r in rows}
    assert by_id["Q001"]["settled"] is True
    assert by_id["Q001"]["levers"] == [], "動かないのに動いたことにしている"
    assert by_id["Q002"]["levers"] == ["患者層"]


def test_an_uncertain_answer_is_not_an_answer():
    """「調べたが、どちらとも言えなかった」を答えが出たと数えない。"""
    from kaigyou_intel import question_quality as q

    rows = q.outcomes(
        {"questions": [{"id": "Q001", "question": "q"}]},
        {"hypotheses": [{"id": "H001", "question_id": "Q001",
                         "status": "UNCERTAIN", "changes": ["患者層"]}]})
    assert rows[0]["settled"] is False
    assert rows[0]["hypotheses"] == 1, "仮説が立ったことは残す"


def test_a_question_sent_to_the_field_is_not_a_failure():
    """検索では決着しない問いを、失敗として数えない。

    それは「開業前に現地で確かめること」で、この調査の結論の一部です。
    """
    from kaigyou_intel import question_quality as q

    rows = q.outcomes(
        {"questions": [{"id": "Q001", "question": "q"}]},
        {"open_questions": [{"question_id": "Q001", "why": "w",
                             "what_would_settle_it": "平日12時台に現地で数える"}]})
    assert rows[0]["left_to_the_field"] is True
    assert rows[0]["settled"] is False


def test_a_primary_source_and_a_blog_are_not_the_same_evidence():
    """企業ブログで「支持された」問いと、官公庁の資料で支持された問いを分ける。"""
    from kaigyou_intel import question_quality as q

    step2 = {
        "external_facts": [
            {"id": "C001", "source_url": "https://www.mhlw.go.jp/a"},
            {"id": "C002", "source_url": "https://example.com/b"}],
        "hypotheses": [
            {"id": "H001", "question_id": "Q001", "status": "SUPPORTED",
             "changes": ["患者層"],
             "evidence_links": [{"fact_id": "C001", "stance": "supports"}]},
            {"id": "H002", "question_id": "Q002", "status": "SUPPORTED",
             "changes": ["患者層"],
             "evidence_links": [{"fact_id": "C002", "stance": "supports"}]}],
    }
    sources = [{"url": "https://www.mhlw.go.jp/a", "source_type": "government"},
               {"url": "https://example.com/b", "source_type": "company"}]
    rows = q.outcomes({"questions": [{"id": "Q001"}, {"id": "Q002"}]},
                      step2, sources)
    by_id = {r["question_id"]: r for r in rows}
    assert by_id["Q001"]["primary_evidence"] is True
    assert by_id["Q002"]["primary_evidence"] is False


def test_the_tally_does_not_average_across_prompt_versions(conn, dataset):
    """版を跨いで平均しない。**直した効果が薄まって見えます。**"""
    from kaigyou_intel import jobs
    from kaigyou_intel import question_quality as q

    made = []
    try:
        for version, status in (("step1-v9", "SUPPORTED"), ("step1-v10", "UNCERTAIN")):
            job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                                     dataset=to_jsonable(dataset), base_hash=version)
            made.append(job_id)
            jobs.start_step(conn, job_id, 1, {}, {"prompt_version": version})
            jobs.finish_step(conn, job_id, 1,
                             {"questions": [{"id": "Q001", "question": "q"}]}, {})
            jobs.start_step(conn, job_id, 2, {}, {})
            jobs.finish_step(conn, job_id, 2, {
                "hypotheses": [{"id": "H001", "question_id": "Q001",
                                "status": status, "changes": ["患者層"]}]}, {})

        summary = q.across_jobs(conn)
        by_version = {r["prompt_version"]: r for r in summary["by_prompt_version"]}
        assert by_version["step1-v9"]["settled"] == 1
        assert by_version["step1-v10"]["settled"] == 0
    finally:
        for job_id in made:
            _drop_job(job_id)


def test_jobs_from_before_questions_existed_are_not_in_the_denominator(
        conn, dataset):
    """問いを記録していない頃のジョブを母数に混ぜない。

    混ぜると、版の違いが「答えが出た割合の低下」に見えます。
    """
    from kaigyou_intel import jobs
    from kaigyou_intel import question_quality as q

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="old")
    try:
        jobs.start_step(conn, job_id, 1, {}, {"prompt_version": "step1-v1.0"})
        jobs.finish_step(conn, job_id, 1, {"facts": [{"id": "F001"}]}, {})
        summary = q.across_jobs(conn)
        assert all(r["job_id"] != job_id for r in summary["rows"])
    finally:
        _drop_job(job_id)


def test_a_round_that_added_nothing_stops_the_next_one(monkeypatch):
    """**何も足せなかった周の次は走らせません。**

    同じ問いに、同じ「公表されていない」が返ってくるだけです。増えるのは
    費用と時間で、分かることは増えません。
    """
    from kaigyou_core import config as cfg
    from kaigyou_intel.schemas import Step2Output
    from kaigyou_intel.steps import step2_research

    base = cfg.analysis_config()
    monkeypatch.setattr(cfg, "analysis_config", lambda: {
        **base, "limits": {**(base.get("limits") or {}), "research_rounds": 4}})

    # 2 周目は何も見つけない（外部事実 0 件）。
    fake_ask, calls = _round_two_asks(followup=Step2Output())
    monkeypatch.setattr(llm, "ask", fake_ask)
    payload = step2_research.build_input(
        {**_step1_for_step2(), "questions": [{"id": "Q001", "question": "q"},
                                             {"id": "Q002", "question": "q2"}]},
        {"location": {"name": "銀座"}})
    output, _, _ = step2_research.run(payload)

    assert len(calls) == 4, "空振りした 2 周目のあとに 3 周目を走らせている"
    assert any("打ち切り" in u for u in output["unanswered"])


def test_a_third_round_runs_when_the_second_one_found_something(monkeypatch):
    """周の数は設定で決まります。2 で固定していません。"""
    from kaigyou_core import config as cfg
    from kaigyou_intel.schemas import ExternalFact, Hypothesis, Step2Output
    from kaigyou_intel.steps import step2_research

    base = cfg.analysis_config()
    monkeypatch.setattr(cfg, "analysis_config", lambda: {
        **base, "limits": {**(base.get("limits") or {}), "research_rounds": 3}})

    def found(url: str, statement: str) -> Step2Output:
        # 問いは決着させず（open のまま）、事実だけ増やす周。
        return Step2Output(
            external_facts=[ExternalFact(
                id="C001", pattern_id="P001", statement=statement,
                source_url=url, source_title="t", confidence="high")],
            hypotheses=[Hypothesis(
                id="H001", pattern_id="P001", question_id="Q002",
                statement=statement, status="UNCERTAIN", evidence=[],
                reasoning="r", confidence="low", changes=["患者層"],
                decision_impact="親子ではなく単身勤務者を一組として獲得する設計にする。")])

    calls: list[dict] = []

    def fake_ask(*, step_number, system, user, schema=None, **kw):
        calls.append({"schema": schema})
        research = len([c for c in calls if c["schema"] is None])
        if schema is None:
            url = ("https://www.city.chuo.lg.jp/toukei/jinkou.html" if research == 1
                   else f"https://www.city.chuo.lg.jp/r{research}.html")
            return llm.Result(
                parsed=None, text=f"{research}周目。",
                usage=llm.Usage(web_searches=1), model="m",
                sources=[{"url": url, "title": "t", "page_age": None}])
        nth = len([c for c in calls if c["schema"] is not None])
        if nth == 1:
            return llm.Result(parsed=_step2_with_a_dead_end(),
                              usage=llm.Usage(), model="m")
        return llm.Result(parsed=found(f"https://www.city.chuo.lg.jp/r{nth}.html",
                                       f"{nth}周目の事実"),
                          usage=llm.Usage(), model="m")

    monkeypatch.setattr(llm, "ask", fake_ask)
    payload = step2_research.build_input(
        {**_step1_for_step2(), "questions": [{"id": "Q001", "question": "q"},
                                             {"id": "Q002", "question": "q2"}]},
        {"location": {"name": "銀座"}})
    output, _, _ = step2_research.run(payload)

    assert len(calls) == 6, "3 周目まで回っていない（調べる×3・写す×3）"
    assert [f["round"] for f in output["external_facts"]] == [1, 2, 3]
    assert [f["id"] for f in output["external_facts"]] == ["C001", "C002", "C003"]


# ------------------------------------------------- 中間 JSON（指示書 §21）

def test_each_step_is_written_as_a_file_you_can_open(conn, dataset, tmp_path):
    """DB に入っていることと、手元でファイルとして読めることは違います。

    プロンプトを直したあと「なぜこの PATTERN が出たのか」を追うのに、
    DB クライアントを開かせません。
    """
    from kaigyou_intel import jobs, report

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x",
                             location_name="銀座4丁目")
    try:
        jobs.start_step(conn, job_id, 1, {"patterns_asked_for": 4}, {})
        jobs.finish_step(conn, job_id, 1, {"patterns": [{"id": "P001"}]}, {})
        written = report.write_all_step_files(conn, job_id, str(tmp_path))

        assert len(written) == 1
        saved = json.loads(written[0].read_text(encoding="utf-8"))
        assert saved["output"]["patterns"] == [{"id": "P001"}]
        # **入力も書きます。**出力だけあっても、何を渡したときにそうなった
        # のかが分からなければ再現できません。
        assert saved["input"] == {"patterns_asked_for": 4}
        # レポートと同じ名前のフォルダにまとまること。
        assert "銀座4丁目" in str(written[0].parent)
    finally:
        _drop_job(job_id)


def test_an_unwritable_place_does_not_lose_a_finished_step(conn, dataset,
                                                           monkeypatch):
    """おまけの失敗で、仕上がった段を捨てさせない。

    ホスティングされた関数のファイルシステムは読み取り専用です（実測：Vercel で
    ``OSError: [Errno 30] Read-only file system: 'reports'``）。
    """
    from kaigyou_intel import jobs, report

    def refuse(*a, **kw):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(Path, "mkdir", refuse)
    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    try:
        jobs.start_step(conn, job_id, 1, {}, {})
        jobs.finish_step(conn, job_id, 1, {"patterns": []}, {})
        assert report.write_step_file(conn, job_id, 1, {}, {}) is None
    finally:
        _drop_job(job_id)


def test_writing_step_files_can_be_switched_off(conn, dataset, tmp_path,
                                                monkeypatch):
    from kaigyou_core import config as cfg
    from kaigyou_intel import jobs, report

    base = cfg.analysis_config()
    monkeypatch.setattr(cfg, "analysis_config", lambda: {
        **base, "report": {**(base.get("report") or {}), "write_step_json": False}})
    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    try:
        jobs.start_step(conn, job_id, 1, {}, {})
        jobs.finish_step(conn, job_id, 1, {"patterns": []}, {})
        assert report.write_step_file(conn, job_id, 1, {}, {},
                                      str(tmp_path)) is None
        assert not list(tmp_path.iterdir())
    finally:
        _drop_job(job_id)


# --------------------------------------------- 往復を減らす（地図の速さ）
#
# 手元では 1 往復が 0.2ms（unix ソケット）なので誰も気づきません。**Vercel から
# Supabase へは 1 往復が 10〜30ms** です。実測：`/api/dataset` は 114 往復して
# いて、うち 47 往復は「同じ表の同じ列があるか」を訊き直しているだけでした。

@contextmanager
def _counting_queries():
    """この中で投げられた SQL の本数を数える。"""
    count = [0]
    original = psycopg.Cursor.execute

    def execute(self, query, params=None, **kw):
        count[0] += 1
        return original(self, query, params, **kw)

    psycopg.Cursor.execute = execute
    try:
        yield count
    finally:
        psycopg.Cursor.execute = original


def test_the_same_schema_question_is_not_asked_twice_on_one_connection(conn):
    """1 リクエストの中で、同じ問いを何度も往復させない。

    実測：`mesh_scores.facility_category` があるかを 1 リクエストで 15 回
    訊いていました。スキーマは 1 リクエストの間に変わりません。
    """
    from kaigyou_core.db import column_exists, forget_schema

    forget_schema(conn)
    with _counting_queries() as n:
        column_exists(conn, "mesh_scores", "facility_category")
        assert n[0] == 1
        for _ in range(10):
            column_exists(conn, "mesh_scores", "facility_category")
        assert n[0] == 1, "同じ問いで往復している"

        # 覚えているのは接続ごとです。捨てれば訊き直します。
        forget_schema(conn)
        column_exists(conn, "mesh_scores", "facility_category")
        assert n[0] == 2


def test_many_columns_are_asked_for_in_one_round_trip(conn):
    """問いが 14 個あることと、往復が 14 回必要なことは別です。"""
    from kaigyou_core.db import column_exists, columns_that_exist, forget_schema

    columns = ["population", "score", "この列はありません", "facility_category"]
    forget_schema(conn)
    with _counting_queries() as n:
        present = columns_that_exist(conn, "mesh_scores", columns)
        assert n[0] == 1, "列ごとに往復している"
        assert "この列はありません" not in present

        # まとめて訊いた答えは、1 個ずつ訊かれても往復しません。
        for column in columns:
            assert column_exists(conn, "mesh_scores", column) == (column in present)
        assert n[0] == 1


def test_a_missing_column_is_still_reported_as_missing(conn):
    """速くするために、デプロイの窓の守りを外していないこと。

    コードは push で即デプロイされますが、マイグレーションは手で当てます。
    存在しない列を SELECT すると分析全体が 500 になります（実際に静岡で
    起きました）。
    """
    from kaigyou_core.db import column_exists, forget_schema, table_exists

    forget_schema(conn)
    assert column_exists(conn, "mesh_scores", "そんな列はない") is False
    assert table_exists(conn, "そんな表はない") is False
    assert table_exists(conn, "mesh_scores") is True


def test_building_a_dataset_does_not_ask_the_schema_over_and_over(conn, dataset):
    """**この 1 本が、地図をクリックしてからパネルが出るまでの速さです。**

    実測：`/api/dataset` は 114 往復していました。うち 47 往復は同じ問いの
    訊き直しと、まとめられる問いを 1 つずつ訊いていたぶんです。
    """
    from kaigyou_core.dataset import build_dataset
    from kaigyou_core.db import forget_schema

    forget_schema(conn)
    with _counting_queries() as n:
        build_dataset(conn, 35.6717, 139.7650, 1000)
    assert n[0] < 90, f"1 リクエストの往復が {n[0]} 本まで増えています"


def test_creating_a_job_does_not_wait_for_the_next_cron_minute(conn, dataset,
                                                               monkeypatch):
    """押してから最初の 1 歩まで、最大 60 秒 何も起きませんでした。

    cron が 1 分おきなのは「1 分に 1 回進む」という意味で、「押してから 1 分
    待つ」という意味ではないはずでした。作ったジョブを最初に拾うのも cron
    だったので、平均 30 秒、画面には「順番待ち」とだけ出ていました。
    """
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app
    from kaigyou_api.routers import intel as router

    started: list[bool] = []
    monkeypatch.setattr(router, "_start_soon", lambda: started.append(True))
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("KAIGYOU_ANALYSIS_TOKEN", raising=False)

    res = TestClient(app).post(
        "/api/analysis", params={"lat": 35.6717, "lng": 139.7650, "radius": 1000})
    assert res.status_code == 200, res.text
    try:
        assert started == [True], "cron を待たずに起こしていない"
    finally:
        _drop_job(res.json()["job_id"])


def test_the_api_does_not_start_a_step_it_has_no_key_for(monkeypatch):
    """鍵の無い側で走らせない。

    手元では API サーバに ANTHROPIC_API_KEY が無く、worker を別の端末で回すのが
    普通の形です。鍵の無い側で走らせると、そのステップは失敗し、やり直し回数
    だけ減ります。
    """
    from kaigyou_api.routers import intel as router

    ticked: list[bool] = []
    monkeypatch.setattr(llm, "is_configured", lambda: False)
    monkeypatch.setattr("kaigyou_intel.worker.tick",
                        lambda conn: ticked.append(True))
    router._start_soon()
    assert ticked == []


# ============================================================ 問いのセンス
#
# 良い問いとは、**既に持っているデータから「見落とされている重要な前提」を
# 発見し、その前提を外部情報によって検証できる問い**（指示書 §51）。
#
# 実測（沼津）で出た悪い問い：
#   「市内の歯科衛生士・歯科医師の年齢構成は」
#   → 市区町村単位では公表されていません。**分かっているのに毎回検索して
#     いました。**
# 同じ分析で出た良い問い：
#   「復興土地区画整理事業中央工区の保留地・住宅供給計画は、将来推計人口の
#     減少を緩和しうる規模か」
#   → 手元の将来推計（減少）と外部の事実（区画整理）を突き合わせ、
#     「推計は住宅供給を織り込んでいる」という前提を疑っています。

def _question(**overrides):
    from kaigyou_intel.schemas import Question

    data = {
        "id": "Q001", "pattern_id": "P001",
        "question": "区画整理の保留地は、将来推計人口の減少を緩和しうる規模か",
        "why_it_matters": "減少を前提にした立地判断そのものが変わる",
        "what_would_answer_it": "市の区画整理事業計画書（保留地の残数と計画戸数）",
        "assumption_id": "A001",
        "trigger": {"type": "data_external_conflict",
                    "facts": ["将来推計人口は2040年まで減少予測",
                              "中央工区で土地区画整理事業が進行中"],
                    "reason": "住宅供給が推計に織り込まれているか確かめられる"},
        "researchability": "high",
        "researchability_reason": "市の事業計画書は公表されている",
        "decision_levers": ["立地判断"],
        "importance": "high",
    }
    data.update(overrides)
    return Question(**data)


def _step1_with_questions(**overrides):
    from kaigyou_intel.schemas import Assumption, Step1Output

    data = {
        "facts": [Fact(id="F001", statement="2040年の推計人口は8,500人",
                       measure_key="population"),
                  Fact(id="F002", statement="歯科医院は8院",
                       measure_key="dental_clinics")],
        "patterns": [Pattern(id="P001", title="将来人口は減少する見込み",
                             evidence=["F001", "F002"], evidence_summary="…",
                             importance="medium",
                             research_questions=["なぜか"])],
        "assumptions": [Assumption(
            id="A001",
            statement="将来推計人口は、この地区の住宅供給を織り込んでいる",
            rests_on=["F001"],
            if_wrong="人口が増える側に振れ、立地判断が変わる")],
        "questions": [_question()],
    }
    data.update(overrides)
    return Step1Output(**data)


def test_a_question_that_the_data_already_answers_is_rejected():
    """**手元にある答えを、外部に訊きに行かせない。**

    「昼間人口は何人か」「周辺の歯科医院は何件か」はデータセットに書いて
    あります（指示書 §62）。それを外部調査の問いにすると、検索の上限が
    既に知っていることに使われます。
    """
    from kaigyou_intel.schemas import verify_step1

    output = _step1_with_questions(
        questions=[_question(question="昼間人口は何人か", already_in_data=True)])
    problems = verify_step1(output, {"population", "dental_clinics"})
    assert any("already_in_data" in p.problem for p in problems), problems


def test_a_question_that_moves_nothing_is_rejected():
    """答えが出ても何も動かない問いは、知識が増えるだけです（§59）。

    検索の上限は決まっているので、そこに使うと動かせる問いに回りません。
    """
    from kaigyou_intel.schemas import verify_step1

    output = _step1_with_questions(
        questions=[_question(question="この地区の区画整理はいつ完了したか",
                             decision_levers=[])])
    problems = verify_step1(output, {"population", "dental_clinics"})
    assert any("decision_levers" in p.problem for p in problems), problems


def test_a_question_from_a_single_fact_is_not_a_cross_check():
    """**1 つの事実からしか出ていない問いは、突き合わせではありません**（§58）。

    「若年人口が多い」だけからは「なぜ若者が多いのか」しか出ず、それは
    地域紹介の質問です。
    """
    from kaigyou_intel.schemas import verify_step1

    output = _step1_with_questions(questions=[_question(
        trigger={"type": "deviation_from_peers",
                 "facts": ["20〜39歳が周辺より15ポイント多い"], "reason": "r"})])
    problems = verify_step1(output, {"population", "dental_clinics"})
    assert any("突き合わせ" in p.problem for p in problems), problems


def test_an_unresearchable_question_must_say_where_it_looked():
    """`low` は失敗ではありませんが、行き先が要ります（§56）。

    どこに公表されていないのかが、そのまま現地確認の理由になります。
    """
    from kaigyou_intel.schemas import verify_step1

    output = _step1_with_questions(questions=[_question(
        researchability="low", researchability_reason="")])
    problems = verify_step1(output, {"population", "dental_clinics"})
    assert any("researchability" in p.problem for p in problems), problems

    ok = _step1_with_questions(questions=[_question(
        researchability="low",
        researchability_reason="医師・歯科医師・薬剤師統計は都道府県単位まで")])
    assert not verify_step1(ok, {"population", "dental_clinics"})


def test_the_good_question_passes():
    """区画整理の例が、そのまま通ること。**これが目標の形です。**"""
    from kaigyou_intel.schemas import verify_step1

    assert not verify_step1(_step1_with_questions(),
                            {"population", "dental_clinics"})


def test_a_job_from_before_questions_existed_is_not_judged():
    """古い形（questions が空）に新しい検算を掛けない。

    掛けると、問いを第一級にする前に保存されたジョブが再実行のたびに落ちます。
    """
    from kaigyou_intel.schemas import Step1Output, verify_step1

    old = Step1Output(
        facts=[Fact(id="F001", statement="s", measure_key="population"),
               Fact(id="F002", statement="s", measure_key="dental_clinics")],
        patterns=[Pattern(id="P001", title="t", evidence=["F001", "F002"],
                          evidence_summary="s", importance="medium",
                          research_questions=["q"])])
    assert not verify_step1(old, {"population", "dental_clinics"})


def test_a_question_known_to_have_no_public_answer_is_never_searched():
    """**これが「なぜ毎回調べるのか」への答えです。**

    公表されていないと分かっている問いは、検索に回しません。落とすのでも
    ありません——そのまま「開業前に現地で確かめること」になります（§56）。
    """
    from kaigyou_intel.steps import step2_research

    step1 = {
        "patterns": [{"id": "P001", "title": "t", "evidence_summary": "s",
                      "importance": "high", "research_questions": ["q"]}],
        "questions": [
            _question().model_dump(),
            _question(id="Q002",
                      question="市内の歯科衛生士・歯科医師の年齢構成は",
                      researchability="low",
                      researchability_reason="医師・歯科医師・薬剤師統計は"
                                             "都道府県単位までで、市区町村別の"
                                             "年齢階級は公表されていない",
                      what_would_answer_it="県の歯科医師会に照会する").model_dump(),
        ],
    }
    payload = step2_research.build_input(step1, {"location": {"name": "沼津"}})
    # 検索に回るのは researchability が low でないものだけ。
    assert [q["id"] for q in payload["questions"]] == ["Q001"]
    assert [q["id"] for q in payload["questions_for_the_field"]] == ["Q002"]


def test_the_unsearched_question_still_reaches_the_report(monkeypatch):
    """検索しなかった問いが、**そのまま現地確認の項目になる**こと。

    黙って消すと、「調べていない」と「調べようがない」の区別がつきません。
    """
    from kaigyou_intel.steps import step2_research

    calls: list[dict] = []

    def fake_ask(*, step_number, system, user, schema=None, **kw):
        calls.append({"schema": schema, "user": user})
        if schema is None:
            return llm.Result(
                parsed=None, text="調査結果。", usage=llm.Usage(web_searches=1),
                model="m",
                sources=[{"url": "https://www.city.chuo.lg.jp/toukei/jinkou.html",
                          "title": "t", "page_age": None}])
        return llm.Result(parsed=_step2_output(), usage=llm.Usage(), model="m")

    monkeypatch.setattr(llm, "ask", fake_ask)
    step1 = {
        "patterns": [{"id": "P001", "title": "t", "evidence_summary": "s",
                      "importance": "high", "research_questions": ["q"]}],
        "questions": [_question(
            id="Q002", question="市内の歯科衛生士の年齢構成は",
            researchability="low",
            researchability_reason="市区町村別の年齢階級は公表されていない",
            what_would_answer_it="県の歯科医師会に照会する").model_dump()],
    }
    payload = step2_research.build_input(step1, {"location": {"name": "沼津"}})
    output, _, _ = step2_research.run(payload)

    # 検索の本文に、その問いが混ざっていないこと。
    research = [c for c in calls if c["schema"] is None]
    assert all("歯科衛生士の年齢構成" not in c["user"] for c in research), \
        "調べないと決めた問いを検索に渡している"
    # それでも記録には残り、決着のさせ方が付いていること。
    field = {q["question_id"]: q for q in output["open_questions"]}
    assert "Q002" in field
    assert "公表されていない" in field["Q002"]["why"]
    assert "歯科医師会に照会" in field["Q002"]["what_would_settle_it"]


def test_the_report_says_which_assumption_the_question_was_doubting(dataset):
    """問いだけを並べると、なぜそれを訊いたのかが分かりません（§57）。

    **そこが、この文書と地域紹介との違いです。**
    """
    from kaigyou_intel.report import to_markdown

    inquiry = {
        "questions": [_question().model_dump()],
        "assumptions": [{
            "id": "A001",
            "statement": "将来推計人口は、この地区の住宅供給を織り込んでいる",
            "rests_on": ["F001"],
            "if_wrong": "人口が増える側に振れ、立地判断が変わる"}],
        "hypotheses": [], "external_facts": [], "open_questions": [],
    }
    markdown = to_markdown(_report_output().model_dump(), to_jsonable(dataset),
                           [], None, None, inquiry)
    assert "疑っている前提" in markdown
    assert "住宅供給を織り込んでいる" in markdown
    assert "外れていた場合" in markdown
    # 突き合わせた事実も出ること。1 つの事実からは良い問いは出ません。
    assert "手元のデータと、外部で見つかった事実が食い違う" in markdown
    assert "中央工区で土地区画整理事業が進行中" in markdown


def test_a_question_sent_to_the_field_is_not_counted_as_a_wasted_search(conn,
                                                                        dataset):
    """最初から現地確認へ回した問いを、空振りに数えない。

    **あれは判断であって、失敗ではありません。** 空振りに数えると、正しく
    判断するほど成績が下がります。
    """
    from kaigyou_intel import jobs
    from kaigyou_intel import question_quality as q

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    try:
        jobs.start_step(conn, job_id, 1, {}, {"prompt_version": "v"})
        jobs.finish_step(conn, job_id, 1, {"questions": [
            _question(id="Q001", researchability="low",
                      researchability_reason="公表されていない").model_dump(),
            _question(id="Q002", researchability="high").model_dump()]}, {})
        jobs.start_step(conn, job_id, 2, {}, {})
        jobs.finish_step(conn, job_id, 2, {
            "open_questions": [{"question_id": "Q001", "why": "w",
                                "what_would_settle_it": "現地で確かめる"}]}, {})

        summary = q.across_jobs(conn)
        assert summary["searched"] == 1, "現地へ回した問いを検索に数えている"
        assert summary["wasted_searches"] == 1, "Q002 は検索して空振り"
    finally:
        _drop_job(job_id)


def test_the_old_string_list_does_not_smuggle_the_question_back_in():
    """**外したつもりで外れていない、がいちばん質が悪い。**

    `questions` と `research_questions` は同じ問いを別の形で持っています。
    片方だけ落としても、STEP2 の調査プロンプトは「この PATTERN の
    research_questions にだけ答えてください」と言うので、そのまま検索されます。
    画面にもレポートにも何も出ません。
    """
    from kaigyou_intel.steps import step2_research

    unsearchable = "市内の歯科衛生士・歯科医師の年齢構成は"
    step1 = {
        "patterns": [{"id": "P001", "title": "t", "evidence_summary": "s",
                      "importance": "high",
                      "research_questions": [unsearchable, "保留地は残っているか"]}],
        "questions": [_question(
            id="Q002", question=unsearchable, researchability="low",
            researchability_reason="市区町村別の年齢階級は公表されていない",
        ).model_dump()],
    }
    payload = step2_research.build_input(step1, {"location": {"name": "沼津"}})
    asked = payload["patterns"][0]["research_questions"]
    assert unsearchable not in asked, "古い文字列の並びから検索に渡っている"
    assert "保留地は残っているか" in asked


# ================================================= GIS が Fact を確定し、LLM が解釈する
#
# 指示書 §7・§17。**引き算で出るものを LLM にやらせない。**
#
#   人口：       市街地 上位 8%
#   歯科医院数： 市街地 上位 35%
#
# この 2 行から「人口規模に対して歯科医院が相対的に少ない」を読み取るのは、
# 以前は LLM の仕事でした。それは Fact ではなく解釈で、検算できません。

def _bench(scope: str, percentile: float, *, discriminating: bool = True):
    from kaigyou_core.measures import Benchmark

    return Benchmark(type=scope, label=f"{scope}の商圏", value=1.0,
                     comparison="中央値", sample_count=1000,
                     # **0〜100（%）です。**0〜1 ではありません。
                     percentile=percentile, discriminating=discriminating)


def _measure(key: str, label: str, layer: str, *benchmarks):
    from kaigyou_core.measures import Measure

    return Measure(key=key, label=label, value=1.0, unit="", source="s",
                   layer=layer, benchmarks=list(benchmarks))


def _positioning_config():
    return {
        "benchmark_version": "v1.0",
        "prefer_scopes": ["urban", "prefecture"],
        "axes": {
            "population_mass": {"label": "人口集積", "measures": ["population"]},
            "supply": {"label": "歯科の供給", "measures": ["clinics_per_10k"]},
        },
        "bands": [{"at_least": 0.90, "label": "非常に高い"},
                  {"at_least": 0.40, "label": "平均的"},
                  {"at_least": 0.00, "label": "低い"}],
        "gaps": {
            "demand_vs_supply": {
                "label": "需要と供給", "a": "population", "b": "clinics_per_10k",
                "threshold": 0.20,
                "a_over_b": "人口規模に対して、歯科医院が相対的に少ない",
                "b_over_a": "人口規模に対して、歯科医院が相対的に多い"}},
        "region_types": [
            {"label": "住宅集積型",
             "when": {"population_mass": {"at_least": 0.75},
                      "supply": {"below": 0.50}}}],
    }


def test_the_imbalance_is_computed_not_inferred():
    """**引き算は GIS がやります。** LLM が言い直すと検算できません。"""
    from kaigyou_core import positioning

    result = positioning.build(
        [_measure("population", "商圏人口", "residents", _bench("urban", 92.0)),
         _measure("clinics_per_10k", "人口1万人あたり歯科医院数", "competition",
                  _bench("urban", 35.0))],
        _positioning_config())

    gap = {g["key"]: g for g in result["gaps"]}["demand_vs_supply"]
    assert gap["present"] is True
    assert gap["statement"] == "人口規模に対して、歯科医院が相対的に少ない"
    # percentile は 0〜100 で入ってきます。**単位を取り違えると落ちずに
    # それらしい数字が出ます**（実測：軸の点が 9560 になりました）。
    assert gap["gap"] == pytest.approx(0.57, abs=0.01)
    assert {a["key"]: a["score"] for a in result["axes"]}["population_mass"] == 92


def test_a_small_difference_is_not_sold_as_a_feature():
    """しきい値に届かない差を「アンバランス」と言わない。

    **「調べたが差は無かった」と「見ていない」は別のこと**なので、行としては
    残します。
    """
    from kaigyou_core import positioning

    result = positioning.build(
        [_measure("population", "商圏人口", "residents", _bench("urban", 55.0)),
         _measure("clinics_per_10k", "医院数", "competition", _bench("urban", 48.0))],
        _positioning_config())
    gap = {g["key"]: g for g in result["gaps"]}["demand_vs_supply"]
    assert gap["present"] is False
    assert gap["statement"] is None
    assert gap["gap"] is not None, "調べたことは残す"


def test_percentiles_from_different_populations_are_never_subtracted():
    """**母集団が違えば同じ数字の意味が変わります。**

    人口を市街地の中で、医院数を県内全メッシュの中で見て引き算すると、
    意味のない数字がそれらしい顔で出ます。
    """
    from kaigyou_core import positioning

    result = positioning.build(
        [_measure("population", "商圏人口", "residents", _bench("urban", 92.0)),
         # 医院数は市街地の分布を持たない（県内全メッシュだけ）。
         _measure("clinics_per_10k", "医院数", "competition",
                  _bench("prefecture", 35.0))],
        _positioning_config())
    assert result["compared_with"]["type"] == "urban"
    gap = {g["key"]: g for g in result["gaps"]}["demand_vs_supply"]
    assert gap.get("present") is False
    assert "取れませんでした" in gap["unavailable_reason"]


def test_a_population_that_cannot_discriminate_is_not_used():
    """県内全メッシュのように大半が無人の母集団では、上位・下位を語りません。

    町の中心はどこでも上位数%に入り、percentile は「市街地かどうか」しか
    測っていません。
    """
    from kaigyou_core import positioning

    result = positioning.build(
        [_measure("population", "商圏人口", "residents",
                  _bench("prefecture", 92.0, discriminating=False))],
        _positioning_config())
    assert result["available"] is False
    assert result["why_not"]


def test_a_missing_measure_is_not_scored_as_low():
    """**0 として扱うと、データが無いことが「低い」になります。**"""
    from kaigyou_core import positioning

    result = positioning.build(
        [_measure("population", "商圏人口", "residents", _bench("urban", 92.0))],
        _positioning_config())
    supply = {a["key"]: a for a in result["axes"]}["supply"]
    assert supply["score"] is None
    assert supply["unavailable_reason"], "黙って落とすと『調べたうえで低い』に見える"


def test_the_region_type_is_decided_by_a_rule_not_by_the_model():
    """指示書 §9。**名称を LLM に決めさせません。**"""
    from kaigyou_core import positioning

    result = positioning.build(
        [_measure("population", "商圏人口", "residents", _bench("urban", 92.0)),
         _measure("clinics_per_10k", "医院数", "competition", _bench("urban", 20.0))],
        _positioning_config())
    assert result["region_type"]["label"] == "住宅集積型"
    assert result["region_type"]["because"], "なぜその型なのかが残ること"


def test_no_type_is_better_than_an_invented_one():
    """どれにも当てはまらないときに、無理に名前を付けない。

    付けた名前は、そのあと読み手の頭の中で事実として働きます。
    """
    from kaigyou_core import positioning

    result = positioning.build(
        [_measure("population", "商圏人口", "residents", _bench("urban", 30.0)),
         _measure("clinics_per_10k", "医院数", "competition", _bench("urban", 80.0))],
        _positioning_config())
    assert result["region_type"]["label"] is None
    assert result["region_type"]["why_not"]


def test_the_model_can_cite_the_computed_position():
    """**引用できない数字は、LLM が言い直すしかありません。**

    言い直した瞬間に検算できなくなります。
    """
    from kaigyou_core import positioning

    result = positioning.build(
        [_measure("population", "商圏人口", "residents", _bench("urban", 92.0)),
         _measure("clinics_per_10k", "医院数", "competition", _bench("urban", 35.0))],
        _positioning_config())
    citable = positioning.citable(
        result, {"population": "residents", "clinics_per_10k": "competition"})
    keys = {c["key"]: c for c in citable}
    assert "positioning.gap.demand_vs_supply" in keys
    assert "positioning.axis.population_mass" in keys
    # **層は元の指標のもの。** positioning という層を新設すると、ギャップを
    # 2 つ引いた PATTERN が「層を跨いだ」と判定されます。
    assert keys["positioning.axis.population_mass"]["layer"] == "residents"
    assert "positioning" not in {c["layer"] for c in citable}
    # どの母集団と比べたのかが、引用した先にも残ること。
    assert "urban" in keys["positioning.gap.demand_vs_supply"]["source"]


def test_the_computed_position_reaches_the_model_as_a_citable_key(conn):
    """STEP1 の入力で、実際に measure_key として引ける形になっていること。"""
    from kaigyou_core.dataset import build_dataset
    from kaigyou_intel.projection import citable_keys, for_step1

    dataset = build_dataset(conn, 35.6717, 139.7650, 1000)
    payload = for_step1(dataset, {})
    keys = citable_keys(payload)
    assert any(k.startswith("positioning.axis.") for k in keys), sorted(keys)[:5]
    # 位置づけそのものも渡すこと（軸・ギャップ・母集団・版）。
    assert payload["positioning"]["compared_with"]["label"]
    assert payload["positioning"]["benchmark_version"]


def test_the_report_shows_the_position_before_the_prose(dataset):
    """**GIS が計算した節を、LLM が書いた本文より前に置く。**

    あとに置くと、文章を信じたあとで根拠を照合することになります。
    """
    from kaigyou_intel.report import to_markdown

    with_position = dict(dataset)
    with_position["positioning"] = {
        "available": True,
        "compared_with": {"type": "urban", "label": "静岡県内の市街地",
                          "sample_count": 1016},
        "benchmark_version": "v1.0", "calculated_on": "2026-09-02",
        "axes": [{"key": "population_mass", "label": "人口集積", "score": 92,
                  "percentile": 0.92, "assessment": "非常に高い",
                  "means": "そこに住んでいる人の多さ", "from_measures": ["population"]},
                 {"key": "cost", "label": "立地コスト", "score": None,
                  "percentile": None, "assessment": None,
                  "unavailable_reason": "地価の分布が取れないため未確認です。"}],
        "gaps": [{"key": "demand_vs_supply", "label": "需要と供給", "present": True,
                  "gap": 0.57, "threshold": 0.2,
                  "statement": "人口規模に対して、歯科医院が相対的に少ない",
                  "a": {"key": "population", "label": "商圏人口", "percentile": 0.92},
                  "b": {"key": "clinics_per_10k", "label": "医院数",
                        "percentile": 0.35},
                  "note": "医院数は施設の数で、ユニット数ではありません。"}],
        "region_type": {"label": "住宅集積型", "because": ["population_mass=0.92"]},
        "note": "percentile は相対値です。予測ではありません。",
    }
    markdown = to_markdown({"title": "t", "summary": "s", "sections": [],
                            "executive_summary": "e"}, to_jsonable(with_position))

    assert markdown.index("## この地域はどんな場所か") < markdown.index("## 結論")
    assert "この節は生成された文章ではありません" in markdown
    assert "住宅集積型" in markdown
    assert "人口規模に対して、歯科医院が相対的に少ない" in markdown
    # **どれと比べたのか・いつ・どの版か**（指示書 §16・§19）。
    assert "静岡県内の市街地" in markdown and "1,016 商圏" in markdown
    assert "v1.0" in markdown and "2026-09-02" in markdown
    # 取れなかった軸を黙って落とさない。
    assert "未確認" in markdown


def test_the_prompts_still_say_the_model_does_not_make_the_numbers():
    """**この約束が薄まると、このサービスは「地域について書ける生成AI」に戻ります。**

    文章の巧さで差はつきません。差がつくのは、書いてあることが確かめられるか
    どうかです。
    """
    step1 = cfg.prompt_text("step1_features.md")
    assert "あなたは数字を作りません" in step1
    assert "暗算しないでください" in step1
    assert "positioning" in step1

    step3 = cfg.prompt_text("step3_demand.md")
    assert "数字を確定させるのは GIS です" in step3

    step4 = cfg.prompt_text("step4_client_report.md")
    assert "数字も作りません" in step4
    assert "母集団を書かずに" in step4


# ================================================ 母集団の分布を事前計算する
#
# 実測（銀座1km・手元の PostgreSQL）：
#   measures.measure_scope_shape   8 往復  18.0ms
#   measures._scope_statistics     6 往復  31.3ms
#   analysis.analyze_point         3 往復  14.7ms   ← 商圏の集計そのもの
#
# **母集団の形を測るのに 14 往復・49ms、商圏の集計そのものは 15ms。**
# そして周りは、クリックした地点によって変わりません。

def test_the_stored_distribution_gives_the_same_answer_as_measuring_it(conn):
    """**これが、この表を入れてよい唯一の理由です。**

    速いだけで答えが変わるなら、入れてはいけません。
    """
    from kaigyou_core import measures as M
    from kaigyou_core.dataset import build_dataset
    from kaigyou_etl import benchmarks as bench

    bench.compute(conn, prefecture_code="13", profile="default", radius_m=1000)

    def positions(dataset):
        out = {}
        for measure in dataset["measures"]["items"]:
            for b in measure.get("benchmarks") or []:
                out[(measure["key"], b["benchmark_type"])] = (
                    b.get("percentile"), b.get("rank"), b.get("of"),
                    b.get("position_label"), b.get("significance"))
        return out

    stored = positions(build_dataset(conn, 35.6717, 139.7650, 1000))
    original = M.stored_distributions
    M.stored_distributions = lambda *a, **k: {}
    try:
        live = positions(build_dataset(conn, 35.6717, 139.7650, 1000))
    finally:
        M.stored_distributions = original

    assert stored and live
    assert stored == live, {k: (stored.get(k), live.get(k))
                            for k in set(stored) | set(live)
                            if stored.get(k) != live.get(k)}


def test_a_population_that_moves_with_the_point_is_never_stored():
    """**この 2 つは事前計算できません。**

    「この地点から10km以内」と「商圏人口が同規模」は、クリックした場所で
    母集団そのものが変わります。鍵を作れないので、貯めません。
    """
    from kaigyou_core.measures import BenchmarkScope
    from kaigyou_etl.benchmarks import scope_key_of

    for kind in ("nearby", "similar_population"):
        scope = BenchmarkScope(kind, "l", "", ())
        assert scope_key_of(scope, "13", "新宿区") is None

    assert scope_key_of(BenchmarkScope("urban", "l", "", ()), "13", None) == "13"
    assert scope_key_of(BenchmarkScope("municipality", "l", "", ()),
                        "13", "新宿区") == "13:新宿区"


def test_a_missing_distribution_falls_back_to_measuring(conn):
    """**新しく県を読み込んだ直後に位置づけが出ないより、遅いほうがましです。**"""
    from kaigyou_core.dataset import build_dataset

    with conn.cursor() as cur:
        cur.execute("DELETE FROM benchmark_distributions")
    conn.commit()
    try:
        dataset = build_dataset(conn, 35.6717, 139.7650, 1000)
        items = {m["key"]: m for m in dataset["measures"]["items"]}
        assert items["population"]["benchmarks"], "測り直していない"
        # 何をその場で測ったかが残ること。「なぜ遅いのか」に後から答えるため。
        assert dataset["measures"]["primary_benchmark"]["measured_live"]
    finally:
        from kaigyou_etl import benchmarks as bench
        bench.compute(conn, prefecture_code="13", profile="default",
                      radius_m=1000)


def test_the_grid_form_is_used_only_for_large_populations():
    """小さい母集団は全部そのまま貯めます。**順位が厳密に出ます。**

    「5,448 件中 1 位」を「上位0.1%」と言えるかどうかがここで決まります。
    """
    from kaigyou_core.measures import BenchmarkScope, statistics_from_stored

    scope = BenchmarkScope("urban", "市街地", "", ())
    scope.sample_count = 5
    exact = {"population": {
        "boundaries": [10.0, 20.0, 30.0, 40.0, 50.0], "is_exact": True,
        "value_count": 5, "median": 30.0, "p25": 20.0, "p75": 40.0,
        "sample_count": 5, "scope_label": "市街地",
        "discriminating": True, "not_discriminating_reason": None,
        "share_below_viable_floor": None}}
    out = statistics_from_stored({"population": {}}, {"population": 40.0},
                                 scope, exact)
    bench = out["population"]
    # 40 以下は 4 件 / 5 件 = 80.0 percentile、順位は 5 - 4 + 1 = 2 位。
    assert bench.percentile == 80.0
    assert bench.rank == 2 and bench.of == 5


def test_the_point_is_placed_without_a_single_query(conn):
    """**地点をクリックしたときに、母集団を測り直しません。**

    保存済みの母集団については SQL を投げないこと。投げていたら、この表は
    ただ場所を取っているだけです。
    """
    from kaigyou_core.measures import (
        BenchmarkScope, shape_from_stored, statistics_from_stored,
        stored_distributions)
    from kaigyou_etl import benchmarks as bench

    bench.compute(conn, prefecture_code="13", profile="default", radius_m=1000)
    stored = stored_distributions(
        conn, prefecture_code="13", municipality=None, profile="default",
        radius_m=1000)
    assert "urban" in stored, sorted(stored)

    scope = BenchmarkScope("urban", "", "", ())
    with _counting_queries() as n:
        assert shape_from_stored(scope, stored["urban"]) is True
        out = statistics_from_stored(
            {"population": {}}, {"population": 12430.0}, scope, stored["urban"])
        assert n[0] == 0, "保存済みの母集団に SQL を投げている"
    assert out["population"].percentile is not None
    # 母集団の名前も一緒に持っていること。**書かずに「上位8%」と言えば、
    # それはもう統計ではありません。**
    assert scope.label


def test_the_stored_distribution_records_how_it_was_computed(conn):
    """再現性（指示書 §19）。データが更新されても、どの版の計算かが分かること。"""
    from kaigyou_etl import benchmarks as bench

    bench.compute(conn, prefecture_code="13", profile="default", radius_m=1000)
    with conn.cursor() as cur:
        cur.execute("SELECT benchmark_version, computed_at, scope_label, "
                    "sample_count FROM benchmark_distributions LIMIT 1")
        row = cur.fetchone()
    assert row["benchmark_version"]
    assert row["computed_at"]
    assert row["scope_label"], "どの母集団かが読める形で入っていること"
    assert row["sample_count"] > 0
