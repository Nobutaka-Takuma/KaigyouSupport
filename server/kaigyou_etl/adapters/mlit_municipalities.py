"""国土数値情報「行政区域」(N03) -> municipalities.

N03 publishes one polygon per administrative *part*, so a municipality with
islands or exclaves appears many times over -- Ogasawara alone is 4,812 parts
and 234k vertices. Parts sharing a JIS code are collected into one MultiPolygon
so the table keeps one row per municipality.

The parts of a municipality are disjoint by construction, so they are collected
rather than unioned: ST_UnaryUnion over five thousand island polygons is
expensive and would not change the result of a point-in-polygon test.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator

import psycopg

from kaigyou_etl.acquisition import ERROR_EMPTY, AcquisitionError
from kaigyou_etl.adapters._util import read_shapefile, shape_to_wkt
from kaigyou_etl.adapters.base import SourceAdapter


class MLITMunicipalitiesAdapter(SourceAdapter):
    target_tables = ("municipalities",)

    def _read(self, artifact: Path) -> tuple[list[str], list[Any]]:
        fields, records = read_shapefile(artifact)
        if not records:
            raise AcquisitionError(ERROR_EMPTY, f"{artifact.name} contains no features")
        return fields, records

    def _excluded(self, code: str) -> bool:
        """True for the prefecture-level catch-all rather than a municipality."""
        suffixes = self.spec.get("exclude_code_suffixes") or []
        return any(code.endswith(str(sfx)) for sfx in suffixes)

    def validate(self, artifact: Path) -> dict[str, Any]:
        fields, records = self._read(artifact)
        code_col = self.pick_column(fields, "municipality_code")
        name_col = self.pick_column(fields, "municipality_name")

        codes = Counter()
        excluded = Counter()
        vertices = 0
        for rec in records:
            row = dict(zip(fields, rec.record))
            code = str(row.get(code_col) or "").strip()
            if not code:
                continue
            vertices += len(getattr(rec.shape, "points", []) or [])
            if self._excluded(code):
                excluded[f"{code} {str(row.get(name_col) or '').strip()}"] += 1
            else:
                codes[code] += 1

        if not codes:
            raise AcquisitionError(
                ERROR_EMPTY,
                f"no municipality codes in column {code_col!r} after excluding "
                f"{self.spec.get('exclude_code_suffixes')}",
            )

        return {
            "feature_count": len(records),
            "municipality_count": len(codes),
            "vertex_count": vertices,
            "excluded_features": dict(excluded),
            "most_fragmented": dict(codes.most_common(3)),
            "resolved_columns": {"municipality_code": code_col,
                                 "municipality_name": name_col},
        }

    def transform(self, artifact: Path) -> Iterator[dict[str, Any]]:
        fields, records = self._read(artifact)
        code_col = self.pick_column(fields, "municipality_code")
        name_col = self.pick_column(fields, "municipality_name")
        pref_col = self.pick_column(fields, "prefecture_name", required=False)
        district_col = self.pick_column(fields, "district_name", required=False)
        source_date = self.source_date() or date.today()
        fallback_pref = self.ctx.prefecture_code
        wanted = self.spec.get("prefecture_filter") or self.ctx.prefecture_filter

        grouped: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"parts": [], "names": Counter(), "prefecture_name": None,
                     "district": None}
        )
        for rec in records:
            row = dict(zip(fields, rec.record))
            code = str(row.get(code_col) or "").strip()
            if not code or self._excluded(code):
                continue
            wkt = shape_to_wkt(rec.shape)
            if wkt is None:
                continue
            entry = grouped[code]
            entry["parts"].append(wkt)
            name = str(row.get(name_col) or "").strip()
            if name:
                entry["names"][name] += 1
            if pref_col and not entry["prefecture_name"]:
                entry["prefecture_name"] = str(row.get(pref_col) or "").strip() or None
            # The district is filled in on only a handful of rows, so take it
            # from whichever part carries it.
            if district_col and not entry["district"]:
                entry["district"] = str(row.get(district_col) or "").strip() or None

        for code, entry in grouped.items():
            if not entry["names"]:
                continue
            prefecture_code = code[:2] if len(code) >= 2 else fallback_pref
            if wanted and prefecture_code != str(wanted):
                continue
            yield {
                "municipality_code": code,
                # The modal spelling; a stray variant on one island loses.
                "name": entry["names"].most_common(1)[0][0],
                "prefecture_code": prefecture_code,
                "prefecture_name": entry["prefecture_name"],
                "parts": entry["parts"],
                "source_date": source_date,
            }

    def load(self, conn: psycopg.Connection, records: Iterable[dict[str, Any]]) -> int:
        count = 0
        with conn.cursor() as cur:
            cur.execute("DELETE FROM municipalities WHERE source_id = %s", (self.source_id,))
            for rec in records:
                cur.execute(
                    """
                    INSERT INTO municipalities (
                        source_id, municipality_code, name, prefecture_code,
                        prefecture_name, geom, source_date, last_updated
                    ) VALUES (
                        %(source_id)s, %(municipality_code)s, %(name)s,
                        %(prefecture_code)s, %(prefecture_name)s,
                        (
                            SELECT ST_Multi(ST_CollectionExtract(
                                ST_Collect(ST_MakeValid(ST_GeomFromText(w, 4326))), 3))
                            FROM unnest(%(parts)s::text[]) AS w
                        ),
                        %(source_date)s, now()
                    )
                    ON CONFLICT (source_id, municipality_code) DO UPDATE SET
                        name = EXCLUDED.name,
                        geom = EXCLUDED.geom,
                        source_date = EXCLUDED.source_date,
                        last_updated = now()
                    """,
                    rec | {"source_id": self.source_id},
                )
                count += 1
        return count
