"""似た規模の土地との比較。

**この機能がいちばん静かに壊れる形は、比較が成立していないのに表が出ること**
です。同規模でない相手を並べても、表としては成立します——読み手は、それを
「同規模との比較」として読みます。だからここで見張るのは主に次の 3 つです。

    帯の外の相手を、統計（中央値・順位）に混ぜていないか
    「その軸だけ同規模」を、同規模と呼んでいないか
    比較できなかったときに、**空欄ではなく理由**が出るか

DB を要るものと要らないものを分けてあります。選び方と数え方は要りません
（渡した行を並べるだけなので）。実際に相手を選ぶところだけ DB を使います。
"""
from __future__ import annotations

import pytest

from kaigyou_core import config as cfg
from kaigyou_core import dd, peers
from kaigyou_intel import dd_report


def _specs():
    return peers.metric_specs(cfg.peers_config("dental_clinic"))


def _config(**overrides):
    return {**cfg.peers_config("dental_clinic"), **overrides}


def _peer(name, *, population, clinics, per_clinic, aged=20.0, workers=10000,
          inside=True):
    return {
        "label": name, "inside_band": inside, "distance_km": 5.0,
        "values": {"population": population, "clinics": clinics,
                   "population_per_clinic": per_clinic,
                   "age_65_plus_pct": aged, "age_0_14_pct": 10.0,
                   "population_growth_pct": 1.0, "workers": workers,
                   "daily_passengers": 50000},
    }


def _site(**values):
    base = {"population": 50000, "clinics": 30, "population_per_clinic": 1667,
            "age_65_plus_pct": 20.0, "age_0_14_pct": 10.0,
            "population_growth_pct": 1.0, "workers": 10000,
            "daily_passengers": 50000}
    return {"label": "この地点", "inside_band": True, "values": {**base, **values}}


# ------------------------------------------------------------ 設定の書き間違い
def test_a_metric_pointing_at_a_column_that_does_not_exist_fails_loudly():
    """**黙って落とすと、その軸は永久に比較されません。**

    表は成立して見えます。気づくのは、比べたはずの軸が入っていないことに
    読み手が気づいたときです。
    """
    with pytest.raises(peers.UnknownPeerColumn) as caught:
        peers.metric_specs({"metrics": [
            {"key": "x", "label": "架空の指標", "column": "populaton"}]})
    assert "populaton" in str(caught.value)


def test_the_shipped_configuration_only_names_columns_that_exist():
    """出荷している設定が、実装の許可リストと合っていること。"""
    for spec in _specs():
        assert spec["column"] in peers.ALLOWED_COLUMNS


# ------------------------------------------------------------------ 統計の作り
def test_the_reference_peers_are_the_ones_inside_the_band():
    """帯の外の相手は**表には出すが、中央値には混ぜない。**

    混ぜると、同規模でない地点との差が「同規模の中での差」として読まれます。
    """
    table = peers._table(
        key="station_scale", label="乗降客数が同規模の駅", reference={},
        peers=[_peer("A駅周辺", population=40000, clinics=20, per_clinic=2000),
               _peer("B駅周辺", population=60000, clinics=30, per_clinic=2000),
               _peer("巨大駅周辺", population=500000, clinics=300,
                     per_clinic=1667, inside=False)],
        site_metrics={"population": 50000, "facility_count": 30,
                      "population_per_facility": 1667},
        specs=_specs(), config=_config(), empty_reason="—")

    assert table["peer_count"] == 3
    assert table["comparable_count"] == 2
    population = next(r for r in table["ranks"] if r["metric"] == "population")
    assert population["median"] == 50000, "帯の外の 500,000 が中央値に入っています"
    assert population["of"] == 3, "順位の母数に参考の相手が入っています"


def test_a_rate_is_compared_in_points_not_in_percent():
    """**率どうしの差を率で語ると、意味が変わります。**

    高齢化率 18.5% と中央値 20.4% の差は「1.9 ポイント」であって「9% 低い」
    ではありません。後者は、読み手が人数の話だと受け取ります。
    """
    table = peers._table(
        key="k", label="l", reference={},
        peers=[_peer("A", population=50000, clinics=30, per_clinic=1667, aged=25.0),
               _peer("B", population=50000, clinics=30, per_clinic=1667, aged=27.0),
               _peer("C", population=50000, clinics=30, per_clinic=1667, aged=26.0)],
        site_metrics={"population": 50000, "age_65_plus": 9000,
                      "facility_count": 30},
        specs=_specs(), config=_config(), empty_reason="—")

    aged = next(r for r in table["ranks"] if r["metric"] == "age_65_plus_pct")
    assert aged["value"] == 18.0
    assert aged["gap_points"] == pytest.approx(-8.0, abs=0.05)  # 26.0 - 18.0
    standout = next(s for s in table["standouts"] if s["metric"] == "age_65_plus_pct")
    assert standout["gap_points"] is not None
    assert standout["direction"] == "low"
    assert standout["reading"] == "高齢層が薄い"


def test_a_small_difference_in_a_rate_is_not_called_a_standout():
    """0.4 ポイントの差を「際立っている」と書かせない。"""
    table = peers._table(
        key="k", label="l", reference={},
        peers=[_peer("A", population=50000, clinics=30, per_clinic=1667, aged=20.4),
               _peer("B", population=50000, clinics=30, per_clinic=1667, aged=20.4),
               _peer("C", population=50000, clinics=30, per_clinic=1667, aged=20.4)],
        site_metrics={"population": 50000, "age_65_plus": 10000,
                      "facility_count": 30},
        specs=_specs(), config=_config(), empty_reason="—")
    assert not [s for s in table["standouts"] if s["metric"] == "age_65_plus_pct"]


def test_standouts_are_withheld_when_there_are_too_few_peers():
    """**2 件の中の「際立って高い」は、分布ではありません。**"""
    table = peers._table(
        key="k", label="l", reference={},
        peers=[_peer("A", population=10000, clinics=30, per_clinic=333),
               _peer("B", population=10000, clinics=30, per_clinic=333)],
        site_metrics={"population": 90000, "facility_count": 30},
        specs=_specs(), config=_config(), empty_reason="—")
    assert table["ranks"], "順位は出してよい"
    assert table["standouts"] == [], "母数 2 件で『際立っている』とは言えません"


def test_the_axis_that_only_looks_similar_is_dropped_to_a_reference_row():
    """**「その軸だけ同規模」を、同規模と呼ばせない。**

    実測：東京駅前（商圏人口 13,010 人）の「人口が同規模の地点」に武蔵村山市が
    並びました。住んでいる人の数は同じですが、従業者数は 571,810 対 2,963 です。
    """
    rows = [_peer("武蔵村山市", population=12979, clinics=2, per_clinic=6490,
                  workers=2963)]
    notes = peers._guard(rows, _site(workers=571810), [
        {"metric": "workers", "max_ratio": 5, "why": "昼間人口が桁違いです"}],
        _specs())
    assert rows[0]["inside_band"] is False
    assert "従業者数" in rows[0]["guard_reason"]
    assert notes and notes[0]["ratio"] == pytest.approx(193.0, abs=1.0)


def test_the_supply_gap_list_is_not_a_recommendation():
    """挙げるのは**数え上げ**だけ。順位も理由も、そこから先は書かない。"""
    site = {"population": 50000, "facility_count": 30,
            "population_per_facility": 1000}
    table = peers._table(
        key="k", label="l", reference={},
        peers=[_peer("A", population=50000, clinics=10, per_clinic=5000),
               _peer("B", population=50000, clinics=30, per_clinic=1050),
               _peer("C", population=50000, clinics=25, per_clinic=2000)],
        site_metrics=site, specs=_specs(), config=_config(), empty_reason="—")

    listed = [row["label"] for row in table["supply_gap"]]
    assert listed == ["A", "C"], "5% しか違わない B を挙げています"
    assert all(set(row) >= {"value", "here", "gap_pct"}
               for row in table["supply_gap"])


def test_the_station_ridership_of_a_peer_comes_from_the_station_itself():
    """メッシュの列は「そのメッシュの最寄駅」の値で、**別の駅のことがあります。**

    実測：東京駅の比較相手に並べた新宿駅の行に 199,171 人/日 が入っていました
    （新宿の実際は 2,713,386）。同じ列名の、別の量です。
    """
    row = peers._peer_row(
        {"daily_passengers": 199171, "population": 27655, "distance_m": 6200},
        _specs(), name="新宿駅周辺", inside_band=True,
        overrides={"daily_passengers": 2713386})
    assert row["values"]["daily_passengers"] == 2713386


def test_nothing_to_compare_against_is_said_out_loud():
    """**比較できないことは、書きます。** 空欄は「比較したが差が無い」に見えます。"""
    table = peers._table(key="k", label="乗降客数が同規模の駅", reference={},
                         peers=[], site_metrics=_site()["values"],
                         specs=_specs(), config=_config(),
                         empty_reason="同規模の駅が見つかりませんでした")
    assert table["unavailable"]["what"] == "乗降客数が同規模の駅"
    assert "見つかりません" in table["unavailable"]["why"]


# ------------------------------------------------------------------ 文書の側
def _pack_with(view):
    return {"chapters": cfg.dd_config("dental_clinic")["chapters"],
            "peers": view, "trade_area": {}, "competition": {},
            "location_quality": {}, "demand": {}, "outlook": {}, "risks": [],
            "growth": {}, "further_dd": []}


def test_the_report_names_the_places_it_compared_against():
    """percentile と違って、**比較相手は名前で出ます。**"""
    view = {
        "basis": {"pool": {"prefectures": ["東京都"]}, "note": "…"},
        "site": _site(),
        "tables": [{
            "key": "station_scale", "label": "乗降客数が同規模の駅",
            "reference": {"description": "沼津駅の乗降客数 37,857人/日 の0.4〜2.5倍"},
            "columns": [{"key": "population", "label": "商圏人口", "unit": "人"}],
            "peers": [_peer("三島駅周辺", population=40000, clinics=20,
                            per_clinic=2000)],
            "peer_count": 1, "comparable_count": 1, "site": _site(),
            "ranks": [], "standouts": [], "supply_gap": [], "caution": [],
        }],
        "unavailable": [{"what": "商圏人口が同規模の地点", "why": "取れていません"}],
        "available": True,
    }
    markdown = "\n".join(dd_report._peers(_pack_with(view)))
    assert "三島駅周辺" in markdown
    assert "沼津駅の乗降客数 37,857人/日" in markdown
    assert "**この地点**" in markdown, "自分の行が無いと、比較表になりません"
    assert "取れていません" in markdown, "比較できなかった理由が消えています"


def test_a_reference_row_is_marked_as_one_in_the_document():
    """帯の外の相手は、**字面で分けます。**"""
    view = {
        "basis": {}, "site": _site(),
        "tables": [{
            "key": "k", "label": "乗降客数が同規模の駅", "reference": {},
            "columns": [{"key": "population", "label": "商圏人口", "unit": "人"}],
            "peers": [_peer("巨大駅周辺", population=500000, clinics=300,
                            per_clinic=1667, inside=False)],
            "peer_count": 1, "comparable_count": 0, "site": _site(),
            "ranks": [], "standouts": [], "supply_gap": [], "caution": [],
        }],
        "unavailable": [], "available": True,
    }
    markdown = "\n".join(dd_report._peers(_pack_with(view)))
    assert "巨大駅周辺（参考）" in markdown
    assert "同規模と呼べる相手がいない" in markdown


def test_the_supply_gap_section_says_it_is_not_a_recommendation():
    """**開業の成否は述べません。** 数え上げだと字面で断ること。"""
    view = {
        "basis": {}, "site": _site(),
        "tables": [{
            "key": "k", "label": "乗降客数が同規模の駅", "reference": {},
            "columns": [], "peers": [], "peer_count": 0, "comparable_count": 0,
            "site": _site(), "ranks": [], "standouts": [],
            "supply_gap": [{"label": "掛川駅周辺", "metric": "population_per_clinic",
                            "metric_label": "医院1件あたり人口", "unit": "人",
                            "value": 2400, "here": 1600, "gap_pct": 50.0,
                            "distance_km": 30.2}],
            "caution": [],
        }],
        "unavailable": [], "available": True,
    }
    markdown = "\n".join(dd_report._peer_standouts(_pack_with(view)))
    assert "掛川駅周辺" in markdown
    assert "推奨ではありません" in markdown
    assert "予測もしません" in markdown


def test_the_prompt_gets_the_position_not_the_whole_matrix():
    """**表は Python が描きます。** LLM に相手 5 件 × 軸 8 本の値は要りません。

    実測：束の 1 割（9,176 字 ≒ 4,200 トークン）が比較表の値でした。段 2 本に
    毎回載せると 1 本あたり 2〜3 セント増えます。落とすのは値の一覧だけで、
    中央値も順位も際立った軸も残ります。
    """
    view = {
        "basis": {"note": "…"}, "site": _site(),
        "tables": [{
            "key": "k", "label": "乗降客数が同規模の駅",
            "reference": {"description": "沼津駅の乗降客数 37,857人/日 の0.4〜2.5倍"},
            "columns": [{"key": "population", "label": "商圏人口", "unit": "人"}],
            "peers": [_peer("三島駅周辺", population=40000, clinics=20,
                            per_clinic=2000),
                      _peer("巨大駅周辺", population=999999, clinics=300,
                            per_clinic=3333, inside=False)],
            "peer_count": 2, "comparable_count": 1, "site": _site(),
            "ranks": [{"metric": "population", "label": "商圏人口", "value": 50000,
                       "median": 40000, "gap_vs_median_pct": 25.0}],
            "standouts": [], "supply_gap": [], "caution": [],
        }],
        "unavailable": [], "available": True,
    }
    trimmed = dd.peers_for_prompt({"peers": view})
    table = trimmed["tables"][0]
    assert table["peers"] == ["三島駅周辺", "巨大駅周辺（参考）"], \
        "比較相手の名前は残すこと（名前のある比較であることが要点です）"
    assert table["ranks"][0]["median"] == 40000
    assert "columns" not in table and "site" not in table
    assert "values" not in str(table), "値の一覧が落ちていません"


# ------------------------------------------------------------------ DB を使う
@pytest.fixture
def conn():
    psycopg = pytest.importorskip("psycopg")
    from kaigyou_core.db import connect

    try:
        with connect() as connection:
            with connection.cursor() as cur:
                cur.execute("SELECT count(*) AS n FROM mesh_scores")
                if cur.fetchone()["n"] == 0:
                    pytest.skip("mesh scores not computed here")
            yield connection
    except psycopg.OperationalError as exc:
        pytest.skip(f"database unavailable: {exc}")


def test_the_peers_of_a_station_front_are_other_station_fronts(conn):
    """**駅前は駅前と比べます。** 規模の近い順に、名前のある相手が並ぶこと。"""
    from kaigyou_core.analysis import analyze_point
    from kaigyou_core.dataset import resolve_mesh_size

    lat, lng = 35.6812, 139.7671              # 東京駅
    mesh = resolve_mesh_size(conn, None, "13")
    metrics = analyze_point(conn, lat, lng, 1000, "dental_clinic", mesh,
                            "circle", None)
    view = peers.comparison(conn, lat=lat, lng=lng, radius_m=1000,
                            profile="default", site_metrics=metrics,
                            config=cfg.peers_config("dental_clinic"))
    table = next((t for t in view["tables"] if t["key"] == "station_scale"), None)
    if table is None:
        pytest.skip("この環境には乗降客数つきの駅がありません")

    assert table["peers"], "同規模の駅が 1 件も出ていません"
    assert all(p["label"].endswith("駅周辺") for p in table["peers"])
    # **自分自身は相手になりません。** 商圏が重なる相手も同じ理由で外します。
    assert all((p["distance_km"] or 0) * 1000 >= 3000 for p in table["peers"])


def test_the_pool_says_which_prefectures_it_could_compare_against(conn):
    """**取り込んでいない県は比べられません。** それを黙らないこと。"""
    view = peers.comparison(conn, lat=35.6812, lng=139.7671, radius_m=1000,
                            profile="default",
                            site_metrics={"population": 13010,
                                          "facility_count": 157},
                            config=cfg.peers_config("dental_clinic"))
    assert view["basis"]["pool"]["prefectures"], "母集団の県名が出ていません"
    assert view["basis"]["note"], "この地点だけ測り方が違うことを書いていません"


def test_the_fact_pack_carries_the_comparison(conn):
    """事実の束に入っていなければ、**本文はその数字を引用できません。**"""
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app

    response = TestClient(app).get("/api/dataset", params={
        "lat": 35.6433, "lng": 139.6690, "radius": 1000,
        "profile": "default", "max_clinics": 0})
    assert response.status_code == 200
    pack = dd.fact_pack(response.json(), None, "dental_clinic")
    assert "peers" in pack
    if not pack["peers"]["available"]:
        pytest.skip("この環境では比較相手が見つかりません")

    numbers = dd.numbers_in(pack)
    value = pack["peers"]["tables"][0]["peers"][0]["values"]["population"]
    assert f"{value:.0f}" in numbers, "比較相手の数字が検算の集合に入っていません"
