"""国土数値情報「駅別乗降客数」(S12) -> stations.

S12 ships as a zipped shapefile whose geometry is the platform *line*, not a
point, so each record is reduced to the line's midpoint. Passenger counts are
published one column per survey year; the adapter takes the most recent
non-null column listed in ``config/sources.yaml``, and records which one it
used so the figure can be attributed.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator

import psycopg

from kaigyou_etl.acquisition import ERROR_EMPTY, ERROR_SCHEMA, AcquisitionError
from kaigyou_etl.adapters._util import read_shapefile, to_int
from kaigyou_etl.adapters.base import SourceAdapter

# S12 passenger columns are named S12_0NN; the trailing digits increase with
# the survey year, which is what "most recent" means below.
_YEARLY = re.compile(r"^S12_0\d\d$")


class MLITStationsAdapter(SourceAdapter):
    target_tables = ("stations",)

    def _read(self, artifact: Path) -> tuple[list[str], list[Any]]:
        fields, records = read_shapefile(artifact)
        if not records:
            raise AcquisitionError(ERROR_EMPTY, f"{artifact.name} contains no features")
        return fields, records

    def _resolve(self, fields: list[str]) -> dict[str, Any]:
        col = {
            "station_id": self.pick_column(fields, "station_id", required=False),
            "name": self.pick_column(fields, "name", required=True),
            "operator": self.pick_column(fields, "operator", required=False),
            "railway_line": self.pick_column(fields, "railway_line", required=False),
        }
        candidates = [c for c in self.column_map().get("passengers", []) if c in fields]
        col["passenger_columns"] = sorted(
            (c for c in candidates if _YEARLY.match(c)), reverse=True
        ) or candidates
        return col

    def validate(self, artifact: Path) -> dict[str, Any]:
        fields, records = self._read(artifact)
        col = self._resolve(fields)
        if not col["passenger_columns"]:
            raise AcquisitionError(
                ERROR_SCHEMA,
                "no passenger column found; tried "
                f"{self.column_map().get('passengers')}, file has {fields}",
            )
        with_passengers = 0
        for rec in records[:5000]:
            row = dict(zip(fields, rec.record))
            if any(to_int(row.get(c)) for c in col["passenger_columns"]):
                with_passengers += 1
        return {
            "feature_count": len(records),
            "fields": fields,
            "passenger_columns": col["passenger_columns"],
            "features_with_passengers_in_sample": with_passengers,
        }

    def transform(self, artifact: Path) -> Iterator[dict[str, Any]]:
        fields, records = self._read(artifact)
        col = self._resolve(fields)
        source_date = self.source_date() or date.today()
        pref = self.ctx.prefecture_code
        seen: set[str] = set()

        for rec in records:
            row = dict(zip(fields, rec.record))
            name = str(row.get(col["name"]) or "").strip()
            if not name:
                continue

            point = _midpoint(rec.shape)
            if point is None:
                continue
            lng, lat = point

            passengers = None
            passengers_column = None
            for candidate in col["passenger_columns"]:
                value = to_int(row.get(candidate))
                if value:
                    passengers, passengers_column = value, candidate
                    break

            station_id = str(row.get(col["station_id"]) or "").strip() if col["station_id"] else ""
            if not station_id:
                station_id = f"{name}:{lng:.5f},{lat:.5f}"
            if station_id in seen:
                continue
            seen.add(station_id)

            yield {
                "station_id": station_id,
                "name": name,
                "operator": _opt(row, col, "operator"),
                "railway_line": _opt(row, col, "railway_line"),
                "prefecture_code": pref,
                "lng": lng,
                "lat": lat,
                "daily_passengers": passengers,
                "passengers_year": _year_of(passengers_column),
                "source_date": source_date,
            }

    def load(self, conn: psycopg.Connection, records: Iterable[dict[str, Any]]) -> int:
        count = 0
        with conn.cursor() as cur:
            cur.execute("DELETE FROM stations WHERE source_id = %s", (self.source_id,))
            for rec in records:
                cur.execute(
                    """
                    INSERT INTO stations (
                        source_id, station_id, name, operator, railway_line,
                        prefecture_code, geom, daily_passengers, passengers_year,
                        source_date, last_updated
                    ) VALUES (
                        %(source_id)s, %(station_id)s, %(name)s, %(operator)s,
                        %(railway_line)s, %(prefecture_code)s,
                        ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326),
                        %(daily_passengers)s, %(passengers_year)s, %(source_date)s, now()
                    )
                    ON CONFLICT (source_id, station_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        geom = EXCLUDED.geom,
                        daily_passengers = EXCLUDED.daily_passengers,
                        passengers_year = EXCLUDED.passengers_year,
                        source_date = EXCLUDED.source_date,
                        last_updated = now()
                    """,
                    {**rec, "source_id": self.source_id},
                )
                count += 1
        return count


def _opt(row: dict[str, Any], col: dict[str, Any], field: str) -> str | None:
    name = col.get(field)
    if not name:
        return None
    value = str(row.get(name) or "").strip()
    return value or None


def _midpoint(shape: Any) -> tuple[float, float] | None:
    """Representative point of a station feature (line, point or polygon)."""
    points = list(getattr(shape, "points", []) or [])
    if not points:
        return None
    lngs = [p[0] for p in points]
    lats = [p[1] for p in points]
    return (sum(lngs) / len(lngs), sum(lats) / len(lats))


def _year_of(column: str | None) -> int | None:
    """S12 passenger columns do not carry the year in the name; the mapping
    lives with the dataset documentation, so this is intentionally left
    unresolved rather than guessed."""
    return None
