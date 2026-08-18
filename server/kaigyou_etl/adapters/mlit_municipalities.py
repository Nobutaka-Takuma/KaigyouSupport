"""国土数値情報「行政区域」(N03) -> municipalities.

N03 publishes one polygon per administrative part, so a ward or city with
detached areas appears several times. Parts sharing a JIS code are merged into
one MultiPolygon by PostGIS on load (ST_Union), keeping one row per
municipality.
"""
from __future__ import annotations

from collections import defaultdict
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

    def validate(self, artifact: Path) -> dict[str, Any]:
        fields, records = self._read(artifact)
        code_col = self.pick_column(fields, "municipality_code")
        codes = {str(dict(zip(fields, r.record)).get(code_col) or "").strip()
                 for r in records}
        codes.discard("")
        if not codes:
            raise AcquisitionError(
                ERROR_EMPTY, f"no municipality codes in column {code_col!r}"
            )
        return {
            "feature_count": len(records),
            "municipality_count": len(codes),
            "fields": fields,
        }

    def transform(self, artifact: Path) -> Iterator[dict[str, Any]]:
        fields, records = self._read(artifact)
        code_col = self.pick_column(fields, "municipality_code")
        name_col = self.pick_column(fields, "municipality_name")
        pref_col = self.pick_column(fields, "prefecture_name", required=False)
        source_date = self.source_date() or date.today()
        pref = self.ctx.prefecture_code

        grouped: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"parts": [], "name": None, "prefecture_name": None}
        )
        for rec in records:
            row = dict(zip(fields, rec.record))
            code = str(row.get(code_col) or "").strip()
            if not code:
                continue
            wkt = shape_to_wkt(rec.shape)
            if wkt is None:
                continue
            entry = grouped[code]
            entry["parts"].append(wkt)
            entry["name"] = entry["name"] or (str(row.get(name_col) or "").strip() or None)
            if pref_col:
                entry["prefecture_name"] = entry["prefecture_name"] or (
                    str(row.get(pref_col) or "").strip() or None
                )

        for code, entry in grouped.items():
            if not entry["name"]:
                continue
            yield {
                "municipality_code": code,
                "name": entry["name"],
                "prefecture_code": code[:2] if len(code) >= 2 else pref,
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
                        ST_Multi(ST_CollectionExtract(
                            ST_MakeValid(ST_UnaryUnion(ST_Collect(
                                (SELECT array_agg(ST_GeomFromText(w, 4326))
                                   FROM unnest(%(parts)s::text[]) AS w)
                            ))), 3)),
                        %(source_date)s, now()
                    )
                    ON CONFLICT (source_id, municipality_code) DO UPDATE SET
                        name = EXCLUDED.name,
                        geom = EXCLUDED.geom,
                        source_date = EXCLUDED.source_date,
                        last_updated = now()
                    """,
                    {**rec, "source_id": self.source_id},
                )
                count += 1
        return count
