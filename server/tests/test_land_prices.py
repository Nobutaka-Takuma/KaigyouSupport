"""地価公示（L01）: published prices, reported as published.

The risk this dataset carries is not a parsing bug. It is that a number in
yen next to a candidate location gets read as "what this would cost" -- which
the requirements rule out and which the data does not support: 地価公示 is the
price of a surveyed parcel of *land*, per square metre, on 1 January, and says
nothing about a building, a floor or a lease. So the tests below cover the
figures being right, and the framing around them staying put.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from kaigyou_core import config as cfg
from kaigyou_core.db import connect, table_exists
from kaigyou_etl.adapters import AdapterContext, get_adapter

FIXTURE = Path(__file__).parent / "fixtures" / "mlit_l01_land_prices.zip"
REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ID = "__land_price_test__"


def _adapter(tmp_path: Path, prefecture: str = "13"):
    sources = cfg.sources_config()
    spec = dict(sources["sources"]["mlit_land_prices"])
    ctx = AdapterContext(source_id=SOURCE_ID, spec=spec,
                         defaults=sources.get("defaults", {}), raw_dir=tmp_path,
                         prefecture_override=prefecture)
    return get_adapter(spec["adapter"])(ctx)


# ------------------------------------------------------------------ parsing
def test_the_published_columns_are_read_as_published(tmp_path):
    facts = _adapter(tmp_path).validate(FIXTURE)
    assert facts["point_count"] == 12
    assert facts["priced_points"] == 12
    assert facts["survey_years"] == {2026: 12}
    # Every use division in the fixture is one this project has a label for.
    assert facts["use_codes_not_configured"] == []


def test_a_price_read_from_the_wrong_column_is_refused(tmp_path, monkeypatch):
    """円/m² is a five-to-eight digit number; a serial number is not.

    The columns are named L01_001..L01_148 and nothing in the file says which
    is which, so a release that renumbers them would otherwise load silently
    with the wrong figures.
    """
    from kaigyou_etl.acquisition import AcquisitionError

    adapter = _adapter(tmp_path)
    # L01_003 is the serial number within the municipality: small integers.
    monkeypatch.setitem(adapter.spec, "columns",
                        dict(adapter.spec["columns"], price=["L01_003"]))
    with pytest.raises(AcquisitionError) as caught:
        adapter.validate(FIXTURE)
    assert "price column" in str(caught.value)


def test_the_use_divisions_are_kept_apart(tmp_path):
    """住宅地 and 商業地 in one ward differ several times over.

    A single blended "land price here" would describe neither, so the division
    is loaded rather than collapsed.
    """
    rows = list(_adapter(tmp_path).transform(FIXTURE))
    labels = {r["use_category"] for r in rows}
    assert {"住宅地", "商業地", "工業地", "林地"} <= labels

    import statistics

    residential = [r["price_yen_per_sqm"] for r in rows if r["use_category"] == "住宅地"]
    commercial = [r["price_yen_per_sqm"] for r in rows if r["use_category"] == "商業地"]
    # Medians, not extremes: a quiet commercial corner can be cheaper than a
    # prime residential one, and the divisions still differ as populations.
    assert statistics.median(commercial) > statistics.median(residential), (
        "commercial land in central Tokyo outprices residential; if not, the "
        "use division column is being read wrong")


def test_the_files_own_placeholder_does_not_become_text(tmp_path):
    """L01 writes a bare underscore where a field does not apply."""
    rows = list(_adapter(tmp_path).transform(FIXTURE))
    for row in rows:
        for field in ("address", "current_use", "zoning", "nearest_station"):
            assert row[field] != "_", f"{field} kept the file's placeholder"


def test_the_point_code_survives_a_reload(tmp_path):
    """標準地番号 is stable between years, which is what lets 2027 land beside 2026."""
    first = {r["point_code"] for r in _adapter(tmp_path).transform(FIXTURE)}
    again = {r["point_code"] for r in _adapter(tmp_path).transform(FIXTURE)}
    assert first == again and len(first) == 12


# --------------------------------------------------------------- database
@pytest.fixture
def loaded(tmp_path):
    try:
        with connect() as probe:
            if not table_exists(probe, "land_prices"):
                pytest.skip("land_prices not migrated here")
    except psycopg.OperationalError:
        pytest.skip("no database")

    adapter = _adapter(tmp_path)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO data_sources (id, name, publisher, dataset_kind)
                   VALUES (%s, 'test land prices', 'test', 'sample')
                   ON CONFLICT (id) DO NOTHING""", (SOURCE_ID,))
        conn.commit()
        adapter.load(conn, adapter.transform(FIXTURE))
        conn.commit()
    yield
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM data_sources WHERE id = %s", (SOURCE_ID,))
        conn.commit()


def test_a_second_load_replaces_rather_than_duplicates(loaded, tmp_path):
    adapter = _adapter(tmp_path)
    with connect() as conn:
        adapter.load(conn, adapter.transform(FIXTURE))
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM land_prices WHERE source_id = %s",
                        (SOURCE_ID,))
            assert cur.fetchone()["n"] == 12


def test_the_summary_reports_a_median_per_use_division(loaded):
    from kaigyou_core.analysis import land_prices_near

    with connect() as conn:
        # Around the first fixture point, wide enough to catch several.
        summary = land_prices_near(conn, 35.69014, 139.744815, 20_000)

    assert summary is not None
    assert summary["by_use"], "the fixture points are within 20km of each other"
    for row in summary["by_use"]:
        assert row["points"] >= 1
        assert row["median_yen_per_sqm"] >= row["min_yen_per_sqm"]
        assert row["median_yen_per_sqm"] <= row["max_yen_per_sqm"]
    assert summary["nearest"], "the nearest points are named, not only aggregated"
    assert summary["nearest"][0]["distance_m"] < 100


def test_the_summary_says_it_is_not_rent(loaded):
    """The one sentence that must not be dropped."""
    from kaigyou_core.analysis import land_prices_near

    with connect() as conn:
        summary = land_prices_near(conn, 35.69014, 139.744815, 2_000)
    assert "賃料" in summary["note"] and "スコア" in summary["note"]


def test_no_land_price_data_is_absent_not_zero():
    """A point far from every surveyed parcel has no price, not a price of nil."""
    from kaigyou_core.analysis import land_prices_near

    try:
        with connect() as conn:
            summary = land_prices_near(conn, 0.0, 0.0, 1_000)
    except psycopg.OperationalError:
        pytest.skip("no database")
    if summary is None:
        return
    assert summary["by_use"] == [], "nowhere near land: no divisions to report"


# ------------------------------------------------------------- the cost axis
def test_the_existing_models_are_unchanged():
    """A cost axis is an opinion. It is offered beside the old answer.

    `default`, `pediatric` and `office` are what the reader has been looking
    at; silently reweighting them would move every number on the site with
    nothing on screen to say why.
    """
    profiles = cfg.scoring_config()["profiles"]
    for name in ("default", "pediatric", "office"):
        assert "cost" not in profiles[name]["overall_weights"], (
            f"{name} gained a cost weight; add a profile instead of changing one")
    assert "cost" in profiles["cost_aware"]["overall_weights"]


def test_cheaper_land_scores_higher():
    """The direction of the axis, which is the one thing easy to get backwards."""
    from kaigyou_core.scoring import Distribution, ScoringModel

    model = ScoringModel(cfg.scoring_config(), "cost_aware")
    dist = {"land_price_yen_per_sqm": Distribution(
        metric="land_price_yen_per_sqm", p05=60_000, p50=450_000, p95=3_000_000,
        sample_count=4000)}

    cheap = model.cost({"land_price_yen_per_sqm": 80_000, "land_price_points": 10}, dist)
    dear = model.cost({"land_price_yen_per_sqm": 2_500_000, "land_price_points": 10}, dist)
    assert cheap.value > dear.value


def test_the_scale_is_logarithmic():
    """地価公示 spans four orders of magnitude inside one prefecture.

    On a linear percentile scale every suburb lands on the same value and the
    component stops separating anything.
    """
    from kaigyou_core.scoring import Distribution, ScoringModel

    model = ScoringModel(cfg.scoring_config(), "cost_aware")
    dist = {"land_price_yen_per_sqm": Distribution(
        metric="land_price_yen_per_sqm", p05=60_000, p50=450_000, p95=3_000_000,
        sample_count=4000)}

    def at(price: float) -> float:
        return model.cost({"land_price_yen_per_sqm": price,
                           "land_price_points": 10}, dist).value

    # Two suburban prices a factor of three apart must be visibly different.
    assert abs(at(100_000) - at(300_000)) > 10
    # Linear would put both within a couple of points of the top of the scale.


def test_one_surveyed_parcel_is_not_a_market():
    """地価公示 samples a few thousand parcels per prefecture."""
    from kaigyou_core.scoring import Distribution, ScoringModel

    model = ScoringModel(cfg.scoring_config(), "cost_aware")
    dist = {"land_price_yen_per_sqm": Distribution(
        metric="land_price_yen_per_sqm", p05=60_000, p50=450_000, p95=3_000_000,
        sample_count=4000)}

    thin = model.cost({"land_price_yen_per_sqm": 500_000, "land_price_points": 1}, dist)
    assert thin.value is None and "1地点" in (thin.note or "")


def test_a_place_that_cannot_be_costed_is_not_ranked_as_free():
    """The artefact this design exists to avoid.

    Dropping an unavailable component and renormalising the rest is right when
    an input is merely absent. For cost it inverts the meaning: "we could not
    price this" becomes "this is free", and the places with the least data rank
    highest. Measured before the fix, four of the top five meshes under the
    cost model had no cost score at all.
    """
    from kaigyou_core.scoring import Distribution, ScoringModel

    model = ScoringModel(cfg.scoring_config(), "cost_aware")
    # Enough of the model's other inputs to produce a score at all, so that the
    # only thing under test is what cost does.
    dist = {m: Distribution(metric=m, p05=lo, p50=(lo + hi) / 2, p95=hi,
                            sample_count=4000)
            for m, lo, hi in (
                ("land_price_yen_per_sqm", 60_000, 3_000_000),
                ("population", 500, 40_000),
                ("age_0_14", 50, 5_000),
                ("age_65_plus", 100, 9_000),
                ("households", 200, 20_000),
                ("workers", 100, 60_000),
                ("population_per_facility", 500, 20_000),
            )}
    metrics = {
        "population": 20_000, "age_0_14": 2_000, "age_65_plus": 4_000,
        "households": 10_000, "population_growth": 0.05,
        "population_per_facility": 5_000, "facility_count": 4,
        "station_distance_m": 300, "daily_passengers": 50_000,
        # No land price at all.
        "land_price_yen_per_sqm": None, "land_price_points": 0,
    }
    scored = model.score(metrics, dist)
    assert scored["overall"] is None, (
        "a place with no cost data must not out-rank one that was priced")
    assert scored["missing_required_components"] == ["cost"]

    # The same place, priced, does get a score.
    priced = model.score(dict(metrics, land_price_yen_per_sqm=400_000,
                              land_price_points=8), dist)
    assert priced["overall"] is not None


def test_the_cost_axis_is_still_not_a_rent():
    """It moves the score now; it still is not what a practice would pay."""
    profile = cfg.scoring_config()["profiles"]["cost_aware"]
    assert profile["cost"]["metric"] == "land_price_yen_per_sqm"
    assert "賃料" in profile["description"] or "地価" in profile["description"]

    source = (REPO_ROOT / "web" / "src" / "components" / "ScorePanel.tsx").read_text(
        encoding="utf-8")
    assert "地価は賃料そのものではありません" in source, (
        "the panel that shows the cost comparison has to say what the axis is")


def test_the_panel_keeps_the_caveat_next_to_the_number():
    source = (REPO_ROOT / "web" / "src" / "components" / "ScorePanel.tsx").read_text(
        encoding="utf-8")
    assert "LandPriceTable" in source
    assert "円/m²" in source, "the unit is what stops it being read as a rent"
    assert "land.note" in source, "the API's caveat has to be rendered, not just sent"
