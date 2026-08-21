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
