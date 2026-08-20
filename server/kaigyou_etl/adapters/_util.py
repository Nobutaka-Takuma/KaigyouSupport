"""Small helpers shared by the file-based adapters."""
from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path
from typing import Any, Iterator

from kaigyou_etl.acquisition import ERROR_PARSE, ERROR_SCHEMA, AcquisitionError

# Publishers ship Shift-JIS far more often than UTF-8; try both, plus BOM'd UTF-8.
ENCODINGS = ("cp932", "utf-8-sig", "utf-8", "euc_jp")


def read_text(path: Path, preferred: str | None = None) -> str:
    candidates = ([preferred] if preferred else []) + [e for e in ENCODINGS if e != preferred]
    raw = path.read_bytes()
    for enc in candidates:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    raise AcquisitionError(ERROR_PARSE, f"could not decode {path.name} as any of {candidates}")


def decode_bytes(raw: bytes, preferred: str | None = None) -> str:
    candidates = ([preferred] if preferred else []) + [e for e in ENCODINGS if e != preferred]
    for enc in candidates:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    raise AcquisitionError(ERROR_PARSE, f"could not decode payload as any of {candidates}")


def is_zip(path: Path) -> bool:
    return zipfile.is_zipfile(path)


def zip_members(path: Path, suffixes: tuple[str, ...]) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return [n for n in zf.namelist()
                if n.lower().endswith(suffixes) and not n.startswith("__MACOSX")]


def open_member(path: Path, member: str) -> bytes:
    with zipfile.ZipFile(path) as zf:
        return zf.read(member)


def csv_rows(text: str) -> Iterator[dict[str, str]]:
    """Yield CSV rows as dicts, tolerating a leading metadata/units row.

    e-Stat exports repeat the header meaning on a second line; if the row
    directly under the header has no numeric content at all it is skipped.
    """
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise AcquisitionError(ERROR_SCHEMA, "CSV has no header row")
    first = True
    for row in reader:
        if first:
            first = False
            values = [v for v in row.values() if v not in (None, "")]
            if values and not any(_looks_numeric(v) for v in values):
                continue
        yield row


def _looks_numeric(value: Any) -> bool:
    try:
        float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return False
    return True


def to_int(value: Any) -> int | None:
    """Parse a statistics cell. '-', '*', 'X' and '' all mean 'not published'."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("　", "")
    if text in ("", "-", "－", "*", "＊", "X", "x", "・", "…", "NA"):
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in ("", "-", "－", "*", "＊", "X", "x", "・", "…", "NA"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def read_shapefile(path: Path, member_prefix: str | None = None,
                   encoding: str = "cp932") -> tuple[list[str], list[Any]]:
    """Read a shapefile from a zip (or a bare .shp) using pyshp.

    ``member_prefix`` selects among several copies inside one archive -- the
    S12 archives ship the same layer twice, under ``Shift-JIS/`` and
    ``UTF-8/``, and reading one with the other's encoding silently mangles
    every Japanese name.

    A ``.cpg`` sidecar, when present, is the publisher's own declaration of the
    dbf encoding and wins over ``encoding``: N03 ships flat and UTF-8 while
    older 国土数値情報 layers are Shift-JIS, and guessing wrong corrupts every
    municipality name without erroring.

    Returns (field names, list of pyshp ShapeRecord).
    """
    import shapefile  # pyshp

    if is_zip(path):
        shp_members = zip_members(path, (".shp",))
        if member_prefix:
            matched = [m for m in shp_members if member_prefix in m]
            if not matched:
                raise AcquisitionError(
                    ERROR_SCHEMA,
                    f"{path.name} has no .shp under {member_prefix!r}; "
                    f"members: {shp_members[:5]}",
                )
            shp_members = matched
        if not shp_members:
            raise AcquisitionError(ERROR_SCHEMA, f"{path.name} contains no .shp member")
        stem = shp_members[0][:-4]
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            parts: dict[str, io.BytesIO] = {}
            for ext in ("shp", "dbf", "shx"):
                member = f"{stem}.{ext}"
                if member not in names:
                    member = next((n for n in names if n.lower() == member.lower()), None)
                if member is None:
                    if ext == "shx":
                        continue
                    raise AcquisitionError(ERROR_SCHEMA, f"{path.name} missing .{ext} for {stem}")
                parts[ext] = io.BytesIO(zf.read(member))
            declared = zf.read(f"{stem}.cpg").decode("ascii", "ignore").strip() \
                if f"{stem}.cpg" in names else ""
        encoding = _resolve_encoding(declared, encoding)
        reader = shapefile.Reader(**{k: v for k, v in parts.items()}, encoding=encoding,
                                  encodingErrors="replace")
    else:
        cpg = path.with_suffix(".cpg")
        declared = cpg.read_text(encoding="ascii", errors="ignore").strip() \
            if cpg.exists() else ""
        encoding = _resolve_encoding(declared, encoding)
        reader = shapefile.Reader(str(path), encoding=encoding, encodingErrors="replace")

    fields = [f[0] for f in reader.fields[1:]]
    return fields, list(reader.iterShapeRecords())


def _resolve_encoding(declared: str, fallback: str) -> str:
    """Trust a .cpg declaration when Python recognises the codec it names."""
    if not declared:
        return fallback
    candidate = declared.replace("-", "_").lower()
    aliases = {"utf_8": "utf-8", "utf8": "utf-8", "sjis": "cp932",
               "shift_jis": "cp932", "ms932": "cp932"}
    candidate = aliases.get(candidate, declared)
    try:
        "".encode(candidate)
    except LookupError:
        return fallback
    return candidate


def shape_to_wkt(shape: Any) -> str | None:
    """Convert a pyshp shape to WKT. Points, polylines and polygons only."""
    geo = shape.__geo_interface__
    gtype = geo.get("type")
    coords = geo.get("coordinates")
    if gtype == "Point":
        return f"POINT({coords[0]:.8f} {coords[1]:.8f})"
    if gtype in ("LineString", "MultiLineString"):
        lines = [coords] if gtype == "LineString" else list(coords)
        parts = ", ".join(
            "(" + ", ".join(f"{x:.8f} {y:.8f}" for x, y, *_ in line) + ")" for line in lines
        )
        return f"MULTILINESTRING({parts})"
    if gtype in ("Polygon", "MultiPolygon"):
        polys = [coords] if gtype == "Polygon" else list(coords)
        parts = []
        for poly in polys:
            rings = ", ".join(
                "(" + ", ".join(f"{x:.8f} {y:.8f}" for x, y, *_ in ring) + ")" for ring in poly
            )
            parts.append(f"({rings})")
        return f"MULTIPOLYGON({', '.join(parts)})"
    return None
