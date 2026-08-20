"""厚生労働省「医療情報ネット」オープンデータ -> facilities.

The published extract is a per-prefecture CSV in Shift-JIS. Header names have
changed between releases, so every field is resolved through the candidate
lists in ``config/sources.yaml`` rather than hard-coded here.

The MVP does not geocode: rows without usable coordinates are counted and
reported, not guessed at.
"""
from __future__ import annotations

import csv
import io
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

import psycopg
from psycopg.types.json import Json

from kaigyou_etl.acquisition import ERROR_EMPTY, ERROR_SCHEMA, AcquisitionError
from kaigyou_etl.adapters._util import read_text, to_float
from kaigyou_etl.adapters.base import SourceAdapter

# Japan lat/lng envelope, used only to reject obviously broken coordinates
# (swapped lat/lng, zeroes, unparsed degrees-minutes-seconds).
_SANE_LAT = (20.0, 46.0)
_SANE_LNG = (122.0, 154.0)

# Fields without which a row cannot be placed on the map at all.
_REQUIRED = ("facility_id", "name", "lat", "lng")


def _in_japan(lat: float, lng: float) -> bool:
    return _SANE_LAT[0] <= lat <= _SANE_LAT[1] and _SANE_LNG[0] <= lng <= _SANE_LNG[1]


def _municipality_code(value: str | None, prefecture_code: str) -> str | None:
    """Normalise to the 5-digit JIS X0402 code.

    The facility file publishes only the 3-digit municipality part, which is
    ambiguous on its own -- 101 is Chiyoda in Tokyo and Aoba in Yokohama.
    """
    if not value:
        return None
    digits = value.strip()
    if len(digits) == 3 and prefecture_code:
        return f"{prefecture_code}{digits}"
    if len(digits) == 5:
        return digits
    return digits or None

# Separators seen in the 診療科目 column across releases.
_TYPE_SPLIT = re.compile(r"[、,/|・\s]+")


class MHLWClinicsAdapter(SourceAdapter):
    target_tables = ("facilities",)

    def _rows(self, artifact: Path) -> tuple[list[str], list[dict[str, str]]]:
        text = read_text(artifact, self.spec.get("encoding"))
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise AcquisitionError(ERROR_SCHEMA, f"{artifact.name}: no CSV header")
        return list(reader.fieldnames), list(reader)

    def validate(self, artifact: Path) -> dict[str, Any]:
        headers, rows = self._rows(artifact)
        if not rows:
            raise AcquisitionError(ERROR_EMPTY, f"{artifact.name} contains no data rows")

        resolved = {
            field: self.pick_column(headers, field, required=field in _REQUIRED)
            for field in self.column_map()
        }

        # Coordinate quality is the single thing that decides whether a row can
        # be used at all, so it is measured up front and reported. Roughly 6%
        # of the national file carries 0,0 -- those rows are dropped, never
        # placed at the equator and never geocoded from the address string.
        usable = missing = zero = out_of_range = 0
        for r in rows:
            lat = to_float(r.get(resolved["lat"]))
            lng = to_float(r.get(resolved["lng"]))
            if lat is None or lng is None:
                missing += 1
            elif lat == 0.0 and lng == 0.0:
                zero += 1
            elif not _in_japan(lat, lng):
                out_of_range += 1
            else:
                usable += 1

        facts: dict[str, Any] = {
            "row_count": len(rows),
            "rows_usable": usable,
            "rows_dropped_no_coordinates": missing,
            "rows_dropped_zero_coordinates": zero,
            "rows_dropped_outside_japan": out_of_range,
            "headers": headers[:40],
            "resolved_columns": {k: v for k, v in resolved.items() if v},
        }

        pref_col = resolved.get("prefecture_code")
        if pref_col:
            by_pref = Counter((r.get(pref_col) or "").strip() for r in rows)
            facts["prefectures_in_file"] = len(by_pref)
            facts["rows_by_prefecture"] = dict(sorted(by_pref.items()))

        if usable == 0:
            raise AcquisitionError(
                ERROR_EMPTY,
                f"{artifact.name}: no row has usable coordinates "
                f"(missing={missing}, zero={zero}, out_of_range={out_of_range})",
            )
        return facts

    def transform(self, artifact: Path) -> Iterator[dict[str, Any]]:
        headers, rows = self._rows(artifact)
        col = {f: self.pick_column(headers, f, required=f in _REQUIRED)
               for f in self.column_map()}
        attribute_columns = {
            key: header for key, header in (self.spec.get("attribute_columns") or {}).items()
            if header in headers
        }
        category = self.spec.get("facility_category", "dental_clinic")
        source_date = self.source_date() or date.today()
        default_pref = self.ctx.prefecture_code
        wanted = self.spec.get("prefecture_filter") or self.ctx.prefecture_filter
        wanted = str(wanted) if wanted else None
        seen: set[str] = set()

        for row in rows:
            lat = to_float(row.get(col["lat"]))
            lng = to_float(row.get(col["lng"]))
            if lat is None or lng is None or not _in_japan(lat, lng):
                continue

            facility_id = (row.get(col["facility_id"]) or "").strip()
            name = (row.get(col["name"]) or "").strip()
            if not facility_id or not name or facility_id in seen:
                continue

            # The file spans all 47 prefectures, so the prefecture comes from
            # the row when the publisher provides it.
            pref = (row.get(col["prefecture_code"]) or "").strip() if col.get("prefecture_code") else ""
            pref = pref.zfill(2) if pref else default_pref
            if wanted and pref != wanted:
                continue

            seen.add(facility_id)
            yield {
                "facility_id": facility_id,
                "facility_category": category,
                "name": name,
                "address": _get(row, col, "address"),
                "prefecture_code": pref,
                "municipality_code": _municipality_code(_get(row, col, "municipality_code"), pref),
                "lat": lat,
                "lng": lng,
                "opening_date": _parse_date(_get(row, col, "opening_date")),
                "clinic_types": _split_types(_get(row, col, "clinic_types")),
                "founder_type": _get(row, col, "founder_type"),
                "attributes": {
                    key: value
                    for key, header in attribute_columns.items()
                    if (value := (row.get(header) or "").strip())
                },
                "source_date": source_date,
            }

    def load(self, conn: psycopg.Connection, records: Iterable[dict[str, Any]]) -> int:
        rows = [{**rec, "source_id": self.source_id,
                 "attributes": Json(rec.get("attributes") or {})}
                for rec in records]
        with conn.cursor() as cur:
            cur.execute("DELETE FROM facilities WHERE source_id = %s", (self.source_id,))
            return self.insert_many(
                cur,
                """
                    INSERT INTO facilities (
                        source_id, facility_id, facility_category, name, address,
                        prefecture_code, municipality_code, geom, opening_date,
                        clinic_types, founder_type, attributes, source_date, last_updated
                    ) VALUES (
                        %(source_id)s, %(facility_id)s, %(facility_category)s, %(name)s,
                        %(address)s, %(prefecture_code)s, %(municipality_code)s,
                        ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326),
                        %(opening_date)s, %(clinic_types)s, %(founder_type)s,
                        %(attributes)s, %(source_date)s, now()
                    )
                    ON CONFLICT (source_id, facility_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        address = EXCLUDED.address,
                        geom = EXCLUDED.geom,
                        clinic_types = EXCLUDED.clinic_types,
                        attributes = EXCLUDED.attributes,
                        prefecture_code = EXCLUDED.prefecture_code,
                        municipality_code = EXCLUDED.municipality_code,
                        source_date = EXCLUDED.source_date,
                        last_updated = now()
                """,
                rows,
            )


def _get(row: dict[str, str], col: dict[str, str | None], field: str) -> str | None:
    name = col.get(field)
    if not name:
        return None
    value = (row.get(name) or "").strip()
    return value or None


def _split_types(value: str | None) -> list[str]:
    if not value:
        return []
    return [t for t in (part.strip() for part in _TYPE_SPLIT.split(value)) if t]


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    text = value.strip().replace("/", "-").replace("年", "-").replace("月", "-").replace("日", "")
    text = text.rstrip("-")
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None
