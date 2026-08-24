"""統計を「値」から「自己記述する測定値」に変える層。

ここで守りたいのは計算の正しさだけではありません。**比較していないものを
比較したように見せない**ことです。

percentile が欠けている指標は「平凡だった」ように読めます。比較対象を書かない
「上位6%」はもう統計ではありません。「未確認」を落として渡せば、読み手は
「調べたうえで該当なし」と受け取ります。どれも数字は 1 つも間違っていないまま、
結論だけが間違います。下のテストはその 3 つを固定しています。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kaigyou_core import config as cfg
from kaigyou_core.measures import (
    MEASURE_SPECS,
    SIGNIFICANCE_BANDS,
    SPECIALTY_SPECS,
    Benchmark,
    Measure,
    benchmark_scopes,
    build_insights,
    measure_value,
    significance_for,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------- 位置の言葉
@pytest.mark.parametrize("percentile, expected", [
    (99.0, "very_high"), (95.0, "very_high"),
    (94.9, "high"), (80.0, "high"),
    (79.9, "typical"), (50.0, "typical"), (20.0, "typical"),
    (19.9, "low"), (5.0, "low"),
    (4.9, "very_low"), (0.0, "very_low"),
])
def test_significance_comes_from_a_threshold_not_a_feeling(percentile, expected):
    key, label = significance_for(percentile)
    assert key == expected
    assert label, "区分には日本語のラベルが要る（そのまま文に使うため）"


def test_significance_is_withheld_when_there_is_no_percentile():
    assert significance_for(None) == (None, None)


def test_the_bands_are_published_so_a_reader_can_check_them():
    """「極めて高い」が何を指すかは、文書の中に書いてあること。"""
    from kaigyou_core.measures import READING_GUIDE

    published = {b["key"] for b in READING_GUIDE["significance_bands"]}
    assert published == {key for _t, key, _l in SIGNIFICANCE_BANDS}


def test_a_low_value_is_not_described_as_top_94_percent():
    """低い値を top_share_pct だけで書くと、数は正しいまま意味が逆になる。

    歯科医院1院あたり人口が下位6%の商圏は「上位94%」ではない。
    """
    from kaigyou_core.measures import _position_label

    assert _position_label(4286, 4553) == "下位5.9%"
    assert _position_label(268, 4553) == "上位5.9%"
    # いちばん上は「上位0%」ではない。percentile は最大値で 100 に頭打ちになる
    # ので、そこから引くと 5,448 件中 1 位が 0% になる。
    assert _position_label(1, 5448) == "上位0.1%"
    assert _position_label(5448, 5448) == "下位0.1%"


def test_the_position_is_always_a_true_containment_statement():
    """「上位x%」は「上位x%に入っている」と読まれる。切り捨ててはいけない。"""
    from kaigyou_core.measures import _position_label

    for rank, total in ((7, 1000), (123, 4553), (1, 30), (999, 1000)):
        label = _position_label(rank, total)
        share = float(label.rstrip("%").removeprefix("上位").removeprefix("下位"))
        actual = 100.0 * (rank if label.startswith("上位") else total - rank + 1) / total
        assert share >= actual - 1e-9, f"{label} excludes rank {rank}/{total}"


# ------------------------------------------------------- 同じ量どうしの比較
def test_a_ratio_measure_is_built_the_same_way_as_its_benchmark_column():
    """商圏側が割合で、比較側が実数だと、percentile はそれらしい嘘を返し続ける。

    片方だけ直したときに気づけるよう、両方の式をここで突き合わせる。
    """
    spec = MEASURE_SPECS["child_share"]
    metrics = {"age_0_14": 1000, "population": 10000}
    assert measure_value(spec, metrics) == pytest.approx(10.0)
    # 比較側の式も 100 倍して人口で割っていること。
    assert "100.0 * ms.age_0_14" in spec["column"]
    assert "NULLIF(ms.population, 0)" in spec["column"]


def test_a_percentage_measure_scales_both_sides():
    spec = MEASURE_SPECS["population_growth"]
    assert measure_value(spec, {"population_growth": 0.0433}) == pytest.approx(4.33)
    assert spec["column"].startswith("100.0 *")


def test_clinics_per_10k_matches_its_column():
    spec = MEASURE_SPECS["clinics_per_10k"]
    assert measure_value(spec, {"facility_count": 5, "population": 20000}) == pytest.approx(2.5)
    assert "10000.0 * ms.facility_count" in spec["column"]


def test_a_ratio_with_no_denominator_is_unknown_not_zero():
    assert measure_value(MEASURE_SPECS["child_share"],
                         {"age_0_14": 100, "population": 0}) is None
    assert measure_value(MEASURE_SPECS["child_share"],
                         {"age_0_14": 100, "population": None}) is None


def test_every_measure_declares_its_unit_source_and_year():
    """単位と出典と基準年の無い数字は、読み手には使えない。"""
    for key, spec in {**MEASURE_SPECS, **SPECIALTY_SPECS}.items():
        assert spec.get("unit"), f"{key} に単位がありません"
        assert spec.get("source"), f"{key} に出典がありません"
        assert spec.get("year"), f"{key} に基準年がありません"
        assert spec.get("definition"), f"{key} に定義がありません"
        assert spec.get("higher_means"), f"{key} に higher_means がありません"


# ------------------------------------------------------------- 比較対象の宣言
def test_the_scopes_never_include_a_national_comparison():
    """全国のメッシュ統計は読み込んでいない。都のメッシュを全国と言い換えない。"""
    scopes = benchmark_scopes("13", "東京都", "中央区", 30000, 1000)
    types = {s.type for s in scopes}
    assert types == {"prefecture", "municipality", "similar_population"}
    assert "national" not in types


def test_the_similar_population_scope_is_built_from_this_points_population():
    scopes = {s.type: s for s in benchmark_scopes("13", "東京都", None, 10000, 1000)}
    similar = scopes["similar_population"]
    assert similar.params[1:] == (8000.0, 12000.0)
    assert "8,000" in similar.label and "12,000" in similar.label


def test_scopes_that_cannot_be_built_are_simply_absent():
    """境界データが無ければ市区町村比較は作らない。作れないものは作らない。"""
    types = {s.type for s in benchmark_scopes("13", "東京都", None, None, 1000)}
    assert types == {"prefecture"}


def test_a_measure_without_a_benchmark_says_why():
    """percentile が欠けた指標を「平凡だった」と読ませない。"""
    measure = Measure(key="workers", label="従業者数", value=100, unit="人",
                      source="経済センサス",
                      unavailable_reason="比較用の列が未作成のため")
    out = measure.as_dict()
    assert out["percentile"] is None
    assert out["benchmark_unavailable_reason"] == "比較用の列が未作成のため"


# --------------------------------------------------------------- 複合指標
def _measure(key, label, value, percentile=None):
    m = Measure(key=key, label=label, value=value, unit="人", source="国勢調査")
    if percentile is not None:
        significance, significance_label = significance_for(percentile)
        m.benchmarks = [Benchmark(
            type="prefecture", label="都", value=1.0, comparison="median",
            sample_count=5448, percentile=percentile,
            top_share_pct=round(100 - percentile, 1),
            position_label=f"上位{round(100 - percentile, 1):g}%",
            rank=100, of=5448, direction="high",
            significance=significance, significance_label=significance_label)]
    return m


def test_an_insight_reports_what_it_could_not_establish():
    """これがこの構造のいちばんの働き。

    「小児歯科の供給状況は未確認」と書けるのは、揃わなかったものが残っている
    ときだけ。黙って落とすと「調べたうえで該当なし」に見える。
    """
    config = {"insights": {"child": {
        "label": "小児人口の厚み", "question": "需要側が厚いか",
        "components": ["child_population", "specialty_clinics"]}}}
    measures = [
        _measure("child_population", "0〜14歳人口", 7331, percentile=94.0),
        Measure(key="specialty_clinics", label="小児歯科の標榜医院数", value=None,
                unit="院", source="医療情報ネット",
                unavailable_reason="profile=pediatric で呼ぶと取得できます"),
    ]
    insight = build_insights(measures, config)[0]

    assert insight["complete"] is False
    assert insight["component_count"] == 1
    assert insight["components_requested"] == 2
    assert insight["gaps"] == [
        "小児歯科の標榜医院数: profile=pediatric で呼ぶと取得できます"]
    # 揃ったほうは、位置づけごと渡す。読み手に照合させない。
    child = insight["components"][0]
    assert (child["percentile"], child["top_share_pct"]) == (94.0, 6.0)
    assert child["position_label"] == "上位6%"
    assert child["significance"] == "high"


def test_an_insight_is_complete_only_when_every_component_resolved():
    config = {"insights": {"child": {
        "label": "小児人口の厚み", "components": ["child_population"]}}}
    insight = build_insights(
        [_measure("child_population", "0〜14歳人口", 7331, percentile=94.0)], config)[0]
    assert insight["complete"] is True
    assert insight["gaps"] == []


def test_a_value_without_a_benchmark_is_flagged_inside_the_insight():
    """値はあるが比較できない、は「揃った」ではない。"""
    config = {"insights": {"child": {"components": ["child_population"]}}}
    insight = build_insights(
        [_measure("child_population", "0〜14歳人口", 7331)], config)[0]
    assert insight["complete"] is False
    assert "未確認" in insight["gaps"][0]


def test_insights_carry_no_verdict():
    """複合指標は結論を出さない。

    要件が禁じているのは開業成否・売上・患者数の予測で、「総合的に有望」も
    根拠のない結論という点では同じ。出すのは材料と、揃わなかったものまで。
    """
    config = cfg.insights_config()
    insight = build_insights(
        [_measure("child_population", "0〜14歳人口", 7331, percentile=94.0)], config)[0]
    forbidden = {"score", "verdict", "recommendation", "conclusion", "rating",
                 "success_probability", "revenue", "patients"}
    assert forbidden.isdisjoint(insight)


def test_the_shipped_insights_only_reference_measures_that_exist():
    """設定に打ち間違いがあると、その成分は黙って gaps に落ちる。

    「未確認」が出る理由が「データが無い」ではなく「キーを間違えた」だったら、
    読み手には区別がつかない。
    """
    known = set(MEASURE_SPECS) | set(SPECIALTY_SPECS)
    for key, spec in (cfg.insights_config().get("insights") or {}).items():
        for component in spec.get("components") or []:
            assert component in known, f"{key} が未知の指標 {component!r} を参照しています"


def test_every_shipped_insight_states_the_question_it_helps_answer():
    for key, spec in (cfg.insights_config().get("insights") or {}).items():
        assert spec.get("label"), f"{key} に label がありません"
        assert spec.get("question"), f"{key} に question がありません"
        assert spec.get("components"), f"{key} に components がありません"


# ------------------------------------------------ 組み上がった文書に対して
psycopg = pytest.importorskip("psycopg")


@pytest.fixture(scope="module")
def document():
    """実データに対して 1 回だけ組み立て、そのうえで細かく見る。"""
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app
    from kaigyou_core.db import connect

    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM mesh_scores WHERE overall_score IS NOT NULL")
            if cur.fetchone()["n"] == 0:
                pytest.skip("mesh scores not computed here")
            cur.execute("""
                SELECT ST_Y(pm.centroid) AS lat, ST_X(pm.centroid) AS lng
                FROM mesh_scores ms JOIN population_mesh pm ON pm.id = ms.mesh_id
                WHERE ms.profile = 'pediatric' AND ms.overall_score IS NOT NULL
                ORDER BY ms.overall_score DESC LIMIT 1""")
            point = cur.fetchone()
    except psycopg.OperationalError as exc:
        pytest.skip(f"database unavailable: {exc}")

    response = TestClient(app).get("/api/dataset", params={
        "lat": point["lat"], "lng": point["lng"], "radius": 1000,
        "profile": "pediatric", "max_clinics": 0})
    assert response.status_code == 200, response.text[:400]
    return response.json()


def test_the_document_announces_the_shape_it_is(document):
    assert document["schema_version"] == "2.0"
    assert document["reading_guide"]["start_here"] == "measures"
    # この文書で答えられないことが、文書の中に書いてあること。
    joined = " ".join(document["reading_guide"]["cannot_answer"])
    assert "売上" in joined and "全国比較" in joined


def test_every_measure_carries_everything_needed_to_read_it(document):
    required = ("key", "label", "value", "unit", "data_year", "source",
                "definition", "higher_means", "benchmarks", "growth")
    for item in document["measures"]["items"]:
        for field in required:
            assert field in item, f"{item['key']} に {field} がありません"
        if item["value"] is None:
            assert item.get("benchmark_unavailable_reason")


def test_a_measure_with_a_value_is_placed_against_the_prefecture(document):
    placed = [m for m in document["measures"]["items"] if m["value"] is not None]
    assert placed, "値のある指標が 1 つもありません"
    for item in placed:
        assert item["benchmark_type"] == "prefecture", item["key"]
        assert item["percentile"] is not None, item["key"]
        assert item["rank"] is not None and item["of"], item["key"]
        assert 1 <= item["rank"] <= item["of"], item["key"]
        assert item["position_label"], item["key"]


def test_percentile_rank_and_top_share_agree_with_each_other(document):
    """3 つは同じ 1 つの位置を別の言い方で出したもの。ずれていたら片方が嘘。"""
    for item in document["measures"]["items"]:
        if item["value"] is None:
            continue
        assert item["top_share_pct"] == pytest.approx(100 - item["percentile"], abs=0.11)
        # percentile は小数第1位まで丸めて出しているので、そこから件数を
        # 復元すると母数に比例した誤差が乗る（5,448件なら 0.05% ≒ 2.7件）。
        below = round(item["percentile"] / 100 * item["of"])
        tolerance = item["of"] * 0.0005 + 1
        assert abs(item["rank"] - (item["of"] - below + 1)) <= tolerance, item["key"]


def test_the_comparison_group_is_described_once_and_referenced_by_type(document):
    scopes = {s["benchmark_type"]: s for s in document["measures"]["benchmark_scopes"]}
    assert "prefecture" in scopes
    for scope in scopes.values():
        assert scope["label"] and scope["sample_count"] >= 30
    for item in document["measures"]["items"]:
        for bench in item["benchmarks"]:
            assert bench["benchmark_type"] in scopes


def test_the_document_says_which_comparisons_it_could_not_make(document):
    notes = " ".join(document["data_quality"]["benchmark_notes"])
    assert "全国" in notes, "全国比較が無いことは書いてあるべき"


def test_the_measures_are_compared_at_the_radius_their_distribution_was_built_at(document):
    basis = document["measures"]["measurement_basis"]
    assert basis["radius_m"] and basis["profile"] and basis["compared_against"]


def test_the_definitions_section_no_longer_repeats_the_measures(document):
    """同じ説明が 2 か所にあると、片方だけ直したときに食い違う。"""
    keys = {m["key"] for m in document["measures"]["items"]}
    assert keys.isdisjoint(document["definitions"])
    # measures に入らない欄の説明は残っていること。
    assert "clinic_hours" in document["definitions"]


def test_the_insights_reach_the_document_with_their_gaps(document):
    insights = {i["insight_metric"]: i for i in document["insight_metrics"]}
    assert "child_population_strength" in insights
    for insight in insights.values():
        assert "gaps" in insight and "complete" in insight
        assert insight["complete"] == (not insight["gaps"])
