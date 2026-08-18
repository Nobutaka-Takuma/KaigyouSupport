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
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

import psycopg

from kaigyou_etl.acquisition import ERROR_EMPTY, ERROR_SCHEMA, AcquisitionError
from kaigyou_etl.adapters._util import read_text, to_float
from kaigyou_etl.adapters.base import SourceAdapter

# Japan lat/lng envelope, used only to reject obviously broken coordinates
# (swapped lat/lng, zeroes, unparsed degrees-minutes-seconds).
_SANE_LAT = (20.0, 46.0)
_SANE_LNG = (122.0, 154.0)

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

        required = ("facility_id", "name", "lat", "lng")
        resolved = {
            field: self.pick_column(headers, field, required=field in required)
            for field in self.column_map()
        }
        geocoded = sum(
            1 for r in rows
            if to_float(r.get(resolved["lat"])) is not None
            and to_float(r.get(resolved["lng"])) is not None
        )
        return {
            "row_count": len(rows),
            "rows_with_coordinates": geocoded,
            "rows_without_coordinates": len(rows) - geocoded,
            "headers": headers[:40],
            "resolved_columns": {k: v for k, v in resolved.items() if v},
        }

    def transform(self, artifact: Path) -> Iterator[dict[str, Any]]:
        headers, rows = self._rows(artifact)
        required = ("facility_id", "name", "lat", "lng")
        col = {f: self.pick_column(headers, f, required=f in required)
               for f in self.column_map()}
        category = self.spec.get("facility_category", "dental_clinic")
        source_date = self.source_date() or date.today()
        pref = self.ctx.prefecture_code
        seen: set[str] = set()

        for row in rows:
            lat = to_float(row.get(col["lat"]))
            lng = to_float(row.get(col["lng"]))
            if lat is None or lng is None:
                continue
            if not (_SANE_LAT[0] <= lat <= _SANE_LAT[1] and _SANE_LNG[0] <= lng <= _SANE_LNG[1]):
                continue

            facility_id = (row.get(col["facility_id"]) or "").strip()
            name = (row.get(col["name"]) or "").strip()
            if not facility_id or not name or facility_id in seen:
                continue
            seen.add(facility_id)

            yield {
                "facility_id": facility_id,
                "facility_category": category,
                "name": name,
                "address": _get(row, col, "address"),
                "prefecture_code": pref,
                "municipality_code": _get(row, col, "municipality_code"),
                "lat": lat,
                "lng": lng,
                "opening_date": _parse_date(_get(row, col, "opening_date")),
                "clinic_types": _split_types(_get(row, col, "clinic_types")),
                "founder_type": _get(row, col, "founder_type"),
                "source_date": source_date,
            }

    def load(self, conn: psycopg.Connection, records: Iterable[dict[str, Any]]) -> int:
        count = 0
        with conn.cursor() as cur:
            cur.execute("DELETE FROM facilities WHERE source_id = %s", (self.source_id,))
            for rec in records:
                cur.execute(
                    """
                    INSERT INTO facilities (
                        source_id, facility_id, facility_category, name, address,
                        prefecture_code, municipality_code, geom, opening_date,
                        clinic_types, founder_type, source_date, last_updated
                    ) VALUES (
                        %(source_id)s, %(facility_id)s, %(facility_category)s, %(name)s,
                        %(address)s, %(prefecture_code)s, %(municipality_code)s,
                        ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326),
                        %(opening_date)s, %(clinic_types)s, %(founder_type)s,
                        %(source_date)s, now()
                    )
                    ON CONFLICT (source_id, facility_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        address = EXCLUDED.address,
                        geom = EXCLUDED.geom,
                        clinic_types = EXCLUDED.clinic_types,
                        source_date = EXCLUDED.source_date,
                        last_updated = now()
                    """,
                    {**rec, "source_id": self.source_id},
                )
                count += 1
        return count


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
