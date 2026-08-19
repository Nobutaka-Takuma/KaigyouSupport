"""e-Stat 統計GIS（国勢調査メッシュ統計） -> population_mesh.

The 統計GIS bulk download is a zip containing one or more CSVs keyed by mesh
code. Geometry is not shipped with it -- the mesh code *is* the geometry, so
polygons are derived locally from :mod:`kaigyou_core.mesh`. That also means the
mesh resolution comes from the data (code length) rather than from a constant.

``population_growth`` needs two census rounds. If ``growth_baseline`` is
configured and its artefact is available, the rate is computed per mesh;
otherwise growth is left NULL and the scoring layer reports it as
unavailable rather than assuming zero.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator

import psycopg

from kaigyou_core import mesh as meshlib
from kaigyou_etl.acquisition import (
    ERROR_EMPTY,
    ERROR_INPUT_MISSING,
    ERROR_SCHEMA,
    AcquisitionError,
)
from kaigyou_etl.adapters._util import (
    csv_rows,
    decode_bytes,
    is_zip,
    open_member,
    read_text,
    to_int,
    zip_members,
)
from kaigyou_etl.adapters.base import SourceAdapter


class EStatMeshAdapter(SourceAdapter):
    target_tables = ("population_mesh",)

    # ------------------------------------------------------------- artefacts
    def _csv_texts(self, artifact: Path, encoding: str | None = None) -> list[str]:
        encoding = encoding or self.spec.get("encoding")
        if is_zip(artifact):
            members = zip_members(artifact, (".csv", ".txt"))
            if not members:
                raise AcquisitionError(
                    ERROR_SCHEMA, f"{artifact.name} contains no CSV member"
                )
            return [decode_bytes(open_member(artifact, m), encoding) for m in members]
        return [read_text(artifact, encoding)]

    def _read(self, artifact: Path,
              encoding: str | None = None) -> tuple[list[str], list[dict[str, str]]]:
        rows: list[dict[str, str]] = []
        headers: list[str] = []
        for text in self._csv_texts(artifact, encoding):
            chunk = list(csv_rows(text))
            if chunk and not headers:
                headers = list(chunk[0].keys())
            rows.extend(chunk)
        if not rows:
            raise AcquisitionError(ERROR_EMPTY, f"{artifact.name} contains no data rows")
        return headers, rows

    # -------------------------------------------------------------- pipeline
    def validate(self, artifact: Path) -> dict[str, Any]:
        headers, rows = self._read(artifact)
        resolved = {
            field: self.pick_column(headers, field,
                                    required=field in ("mesh_code", "population"))
            for field in self.column_map()
        }
        code_col = resolved["mesh_code"]
        lengths: dict[int, int] = {}
        bad = 0
        # Everything below is reported for the rows that will actually load, so
        # the totals can be compared directly against the published headline
        # figures rather than being inflated by rows we are about to drop.
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
            "mesh_code_lengths": lengths,
            "mesh_size_m": sorted({meshlib.NOMINAL_SIZE_M[n] for n in lengths}),
            "loadable_rows": len(loadable),
            "invalid_mesh_codes": bad,
            "resolved_columns": {k: v for k, v in resolved.items() if v},
        }

        pop_col = resolved["population"]
        populations = [to_int(r.get(pop_col)) for r in loadable]
        facts["population_total"] = sum(p for p in populations if p is not None)
        facts["rows_without_population"] = sum(1 for p in populations if p is None)

        # Small cells are disclosure-controlled: their counts are merged into a
        # neighbouring mesh rather than published in place. The grand total is
        # preserved, so figures are loaded exactly as published -- reversing the
        # suppression would mean inventing numbers -- but the extent is recorded
        # because it displaces population by up to one mesh locally.
        supp = self.spec.get("suppression_columns") or {}
        flag_col = supp.get("flag")
        if flag_col and flag_col in headers:
            counts: dict[str, int] = {}
            for r in loadable:
                counts[(r.get(flag_col) or "").strip()] = (
                    counts.get((r.get(flag_col) or "").strip(), 0) + 1
                )
            facts["suppression_flag_counts"] = counts
            facts["suppressed_rows"] = sum(v for k, v in counts.items() if k not in ("", "0"))

        baseline, baseline_facts = self._baseline_population()
        facts.update(baseline_facts)
        if baseline:
            codes = {(r.get(code_col) or "").strip() for r in loadable}
            matched = codes & set(baseline)
            facts["baseline_matched_meshes"] = len(matched)
            facts["meshes_without_baseline"] = len(codes - set(baseline))

        return facts

    def _baseline_population(self) -> tuple[dict[str, int], dict[str, Any]]:
        """Population by mesh from the prior census round.

        Optional. Without it ``population_growth`` stays NULL, which the
        scoring layer reports as unavailable rather than assuming no change.

        The prior round is a different statistics table with its own column
        ids, so it is resolved through ``growth_baseline.columns`` rather than
        the current round's mapping.
        """
        baseline = self.spec.get("growth_baseline") or {}
        path = self.ctx.baseline_path or (
            Path(baseline["path"]) if baseline.get("path") else None
        )
        if not baseline or path is None:
            return {}, {"baseline_supplied": False}
        if not path.exists():
            raise AcquisitionError(
                ERROR_INPUT_MISSING, f"--baseline file does not exist: {path}"
            )

        headers, rows = self._read(path, encoding=baseline.get("encoding"))
        columns = {k: (v if isinstance(v, list) else [v])
                   for k, v in (baseline.get("columns") or {}).items()}
        code_col = self._pick(headers, columns, "mesh_code", path)
        pop_col = self._pick(headers, columns, "population", path)

        out: dict[str, int] = {}
        for row in rows:
            code = (row.get(code_col) or "").strip()
            pop = to_int(row.get(pop_col))
            if code and pop is not None:
                out[code] = pop
        if not out:
            raise AcquisitionError(
                ERROR_EMPTY, f"baseline {path.name} yielded no population values"
            )
        return out, {
            "baseline_supplied": True,
            "baseline_file": path.name,
            "baseline_rows": len(out),
            "baseline_columns": {"mesh_code": code_col, "population": pop_col},
        }

    def _pick(self, headers: list[str], columns: dict[str, list[str]],
              field: str, path: Path) -> str:
        lookup = {h.strip().lower(): h for h in headers}
        for candidate in columns.get(field, []):
            hit = lookup.get(str(candidate).strip().lower())
            if hit is not None:
                return hit
        raise AcquisitionError(
            ERROR_SCHEMA,
            f"{path.name}: no column for {field!r}; tried {columns.get(field)}, "
            f"file has {headers[:20]}",
        )

    def transform(self, artifact: Path) -> Iterator[dict[str, Any]]:
        headers, rows = self._read(artifact)
        col = {f: self.pick_column(headers, f,
                                   required=f in ("mesh_code", "population"))
               for f in self.column_map()}
        baseline, _ = self._baseline_population()
        years = float((self.spec.get("growth_baseline") or {}).get("years") or 5)
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

            population = to_int(row.get(col["population"]))
            prior = baseline.get(code)
            growth = None
            if prior and population is not None and prior > 0:
                # Total change across the comparison interval, not annualised.
                growth = (population - prior) / prior

            lng, lat = meshlib.centroid(code)
            yield {
                "mesh_code": code,
                "mesh_size_m": size,
                "prefecture_code": pref,
                "polygon_wkt": meshlib.to_polygon_wkt(code),
                "centroid_lng": lng,
                "centroid_lat": lat,
                "population": population,
                "age_0_14": _opt(row, col, "age_0_14"),
                "age_15_64": _opt(row, col, "age_15_64"),
                "age_65_plus": _opt(row, col, "age_65_plus"),
                "households": _opt(row, col, "households"),
                "population_growth": growth,
                "growth_years": years if growth is not None else None,
                "source_date": source_date,
            }

    def load(self, conn: psycopg.Connection, records: Iterable[dict[str, Any]]) -> int:
        count = 0
        with conn.cursor() as cur:
            cur.execute("DELETE FROM population_mesh WHERE source_id = %s", (self.source_id,))
            for rec in records:
                cur.execute(
                    """
                    INSERT INTO population_mesh (
                        source_id, mesh_code, mesh_size_m, prefecture_code, geom, centroid,
                        population, age_0_14, age_15_64, age_65_plus, households,
                        population_growth, age_breakdown, source_date, last_updated
                    ) VALUES (
                        %(source_id)s, %(mesh_code)s, %(mesh_size_m)s, %(prefecture_code)s,
                        ST_GeomFromText(%(polygon_wkt)s, 4326),
                        ST_SetSRID(ST_MakePoint(%(centroid_lng)s, %(centroid_lat)s), 4326),
                        %(population)s, %(age_0_14)s, %(age_15_64)s, %(age_65_plus)s,
                        %(households)s, %(population_growth)s, '{}'::jsonb,
                        %(source_date)s, now()
                    )
                    ON CONFLICT (source_id, mesh_code) DO UPDATE SET
                        population = EXCLUDED.population,
                        age_0_14 = EXCLUDED.age_0_14,
                        age_15_64 = EXCLUDED.age_15_64,
                        age_65_plus = EXCLUDED.age_65_plus,
                        households = EXCLUDED.households,
                        population_growth = EXCLUDED.population_growth,
                        source_date = EXCLUDED.source_date,
                        last_updated = now()
                    """,
                    {k: v for k, v in rec.items() if k != "growth_years"}
                    | {"source_id": self.source_id},
                )
                count += 1
        return count


def _opt(row: dict[str, str], col: dict[str, str | None], field: str) -> int | None:
    name = col.get(field)
    return to_int(row.get(name)) if name else None
