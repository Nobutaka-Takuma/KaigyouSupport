"""e-Stat 統計GIS（経済センサス メッシュ統計） -> mesh_business.

Where the residential census answers "who sleeps here", this answers "who works
here" -- the half of demand the night-time population cannot see. Tokyo has 286
meshes with establishments and no residents at all; the business districts a
dental practice might open in are exactly those.

The published table repeats the same industry labels twice: the first block
counts establishments, the second counts the people working in them. Nothing in
the file says so, so which block is which comes from ``config/sources.yaml``
rather than from a guess made here.

What this is NOT: 昼間人口. Daytime population is residents, minus those who
commute out, plus everyone who commutes in -- students and visitors included.
Workers at their place of work are the largest part of that inflow and the part
published per mesh, but they are a proxy for it, not a measurement of it. The
API says so wherever the number is shown.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator

import psycopg
from psycopg.types.json import Json

from kaigyou_core import mesh as meshlib
from kaigyou_etl.acquisition import ERROR_EMPTY, ERROR_SCHEMA, AcquisitionError
from kaigyou_etl.adapters._util import to_int
from kaigyou_etl.adapters.base import SourceAdapter
from kaigyou_etl.adapters.estat_mesh import EStatTableReader


class EStatBusinessMeshAdapter(EStatTableReader, SourceAdapter):
    target_tables = ("mesh_business",)

    # ---------------------------------------------------------------- config
    def industry_columns(self) -> dict[str, dict[str, str]]:
        """``{division: {"establishments": col, "workers": col}}`` from config."""
        out: dict[str, dict[str, str]] = {}
        for division, pair in (self.spec.get("industry_columns") or {}).items():
            if isinstance(pair, dict):
                out[division] = {k: str(v) for k, v in pair.items()}
            else:  # [establishments, workers]
                cols = list(pair)
                out[division] = {"establishments": str(cols[0]), "workers": str(cols[1])}
        return out

    # -------------------------------------------------------------- pipeline
    def validate(self, artifact: Path) -> dict[str, Any]:
        headers, rows = self._read(artifact)
        resolved = {
            field: self.pick_column(headers, field,
                                    required=field in ("mesh_code", "workers"))
            for field in self.column_map()
        }
        code_col = resolved["mesh_code"]

        lengths: dict[int, int] = {}
        bad = 0
        loadable: list[dict[str, str]] = []
        for row in rows:
            code = (row.get(code_col) or "").strip()
            try:
                meshlib.size_m(code)
            except meshlib.MeshCodeError:
                bad += 1
                continue
            lengths[len(code)] = lengths.get(len(code), 0) + 1
            loadable.append(row)
        if not lengths:
            raise AcquisitionError(
                ERROR_SCHEMA,
                f"no valid JIS mesh codes in column {code_col!r} "
                f"(sample: {[r.get(code_col) for r in rows[:3]]})",
            )

        facts: dict[str, Any] = {
            "row_count": len(rows),
            "loadable_rows": len(loadable),
            "invalid_mesh_codes": bad,
            "mesh_code_lengths": lengths,
            "mesh_size_m": sorted({meshlib.NOMINAL_SIZE_M[n] for n in lengths}),
            "resolved_columns": {k: v for k, v in resolved.items() if v},
        }

        # Totals for the rows that will actually load, so they can be compared
        # against the publisher's headline figures without adjustment.
        workers = [to_int(r.get(resolved["workers"])) for r in loadable]
        facts["workers_total"] = sum(w for w in workers if w is not None)
        facts["rows_without_workers"] = sum(1 for w in workers if w is None)
        if resolved.get("establishments"):
            counts = [to_int(r.get(resolved["establishments"])) for r in loadable]
            facts["establishments_total"] = sum(c for c in counts if c is not None)

        if not facts["workers_total"]:
            raise AcquisitionError(
                ERROR_EMPTY,
                f"{artifact.name}: worker column {resolved['workers']!r} is zero "
                "for every mesh; check that the column IDs in sources.yaml match "
                "this release",
            )

        # Establishments and workers are published side by side, and reading the
        # two blocks the wrong way round is the mistake this file invites: the
        # labels are identical and only the column number differs. Workers per
        # establishment is the tell -- around 15 in Tokyo, and certainly not
        # below 1. Recorded so a future release that reorders the columns is
        # caught by the number rather than by someone noticing the map is odd.
        if facts.get("establishments_total"):
            facts["workers_per_establishment"] = round(
                facts["workers_total"] / facts["establishments_total"], 2)
            if facts["workers_per_establishment"] < 1:
                raise AcquisitionError(
                    ERROR_SCHEMA,
                    f"{artifact.name}: {facts['workers_total']:,} workers across "
                    f"{facts['establishments_total']:,} establishments is less than "
                    "one person each -- the establishment and worker columns are "
                    "most likely swapped in sources.yaml",
                )

        missing = [d for d, cols in self.industry_columns().items()
                   if cols.get("workers") not in headers]
        if missing:
            facts["industries_not_in_file"] = missing
        return facts

    def transform(self, artifact: Path) -> Iterator[dict[str, Any]]:
        headers, rows = self._read(artifact)
        col = {f: self.pick_column(headers, f,
                                   required=f in ("mesh_code", "workers"))
               for f in self.column_map()}
        industries = {
            division: cols for division, cols in self.industry_columns().items()
            if cols.get("workers") in headers
        }
        source_date = self.source_date() or date.today()
        pref = self.ctx.prefecture_code
        seen: set[str] = set()

        for row in rows:
            code = (row.get(col["mesh_code"]) or "").strip()
            if not code or code in seen:
                continue
            try:
                size = meshlib.size_m(code)
            except meshlib.MeshCodeError:
                continue
            seen.add(code)

            lng, lat = meshlib.centroid(code)
            by_industry: dict[str, dict[str, int]] = {}
            for division, cols in industries.items():
                workers = to_int(row.get(cols["workers"]))
                establishments = to_int(row.get(cols.get("establishments", "")))
                if workers or establishments:
                    by_industry[division] = {
                        "workers": workers or 0,
                        "establishments": establishments or 0,
                    }

            yield {
                "mesh_code": code,
                "mesh_size_m": size,
                "prefecture_code": pref,
                "polygon_wkt": meshlib.to_polygon_wkt(code),
                "centroid_lng": lng,
                "centroid_lat": lat,
                "workers": to_int(row.get(col["workers"])),
                "establishments": (to_int(row.get(col["establishments"]))
                                   if col.get("establishments") else None),
                "industry_workers": {k: v["workers"] for k, v in by_industry.items()},
                "industry_establishments": {
                    k: v["establishments"] for k, v in by_industry.items()},
                "source_date": source_date,
            }

    def load(self, conn: psycopg.Connection, records: Iterable[dict[str, Any]]) -> int:
        rows = [
            rec | {
                "source_id": self.source_id,
                "industry_workers": Json(rec.get("industry_workers") or {}),
                "industry_establishments": Json(rec.get("industry_establishments") or {}),
            }
            for rec in records
        ]
        with conn.cursor() as cur:
            cur.execute("DELETE FROM mesh_business WHERE source_id = %s", (self.source_id,))
            return self.insert_many(
                cur,
                """
                    INSERT INTO mesh_business (
                        source_id, mesh_code, mesh_size_m, prefecture_code, geom, centroid,
                        workers, establishments, industry_workers, industry_establishments,
                        source_date, last_updated
                    ) VALUES (
                        %(source_id)s, %(mesh_code)s, %(mesh_size_m)s, %(prefecture_code)s,
                        ST_GeomFromText(%(polygon_wkt)s, 4326),
                        ST_SetSRID(ST_MakePoint(%(centroid_lng)s, %(centroid_lat)s), 4326),
                        %(workers)s, %(establishments)s,
                        %(industry_workers)s, %(industry_establishments)s,
                        %(source_date)s, now()
                    )
                    ON CONFLICT (source_id, mesh_code) DO UPDATE SET
                        workers = EXCLUDED.workers,
                        establishments = EXCLUDED.establishments,
                        industry_workers = EXCLUDED.industry_workers,
                        industry_establishments = EXCLUDED.industry_establishments,
                        source_date = EXCLUDED.source_date,
                        last_updated = now()
                """,
                rows,
            )
