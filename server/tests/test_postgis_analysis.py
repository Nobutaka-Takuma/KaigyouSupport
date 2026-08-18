"""PostGIS analysis, against controlled geometry.

Runs only when a database is reachable (set ``DATABASE_URL``); skipped
otherwise so the unit suite stays runnable anywhere. Each test inserts its own
fixture rows inside a transaction that is rolled back, so it neither depends on
nor disturbs whatever else is loaded.
"""
from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from kaigyou_core import mesh as meshlib
from kaigyou_core.analysis import analyze_point, facility_counts
from kaigyou_core.db import connect

SOURCE_ID = "__test_fixture__"
# A 1km mesh well away from the Tokyo sample data, so the fixture is isolated.
MESH_CODE = "50302020"


@pytest.fixture
def conn():
    try:
        with connect() as c:
            with c.cursor() as cur:
                cur.execute("SELECT to_regclass('public.population_mesh') AS t")
                if cur.fetchone()["t"] is None:
                    pytest.skip("schema not migrated")
            yield c
            c.rollback()
    except psycopg.OperationalError as exc:
        pytest.skip(f"database unavailable: {exc}")


@pytest.fixture
def fixture_data(conn):
    """One mesh of 10,000 people, and clinics at known distances from its centre."""
    lng, lat = meshlib.centroid(MESH_CODE)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO data_sources (id, name, publisher, dataset_kind)
            VALUES (%s, 'test fixture', 'test', 'sample')
            ON CONFLICT (id) DO NOTHING
            """,
            (SOURCE_ID,),
        )
        cur.execute(
            """
            INSERT INTO population_mesh (
                source_id, mesh_code, mesh_size_m, prefecture_code, geom, centroid,
                population, age_0_14, age_15_64, age_65_plus, households,
                population_growth, source_date
            ) VALUES (
                %s, %s, 1000, '99', ST_GeomFromText(%s, 4326),
                ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                10000, 1500, 6500, 2000, 5000, 0.02, current_date
            )
            """,
            (SOURCE_ID, MESH_CODE, meshlib.to_polygon_wkt(MESH_CODE), lng, lat),
        )
        # Clinics roughly 300m and 1,400m east of the mesh centre.
        for i, offset_m in enumerate((300, 1400)):
            cur.execute(
                """
                INSERT INTO facilities (
                    source_id, facility_id, facility_category, name,
                    prefecture_code, geom, source_date
                ) VALUES (
                    %s, %s, 'dental_clinic', %s, '99',
                    ST_Project(ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                               %s, radians(90))::geometry,
                    current_date
                )
                """,
                (SOURCE_ID, f"fx-{i}", f"fixture clinic {i}", lng, lat, offset_m),
            )
        cur.execute(
            """
            INSERT INTO stations (
                source_id, station_id, name, prefecture_code, geom,
                daily_passengers, source_date
            ) VALUES (
                %s, 'fx-st', 'fixture station', '99',
                ST_Project(ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                           500, radians(0))::geometry,
                30000, current_date
            )
            """,
            (SOURCE_ID, lng, lat),
        )
    return {"lat": lat, "lng": lng}


def analyze(conn, point, radius):
    return analyze_point(conn, point["lat"], point["lng"], radius, mesh_size_m=1000)


# --------------------------------------------------------------- population
def test_a_small_circle_takes_an_area_weighted_share_of_the_mesh(conn, fixture_data):
    """A circle inside one cell takes the circle's share of that cell's people.

    The expected share is computed against the cell's real area: a JIS "1km"
    mesh is 30 x 45 arcseconds, which is only about a square kilometre and
    varies with latitude, so assuming 1.0 km2 here would bake in a 7% error.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ST_Area(geom::geography) AS a FROM population_mesh "
            "WHERE source_id = %s AND mesh_code = %s",
            (SOURCE_ID, MESH_CODE),
        )
        cell_area_m2 = cur.fetchone()["a"]

    result = analyze(conn, fixture_data, 300)
    circle_area_m2 = 3.141592653589793 * 300**2
    expected = 10000 * circle_area_m2 / cell_area_m2
    assert result["population"] == pytest.approx(expected, rel=0.02)


def test_a_circle_enclosing_the_whole_mesh_takes_all_of_it(conn, fixture_data):
    result = analyze(conn, fixture_data, 3000)
    assert result["population"] == pytest.approx(10000, rel=0.01)
    assert result["households"] == pytest.approx(5000, rel=0.01)
    assert result["age_65_plus"] == pytest.approx(2000, rel=0.01)


def test_population_is_monotonic_in_radius(conn, fixture_data):
    values = [analyze(conn, fixture_data, r)["population"] for r in (300, 600, 1000, 2000)]
    assert values == sorted(values)


def test_a_point_with_no_mesh_coverage_reports_nothing_rather_than_zero(conn, fixture_data):
    result = analyze_point(conn, 0.0, 0.0, 1000, mesh_size_m=1000)
    assert result["mesh_count"] == 0
    assert result["population"] is None


def test_age_bands_sum_to_the_total(conn, fixture_data):
    r = analyze(conn, fixture_data, 3000)
    assert r["age_0_14"] + r["age_15_64"] + r["age_65_plus"] == pytest.approx(
        r["population"], rel=0.01
    )


# --------------------------------------------------------------- competition
def test_facility_count_respects_the_radius(conn, fixture_data):
    assert analyze(conn, fixture_data, 500)["facility_count"] == 1
    assert analyze(conn, fixture_data, 2000)["facility_count"] == 2


def test_population_per_facility_is_null_when_there_are_none(conn, fixture_data):
    result = analyze(conn, fixture_data, 200)
    assert result["facility_count"] == 0
    assert result["population_per_facility"] is None


def test_population_per_facility_is_the_ratio(conn, fixture_data):
    result = analyze(conn, fixture_data, 3000)
    assert result["population_per_facility"] == pytest.approx(
        result["population"] / result["facility_count"], rel=0.001
    )


def test_multi_radius_counts_agree_with_single_radius(conn, fixture_data):
    counts = facility_counts(conn, fixture_data["lat"], fixture_data["lng"], [500, 2000])
    assert counts[500] == analyze(conn, fixture_data, 500)["facility_count"]
    assert counts[2000] == analyze(conn, fixture_data, 2000)["facility_count"]


# ----------------------------------------------------------------- nearest
def test_nearest_facility_distance_is_in_metres(conn, fixture_data):
    result = analyze(conn, fixture_data, 1000)
    assert result["nearest_facility_distance_m"] == pytest.approx(300, abs=5)


def test_nearest_facility_is_found_outside_the_trade_area(conn, fixture_data):
    """The nearest clinic is reported even when none is inside the circle."""
    result = analyze(conn, fixture_data, 100)
    assert result["facility_count"] == 0
    assert result["nearest_facility_distance_m"] == pytest.approx(300, abs=5)


def test_nearest_station_carries_its_passenger_count(conn, fixture_data):
    result = analyze(conn, fixture_data, 1000)
    assert result["station_distance_m"] == pytest.approx(500, abs=5)
    assert result["daily_passengers"] == 30000
