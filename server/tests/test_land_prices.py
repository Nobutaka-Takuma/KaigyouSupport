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


# ------------------------------------------------- it must not become a score
def test_land_price_feeds_no_score():
    """Cost is not modelled, and the requirements forbid predicting it.

    A weight on land price would turn a published fact into an opinion about
    what a practice can afford -- and cheaper land is not simply better, since
    it correlates with the demand the score already measures.
    """
    scoring = cfg.scoring_config()
    text = str(scoring)
    for word in ("land_price", "land_prices", "地価"):
        assert word not in text, (
            f"{word!r} appears in scoring.yaml; land price is reference "
            "information, not a score component")

    from kaigyou_core.scoring import DISTRIBUTION_METRICS
    assert not [m for m in DISTRIBUTION_METRICS if "land" in m or "price" in m]


def test_the_panel_keeps_the_caveat_next_to_the_number():
    source = (REPO_ROOT / "web" / "src" / "components" / "ScorePanel.tsx").read_text(
        encoding="utf-8")
    assert "LandPriceTable" in source
    assert "円/m²" in source, "the unit is what stops it being read as a rent"
    assert "land.note" in source, "the API's caveat has to be rendered, not just sent"
