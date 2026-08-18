"""JIS X0410 standard regional grid ("地域メッシュ") helpers.

The MVP analyses 1km meshes (8-digit, 第3次地域区画) but the mesh size is a
property of the data, not a constant: 9-digit (500m) and 10-digit (250m) codes
are handled by the same functions and stored in the same table, so switching
resolution is a matter of loading a different dataset.
"""
from __future__ import annotations

# Degrees spanned by one mesh cell, keyed by code length.
_CELL_SIZE: dict[int, tuple[float, float]] = {
    4: (2 / 3, 1.0),            # 一次メッシュ  ~80km
    6: (1 / 12, 1 / 8),         # 二次メッシュ  ~10km
    8: (1 / 120, 1 / 80),       # 三次メッシュ  ~1km
    9: (1 / 240, 1 / 160),      # 二分の一      ~500m
    10: (1 / 480, 1 / 320),     # 四分の一      ~250m
}

# Nominal edge length in metres, used for the mesh_size_m column.
NOMINAL_SIZE_M: dict[int, int] = {4: 80000, 6: 10000, 8: 1000, 9: 500, 10: 250}


class MeshCodeError(ValueError):
    """Raised for a code that is not a well-formed JIS mesh code."""


def _validate(code: str) -> str:
    code = str(code).strip()
    if not code.isdigit() or len(code) not in _CELL_SIZE:
        raise MeshCodeError(f"not a JIS mesh code: {code!r}")
    # Sub-mesh digits are quadrant selectors (1-4), not free digits: 533946115
    # has the right length but names no cell.
    for pos, label in ((8, "half"), (9, "quarter")):
        if len(code) > pos and code[pos] not in "1234":
            raise MeshCodeError(f"invalid {label}-mesh quadrant in {code!r}")
    return code


def cell_size_deg(code: str) -> tuple[float, float]:
    """Return (lat_span, lng_span) in degrees for the given code."""
    return _CELL_SIZE[len(_validate(code))]


def size_m(code: str) -> int:
    """Nominal edge length in metres."""
    return NOMINAL_SIZE_M[len(_validate(code))]


def to_bbox(code: str) -> tuple[float, float, float, float]:
    """Return (min_lng, min_lat, max_lng, max_lat) of the mesh cell."""
    code = _validate(code)

    lat = int(code[0:2]) / 1.5
    lng = int(code[2:4]) + 100.0

    if len(code) >= 6:
        lat += int(code[4]) / 12
        lng += int(code[5]) / 8
    if len(code) >= 8:
        lat += int(code[6]) / 120
        lng += int(code[7]) / 80
    if len(code) >= 9:
        q = int(code[8])
        if q not in (1, 2, 3, 4):
            raise MeshCodeError(f"invalid half-mesh quadrant in {code!r}")
        lat += ((q - 1) // 2) / 240
        lng += ((q - 1) % 2) / 160
    if len(code) >= 10:
        q = int(code[9])
        if q not in (1, 2, 3, 4):
            raise MeshCodeError(f"invalid quarter-mesh quadrant in {code!r}")
        lat += ((q - 1) // 2) / 480
        lng += ((q - 1) % 2) / 320

    d_lat, d_lng = _CELL_SIZE[len(code)]
    return (lng, lat, lng + d_lng, lat + d_lat)


def to_polygon_wkt(code: str) -> str:
    """Return the mesh cell as a closed POLYGON in WKT (SRID 4326 assumed)."""
    min_lng, min_lat, max_lng, max_lat = to_bbox(code)
    ring = [
        (min_lng, min_lat), (max_lng, min_lat),
        (max_lng, max_lat), (min_lng, max_lat),
        (min_lng, min_lat),
    ]
    coords = ", ".join(f"{x:.10f} {y:.10f}" for x, y in ring)
    return f"POLYGON(({coords}))"


def centroid(code: str) -> tuple[float, float]:
    """Return (lng, lat) of the mesh cell centre."""
    min_lng, min_lat, max_lng, max_lat = to_bbox(code)
    return ((min_lng + max_lng) / 2, (min_lat + max_lat) / 2)


def from_lat_lng(lat: float, lng: float, length: int = 8) -> str:
    """Return the mesh code of the given length containing (lat, lng)."""
    if length not in _CELL_SIZE:
        raise MeshCodeError(f"unsupported mesh code length: {length}")

    p, rem_lat = divmod(lat * 1.5, 1)
    q, rem_lng = divmod(lng - 100.0, 1)
    code = f"{int(p):02d}{int(q):02d}"
    if length == 4:
        return code

    r, rem_lat = divmod(rem_lat / 1.5 * 12, 1)
    s, rem_lng = divmod(rem_lng * 8, 1)
    code += f"{int(r)}{int(s)}"
    if length == 6:
        return code

    t, rem_lat = divmod(rem_lat * 10, 1)
    u, rem_lng = divmod(rem_lng * 10, 1)
    code += f"{int(t)}{int(u)}"
    if length == 8:
        return code

    v_lat, rem_lat = divmod(rem_lat * 2, 1)
    v_lng, rem_lng = divmod(rem_lng * 2, 1)
    code += str(int(v_lat) * 2 + int(v_lng) + 1)
    if length == 9:
        return code

    w_lat = int(rem_lat * 2)
    w_lng = int(rem_lng * 2)
    return code + str(w_lat * 2 + w_lng + 1)
