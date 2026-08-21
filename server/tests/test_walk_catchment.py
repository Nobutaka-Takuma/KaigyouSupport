"""Trade areas measured along the street network.

The point of the whole feature: a circle crosses a river, a walk does not.
These run against a real PostGIS + pgRouting database, loading a synthetic
street grid split by a river with one bridge. Skipped where either is absent,
because the answer then is "circles only" and that is tested separately.
"""
from __future__ import annotations

from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from kaigyou_core import config as cfg
from kaigyou_core.analysis import analyze_point, catchment_geojson, walk_network_status
from kaigyou_core.db import connect, table_exists
from kaigyou_etl.adapters import AdapterContext, get_adapter
from kaigyou_etl.adapters.osm_walk_network import build_topology

FIXTURE = Path(__file__).parent / "fixtures" / "osm_roads_river.shp.zip"

# The fixture grid: a river between these longitudes, crossed only at BRIDGE_LAT.
RIVER_WEST, RIVER_EAST = 139.7655, 139.7677
BRIDGE_LAT = 35.6797
#: South-west of the bridge, well away from it, on the west bank.
POINT = (35.6779, 139.7622)

SOURCE_ID = "__walk_test__"


@pytest.fixture(scope="module")
def network():
    """Load the fixture network into its own source id, and take it out after."""
    try:
        with connect() as probe:
            if not table_exists(probe, "walk_network"):
                pytest.skip("walk_network not migrated here")
            with probe.cursor() as cur:
                cur.execute("SELECT to_regproc('kg_walk_catchment') AS fn")
                if cur.fetchone()["fn"] is None:
                    pytest.skip("pgrouting not available here")
    except psycopg.OperationalError:
        pytest.skip("no database")

    sources = cfg.sources_config()
    spec = dict(sources["sources"]["osm_walk_network"])
    spec["bbox"] = None
    ctx = AdapterContext(source_id=SOURCE_ID, spec=spec, defaults={},
                         raw_dir=Path("data/raw/walk_test"), input_path=None, offline=True)
    adapter = get_adapter("osm_walk_network")(ctx)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO data_sources (id, name, publisher, dataset_kind)
                   VALUES (%s, 'test walk network', 'test', 'sample')
                   ON CONFLICT (id) DO NOTHING""", (SOURCE_ID,))
        conn.commit()
        adapter.load(conn, adapter.transform(FIXTURE))
        summary = build_topology(conn)
    yield summary
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM data_sources WHERE id = %s", (SOURCE_ID,))
        conn.commit()


def test_the_network_is_one_connected_graph(network):
    """Noding is the step that is easy to leave out, and fatal when you do.

    Without pgr_nodeNetwork the streets only join where they happen to share an
    endpoint, so a grid that visibly meets at every corner becomes a graph of
    fragments and every catchment comes out far too small.
    """
    assert network["topology"] == "built"
    assert network["noded_edges"] > network["nodes"] / 2
    assert network["largest_component_share"] == 1.0


def test_a_walk_does_not_cross_a_river_the_circle_crosses(network):
    """The whole reason for the feature."""
    lat, lng = POINT
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH circle AS (SELECT geom g FROM kg_catchment(%s, %s, 500, 'circle')),
                 walk   AS (SELECT geom g FROM kg_catchment(%s, %s, 500, 'walk'))
            SELECT ST_XMax(circle.g) AS circle_east, ST_XMax(walk.g) AS walk_east,
                   ST_Area(circle.g::geography) AS circle_m2,
                   ST_Area(walk.g::geography)   AS walk_m2
            FROM circle, walk
            """, (lat, lng, lat, lng))
        row = cur.fetchone()

    assert row["circle_east"] > RIVER_EAST, "the circle should reach the far bank"
    assert row["walk_east"] < RIVER_EAST, "the walk should stop at the river"
    assert row["walk_m2"] < row["circle_m2"] / 2


def test_a_long_enough_walk_reaches_the_far_bank_over_the_bridge(network):
    """Not simply clipped at the river: the detour exists and is used."""
    lat, lng = POINT
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT ST_XMax(geom) AS east FROM kg_catchment(%s, %s, 1500, 'walk')",
                    (lat, lng))
        assert cur.fetchone()["east"] > RIVER_EAST


def test_the_numbers_come_from_the_shape_that_is_reported(network):
    lat, lng = POINT
    with connect() as conn:
        circle = analyze_point(conn, lat, lng, 500, mesh_size_m=500, catchment="circle")
        walk = analyze_point(conn, lat, lng, 500, mesh_size_m=500, catchment="walk")

    assert circle["catchment_kind"] == "circle"
    assert walk["catchment_kind"] == "walk"
    assert walk["catchment_area_km2"] < circle["catchment_area_km2"]
    # A smaller catchment cannot contain more people.
    assert (walk["population"] or 0) <= (circle["population"] or 0)


def test_asking_for_a_walk_where_there_is_no_network_gives_a_circle(network):
    """Falling back is fine; pretending it did not is not.

    Somewhere far from the fixture grid there is no walkable street, so the
    answer is a circle -- and it says so, which is what lets the API warn.
    """
    with connect() as conn:
        metrics = analyze_point(conn, 35.40, 139.20, 500, mesh_size_m=500, catchment="walk")
    assert metrics["catchment_kind"] == "circle"


def test_the_drawn_polygon_is_the_one_that_was_measured(network):
    lat, lng = POINT
    with connect() as conn:
        shape = catchment_geojson(conn, lat, lng, 500, "walk")
        metrics = analyze_point(conn, lat, lng, 500, mesh_size_m=500, catchment="walk")
    assert shape is not None
    assert shape["kind"] == metrics["catchment_kind"] == "walk"
    assert shape["geometry"]["type"] in ("Polygon", "MultiPolygon")


def test_status_reports_the_network_as_available(network):
    with connect() as conn:
        assert walk_network_status(conn)["available"] is True


def test_migrate_repairs_the_routing_function_if_pgrouting_arrives_later():
    """Enabling pgRouting after the first migrate must not be a dead end.

    The pgRouting-conditional migrations skip the routing function when the
    extension is absent -- correct, because migrate has to succeed without it --
    and are then recorded as applied. Enabling the extension afterwards would
    otherwise leave the function missing for good, with the app reporting
    walking catchments as unavailable on a database that could do them.
    """
    from kaigyou_etl.migrate import migrate

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM pg_extension WHERE extname = 'pgrouting'")
            if not cur.fetchone()["n"]:
                pytest.skip("pgrouting not installed here")
            cur.execute(
                "DROP FUNCTION IF EXISTS kg_walk_catchment(double precision,"
                "double precision,double precision,double precision,double precision)")
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT to_regproc('kg_walk_catchment') AS fn")
            assert cur.fetchone()["fn"] is None, "precondition: function removed"

        migrate(conn)          # reports nothing new applied, and repairs anyway

        with conn.cursor() as cur:
            cur.execute("SELECT to_regproc('kg_walk_catchment') AS fn")
            assert cur.fetchone()["fn"] is not None
            # And exactly one kg_analyze_point: replaying must not leave two.
            cur.execute("SELECT count(*) AS n FROM pg_proc WHERE proname = 'kg_analyze_point'")
            assert cur.fetchone()["n"] == 1


def test_doctor_names_the_error_instead_of_leaving_it_a_500(network):
    """A broken walking query must be a named failure, not "Internal Server Error".

    Everything about the walking catchment is optional and falls back to a
    circle, which is right for a visitor and unhelpful for whoever has to fix
    it: the browser shows the same generic 500 whatever went wrong underneath.
    `doctor` runs the query itself and prints what PostgreSQL said.
    """
    from kaigyou_etl import doctor

    signature = ("double precision,double precision,double precision,"
                 "double precision,double precision")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"ALTER FUNCTION kg_walk_catchment({signature}) "
                        "RENAME TO kg_walk_catchment__saved")
            cur.execute("""
                CREATE FUNCTION kg_walk_catchment(
                    p_lat double precision, p_lng double precision,
                    p_distance_m double precision,
                    p_buffer_m double precision DEFAULT 40,
                    p_snap_m double precision DEFAULT 300)
                RETURNS geometry LANGUAGE plpgsql STABLE AS
                $$ BEGIN RAISE EXCEPTION 'pgr_drivingdistance does not exist'; END $$
            """)
        conn.commit()
        try:
            report = doctor.Report()
            doctor._check_walk_network(report, conn)
            # The connection has to survive a failed statement, or every check
            # after this one reports the same aborted transaction instead of
            # its own result.
            doctor._check_scores(report, conn)
        finally:
            with conn.cursor() as cur:
                cur.execute(f"DROP FUNCTION kg_walk_catchment({signature})")
                cur.execute(f"ALTER FUNCTION kg_walk_catchment__saved({signature}) "
                            "RENAME TO kg_walk_catchment")
            conn.commit()

    failed = [c for c in report.checks if c.status == doctor.FAIL]
    assert failed, "a query that raises must not be reported as healthy"
    assert "pgr_drivingdistance does not exist" in failed[0].detail
    assert not [c for c in report.checks
                if c.status == doctor.FAIL and "current transaction is aborted" in c.detail]
