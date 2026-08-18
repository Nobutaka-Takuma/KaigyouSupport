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
