"""e-Stat 統計GIS（国勢調査 就業状態等基本集計 メッシュ） -> mesh_resident_profile.

**昼間人口ではありません。** この集計は常住地基準です。実測（東京都・
T001108）：大学・大学院在学者がいちばん多いメッシュでも 835 人、早稲田駅の
メッシュで 393 人。通学地基準ならキャンパスのメッシュに数万人が出るはずで、
出ていません。「そこに住んでいる学生」であって「そこに通ってくる学生」では
ありません。

では何のために取り込むのか。**歯科の判断を変えるものが 3 つあるからです。**

- **利用交通手段** … 駐車場が要るかどうかの代理。これまで「データが無いので
  現地で確認」としか書けませんでした
- **居住期間** … かかりつけとリコールが回る街かどうか。年齢構成からは
  分かりません
- **未就学者の内訳** … 0〜14歳より小児歯科の需要に近い

読み方は他の 統計GIS 表と同じなので ``EStatTableReader`` をそのまま使います。
違うのは列の意味だけです。列 ID は設定に置きます（版が変われば動くので）。
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

#: 入れる列。``mesh_code`` 以外はすべて任意です。取れなければ NULL のまま
#: にします。**0 で埋めません**——「自家用車が 0 人」と「自家用車が分からない」
#: は別のことで、混ぜるとレポートが「車は使われていない」と書きます。
_FIELDS = (
    "commute_walk", "commute_rail", "commute_bus", "commute_car",
    "commute_motorcycle", "commute_bicycle",
    "resident_under_1y", "resident_1_to_5y", "resident_20y_plus",
    "preschool_total", "preschool_nursery",
    "students_high_school", "students_university",
    "workers_living_here", "students_living_here",
    "employees_regular", "employees_part_time", "self_employed",
)


class EStatResidentProfileAdapter(EStatTableReader, SourceAdapter):
    target_tables = ("mesh_resident_profile",)

    # -------------------------------------------------------------- pipeline
    def validate(self, artifact: Path) -> dict[str, Any]:
        # ファイル名だけが「どの都道府県か」を言っています。取り違えると、
        # 別の都道府県をこの数字で上書きします。
        named_prefecture = self.check_prefecture_matches_filename(artifact)
        headers, rows = self._read(artifact)
        resolved = {field: self.pick_column(headers, field,
                                            required=field == "mesh_code")
                    for field in self.column_map()}
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

        def total(field: str) -> int | None:
            column = resolved.get(field)
            if not column:
                return None
            values = [to_int(r.get(column)) for r in loadable]
            return sum(v for v in values if v is not None)

        facts: dict[str, Any] = {
            "row_count": len(rows),
            "loadable_rows": len(loadable),
            "invalid_mesh_codes": bad,
            "mesh_code_lengths": lengths,
            "mesh_size_m": sorted({meshlib.NOMINAL_SIZE_M[n] for n in lengths}),
            "prefecture_code": self.ctx.prefecture_code,
            "prefecture_from_filename": named_prefecture,
            "totals": {f: total(f) for f in _FIELDS if resolved.get(f)},
        }
        # 取れなかった列は名前を挙げて残します。**黙って落とすと、次に読む人が
        # 「自家用車は0人だった」と読みます。**
        facts["columns_not_in_file"] = [f for f in _FIELDS if not resolved.get(f)]

        if not any(facts["totals"].values()):
            raise AcquisitionError(
                ERROR_EMPTY,
                f"{artifact.name}: 取り込む列がどれも 0 か、1つも見つかりません"
                f"でした。config/sources.yaml の列 ID がこの版に合っているか"
                f"確認してください。ファイルにある列: {list(headers)[:40]}")

        # **常住地基準であることの確認。** 通学地基準の表を間違えて入れると、
        # 大学生が1メッシュに数万人という形で現れます。常住なら、いちばん多い
        # メッシュでも千人の桁です（実測：東京都で最大 835 人）。
        if resolved.get("students_university"):
            peak = max((to_int(r.get(resolved["students_university"])) or 0)
                       for r in loadable)
            facts["students_university_peak_mesh"] = peak
            if peak > 10_000:
                raise AcquisitionError(
                    ERROR_SCHEMA,
                    f"{artifact.name}: 1メッシュに大学・大学院在学者が "
                    f"{peak:,} 人います。この取り込みは**常住地基準**の表を"
                    "前提にしています。通学地基準（昼間人口）の表を指して"
                    "いる可能性があります")
        return facts

    def transform(self, artifact: Path) -> Iterator[dict[str, Any]]:
        headers, rows = self._read(artifact)
        col = {f: self.pick_column(headers, f, required=f == "mesh_code")
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
            record: dict[str, Any] = {
                "mesh_code": code,
                "mesh_size_m": size,
                "prefecture_code": pref,
                "source_date": source_date,
            }
            for field in _FIELDS:
                record[field] = (to_int(row.get(col[field]))
                                 if col.get(field) else None)
            yield record

    def load(self, conn: psycopg.Connection,
             records: Iterable[dict[str, Any]]) -> int:
        rows = [rec | {"source_id": self.source_id} for rec in records]
        columns = ", ".join(_FIELDS)
        placeholders = ", ".join(f"%({f})s" for f in _FIELDS)
        updates = ", ".join(f"{f} = EXCLUDED.{f}" for f in _FIELDS)
        with conn.cursor() as cur:
            # **都道府県で絞って置換します。** source_id だけで消すと、静岡を
            # 入れたあとに東京を入れた時点で静岡が消えます（将来推計人口の
            # 取り込みで実際にそうなりました）。
            cur.execute("DELETE FROM mesh_resident_profile "
                        "WHERE source_id = %s AND prefecture_code = %s",
                        (self.source_id, self.ctx.prefecture_code))
            return self.insert_many(
                cur,
                f"""
                INSERT INTO mesh_resident_profile (
                    source_id, mesh_code, mesh_size_m, prefecture_code,
                    {columns}, source_date, last_updated
                ) VALUES (
                    %(source_id)s, %(mesh_code)s, %(mesh_size_m)s,
                    %(prefecture_code)s, {placeholders}, %(source_date)s, now()
                )
                ON CONFLICT (source_id, mesh_code) DO UPDATE SET
                    {updates},
                    source_date = EXCLUDED.source_date,
                    last_updated = now()
                """,
                rows,
            )
