"""The one-point dataset: what it must contain, and what it must not imply.

This endpoint exists to be read by something that has never seen the project
-- another program, a person with the JSON open, a model. That reader cannot
ask a follow-up question, so everything it needs to avoid a confident wrong
reading has to be in the document: the unit of every figure, the difference
between a zero and an unknown, and the caveats that separate 従業者数 from
昼間人口 and 地価 from 賃料.
"""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from kaigyou_api.main import app
from kaigyou_core.db import connect, table_exists

#: 銀座4丁目. Dense enough that every section has something in it.
GINZA = {"lat": 35.6717, "lng": 139.7650, "radius": 1000}


@pytest.fixture(scope="module")
def client():
    try:
        with connect() as conn:
            if not table_exists(conn, "population_mesh"):
                pytest.skip("schema not migrated here")
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) AS n FROM population_mesh")
                if not cur.fetchone()["n"]:
                    pytest.skip("no data loaded here")
    except psycopg.OperationalError:
        pytest.skip("no database")
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def doc(client):
    response = client.get("/api/dataset", params=GINZA)
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------------- shape
def test_the_document_is_versioned_and_dated(doc):
    """A reader that stores one of these needs to know what it is holding."""
    assert doc["schema_version"]
    assert doc["generated_at"].endswith("+00:00"), "timestamps are UTC and say so"
    assert doc["query"]["radius_m"] == GINZA["radius"]


def test_every_section_is_present(doc):
    for section in ("location", "catchment", "demand", "competition", "access",
                    "cost", "scores", "data_quality", "definitions", "provenance"):
        assert section in doc, f"missing section: {section}"


def test_the_requested_radius_is_among_the_radii_reported(doc):
    assert str(GINZA["radius"]) in doc["demand"]["residents"]["by_radius"]


# ------------------------------------------------------- self-describing
def test_every_reported_figure_has_a_unit(doc):
    """The guard against adding a number and leaving the reader to guess.

    A figure whose unit has to be inferred will be inferred wrongly sooner or
    later -- 円/m² read as a rent, 従業者数 read as daytime population.
    """
    described = set(doc["definitions"])
    reported = set()
    for section in (doc["demand"]["residents"]["by_radius"],
                    doc["demand"]["daytime"]["by_radius"],
                    doc["competition"]["by_radius"]):
        for values in section.values():
            reported |= set(values)
    # mesh_count is bookkeeping rather than a figure about the place.
    reported -= {"mesh_count"}
    assert reported <= described, f"no definition for: {sorted(reported - described)}"


def test_the_definitions_say_what_the_dangerous_ones_are_not(doc):
    """Two figures are routinely read as something they are not."""
    assert "昼間人口ではない" in doc["definitions"]["workers"]["description"]
    assert "賃料ではない" in doc["definitions"]["land_price_yen_per_sqm"]["description"]


def test_the_caveats_travel_with_the_data(doc):
    joined = " ".join(doc["data_quality"]["caveats"])
    for phrase in ("予測するものではありません", "都道府県をまたい", "賃料", "昼間人口"):
        assert phrase in joined, f"caveat missing: {phrase}"
    assert doc["disclaimer"]


# ---------------------------------------------------- absent is not zero
def test_a_place_with_no_mesh_data_reports_null_not_zero(client):
    """Somewhere with no census mesh loaded has unknown population, not none.

    A zero here would read as "nobody lives here", which is a claim the data
    does not make.
    """
    response = client.get("/api/dataset",
                          params={"lat": 34.9756, "lng": 138.3827, "radius": 1000})
    assert response.status_code == 200
    body = response.json()
    residents = body["demand"]["residents"]["by_radius"]["1000"]
    if residents["population"] is not None:
        pytest.skip("this point has mesh data loaded here")
    assert residents["population"] is None
    assert residents["households"] is None
    assert any("人口メッシュ" in n for n in body["data_quality"]["notes"])


def test_an_empty_clinic_type_is_reported_as_unavailable_not_as_none_offered(doc):
    """The published file has no 診療科目 column, so every list is empty.

    Left unexplained, a reader concludes that no clinic in Tokyo offers
    paediatric dentistry.
    """
    assert any("clinic_types" in n for n in doc["data_quality"]["notes"])


# ------------------------------------------------------------- the counts
def test_the_count_is_the_whole_count_even_when_the_list_is_cut(doc):
    clinics = doc["competition"]["clinics_in_radius"]
    assert clinics["count"] >= clinics["listed"]
    if clinics["truncated"]:
        assert clinics["count"] > clinics["listed"]
    # And the count agrees with the aggregate the score is built on.
    assert clinics["count"] == doc["competition"]["by_radius"]["1000"]["dental_clinics"]


def test_the_list_can_be_dropped_without_losing_the_count(client):
    body = client.get("/api/dataset", params={**GINZA, "max_clinics": 0}).json()
    clinics = body["competition"]["clinics_in_radius"]
    assert clinics["listed"] == 0 and clinics["items"] == []
    assert clinics["count"] > 0, "the count survives; only the enumeration goes"


# --------------------------------------------------------------- content
def test_the_daytime_side_carries_its_industry_mix(doc):
    """Stored per mesh since the economic census landed, surfaced nowhere else.

    "300,000 people work here" and "300,000 work here, mostly in offices" are
    different places to open a practice.
    """
    mix = doc["demand"]["daytime"]["industry_mix"]
    if mix is None:
        pytest.skip("economic census not loaded here")
    assert "tertiary" in mix
    assert mix["tertiary"]["workers"] > 0
    assert "産業分類別" in doc["definitions"]["industry_workers"]["description"]


def test_stations_come_with_their_operators_and_passenger_counts(doc):
    stations = doc["access"]["stations_in_radius"]["items"]
    if not stations:
        pytest.skip("no stations loaded here")
    assert stations[0]["name"]
    assert "distance_m" in stations[0] and "daily_passengers" in stations[0]
    assert doc["access"]["nearest_station"]["name"] == stations[0]["name"]


def test_every_configured_model_is_scored(doc, client):
    from kaigyou_core import config as cfg

    names = {s["profile"] for s in doc["scores"]["by_profile"]}
    assert names == set(cfg.scoring_config()["profiles"])
    for entry in doc["scores"]["by_profile"]:
        assert entry["is_provisional"] is True
        assert "weights" in entry, "the reader can see what produced the number"


def test_the_scores_name_the_scope_they_are_relative_to(doc):
    """A score without its normalisation scope invites a cross-prefecture read."""
    assert "pref" in doc["scores"]["normalization_scope"]
    assert "都道府県" in doc["scores"]["note"]


def test_the_catchment_says_which_shape_produced_the_numbers(doc):
    assert doc["catchment"]["kind"] in ("circle", "walk")
    assert doc["catchment"]["area_km2"] > 0


def test_the_geometry_is_opt_in(client):
    without = client.get("/api/dataset", params=GINZA).json()
    assert "geometry" not in without["catchment"]
    with_geom = client.get("/api/dataset", params={**GINZA, "geometry": "true"}).json()
    assert with_geom["catchment"]["geometry"]["type"] in ("Polygon", "MultiPolygon")


def test_provenance_names_the_sources_and_their_dates(doc):
    sources = doc["provenance"]["sources"]
    assert sources, "a document with no provenance cannot be checked by its reader"
    for entry in sources:
        assert entry["publisher"]
        assert "source_date" in entry


def test_the_dataset_is_a_document_not_an_answer(doc):
    """No prose, no recommendation, no prediction. Numbers and their meaning.

    What reads this may write prose; that is its business, and it will have
    the caveats to do it honestly. This endpoint must not do it for them.
    """
    for banned in ("recommendation", "推奨", "おすすめ", "成功確率", "売上予測"):
        assert banned not in str(doc), f"the dataset offered a judgement: {banned}"


# ------------------------------------------- what a reader cannot work out
def test_the_point_is_placed_in_its_own_distribution(doc):
    """The most useful thing here, and the least guessable.

    That Ginza's land is expensive is common knowledge. That its 1km resident
    population sits at the 30th percentile of Tokyo while its clinic count and
    its land price both sit at the 100th is the shape of the place, and no
    reader can derive it without the distribution in front of them.
    """
    rel = doc["relative_position"]
    if rel is None:
        pytest.skip("no scored meshes to compare against here")
    pref = rel["prefecture"]
    assert pref["compared_against_meshes"] > 100
    for metric, value in pref.items():
        if metric == "compared_against_meshes":
            continue
        assert 0 <= value <= 100, f"{metric} is not a percentile: {value}"
    assert "%" in rel["definition"], "the reader has to know which way it runs"


def test_the_comparison_names_the_radius_it_was_made_at(doc):
    """Meshes are scored at one radius; comparing a 2km catchment to them
    would be a different quantity under the same name."""
    rel = doc["relative_position"]
    if rel is None:
        pytest.skip("no scored meshes here")
    assert rel["radius_m"] > 0 and rel["profile"]


def test_both_scopes_are_offered_where_boundaries_are_loaded(doc):
    """Tokyo-wide and inside the ward answer different questions.

    A mesh can be unremarkable for Tokyo and the densest in its own ward.
    """
    rel = doc["relative_position"]
    if rel is None or "municipality" not in rel:
        pytest.skip("no municipality scope available here")
    assert rel["municipality"]["compared_against_meshes"] >= 1
    assert rel["municipality"]["compared_against_meshes"] <= \
        rel["prefecture"]["compared_against_meshes"]


def test_the_competitor_ladder_is_monotonic(doc):
    """The 20th nearest cannot be closer than the 1st."""
    ladder = doc["competition"]["proximity"]["nth_nearest_distance_m"]
    if len(ladder) < 2:
        pytest.skip("too few clinics loaded here")
    ranks = sorted(int(k) for k in ladder)
    distances = [ladder[str(r)] for r in ranks]
    assert distances == sorted(distances)
    assert doc["competition"]["proximity"]["per_km2"] > 0


def test_the_catchment_says_how_its_people_are_spread(doc):
    """20,000 residents can be one tower block or forty streets of houses."""
    shape = doc["demand"]["distribution"]
    if shape is None:
        pytest.skip("no mesh data here")
    assert shape["meshes"] > 0
    assert 0 <= shape["largest_mesh_share"] <= 1
    assert shape["population_largest_mesh"] >= shape["population_median_per_mesh"]
    assert shape["meshes_with_no_residents"] <= shape["meshes"]


def test_the_regulation_section_says_what_may_be_built(doc):
    """用途地域 decides whether the other figures can be acted on at all.

    A practice is a tenant, and 96% floor area ratio and 583% are different
    quantities of tenantable floor above the same population.
    """
    zoning = doc["regulation"]
    if zoning is None:
        pytest.skip("no land price data loaded here")
    nearest = zoning["at_nearest_surveyed_point"]
    assert nearest["zoning"]
    assert nearest["floor_area_ratio_pct"] > 0
    assert "用途地域図そのものではない" in zoning["definition"], (
        "the reader must not take surveyed points for a zoning map")


def test_numbers_are_numbers(doc):
    """Postgres numeric arrives as Decimal and serialises as a quoted string.

    A reader comparing "769" with 800 gets a lexicographic answer, silently.
    """
    def walk(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")
        elif isinstance(node, str):
            numeric_field = any(path.endswith(suffix) for suffix in (
                "_pct", "_m", "_km2", "_count", "points", "workers",
                "establishments", "population", "households"))
            assert not numeric_field, f"{path} is a string: {node!r}"

    walk(doc)


def test_the_new_sections_are_defined_too(doc):
    """The unit guard is only worth having if it covers what was added last."""
    described = set(doc["definitions"])
    for field in ("percentile", "nth_nearest_distance_m", "largest_mesh_share",
                  "floor_area_ratio_pct", "building_coverage_pct", "zoning"):
        assert field in described, f"no definition for {field}"
