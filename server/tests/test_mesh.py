"""JIS mesh geometry.

The reference values are the published mesh codes for well-known points, so a
regression here means the mesh polygons -- and therefore every population
figure -- would be in the wrong place.
"""
import pytest

from kaigyou_core import mesh


def test_tokyo_station_1km_mesh():
    assert mesh.from_lat_lng(35.681236, 139.767125, 8) == "53394611"


def test_mesh_code_lengths_select_resolution():
    lat, lng = 35.681236, 139.767125
    assert mesh.from_lat_lng(lat, lng, 4) == "5339"
    assert mesh.from_lat_lng(lat, lng, 6) == "533946"
    assert len(mesh.from_lat_lng(lat, lng, 9)) == 9
    assert len(mesh.from_lat_lng(lat, lng, 10)) == 10


def test_size_m_follows_code_length_not_a_constant():
    assert mesh.size_m("53394611") == 1000
    assert mesh.size_m("533946113") == 500
    assert mesh.size_m("5339461132") == 250


@pytest.mark.parametrize("code", ["53394611", "533946113", "5339461132"])
def test_centroid_round_trips_to_its_own_code(code):
    lng, lat = mesh.centroid(code)
    assert mesh.from_lat_lng(lat, lng, len(code)) == code


def test_bbox_matches_declared_cell_size():
    min_lng, min_lat, max_lng, max_lat = mesh.to_bbox("53394611")
    d_lat, d_lng = mesh.cell_size_deg("53394611")
    assert max_lat - min_lat == pytest.approx(d_lat)
    assert max_lng - min_lng == pytest.approx(d_lng)


def test_polygon_wkt_is_closed():
    wkt = mesh.to_polygon_wkt("53394611")
    assert wkt.startswith("POLYGON((")
    coords = wkt[len("POLYGON(("):-2].split(", ")
    assert coords[0] == coords[-1]


@pytest.mark.parametrize("bad", ["", "abc", "5339461", "533946115", "12345678901"])
def test_invalid_codes_raise(bad):
    with pytest.raises(mesh.MeshCodeError):
        mesh.size_m(bad)
