"""e-Stat 統計GIS（国勢調査 就業状態等基本集計 メッシュ） -> mesh_resident_profile.

**昼間人口ではありません。** この集計は常住地基準です。データセットの表題は
「人口移動、就業状態等及び従業地・通学地」ですが、これは3つの集計をまとめた
**ひとくくりの名前**で、T001108 はそのうちの就業状態等基本集計（常住地）
です。従業地・通学地集計は同じ配布物の中の**別の表 ID** で、まだ手元に
ありません。

実測（令和2年・東京都 T001108、列 T001108046 大学・大学院在学者）:

    早稲田キャンパス（本部） 533945572   669人  （学生2万人超のはず）
    戸山キャンパス           533945473   149人
    西早稲田キャンパス（理工）533945463   169人
    早稲田駅                 533945474   393人

都全体でも、未就学 694,294 + 在学 1,740,168 + 卒業（15歳以上）11,172,439 =
13,606,901 人で、令和2年の常住人口 14,047,594 人の 96.9%。通学地基準なら
他県からの流入で常住人口を**超える**はずです。「そこに住んでいる学生」で
あって「そこに通ってくる学生」ではありません。

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

        self._check_is_residence_based(artifact, resolved, loadable, facts)
        self._check_columns_line_up(artifact, resolved, loadable, facts)
        return facts

    # ------------------------------------------------------------- 検算
    def _check_is_residence_based(
        self, artifact: Path, resolved: dict[str, str | None],
        loadable: list[dict[str, str]], facts: dict[str, Any],
    ) -> None:
        """常住地基準の表であることを、定義から確かめる。

        以前ここは「1メッシュの大学生が1万人を超えたら落とす」でした。
        **効きません。** 通学地基準で早稲田キャンパスのメッシュを数えても
        2万人程度で、1万を超えるのはごく一部です。人数の大小では常住と
        通学を分けられません。

        分けられるのは**定義**です。居住期間（そこに何年住んでいるか）は
        常住地にしか存在しない概念で、従業地・通学地集計にはこの列が
        ありません。あるかないかで判定します。
        """
        present = [f for f in ("resident_under_1y", "resident_1_to_5y",
                               "resident_20y_plus") if resolved.get(f)]
        alive = [f for f in present
                 if any((to_int(r.get(resolved[f])) or 0) for r in loadable)]
        facts["residence_duration_columns"] = alive
        if not alive:
            raise AcquisitionError(
                ERROR_SCHEMA,
                f"{artifact.name}: 居住期間の列（{present or '設定なし'}）が"
                "見つからないか、全メッシュで 0 です。居住期間は**常住地基準**"
                "にしかない項目なので、この表は就業状態等基本集計（常住地）"
                "ではない可能性があります。従業地・通学地集計（昼間人口）は"
                "別の表 ID で、別のアダプタ（estat_daytime_mesh）で読みます")

        # 常住地なら、大学・大学院在学者はそのメッシュに住む大人の数と
        # 桁が合います。実測（令和2年・東京都、分母50人以上）の最大は
        # 5.36 倍（メッシュ 533933383、343人 / 64人。学生寮です）。
        # これは落とすための閾値ではなく、次に読む人のための目盛りです。
        uni, grad = resolved.get("students_university"), resolved.get("check_graduates_15_plus")
        if uni:
            facts["students_university_peak_mesh"] = max(
                (to_int(r.get(uni)) or 0) for r in loadable)
        if uni and grad:
            ratios = [(to_int(r.get(uni)) or 0) / adults
                      for r in loadable
                      if (adults := to_int(r.get(grad)) or 0) >= 50]
            if ratios:
                facts["students_per_adult_peak"] = round(max(ratios), 3)

    def _check_columns_line_up(
        self, artifact: Path, resolved: dict[str, str | None],
        loadable: list[dict[str, str]], facts: dict[str, Any],
    ) -> None:
        """内訳が総数に収まるか。**列 ID が1つずれたら、ここで落ちます。**

        列 ID を設定に置いた以上、版が変わって列が動いても静かに通ります。
        在学者は「総数」と「うち 小中・高校・短大高専・大学院」が同じ表に
        あるので、内訳が総数を超えないことと、合計が総数にほぼ届くことを
        見れば、ずれが露見します。実測（令和2年・東京都）：合計は総数の
        99.95%、超過メッシュ 0 件。
        """
        total = resolved.get("check_students_total")
        parts = [resolved.get(f) for f in ("check_students_primary",
                                           "students_high_school",
                                           "check_students_junior",
                                           "students_university")]
        if not total or not all(parts):
            facts["students_breakdown_checked"] = False
            return

        sum_total = over = 0
        sum_parts = 0
        for row in loadable:
            whole = to_int(row.get(total)) or 0
            piece = sum((to_int(row.get(c)) or 0) for c in parts)
            sum_total += whole
            sum_parts += piece
            if piece > whole:
                over += 1
        facts["students_breakdown_checked"] = True
        facts["students_breakdown_over_total_meshes"] = over
        if not sum_total:
            return
        share = sum_parts / sum_total
        facts["students_breakdown_share"] = round(share, 4)
        # 内訳が総数を超えるのは、列が別の項目を指しているということです。
        # 逆に届かなすぎるのも同じ（専修学校などが外れる分の 0.05% 以外に
        # 説明がつきません）。
        if over or not 0.95 <= share <= 1.0:
            raise AcquisitionError(
                ERROR_SCHEMA,
                f"{artifact.name}: 在学者の内訳が総数と合いません"
                f"（合計/総数 = {share:.3f}、内訳が総数を超えるメッシュ "
                f"{over:,} 件）。config/sources.yaml の列 ID がこの版と"
                "ずれている可能性があります")

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
