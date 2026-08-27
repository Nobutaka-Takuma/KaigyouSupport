"""国土数値情報 500mメッシュ別将来推計人口 -> mesh_population_projection.

なぜ要るか。総合スコアの 20% を占める「成長」が、2015→2020 の実績だけで
決まっていました。歯科の開業は 20〜30 年の意思決定なので、過去 5 年の増減は
これから 30 年の代理としてかなり弱い。過去は増えていても将来推計では減る
地域はいくらでもあります。

**属性名は設定に置きます。** 国土数値情報のメッシュ系ファイルは、推計の版が
変わると属性名も変わります（PTN_2025 / PT01_2025 / POP2025 …）。コードに
書くと、版が変わるたびにコードを直すことになり、しかも間違えても静かに
0 件になります。設定に置いておけば、版の変更は設定の変更で済みます。

**合わなければ、実際の属性名を並べて落とします。** 「0 件でした」で終わると、
その地域に人が住んでいないのか、列名が違うのかが区別できません。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator

import psycopg

from kaigyou_etl.acquisition import ERROR_EMPTY, ERROR_SCHEMA, AcquisitionError
from kaigyou_etl.adapters._util import read_shapefile
from kaigyou_etl.adapters.base import SourceAdapter

#: 推計値は按分の結果なので小数です。丸めません（丸めるのは表示の仕事）。
def _to_float(value: Any) -> float | None:
    text = str(value if value is not None else "").strip()
    if not text or text in ("_", "-", "*"):
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


class MLITFuturePopulationAdapter(SourceAdapter):
    target_tables = ("mesh_population_projection",)

    # ---------------------------------------------------------------- config
    def years(self) -> list[int]:
        raw = self.spec.get("years") or []
        return sorted(int(y) for y in raw)

    def year_fields(self) -> dict[str, str]:
        """論理名 -> 属性名の雛形（``{year}`` を含む）。"""
        return {str(k): str(v) for k, v in (self.spec.get("year_fields") or {}).items()}

    def mesh_size_m(self) -> int:
        return int(self.spec.get("mesh_size_m", 500))

    # -------------------------------------------------------------- pipeline
    def _rows(self, artifact: Path):
        fields, records = read_shapefile(
            artifact, member_prefix=self.spec.get("shapefile_member"),
            encoding=self.spec.get("encoding") or "cp932")
        if not records:
            raise AcquisitionError(ERROR_EMPTY,
                                   f"{artifact.name}: shapefile has no records")
        return fields, records

    def _resolve(self, fields: list[str]) -> tuple[str, dict[int, dict[str, str]]]:
        """メッシュコードの列と、年ごとの属性名を実ファイルに突き合わせる。"""
        mesh_col = self.pick_column(fields, "mesh_code", required=True)

        lookup = {f.strip().lower(): f for f in fields}
        resolved: dict[int, dict[str, str]] = {}
        for year in self.years():
            found: dict[str, str] = {}
            for logical, template in self.year_fields().items():
                name = template.format(year=year)
                hit = lookup.get(name.strip().lower())
                if hit is not None:
                    found[logical] = hit
            # 総人口が取れない年は、その年ごと落とします。年齢内訳だけ
            # あっても、商圏人口としては使えません。
            if "population" in found:
                resolved[year] = found

        if not resolved:
            wanted = [t.format(year=y) for y in self.years()
                      for t in self.year_fields().values()]
            raise AcquisitionError(
                ERROR_SCHEMA,
                f"{self.source_id}: 将来推計人口の属性が1つも見つかりません。"
                f"探したもの: {wanted[:12]}… / "
                f"ファイルにある属性: {list(fields)[:40]} — "
                "config/sources.yaml の year_fields と years をこの版に合わせてください")
        return mesh_col, resolved

    def validate(self, artifact: Path) -> dict[str, Any]:
        fields, records = self._rows(artifact)
        mesh_col, resolved = self._resolve(list(fields))

        totals: dict[int, float] = {}
        meshes = 0
        for record in records:
            values = dict(zip(fields, record.record))
            if not str(values.get(mesh_col) or "").strip():
                continue
            meshes += 1
            for year, cols in resolved.items():
                value = _to_float(values.get(cols["population"]))
                if value is not None:
                    totals[year] = totals.get(year, 0.0) + value

        if not totals:
            raise AcquisitionError(
                ERROR_SCHEMA,
                f"{artifact.name}: 属性は見つかりましたが値が全て空です。"
                f"列 {sorted({c['population'] for c in resolved.values()})} を確認してください")

        ordered = sorted(totals.items())
        first_year, first_total = ordered[0]
        last_year, last_total = ordered[-1]
        return {
            "mesh_count": meshes,
            "mesh_size_m": self.mesh_size_m(),
            "prefecture_code": self.ctx.prefecture_code,
            "years_loaded": [y for y, _ in ordered],
            "years_configured_but_missing": sorted(set(self.years()) - set(resolved)),
            "population_total_by_year": {y: round(v) for y, v in ordered},
            # 都道府県の総人口が桁違いなら、列の取り違えです。人口の推計が
            # 1万人未満や2億人超になる都道府県はありません。
            "change_first_to_last": (None if not first_total
                                     else round(last_total / first_total, 3)),
            "note": f"{first_year} 年 {first_total:,.0f} 人 → "
                    f"{last_year} 年 {last_total:,.0f} 人",
        }

    def transform(self, artifact: Path) -> Iterator[dict[str, Any]]:
        fields, records = self._rows(artifact)
        mesh_col, resolved = self._resolve(list(fields))
        size = self.mesh_size_m()
        base_year = self.spec.get("base_year")
        label = self.spec.get("estimate_label")
        when = self.spec.get("source_date")
        source_date = date.fromisoformat(when) if isinstance(when, str) else when

        for record in records:
            values = dict(zip(fields, record.record))
            mesh_code = str(values.get(mesh_col) or "").strip()
            if not mesh_code:
                continue
            for year, cols in resolved.items():
                population = _to_float(values.get(cols["population"]))
                if population is None:
                    continue
                yield {
                    "source_id": self.source_id,
                    "mesh_code": mesh_code,
                    "mesh_size_m": size,
                    "prefecture_code": self.ctx.prefecture_code,
                    "projection_year": year,
                    "population": population,
                    "age_0_14": _to_float(values.get(cols.get("age_0_14", ""))),
                    "age_15_64": _to_float(values.get(cols.get("age_15_64", ""))),
                    "age_65_plus": _to_float(values.get(cols.get("age_65_plus", ""))),
                    "age_75_plus": _to_float(values.get(cols.get("age_75_plus", ""))),
                    "base_year": int(base_year) if base_year else None,
                    "estimate_label": label,
                    "source_date": source_date,
                }

    def load(self, conn: psycopg.Connection,
             records: Iterable[dict[str, Any]]) -> int:
        rows = list(records)
        with conn.cursor() as cur:
            # **都道府県で絞って置換します。** source_id だけで消すと、
            # 静岡を入れたあとに東京を入れた時点で静岡が消えます（実測：
            # 127,985 行が 59,917 行になりました）。1ファイル=1都道府県で
            # 配布されるので、置換の単位も都道府県です。estat_mesh も同じ形。
            cur.execute("DELETE FROM mesh_population_projection "
                        "WHERE source_id = %s AND prefecture_code = %s",
                        (self.source_id, self.ctx.prefecture_code))
            return self.insert_many(
                cur,
                """
                INSERT INTO mesh_population_projection (
                    source_id, mesh_code, mesh_size_m, prefecture_code,
                    projection_year, population, age_0_14, age_15_64, age_65_plus,
                    age_75_plus, base_year, estimate_label, source_date, last_updated
                ) VALUES (
                    %(source_id)s, %(mesh_code)s, %(mesh_size_m)s, %(prefecture_code)s,
                    %(projection_year)s, %(population)s, %(age_0_14)s, %(age_15_64)s,
                    %(age_65_plus)s, %(age_75_plus)s, %(base_year)s, %(estimate_label)s,
                    %(source_date)s, now()
                )
                ON CONFLICT (source_id, mesh_code, projection_year) DO UPDATE SET
                    population   = EXCLUDED.population,
                    age_0_14     = EXCLUDED.age_0_14,
                    age_15_64    = EXCLUDED.age_15_64,
                    age_65_plus  = EXCLUDED.age_65_plus,
                    age_75_plus  = EXCLUDED.age_75_plus,
                    last_updated = now()
                """,
                rows,
            )
