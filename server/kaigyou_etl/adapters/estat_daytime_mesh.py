"""e-Stat 統計GIS（国勢調査 従業地・通学地メッシュ） -> mesh_daytime_population.

**なぜ要るか。** 「昼間そこにいる人」を経済センサスの**従業者数**だけで
測っていました。従業者は昼間人口の一部でしかなく、**通学者が丸ごと落ちます**。

実測：早稲田駅前（半径1km）のレポートは、従業者数 52,688 人を昼間人口の
代理として使い、大学生に一言も触れませんでした。早稲田大学の学生は従業者
ではないので、経済センサスには 1 人も現れません。歯科医院にとって、20代
前半の数万人がそこにいるかどうかは、診療内容も診療時間も変える情報です。

出典は令和2年国勢調査の地域メッシュ統計「人口移動、就業状態等及び
従業地・通学地」（2022年12月13日公表）。読み方は他の 統計GIS 表と同じなので、
``EStatTableReader`` をそのまま使います。違うのは列の意味だけです。

**列 ID は設定に置きます。** 版が変わると列 ID が変わり、コードに書くと
そのたびにコードを直すことになります。しかも間違えても静かに 0 件になる。
設定に置けば、版の変更は設定の変更で済みます。合わなければ、実際に
ファイルにある列を並べて落とします。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator

import psycopg

from kaigyou_core import mesh as meshlib
from kaigyou_etl.acquisition import ERROR_EMPTY, ERROR_SCHEMA, AcquisitionError
from kaigyou_etl.adapters._util import to_int
from kaigyou_etl.adapters.base import SourceAdapter
from kaigyou_etl.adapters.estat_mesh import EStatTableReader

#: 取り込む列。``daytime_population`` だけが必須です。内訳（就業者・通学者）は
#: 版によって公表の粒度が違うので、取れなければ NULL のままにします。
#: **0 で埋めません。**「通学者が 0 人」と「通学者が分からない」は別のことです。
_OPTIONAL = ("workers_here", "students_here", "night_population")


class EStatDaytimeMeshAdapter(EStatTableReader, SourceAdapter):
    target_tables = ("mesh_daytime_population",)

    # -------------------------------------------------------------- pipeline
    def validate(self, artifact: Path) -> dict[str, Any]:
        # ファイル名だけが「どの都道府県か」を言っています。取り違えると、
        # 別の都道府県をこの数字で上書きします。
        named_prefecture = self.check_prefecture_matches_filename(artifact)
        headers, rows = self._read(artifact)
        resolved = {
            field: self.pick_column(
                headers, field,
                required=field in ("mesh_code", "daytime_population"))
            for field in self.column_map()
        }
        code_col = resolved["mesh_code"]

        lengths: dict[int, int] = {}
        loadable: list[dict[str, str]] = []
        bad = 0
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
                f"(sample: {[r.get(code_col) for r in rows[:3]]})")

        facts: dict[str, Any] = {
            "row_count": len(rows),
            "loadable_rows": len(loadable),
            "invalid_mesh_codes": bad,
            "mesh_code_lengths": lengths,
            "mesh_size_m": sorted({meshlib.NOMINAL_SIZE_M[n] for n in lengths}),
            "resolved_columns": {k: v for k, v in resolved.items() if v},
            "prefecture_code": self.ctx.prefecture_code,
            "prefecture_from_filename": named_prefecture,
        }

        def total(field: str) -> int | None:
            column = resolved.get(field)
            if not column:
                return None
            values = [to_int(r.get(column)) for r in loadable]
            return sum(v for v in values if v is not None)

        facts["daytime_total"] = total("daytime_population")
        if not facts["daytime_total"]:
            raise AcquisitionError(
                ERROR_EMPTY,
                f"{artifact.name}: 昼間人口の列 "
                f"{resolved['daytime_population']!r} がどのメッシュでも 0 です。"
                f"config/sources.yaml の列 ID がこの版に合っているか確認して"
                f"ください。ファイルにある列: {list(headers)[:40]}")

        for field in _OPTIONAL:
            facts[f"{field}_total"] = total(field)
        # 取れなかった内訳は、名前を挙げて残します。**黙って落とすと、次に
        # 読む人が「通学者は 0 だった」と読みます。**
        facts["columns_not_in_file"] = [f for f in _OPTIONAL if not resolved.get(f)]

        # 内訳が総数を超えたら、列の取り違えです。就業者と通学者を足したものが
        # 昼間人口を超えることはありません（両方とも昼間人口の内数）。
        parts = [facts.get("workers_here_total"), facts.get("students_here_total")]
        counted = sum(p for p in parts if p)
        if counted > facts["daytime_total"]:
            raise AcquisitionError(
                ERROR_SCHEMA,
                f"{artifact.name}: 就業者+通学者 {counted:,} が昼間人口 "
                f"{facts['daytime_total']:,} を超えています。列の対応が"
                "ずれている可能性があります（sources.yaml の columns を確認）")
        return facts

    def transform(self, artifact: Path) -> Iterator[dict[str, Any]]:
        headers, rows = self._read(artifact)
        col = {f: self.pick_column(
            headers, f, required=f in ("mesh_code", "daytime_population"))
            for f in self.column_map()}
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
            record = {
                "mesh_code": code,
                "mesh_size_m": size,
                "prefecture_code": pref,
                "daytime_population": to_int(row.get(col["daytime_population"])),
                "source_date": source_date,
            }
            for field in _OPTIONAL:
                record[field] = (to_int(row.get(col[field]))
                                 if col.get(field) else None)
            yield record

    def load(self, conn: psycopg.Connection,
             records: Iterable[dict[str, Any]]) -> int:
        rows = [rec | {"source_id": self.source_id} for rec in records]
        with conn.cursor() as cur:
            # **都道府県で絞って置換します。** source_id だけで消すと、静岡を
            # 入れたあとに東京を入れた時点で静岡が消えます（将来推計人口の
            # 取り込みで実際にそうなりました：127,985 行が 59,917 行に）。
            cur.execute("DELETE FROM mesh_daytime_population "
                        "WHERE source_id = %s AND prefecture_code = %s",
                        (self.source_id, self.ctx.prefecture_code))
            return self.insert_many(
                cur,
                """
                INSERT INTO mesh_daytime_population (
                    source_id, mesh_code, mesh_size_m, prefecture_code,
                    daytime_population, workers_here, students_here,
                    night_population, source_date, last_updated
                ) VALUES (
                    %(source_id)s, %(mesh_code)s, %(mesh_size_m)s,
                    %(prefecture_code)s, %(daytime_population)s, %(workers_here)s,
                    %(students_here)s, %(night_population)s, %(source_date)s, now()
                )
                ON CONFLICT (source_id, mesh_code) DO UPDATE SET
                    daytime_population = EXCLUDED.daytime_population,
                    workers_here       = EXCLUDED.workers_here,
                    students_here      = EXCLUDED.students_here,
                    night_population   = EXCLUDED.night_population,
                    source_date        = EXCLUDED.source_date,
                    last_updated       = now()
                """,
                rows,
            )
