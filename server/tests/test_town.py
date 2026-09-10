"""「この街、どんな街？」——街の意外な事実。

**この機能がいちばん静かに壊れる形は、意外さのために事実がゆがむこと**です。
カードは常に 5 枚出せます。出すために母集団 30 件の「上位10%」を混ぜても、
取り込んでいない県の分布で語っても、**画面は成立します。**

だからここで見張るのは 4 つです。

    根拠の弱い比較を、カードにしていないか
    同じ話題でカードを埋めていないか
    出せない比較を、黙って埋めていないか
    取り込んでいない土地を、別の県の分布で語っていないか
"""
from __future__ import annotations

import pytest

from kaigyou_core import config as cfg
from kaigyou_core import town


def _config(**overrides):
    base = cfg.load_yaml(cfg.config_dir() / "town_facts.yaml")
    return {**base, **overrides}


def _measure(key, label, *, value, percentile, sample=5000, unit="人",
             discriminating=True, scope="prefecture"):
    benchmark = {"benchmark_type": scope, "percentile": percentile,
                 "position_label": f"上位{100 - percentile:.1f}%", "rank": 10,
                 "of": sample, "benchmark_value": value / 2,
                 "discriminating": discriminating}
    return {"key": key, "label": label, "value": value, "unit": unit,
            "data_year": 2020, "source": "国勢調査", "benchmarks": [benchmark]}


def _dataset(items, *, scopes=None, positioning=None, peers=None):
    return {
        "measures": {"items": items,
                     "benchmark_scopes": scopes or [
                         {"benchmark_type": "prefecture",
                          "label": "東京都の半径1000m商圏（全メッシュ）",
                          "sample_count": 5000}]},
        "positioning": positioning or {},
        "peers": peers or {},
        "location": {"lat": 35.0, "lng": 139.0, "prefecture_name": "東京都"},
        "query": {"radius_m": 1000},
        "demand": {}, "access": {},
    }


# ------------------------------------------------------------ 設定の書き間違い
def test_a_metric_that_does_not_exist_fails_loudly():
    """**黙って落とすと、その指標は永久にカードになりません。**

    しかも画面は成立して見えます。気づくのは、出るはずの事実が出ないことに
    誰かが気づいたときです。
    """
    with pytest.raises(town.UnknownTownMetric) as caught:
        town.diagnose(_dataset([]), _config(metrics=[
            {"key": "populaton", "theme": "x", "at_least": 90, "high": "…"}]))
    assert "populaton" in str(caught.value)


def test_the_shipped_configuration_only_names_metrics_that_exist():
    from kaigyou_core.measures import MEASURE_SPECS

    for spec in _config()["metrics"]:
        assert spec["key"] in MEASURE_SPECS


# ------------------------------------------------------------------ 選び方
def test_a_metric_in_the_middle_is_not_a_finding():
    """**「平均的でした」はカードになりません。** 5 枚を埋めるために出さない。"""
    view = town.diagnose(
        _dataset([_measure("population", "商圏人口", value=30000, percentile=55)]),
        _config())
    assert view["facts"] == []
    assert view["considered"] == 0


def test_a_comparison_against_too_few_places_is_dropped():
    """30 件の中の「上位10%」は 3 位というだけで、分布ではありません。"""
    view = town.diagnose(
        _dataset([_measure("population", "商圏人口", value=30000,
                           percentile=99, sample=12)]),
        _config())
    assert view["facts"] == [], "母数 12 件の順位をカードにしています"


def test_a_population_that_cannot_discriminate_is_dropped():
    """母集団の半分が無人なら、町の中心はどこでも上位に来ます。"""
    view = town.diagnose(
        _dataset([_measure("population", "商圏人口", value=30000,
                           percentile=99, discriminating=False)]),
        _config())
    assert view["facts"] == []


def test_the_same_topic_does_not_fill_the_cards():
    """人口の話が 5 枚並ぶのは「5 つの発見」ではありません。"""
    view = town.diagnose(
        _dataset([_measure("elderly_share", "65歳以上の割合", value=35,
                           percentile=99, unit="%"),
                  _measure("child_share", "0〜14歳の割合", value=4,
                           percentile=1, unit="%")]),
        _config())
    assert len(view["facts"]) == 1, "同じ話題（age）で 2 枚出ています"


def test_a_contrast_outranks_a_plain_high_number():
    """**「人口が多い」は地図で分かります。** 並べないと出ない話を上に。"""
    view = town.diagnose(
        _dataset(
            [_measure("population", "商圏人口", value=80000, percentile=97)],
            positioning={
                "compared_with": {"label": "東京都内の市街地", "sample_count": 4000},
                "gaps": [{"key": "residents_vs_workers", "present": True,
                          "statement": "住んでいる人の規模に対して、働きに来る人が多い",
                          "gap": 0.4, "a": {"label": "商圏人口", "percentile": 0.5},
                          "b": {"label": "従業者数", "percentile": 0.9}}]}),
        _config())
    assert [f["kind"] for f in view["facts"]][0] == "contrast"


def test_every_card_carries_its_comparison_and_source():
    """値だけのカードは作りません。**比較と出典が本体です。**"""
    view = town.diagnose(
        _dataset([_measure("population", "商圏人口", value=80000, percentile=97)]),
        _config())
    card = view["facts"][0]
    assert "5,000件の中で" in card["comparison"]
    assert "東京都の半径1000m商圏" in card["comparison"]
    assert card["source"].endswith("2020年")
    assert 0 < card["confidence"] <= 1


def test_a_middling_percentile_is_not_called_top():
    """**「上位52%」は位置を表しません。** 上位と言われたら高いほうを思います。"""
    assert town._position(0.48) == "真ん中あたり（上位52%）"
    assert town._position(0.97) == "上位3%"
    assert town._position(0.05) == "下位5%"


# ------------------------------------------------------- 言えないことは言う
def test_what_cannot_be_compared_is_named():
    """全国順位が無いのは「意味がないから」ではなく「取り込んでいないから」。"""
    view = town.diagnose(_dataset([]), _config())
    assert any("全国" in gap["what"] for gap in view["unavailable"])


def test_a_dataset_that_lost_its_outlook_says_so():
    dataset = _dataset([])
    dataset["demand"] = {"outlook": {"available": False,
                                     "note": "将来推計人口は取り込まれていません"}}
    view = town.diagnose(dataset, _config())
    assert any("将来推計" in gap["what"] for gap in view["unavailable"])


# ------------------------------------------------------------------ API
@pytest.fixture
def client():
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
    return TestClient(app)


def test_a_place_can_be_asked_for_by_name(client):
    """**入口は 1 行。** 駅名を入れたら、その街のカードが返ること。"""
    response = client.get("/api/town", params={"q": "三軒茶屋"})
    assert response.status_code == 200
    view = response.json()
    assert view["matched"]["name"] == "三軒茶屋駅"
    assert view["place"]["radius_m"] == 1000, "街の話は 1km で見ます"
    for card in view["facts"]:
        assert card["title"] and card["fact"] and card["comparison"]
        assert card["source"], "出典の無いカードを出しています"


def test_the_other_towns_with_the_same_name_come_back(client):
    """「府中」は東京にも広島にもあります。**黙って 1 つ選ばない。**"""
    view = client.get("/api/town", params={"q": "府中"}).json()
    assert view["alternatives"], "同名の候補を返していません"


def test_a_place_outside_the_loaded_data_is_not_analysed_as_another_prefecture(client):
    """**取り込んでいない土地を、別の県の分布で語らない。**

    実測：メッシュが東京都だけの環境で「沼津」を引くと、県が特定できず既定
    （人口最大の県＝東京都）に落ち、沼津駅のカードに湯島・高井戸が並びました。
    分析としては動いていて、画面も成立します——読み手だけが気づけません。
    """
    from kaigyou_core.analysis import prefecture_at
    from kaigyou_core.db import connect

    with connect() as conn:
        loaded = prefecture_at(conn, 35.103, 138.860) is not None
    if loaded:
        pytest.skip("この環境には沼津のメッシュが取り込まれています")

    view = client.get("/api/town", params={"q": "沼津"}).json()
    assert view["facts"] == []
    assert any("メッシュ" in gap["why"] for gap in view["unavailable"])
    assert view["matched"]["name"] == "沼津駅", "どこを引いたかは返すこと"


def test_a_name_that_matches_nothing_says_so(client):
    response = client.get("/api/town", params={"q": "ここにない街の名前"})
    assert response.status_code == 404
    assert "見つかりません" in response.json()["detail"]
