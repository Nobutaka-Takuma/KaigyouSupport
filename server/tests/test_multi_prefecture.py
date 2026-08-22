"""Loading a second prefecture must not be the act of deleting the first.

The app was built for Tokyo, and "one prefecture" was baked in below the
waterline: every mesh row is written under a source id shared by all
prefectures, and the load replaced everything under that id. Nothing about
that is visible from the outside -- the second load reports success, with a
plausible row count, and the first prefecture is simply gone.

These are the properties that make a second prefecture safe to add, tested
against the database rather than by reading the SQL.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from kaigyou_core import config as cfg
from kaigyou_core.analysis import default_prefecture, loaded_prefectures
from kaigyou_core.db import connect, table_exists
from kaigyou_etl.adapters import AdapterContext, get_adapter

FIXTURES = Path(__file__).parent / "fixtures"
MESH_FILE = FIXTURES / "estat_mesh_2020.txt"

#: Not real Shizuoka codes -- the fixture's meshes are in Tokyo. What is under
#: test is the bookkeeping, and it must not care where the polygons are.
OTHER_PREFECTURE = "99"
SOURCE_ID = "estat_population_mesh"


def _adapter(tmp_path: Path, prefecture: str):
    sources = cfg.sources_config()
    spec = dict(sources["sources"][SOURCE_ID])
    ctx = AdapterContext(
        source_id=SOURCE_ID, spec=spec, defaults=sources.get("defaults", {}),
        raw_dir=tmp_path, prefecture_override=prefecture)
    return get_adapter(spec["adapter"])(ctx)


@pytest.fixture
def db():
    try:
        with connect() as probe:
            if not table_exists(probe, "population_mesh"):
                pytest.skip("population_mesh not migrated here")
    except psycopg.OperationalError:
        pytest.skip("no database")
    yield
    # The fixture prefecture never belongs in a real database.
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM population_mesh WHERE prefecture_code = %s",
                        (OTHER_PREFECTURE,))
        conn.commit()


def _count(conn, prefecture: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM population_mesh WHERE prefecture_code = %s",
                    (prefecture,))
        return cur.fetchone()["n"]


def test_a_second_prefecture_leaves_the_first_alone(db, tmp_path):
    """The whole point. Before this, the second load emptied the first."""
    with connect() as conn:
        before = _count(conn, "13")

        adapter = _adapter(tmp_path, OTHER_PREFECTURE)
        loaded = adapter.load(conn, adapter.transform(MESH_FILE))
        conn.commit()

        assert loaded > 0, "precondition: the fixture loaded something"
        assert _count(conn, OTHER_PREFECTURE) == loaded
        assert _count(conn, "13") == before, (
            "loading another prefecture deleted the one that was already there")


def test_reloading_a_prefecture_still_replaces_it(db, tmp_path):
    """Narrowing the delete must not turn a reload into a duplicate."""
    with connect() as conn:
        adapter = _adapter(tmp_path, OTHER_PREFECTURE)
        adapter.load(conn, adapter.transform(MESH_FILE))
        conn.commit()
        first = _count(conn, OTHER_PREFECTURE)

        adapter.load(conn, adapter.transform(MESH_FILE))
        conn.commit()
        assert _count(conn, OTHER_PREFECTURE) == first


def test_the_run_decides_the_prefecture_not_the_config(tmp_path):
    """The e-Stat mesh files say which prefecture they are only in their name.

    Nothing inside tblT001141H22.txt identifies Shizuoka, so if the prefecture
    came from config alone every file would be tagged 13 and the second load
    would land on top of the first.
    """
    assert _adapter(tmp_path, "22").ctx.prefecture_code == "22"
    sources = cfg.sources_config()
    assert sources["defaults"]["prefecture_code"] == "13", "precondition"


def test_what_can_be_analysed_comes_from_the_data(db, tmp_path):
    with connect() as conn:
        adapter = _adapter(tmp_path, OTHER_PREFECTURE)
        adapter.load(conn, adapter.transform(MESH_FILE))
        conn.commit()

        codes = [p["code"] for p in loaded_prefectures(conn)]
        assert OTHER_PREFECTURE in codes and "13" in codes
        # Ordered by population, so the default is the one with most to analyse.
        assert default_prefecture(conn) == codes[0]
        assert default_prefecture(conn, "22") == "22", "an explicit ask always wins"


def test_the_map_is_pointed_at_people_not_at_the_middle_of_the_extent(db):
    """Tokyo reaches Ogasawara; the centre of its bounding box is open ocean."""
    with connect() as conn:
        tokyo = next((p for p in loaded_prefectures(conn) if p["code"] == "13"), None)
        if tokyo is None or not tokyo["population"]:
            pytest.skip("no Tokyo population loaded here")
        lng, lat = tokyo["center"]
        min_lng, min_lat, max_lng, max_lat = tokyo["bbox"]
        assert 139.4 < lng < 140.0 and 35.5 < lat < 35.9, (
            f"the map would open at {lat},{lng}")
        # And the extent really does stretch far enough for that to matter.
        assert max_lng - min_lng > 5, "precondition: the islands are loaded"


def _seed_scores(conn, prefecture: str, profile: str, radius: int, n: int = 3) -> int:
    """Put rows in mesh_scores for a prefecture, as an earlier run would have."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO mesh_scores (mesh_id, profile, radius_m, overall_score)
            SELECT id, %s, %s, 50
            FROM population_mesh WHERE prefecture_code = %s LIMIT %s
            ON CONFLICT DO NOTHING
        """, (profile, radius, prefecture, n))
        return cur.rowcount


def _score_count(conn, prefecture: str, profile: str, radius: int) -> int:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) AS n FROM mesh_scores ms
            JOIN population_mesh pm ON pm.id = ms.mesh_id
            WHERE pm.prefecture_code = %s AND ms.profile = %s AND ms.radius_m = %s
        """, (prefecture, profile, radius))
        return cur.fetchone()["n"]


def test_scoring_a_prefecture_leaves_another_prefectures_ranking_alone(db, tmp_path):
    """The same mistake one layer up: it emptied the Tokyo ranking.

    `DELETE FROM mesh_scores WHERE profile = ... AND radius_m = ...` matches
    every prefecture, so computing Shizuoka's scores removed Tokyo's and the
    site answered "メッシュスコアが未計算です" for a prefecture that had been
    scored ten minutes earlier.
    """
    from kaigyou_etl.scores import compute_mesh_scores, refresh_stats

    with connect() as conn:
        adapter = _adapter(tmp_path, OTHER_PREFECTURE)
        adapter.load(conn, adapter.transform(MESH_FILE))
        conn.commit()

        # A radius no run uses, so the rows below are this test's alone and
        # the cleanup cannot take a real ranking with it.
        model_radius = 999
        profile = "default"
        if not _seed_scores(conn, "13", profile, model_radius):
            pytest.skip("no Tokyo meshes to protect here")
        conn.commit()
        before = _score_count(conn, "13", profile, model_radius)

        refresh_stats(conn, radii=[model_radius], prefecture_code=OTHER_PREFECTURE)
        compute_mesh_scores(conn, profiles=[profile], radius_m=model_radius,
                            prefecture_code=OTHER_PREFECTURE)
        conn.commit()

        try:
            assert _score_count(conn, OTHER_PREFECTURE, profile, model_radius) > 0
            assert _score_count(conn, "13", profile, model_radius) == before, (
                "scoring another prefecture deleted this one's ranking")
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM mesh_scores WHERE radius_m = %s", (model_radius,))
                cur.execute("DELETE FROM metric_distributions WHERE scope LIKE %s",
                            (f"%pref{OTHER_PREFECTURE}",))
                cur.execute("DELETE FROM metric_distributions WHERE scope LIKE %s",
                            (f"%r{model_radius}%",))
            conn.commit()


def test_batching_the_sweep_does_not_change_its_answer(db, tmp_path):
    """Batched because a hosted database cancels a statement that runs too long.

    Shizuoka is 18,000 meshes; one statement for the lot took long enough to
    hit Supabase's timeout, which threw away several minutes of work and left
    the ranking empty. Paging must not change the result.
    """
    from kaigyou_core.analysis import mesh_catchments

    with connect() as conn:
        adapter = _adapter(tmp_path, OTHER_PREFECTURE)
        adapter.load(conn, adapter.transform(MESH_FILE))
        conn.commit()

        whole = mesh_catchments(conn, 500, mesh_size_m=500,
                                prefecture_code=OTHER_PREFECTURE, batch_size=10_000)
        paged = mesh_catchments(conn, 500, mesh_size_m=500,
                                prefecture_code=OTHER_PREFECTURE, batch_size=2)
        assert len(whole) > 2, "precondition: more rows than one page"
        assert [r["mesh_id"] for r in whole] == [r["mesh_id"] for r in paged]
        assert [r["population"] for r in whole] == [r["population"] for r in paged]


def test_a_point_is_analysed_as_the_prefecture_it_is_in(db):
    """Not as whatever the map's dropdown happens to say.

    With Shizuoka selected, a click in Chiyoda was analysed at Shizuoka's mesh
    resolution against Shizuoka's normalisation, and answered "no population in
    this trade area" for one of the densest places in Japan. Nothing on screen
    connected the two.
    """
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app
    from kaigyou_core.analysis import prefecture_at

    with connect() as conn:
        if prefecture_at(conn, 35.685, 139.796) != "13":
            pytest.skip("no Tokyo data loaded here")

    client = TestClient(app, raise_server_exceptions=False)
    body = client.get("/api/candidate-analysis",
                      params={"lat": 35.685, "lng": 139.796, "radius": 1000}).json()
    assert body["prefecture_code"] == "13"
    assert body["population"], "a point in central Tokyo has residents"
    # And the reader is told which prefecture the score belongs to.
    assert body["prefecture_name"]


def test_asking_for_a_prefecture_explicitly_is_still_honoured(db):
    """An explicit request is a question, and it gets answered as asked."""
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app

    client = TestClient(app, raise_server_exceptions=False)
    body = client.get("/api/candidate-analysis",
                      params={"lat": 35.685, "lng": 139.796, "radius": 1000,
                              "prefecture_code": OTHER_PREFECTURE}).json()
    assert body["prefecture_code"] == OTHER_PREFECTURE


def test_the_mesh_layer_follows_the_viewport(db, tmp_path):
    """Prefectures can be published at different resolutions.

    One database-wide answer draws whichever prefecture has more people and
    leaves the other blank as the reader pans into it.
    """
    from kaigyou_core.analysis import resolve_mesh_size

    with connect() as conn:
        tokyo = resolve_mesh_size(conn, None, bbox=[139.6, 35.6, 139.9, 35.8])
        assert tokyo, "precondition: Tokyo meshes are loaded"
        nowhere = resolve_mesh_size(conn, None, bbox=[100.0, 0.0, 101.0, 1.0])
        assert nowhere is None, "an empty viewport has no resolution to report"


# ------------------------------------- the file name is the only prefecture
def test_a_file_named_for_another_prefecture_is_refused(tmp_path):
    """The mistake that put Shizuoka's population under Tokyo's label.

    `load-local download/22` without --prefecture tagged every Shizuoka mesh
    as 13, and the prefecture-scoped replace then deleted Tokyo. The load
    reported success with a plausible row count; the only visible symptom was
    that both prefectures reported exactly the same population, days later.
    """
    from kaigyou_etl.acquisition import AcquisitionError

    shizuoka = tmp_path / "tblT001141H22.txt"
    shizuoka.write_bytes(MESH_FILE.read_bytes())

    with pytest.raises(AcquisitionError) as caught:
        _adapter(tmp_path, "13").validate(shizuoka)
    assert "22" in str(caught.value) and "13" in str(caught.value)


def test_a_file_named_for_this_prefecture_is_accepted(tmp_path):
    named = tmp_path / "tblT001141H22.txt"
    named.write_bytes(MESH_FILE.read_bytes())
    facts = _adapter(tmp_path, "22").validate(named)
    assert facts["prefecture_from_filename"] == "22"
    assert facts["prefecture_code"] == "22"


def test_a_file_that_names_no_prefecture_is_left_to_the_flag(tmp_path):
    """Not every publisher puts it in the name; those must still load."""
    anonymous = tmp_path / "mesh.txt"
    anonymous.write_bytes(MESH_FILE.read_bytes())
    assert _adapter(tmp_path, "13").validate(anonymous)["prefecture_from_filename"] is None


@pytest.mark.parametrize("names, expected", [
    (["tblT001141H22.txt"], "22"),
    (["tblT001141H13.txt"], "13"),
])
def test_load_local_reads_the_prefecture_off_the_files(tmp_path, names, expected):
    from kaigyou_etl.cli import _prefecture_for
    from kaigyou_etl.discover import Discovery

    found = Discovery(directory=tmp_path)
    found.mesh_current = tmp_path / names[0]
    code, problem = _prefecture_for(found, None)
    assert problem is None and code == expected


def test_load_local_refuses_a_folder_holding_two_prefectures(tmp_path):
    """Discovery keeps one file per slot, so the other would be silently dropped."""
    from kaigyou_etl.cli import _prefecture_for
    from kaigyou_etl.discover import Discovery

    found = Discovery(directory=tmp_path)
    found.mesh_current = tmp_path / "tblT001141H13.txt"
    found.mesh_business = tmp_path / "tblT001147H22.txt"
    code, problem = _prefecture_for(found, None)
    assert not code and problem and "複数の都道府県" in problem


def test_load_local_refuses_a_flag_that_contradicts_the_files(tmp_path):
    from kaigyou_etl.cli import _prefecture_for
    from kaigyou_etl.discover import Discovery

    found = Discovery(directory=tmp_path)
    found.mesh_current = tmp_path / "tblT001141H22.txt"
    code, problem = _prefecture_for(found, "13")
    assert not code and problem and "22" in problem


def test_dropping_a_prefecture_takes_only_that_prefecture(db, tmp_path):
    """Undoing a mislabelled load must not undo the correct one next to it."""
    import argparse

    from kaigyou_etl import cli

    with connect() as conn:
        adapter = _adapter(tmp_path, OTHER_PREFECTURE)
        adapter.load(conn, adapter.transform(MESH_FILE))
        conn.commit()
        tokyo_before = _count(conn, "13")
        assert _count(conn, OTHER_PREFECTURE) > 0, "precondition"

    assert cli.cmd_drop_prefecture(argparse.Namespace(
        prefecture=OTHER_PREFECTURE, dry_run=False, yes=True)) == cli.EXIT_OK

    with connect() as conn:
        assert _count(conn, OTHER_PREFECTURE) == 0
        assert _count(conn, "13") == tokyo_before


def test_dropping_needs_saying_so_twice(db, tmp_path):
    """A delete of real published data does not happen on a typo."""
    import argparse

    from kaigyou_etl import cli

    with connect() as conn:
        adapter = _adapter(tmp_path, OTHER_PREFECTURE)
        adapter.load(conn, adapter.transform(MESH_FILE))
        conn.commit()

    assert cli.cmd_drop_prefecture(argparse.Namespace(
        prefecture=OTHER_PREFECTURE, dry_run=False, yes=False)) == cli.EXIT_PARTIAL
    with connect() as conn:
        assert _count(conn, OTHER_PREFECTURE) > 0, "nothing should have been deleted"
