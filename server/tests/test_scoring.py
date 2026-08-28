"""The scoring model.

Two properties matter most here and are asserted directly:

* the weights come from configuration, so changing the config changes the
  result without touching code, and
* missing inputs are dropped rather than treated as zero, because a catchment
  with no station data is not a catchment with a station 0 metres away.
"""
import pytest

from kaigyou_core.scoring import Distribution, ScoringModel

CONFIG = {
    "active_profile": "default",
    "normalization": {"method": "minmax_p05_p95", "clamp": [0, 100]},
    "trade_area_radii_m": [500, 1000, 2000],
    "mesh_scoring_radius_m": 1000,
    "profiles": {
        "default": {
            "label": "test",
            "overall_weights": {"demand": 0.35, "competition": 0.30,
                                "growth": 0.20, "accessibility": 0.15},
            "demand_weights": {"population": 0.35, "age_0_14": 0.15,
                               "age_65_plus": 0.15, "households": 0.20,
                               "population_growth": 0.15},
            "competition": {"metric": "population_per_facility",
                            "zero_facility_score": 95, "zero_population_score": 0,
                            "cap": 20000},
            "growth": {"metric": "population_growth", "low": -0.05, "high": 0.08},
            "accessibility": {
                "weights": {"station_distance": 0.6, "daily_passengers": 0.4},
                "distance_best_m": 200, "distance_worst_m": 2000,
                "passengers_scale": "log10",
                "passengers_min": 1000, "passengers_max": 500000,
            },
        },
        "population_only": {
            "label": "population only",
            "overall_weights": {"demand": 1.0},
            "demand_weights": {"population": 1.0},
            "competition": {"metric": "population_per_facility"},
            "growth": {"metric": "population_growth", "low": -0.05, "high": 0.08},
            "accessibility": {"weights": {"station_distance": 1.0},
                              "distance_best_m": 200, "distance_worst_m": 2000},
        },
    },
}


def dist(metric, p05, p95, n=1000):
    return Distribution(metric=metric, p05=p05, p95=p95, min_value=p05,
                        max_value=p95, mean_value=(p05 + p95) / 2,
                        stddev_value=(p95 - p05) / 4, sample_count=n)


DISTRIBUTIONS = {
    "population": dist("population", 2000, 40000),
    "age_0_14": dist("age_0_14", 200, 5000),
    "age_65_plus": dist("age_65_plus", 400, 9000),
    "households": dist("households", 1000, 20000),
    "population_per_facility": dist("population_per_facility", 1000, 12000),
}

FULL = {
    "population": 25000, "age_0_14": 3000, "age_15_64": 16000,
    "age_65_plus": 6000, "households": 12000, "population_growth": 0.03,
    "facility_count": 5, "population_per_facility": 5000,
    "station_distance_m": 420, "daily_passengers": 24500,
}


def model(profile=None):
    return ScoringModel(CONFIG, profile)


# ------------------------------------------------------------------ basics
def test_all_components_scored_when_data_is_complete():
    result = model().score(FULL, DISTRIBUTIONS)
    for key in ("demand", "competition", "growth", "accessibility", "overall"):
        assert result[key] is not None, key
        assert 0 <= result[key] <= 100
    assert result["unavailable_components"] == []
    assert result["is_provisional"] is True


def test_overall_is_the_configured_weighted_mean():
    result = model().score(FULL, DISTRIBUTIONS)
    w = CONFIG["profiles"]["default"]["overall_weights"]
    expected = sum(result[k] * v for k, v in w.items())
    assert result["overall"] == pytest.approx(expected, abs=0.1)


def test_switching_profile_changes_the_result_without_code_changes():
    a = model("default").score(FULL, DISTRIBUTIONS)
    b = model("population_only").score(FULL, DISTRIBUTIONS)
    assert a["overall"] != b["overall"]
    # population_only weights the overall entirely on demand
    assert b["overall"] == pytest.approx(b["demand"], abs=0.1)


def test_unknown_profile_is_rejected():
    with pytest.raises(KeyError):
        ScoringModel(CONFIG, "does-not-exist")


# --------------------------------------------------------------- competition
def test_zero_facilities_uses_the_configured_cap_not_a_division():
    metrics = {**FULL, "facility_count": 0, "population_per_facility": None}
    result = model().score(metrics, DISTRIBUTIONS)
    assert result["competition"] == 95
    assert result["breakdown"]["competition"]["note"]


def test_zero_facilities_and_zero_population_scores_at_the_floor():
    metrics = {**FULL, "facility_count": 0, "population": 0,
               "population_per_facility": None}
    assert model().score(metrics, DISTRIBUTIONS)["competition"] == 0


def test_population_per_facility_is_capped():
    high = {**FULL, "facility_count": 1, "population_per_facility": 90000}
    at_cap = {**FULL, "facility_count": 1, "population_per_facility": 20000}
    assert (model().score(high, DISTRIBUTIONS)["competition"]
            == model().score(at_cap, DISTRIBUTIONS)["competition"])


# ------------------------------------------------------------ missing inputs
def test_missing_station_data_drops_accessibility_rather_than_zeroing_it():
    metrics = {**FULL, "station_distance_m": None, "daily_passengers": None}
    result = model().score(metrics, DISTRIBUTIONS)
    assert result["accessibility"] is None
    assert "accessibility" in result["unavailable_components"]
    # ...and the overall is the mean of what remains, not dragged down by a zero
    assert result["overall"] > 0


def test_partial_accessibility_renormalises_the_remaining_weight():
    metrics = {**FULL, "daily_passengers": None}
    result = model().score(metrics, DISTRIBUTIONS)
    assert result["accessibility"] == pytest.approx(
        result["breakdown"]["accessibility"]["parts"]["station_distance"], abs=0.1
    )


def test_missing_growth_is_reported_not_assumed_zero():
    metrics = {**FULL, "population_growth": None}
    result = model().score(metrics, DISTRIBUTIONS)
    assert result["growth"] is None
    assert "growth" in result["unavailable_components"]
    assert "population_growth" in result["breakdown"]["demand"]["missing"]


def test_no_distributions_means_no_relative_score():
    result = model().score(FULL, {})
    assert result["demand"] is None
    assert result["competition"] is None


def test_every_input_missing_yields_no_overall():
    result = model().score({"facility_count": None}, {})
    assert result["overall"] is None
    assert set(result["unavailable_components"]) == {
        "demand", "competition", "growth", "accessibility"
    }


# ------------------------------------------------------------- normalisation
def test_normalisation_clamps_to_the_configured_range():
    m = model()
    assert m.normalize("population", -5000, DISTRIBUTIONS) == 0
    assert m.normalize("population", 10_000_000, DISTRIBUTIONS) == 100


def test_nearer_station_scores_higher():
    near = model().score({**FULL, "station_distance_m": 200}, DISTRIBUTIONS)
    far = model().score({**FULL, "station_distance_m": 1900}, DISTRIBUTIONS)
    assert near["accessibility"] > far["accessibility"]


def test_a_degenerate_distribution_is_not_used():
    flat = {"population": Distribution(metric="population", p05=5.0, p95=5.0,
                                       sample_count=100)}
    assert model().normalize("population", 5.0, flat) is None


# ------------------------------------------------- daytime (worker) demand
def _model(demand_weights):
    from kaigyou_core.scoring import ScoringModel

    return ScoringModel({
        "active_profile": "p",
        "normalization": {"method": "minmax_p05_p95", "min_weight_coverage": 0.5},
        "profiles": {"p": {"label": "t", "overall_weights": {"demand": 1.0},
                           "demand_weights": demand_weights}},
    }, "p")


def _dists(*metrics, p05=100, p95=50000):
    """Reuses `dist` above: a Distribution without a sample count is unusable,
    which is correct behaviour and makes for a confusing test failure."""
    return {m: dist(m, p05, p95) for m in metrics}


def test_workers_raise_demand_where_residents_are_few():
    """The point of loading the economic census.

    Two mirrored trade areas, one an office district. Before workers were an
    input the second scored as though nobody was there at all.
    """
    model = _model({"population": 0.5, "workers": 0.5})
    dists = _dists("population", "workers")

    residential = model.demand({"population": 25000, "workers": 500}, dists)
    office = model.demand({"population": 500, "workers": 25000}, dists)
    assert office.value is not None and office.value > 20
    assert office.value == pytest.approx(residential.value)


def test_demand_survives_the_economic_census_being_absent():
    """Workers null means "unknown", never "nobody works here"."""
    model = _model({"population": 0.7, "workers": 0.3})
    dists = _dists("population", "workers")

    without = model.demand({"population": 25000}, dists)
    assert without.value is not None          # 0.7 of the weight is still present
    assert "workers" in without.missing
    assert without.value == model.demand({"population": 25000, "workers": None},
                                         dists).value


def test_a_profile_can_add_an_input_without_touching_code():
    """The demand loop follows the configured weights, not a list in the code."""
    model = _model({"establishments": 1.0})
    assert model.demand({"establishments": 1000},
                        _dists("establishments", p05=10, p95=1000)).value == 100


# ---------------------------------------------------- 目盛りを作る集合
#
# 「静岡の市街地はどこにピンを置いても Demand が90以上になる」という報告への
# 回帰テスト。合成した農村県で測ると、人口が 30,737〜70,792 人（2.3倍）ある
# 市街地 24 件が、全部ちょうど 100 点になっていました。県全域を目盛りにすると
# p95 が市街地の下限より低いところに来るためで、点数は出ているのに地点を
# 選ぶ手掛かりにはなりません。

def _scale(values, low_q=0.05, high_q=0.95):
    ordered = sorted(values)

    def pick(q):
        return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]

    return pick(low_q), pick(high_q)


def _place(value, low, high):
    return max(0.0, min(100.0, 100.0 * (value - low) / (high - low)))


def test_a_scale_made_from_farmland_cannot_separate_town_centres():
    """病理そのもの。県全域を目盛りにすると、市街地が全部上限に張り付く。"""
    # 実験に使った合成県と同じ比率（農村 438 : 市街地 24 = 462 メッシュ）。
    # 市街地が全体の 5% しかないので、p95 が市街地の下限のところに来ます。
    rural = [200 + i * 4 for i in range(438)]          # 農村・山林
    towns = [30000 + i * 1700 for i in range(24)]      # 人口が 2.3 倍違う市街地
    low, high = _scale(rural + towns)

    assert high <= min(towns), "p95 が市街地の下限より下に来ていない（前提が崩れている）"
    scored = [_place(v, low, high) for v in towns]
    assert min(scored) == 100.0 and max(scored) == 100.0, (
        "この集合では市街地が全部上限に張り付くはず（それが直したい症状）")


def test_a_scale_made_from_candidate_sites_separates_them():
    """目盛りを候補地から作れば、同じ市街地に差が付く。"""
    towns = [30000 + i * 1700 for i in range(24)]
    low, high = _scale(towns)

    scored = [_place(v, low, high) for v in towns]
    assert max(scored) - min(scored) > 30, "候補地どうしで差が付いていない"


def test_the_reference_set_is_the_catchments_that_have_a_clinic():
    """候補地の選び方に閾値を置かない。誰も山の中では開業していない。"""
    from kaigyou_etl.scores import _reference_rows

    rows = [{"facility_count": 0, "population": 300} for _ in range(400)]
    rows += [{"facility_count": 2, "population": 30000} for _ in range(60)]

    scale_rows, reference, fallback = _reference_rows(rows, "with_clinics", minimum=50)
    assert reference == "with_clinics" and fallback is False
    assert len(scale_rows) == 60
    assert all(r["facility_count"] > 0 for r in scale_rows)


def test_too_few_candidates_falls_back_and_says_so():
    """候補地が少なすぎる県では目盛りを作れない。黙って作らない。"""
    from kaigyou_etl.scores import _reference_rows

    rows = [{"facility_count": 0, "population": 300} for _ in range(400)]
    rows += [{"facility_count": 1, "population": 9000} for _ in range(5)]

    scale_rows, reference, fallback = _reference_rows(rows, "with_clinics", minimum=50)
    assert (reference, fallback) == ("all", True)
    assert len(scale_rows) == 405


def test_the_scale_used_is_part_of_the_scope_key():
    """目盛りの作り方が違えば、同じ90点でも別のことを指す。だから鍵に入れる。

    入れないと、県全域で作った目盛りを候補地基準の得点に黙って使えてしまい、
    しかも数字はもっともらしいままです。
    """
    from kaigyou_core.scoring import scope_key

    assert scope_key(500, 1000, "13", "with_clinics") != scope_key(500, 1000, "13", "all")
    assert scope_key(500, 1000, "13", "with_clinics").endswith(":with_clinics")


def test_the_business_type_is_part_of_the_scope_key():
    """**目盛りの定義そのものが業態に依存しています。**

    `with_clinics` は「歯科医院が実在する商圏」という意味です。内科では別の
    集合になるので、同じ文字列に二つの意味を持たせることはできません。
    入れないと、歯科の目盛りで内科を採点しても、それらしい点が出ます。
    """
    from kaigyou_core.scoring import scope_key

    dental = scope_key(500, 1000, "13", "with_clinics", "dental_clinic")
    medical = scope_key(500, 1000, "13", "with_clinics", "medical_clinic")
    assert dental != medical
    assert ":catdental_clinic:" in dental


def test_the_migrated_scope_matches_what_the_code_now_builds():
    """**歯科版を止めないための約束です。**

    移行（030）は既存の scope 文字列をその場で書き換えます。書き換えた結果が
    今のコードが作る鍵と 1 文字でも違えば、マイグレーション直後に目盛りが
    見つからなくなり、compute-scores と refresh-stats をやり直すまで（東京・
    静岡で数十分）スコアもランキングも出ません。

    移行の SQL は
        '^(mesh:[0-9]+:r[0-9]+:pref[0-9]+):([a-z_]+)$' -> '\1:catdental_clinic:\2'
    なので、この2つが一致することを、SQL を読まずに確かめられるようにします。
    """
    import re

    from kaigyou_core.scoring import scope_key

    def migrate(old: str) -> str:
        return re.sub(r"^(mesh:[0-9]+:r[0-9]+:pref[0-9]+):([a-z_]+)$",
                      r"\1:catdental_clinic:\2", old)

    for old, size, radius, pref, reference in (
        ("mesh:500:r1000:pref13:with_clinics", 500, 1000, "13", "with_clinics"),
        ("mesh:1000:r1000:pref22:all", 1000, 1000, "22", "all"),
    ):
        assert migrate(old) == scope_key(size, radius, pref, reference,
                                         "dental_clinic")
    # 二度当てても二重に入らないこと（移行後は区切りが6つになり当たらない）。
    once = migrate("mesh:500:r1000:pref13:with_clinics")
    assert migrate(once) == once


def test_a_dropped_prefecture_takes_its_scales_with_it():
    """鍵は pref13 で終わりません。後ろに業態と目盛りの種類が続きます。

    `LIKE '%pref13'` にしていたので、drop-prefecture はこれまで目盛りを
    1 件も消していませんでした。残った目盛りは、入れ直した別の県のデータに
    そのまま使われます。
    """
    from kaigyou_core.scoring import scope_key

    key = scope_key(500, 1000, "13", "with_clinics", "dental_clinic")
    assert not key.endswith("pref13"), "この形を前提にした削除は 1 件も消しません"
    # cli.py が使う条件。
    assert ":pref13:" in key
    assert ":pref22:" not in key


def test_an_input_at_the_end_of_the_scale_is_reported_as_such():
    """上限に達した入力は、そこから先の違いを捨てている。同点は「測れていない」。"""
    model = ScoringModel({
        "profiles": {"p": {"demand_weights": {"population": 1.0}}},
        "normalization": {"method": "minmax_p05_p95", "clamp": [0, 100],
                          "min_weight_coverage": 0.5},
    }, "p")
    distributions = {"population": Distribution(
        metric="population", p05=1000.0, p95=20000.0, sample_count=500)}

    ceiling = model.demand({"population": 90000}, distributions)
    assert ceiling.value == 100.0
    assert ceiling.saturated == ["population"]
    assert "見分けられません" in ceiling.as_dict()["saturated_note"]

    middle = model.demand({"population": 10000}, distributions)
    assert middle.saturated == []
    assert "saturated" not in middle.as_dict()
