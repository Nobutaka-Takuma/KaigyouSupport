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


def test_two_business_types_can_be_scored_on_the_same_mesh(conn):
    """**同じ鍵に別業態の点が入って、片方が消えないこと。**

    移行（030）の前は mesh_scores の主キーが (mesh_id, profile, radius_m) で、
    業態が入っていませんでした。内科でスコアを流すと歯科の行が
    ON CONFLICT で上書きされ、**compute-scores は成功と表示します。**
    ラベルの間違いではなく答えの間違いなので、ここで固定します。
    """
    from kaigyou_core.db import column_exists

    if not column_exists(conn, "mesh_scores", "facility_category"):
        pytest.skip("030 未適用")

    lng, lat = meshlib.centroid(MESH_CODE)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO data_sources (id, name, publisher, dataset_kind) "
                    "VALUES (%s, 'test fixture', 'test', 'sample') "
                    "ON CONFLICT (id) DO NOTHING", (SOURCE_ID,))
        cur.execute(
            """
            INSERT INTO population_mesh (source_id, mesh_code, mesh_size_m,
                prefecture_code, geom, centroid, population)
            VALUES (%s, %s, 1000, '99',
                    ST_SetSRID(ST_Buffer(ST_MakePoint(%s, %s)::geography, 500)::geometry, 4326),
                    ST_SetSRID(ST_MakePoint(%s, %s), 4326), 10000)
            RETURNING id
            """, (SOURCE_ID, MESH_CODE, lng, lat, lng, lat))
        mesh_id = cur.fetchone()["id"]

        for category, score in (("dental_clinic", 70.0), ("medical_clinic", 30.0)):
            cur.execute(
                """
                INSERT INTO mesh_scores (mesh_id, profile, radius_m,
                                         facility_category, overall_score)
                VALUES (%s, 'default', 1000, %s, %s)
                ON CONFLICT (mesh_id, profile, radius_m, facility_category)
                DO UPDATE SET overall_score = EXCLUDED.overall_score
                """, (mesh_id, category, score))

        cur.execute("SELECT facility_category, overall_score FROM mesh_scores "
                    "WHERE mesh_id = %s ORDER BY facility_category", (mesh_id,))
        rows = [(r["facility_category"], r["overall_score"]) for r in cur.fetchall()]

    assert rows == [("dental_clinic", 70.0), ("medical_clinic", 30.0)], \
        "業態ごとに 1 行ずつ残ること。片方が消えたら主キーが足りていません"


def test_loading_a_folder_scores_every_business_type_that_is_in_it(conn):
    """既定の業態だけを回すと、医科を入れたのにランキングが空になります。

    **しかも load-local は成功と表示します。** 気づけるのは画面を見たときです。
    施設が 1 件も無い県では、空を返して採点を丸ごと飛ばすのではなく、既定の
    業態を 1 つ返します（そうしないと原因の分からない「空のランキング」に
    なります）。
    """
    from kaigyou_core.analysis import DEFAULT_CATEGORY
    from kaigyou_etl.cli import _loaded_categories

    assert _loaded_categories("99") == [DEFAULT_CATEGORY], \
        "何も入っていない県では、既定を1つ返すこと"

    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT facility_category AS c FROM facilities "
                    "WHERE prefecture_code = '13' ORDER BY 1")
        loaded = [r["c"] for r in cur.fetchall() if r["c"]]
    if loaded:
        assert _loaded_categories("13") == loaded
