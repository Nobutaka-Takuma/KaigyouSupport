"""国土数値情報「駅別乗降客数」(S12) -> stations.

Three things about the published file shape this adapter:

* **Geometry is a line, not a point.** Each feature is a short segment along
  the platform, so it is reduced to its midpoint.

* **Counts are one four-column block per survey year.** The block layout comes
  from ``config/sources.yaml`` rather than being written here, and the most
  recent year that actually carries a figure is used -- recorded in
  ``passengers_year`` so the number can be attributed.

* **A station appears once per operator.** Shinjuku is eleven rows; only the
  rows flagged as non-duplicate carry a count, and the rest are published as
  zero. Reporting the nearest single row would describe Shinjuku by its Metro
  entrance (200k/day) instead of the station (2.7M/day), so rows sharing a
  group code are combined and their counts summed.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import psycopg
from psycopg.types.json import Json

from kaigyou_etl.acquisition import ERROR_EMPTY, ERROR_SCHEMA, AcquisitionError
from kaigyou_etl.adapters._util import read_shapefile, to_int
from kaigyou_etl.adapters.base import SourceAdapter


class MLITStationsAdapter(SourceAdapter):
    target_tables = ("stations",)

    # ------------------------------------------------------------- artefacts
    def _read(self, artifact: Path) -> tuple[list[str], list[dict[str, Any]]]:
        """Read the preferred encoding variant of the shapefile."""
        variants = self.spec.get("archive_variants") or [{"path": None, "encoding": "cp932"}]
        last_error: Exception | None = None
        for variant in variants:
            try:
                fields, shape_records = read_shapefile(
                    artifact,
                    member_prefix=variant.get("path"),
                    encoding=variant.get("encoding", "cp932"),
                )
            except AcquisitionError as exc:
                last_error = exc
                continue
            if shape_records:
                rows = []
                for sr in shape_records:
                    row = dict(zip(fields, sr.record))
                    point = _midpoint(sr.shape)
                    if point is None:
                        continue
                    row["_lng"], row["_lat"] = point
                    rows.append(row)
                if rows:
                    return fields, rows
        if last_error:
            raise last_error
        raise AcquisitionError(ERROR_EMPTY, f"{artifact.name} contains no usable features")

    # ------------------------------------------------------- year resolution
    def _year_columns(self, fields: Sequence[str]) -> dict[int, tuple[str, str]]:
        """Map survey year -> (duplicate column, passengers column).

        Years whose columns are missing from this edition are dropped, so an
        older or newer file loads without a config change.
        """
        series = self.spec.get("passenger_series") or {}
        template = series.get("column_template", "S12_{index:03d}")
        stride = int(series.get("stride", 4))
        dup_start = int(series.get("duplicate_start", 6))
        pax_start = int(series.get("passengers_start", 9))
        first = int(series.get("first_year", 2011))
        last = int(series.get("last_year", first))

        available = set(fields)
        out: dict[int, tuple[str, str]] = {}
        for offset, year in enumerate(range(first, last + 1)):
            dup = template.format(index=dup_start + stride * offset)
            pax = template.format(index=pax_start + stride * offset)
            if dup in available and pax in available:
                out[year] = (dup, pax)
        if not out:
            raise AcquisitionError(
                ERROR_SCHEMA,
                f"no passenger columns found for {first}-{last}; "
                f"file has {sorted(available)[:20]}",
            )
        return out

    def _prefecture_match_radius_m(self) -> float:
        return float(self.spec.get("prefecture_match_radius_m", 3000))

    def _primary_code(self) -> str:
        return str((self.spec.get("passenger_series") or {}).get(
            "primary_duplicate_code", "1"))

    # -------------------------------------------------------------- grouping
    def _group_key(self, row: dict[str, Any], col: dict[str, str | None]) -> str:
        """Key that ties a station's per-operator rows together.

        A blank group code means the publisher did not group this station;
        using it as a key would collect every such station in the country into
        one. Fall back to the per-operator code in that case.
        """
        group_col = col.get("group_id")
        group = str(row.get(group_col) or "").strip() if group_col else ""
        if group:
            return f"g:{group}"
        station = str(row.get(col["station_id"]) or "").strip() if col.get("station_id") else ""
        return f"s:{station}" if station else f"n:{row.get(col['name'])}"

    def _split_scattered(self, groups: dict[str, list[dict[str, Any]]],
                         col: dict[str, str | None]) -> dict[str, list[dict[str, Any]]]:
        """Break apart any group whose members are not plausibly one station."""
        max_spread = float((self.spec.get("grouping") or {}).get("max_spread_m", 2000))
        out: dict[str, list[dict[str, Any]]] = {}
        for key, rows in groups.items():
            if len(rows) < 2 or _spread_m(rows) <= max_spread:
                out[key] = rows
                continue
            for row in rows:
                station = (str(row.get(col["station_id"]) or "").strip()
                           if col.get("station_id") else "")
                sub = f"{key}/{station or row.get(col['name'])}/{row['_lng']:.4f}"
                out.setdefault(sub, []).append(row)
        return out

    # -------------------------------------------------------------- pipeline
    def _resolve(self, fields: list[str]) -> dict[str, str | None]:
        return {
            "station_id": self.pick_column(fields, "station_id", required=False),
            "group_id": self.pick_column(fields, "group_id", required=False),
            "name": self.pick_column(fields, "name", required=True),
            "operator": self.pick_column(fields, "operator", required=False),
            "railway_line": self.pick_column(fields, "railway_line", required=False),
        }

    def validate(self, artifact: Path) -> dict[str, Any]:
        fields, rows = self._read(artifact)
        col = self._resolve(fields)
        years = self._year_columns(fields)
        primary = self._primary_code()

        groups = self._build_groups(rows, col)
        picked = Counter()
        with_data = 0
        for members in groups.values():
            year, total = self._pick_year(members, years, primary)
            picked[year] += 1
            if total:
                with_data += 1

        return {
            "feature_count": len(rows),
            "station_count": len(groups),
            "years_available": sorted(years),
            "latest_year": max(years),
            "stations_with_passengers": with_data,
            "stations_without_passengers": len(groups) - with_data,
            "passenger_year_counts": {str(k): v for k, v in sorted(
                picked.items(), key=lambda kv: (kv[0] is None, kv[0]), reverse=True)},
            "resolved_columns": {k: v for k, v in col.items() if v},
        }

    def _build_groups(self, rows: list[dict[str, Any]],
                      col: dict[str, str | None]) -> dict[str, list[dict[str, Any]]]:
        if not (self.spec.get("grouping") or {}).get("enabled", True):
            return {f"r:{i}": [row] for i, row in enumerate(rows)}
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[self._group_key(row, col)].append(row)
        return self._split_scattered(dict(groups), col)

    def _pick_year(self, members: list[dict[str, Any]], years: dict[int, tuple[str, str]],
                   primary: str) -> tuple[int | None, int | None]:
        """Newest survey year for which this station has a published figure."""
        for year in sorted(years, reverse=True):
            dup_col, pax_col = years[year]
            total = 0
            found = False
            for row in members:
                if str(row.get(dup_col) or "").strip() != primary:
                    continue
                value = to_int(row.get(pax_col))
                if value is None:
                    continue
                total += value
                found = True
            if found and total > 0:
                return year, total
        return None, None

    def transform(self, artifact: Path) -> Iterator[dict[str, Any]]:
        fields, rows = self._read(artifact)
        col = self._resolve(fields)
        years = self._year_columns(fields)
        primary = self._primary_code()
        source_date = self.source_date() or date.today()

        for key, members in self._build_groups(rows, col).items():
            year, passengers = self._pick_year(members, years, primary)

            # Locate the station on the rows that actually carry a count; a
            # zero-flagged duplicate row can sit at a different entrance.
            counted = members
            if year is not None:
                dup_col = years[year][0]
                counted = [r for r in members
                           if str(r.get(dup_col) or "").strip() == primary] or members
            lng = sum(r["_lng"] for r in counted) / len(counted)
            lat = sum(r["_lat"] for r in counted) / len(counted)

            names = Counter(str(r.get(col["name"]) or "").strip() for r in members)
            names.pop("", None)
            if not names:
                continue
            name = names.most_common(1)[0][0]

            operators = _distinct(members, col.get("operator"))
            lines = _distinct(members, col.get("railway_line"))
            yield {
                # The group key, always -- a station's identity here is the
                # group, not any one operator's row. The published per-operator
                # codes are kept in attributes.
                "station_id": key,
                "name": name,
                "operator": _summarize(operators),
                "railway_line": _summarize(lines),
                "prefecture_code": None,   # S12 publishes none; filled after load
                "lng": lng,
                "lat": lat,
                "daily_passengers": passengers,
                "passengers_year": year,
                "attributes": {
                    "group_key": key,
                    "record_count": len(members),
                    "operators": operators,
                    "railway_lines": lines,
                    "station_codes": _distinct(members, col.get("station_id")),
                },
                "source_date": source_date,
            }

    def load(self, conn: psycopg.Connection, records: Iterable[dict[str, Any]]) -> int:
        rows = [{**rec, "source_id": self.source_id,
                 "attributes": Json(rec.get("attributes") or {})}
                for rec in records]
        with conn.cursor() as cur:
            cur.execute("DELETE FROM stations WHERE source_id = %s", (self.source_id,))
            count = self.insert_many(
                cur,
                """
                    INSERT INTO stations (
                        source_id, station_id, name, operator, railway_line,
                        prefecture_code, geom, daily_passengers, passengers_year,
                        attributes, source_date, last_updated
                    ) VALUES (
                        %(source_id)s, %(station_id)s, %(name)s, %(operator)s,
                        %(railway_line)s, %(prefecture_code)s,
                        ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326),
                        %(daily_passengers)s, %(passengers_year)s, %(attributes)s,
                        %(source_date)s, now()
                    )
                    ON CONFLICT (source_id, station_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        geom = EXCLUDED.geom,
                        daily_passengers = EXCLUDED.daily_passengers,
                        passengers_year = EXCLUDED.passengers_year,
                        attributes = EXCLUDED.attributes,
                        source_date = EXCLUDED.source_date,
                        last_updated = now()
                """,
                rows,
            )

            # S12 carries no prefecture. Rather than guess one, take it from
            # the nearest loaded mesh. Strict containment is not enough: the
            # census omits meshes with no residents, so a station in an office
            # district can sit in a gap and would be left unassigned. Stations
            # with no mesh within the cap keep NULL, which is the honest answer
            # for a nationwide file loaded against one prefecture's statistics.
            cur.execute(
                """
                UPDATE stations s
                SET prefecture_code = (
                    SELECT m.prefecture_code
                    FROM population_mesh m
                    WHERE ST_DWithin(m.geom::geography, s.geom::geography, %s)
                    ORDER BY m.geom::geography <-> s.geom::geography
                    LIMIT 1
                )
                WHERE s.source_id = %s AND s.prefecture_code IS NULL
                """,
                (self._prefecture_match_radius_m(), self.source_id),
            )
        return count


def _midpoint(shape: Any) -> tuple[float, float] | None:
    """Representative point of a station feature (line, point or polygon)."""
    points = list(getattr(shape, "points", []) or [])
    if not points:
        return None
    return (sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points))


def _spread_m(rows: list[dict[str, Any]]) -> float:
    """Greatest distance from the group's centre, in metres."""
    cx = sum(r["_lng"] for r in rows) / len(rows)
    cy = sum(r["_lat"] for r in rows) / len(rows)
    scale = 111_320 * math.cos(math.radians(cy))
    return max(math.hypot((r["_lng"] - cx) * scale, (r["_lat"] - cy) * 111_320)
               for r in rows)


def _distinct(rows: list[dict[str, Any]], column: str | None) -> list[str]:
    if not column:
        return []
    seen: dict[str, None] = {}
    for row in rows:
        value = str(row.get(column) or "").strip()
        if value:
            seen.setdefault(value, None)
    return list(seen)


def _summarize(values: list[str]) -> str | None:
    """One operator name, or the first plus a count of the rest."""
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return f"{values[0]} ほか{len(values) - 1}社"
