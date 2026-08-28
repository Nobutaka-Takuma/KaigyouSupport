"""国勢調査 従業地・通学地集計（市区町村・年齢5歳階級） -> municipality_daytime.

**商圏の数字ではありません。** これがこの取り込みでいちばん大事な区別です。

新宿区の昼間人口は 793,528 人ですが、そのうち何人が早稲田駅前の半径1km に
いるかは、この表からは分かりません。歌舞伎町にも西新宿にもいます。面積で
按分するのは完全に間違いです。商圏の昼間人口が欲しければ 500m メッシュの
従業地・通学地統計（``estat_daytime_mesh``）が要ります。

では何の役に立つのか。**文脈と、年齢の切り口です。** 新宿区の 20〜24歳は
夜間 21,906 人に対して昼間 80,136 人、3.7 倍に膨らみます。「この街には昼間、
若い通学者が大量に流入している」は、商圏の数字が無くても意思決定に効きます。
そして年齢別は、メッシュ統計には無い切り口です。

ファイルの形（令和2年 第1-1表 e01_01.xlsx）:

    行1〜10  表題と多段の見出し。データは 11 行目から
    列A      地域識別コード（a=全国 / 0=市区町村・政令市の区 / 1=政令市計 …）
    列B      都道府県（"13_東京都"）
    列C      地域名（"13104_新宿区"）
    列D      男女（"0_総数" / "1_男" / "2_女"）
    列E      年齢（"00_総数" / "03_20～24歳" …）
    列F      常住地による人口（夜間人口）
    列R      従業地・通学地による人口（昼間人口）

列の位置は設定に置きます。版が変われば列が動くので、コードに書くとその
たびにコードを直すことになります。
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator

import psycopg

from kaigyou_etl.acquisition import ERROR_EMPTY, ERROR_SCHEMA, AcquisitionError
from kaigyou_etl.adapters._util import to_int
from kaigyou_etl.adapters.base import SourceAdapter

#: 「13104_新宿区」「00000_全国」から、コードと名前を分ける。
_LABELLED = re.compile(r"^(?P<code>\d+)_(?P<name>.+)$")

#: 取り込む地域識別コード。**市区町村だけを取ります。**
#:
#: 同じファイルに全国・都道府県・政令市計が同居していて、全部入れると合計が
#: 実際の何倍にもなります。実測（令和2年 e01_01.xlsx）の内訳:
#:
#:     a  48件   全国・都道府県
#:     0  198件  政令市の区 + 東京都特別区
#:     1  21件   政令市計・東京都区部計   ← 区と二重になるので採らない
#:     2  772件  市（政令市を除く）
#:     3  926件  町村
#:
#: 採るのは {0, 2, 3} の 1,896 件。**最初は "0" だけにしていて、validate が
#: 落としました**（区だけだと都市部に偏るので、全国合計の昼夜間人口比が
#: 1.090 になった）。国内の移動を全部足せば 1.0 になるはず、という検算が
#: 思い込みを捕まえたことになります。
_MUNICIPALITY_LEVELS = {"0", "2", "3"}


def _split(label: Any) -> tuple[str | None, str | None]:
    match = _LABELLED.match(str(label or "").strip())
    if not match:
        return None, None
    return match.group("code"), match.group("name")


class EStatDaytimeMunicipalityAdapter(SourceAdapter):
    target_tables = ("municipality_daytime",)

    # ---------------------------------------------------------------- config
    def _positions(self) -> dict[str, int]:
        """論理名 -> 0 始まりの列番号。設定は 1 始まり（Excel の見た目）。"""
        raw = self.spec.get("column_positions") or {}
        return {str(k): int(v) - 1 for k, v in raw.items()}

    def _first_data_row(self) -> int:
        return int(self.spec.get("first_data_row", 11))

    # ------------------------------------------------------------- artefacts
    def _rows(self, artifact: Path) -> Iterator[tuple[Any, ...]]:
        try:
            import openpyxl
        except ImportError as exc:  # pragma: no cover - 環境の問題
            raise AcquisitionError(
                ERROR_SCHEMA,
                "xlsx を読むには openpyxl が要ります: "
                "pip install -e 'server[etl]'") from exc

        book = openpyxl.load_workbook(artifact, read_only=True, data_only=True)
        sheet = book[self.spec["sheet"]] if self.spec.get("sheet") else book.worksheets[0]
        skip = self._first_data_row() - 1
        for index, row in enumerate(sheet.iter_rows(values_only=True)):
            if index >= skip:
                yield row
        book.close()

    def _records(self, artifact: Path) -> Iterator[dict[str, Any]]:
        at = self._positions()
        for field in ("area_level", "prefecture", "area", "sex", "age",
                      "night_population", "daytime_population"):
            if field not in at:
                raise AcquisitionError(
                    ERROR_SCHEMA,
                    f"{self.source_id}: column_positions に {field!r} が"
                    "ありません（config/sources.yaml）")

        source_date = self.source_date() or date.today()
        for row in self._rows(artifact):
            if len(row) <= max(at.values()):
                continue
            if str(row[at["area_level"]] or "").strip() not in _MUNICIPALITY_LEVELS:
                continue
            code, name = _split(row[at["area"]])
            if not code or len(code) != 5:
                continue
            age_code, _age_name = _split(row[at["age"]])
            yield {
                "municipality_code": code,
                "municipality_name": name,
                "prefecture_code": code[:2],
                "age_band": str(row[at["age"]] or "").strip(),
                "age_order": int(age_code) if age_code else None,
                "sex": str(row[at["sex"]] or "").strip(),
                "night_population": to_int(row[at["night_population"]]),
                "daytime_population": to_int(row[at["daytime_population"]]),
                "source_date": source_date,
            }

    # -------------------------------------------------------------- pipeline
    def validate(self, artifact: Path) -> dict[str, Any]:
        municipalities: set[str] = set()
        bands: set[str] = set()
        rows = 0
        totals = {"night": 0, "daytime": 0}
        for record in self._records(artifact):
            rows += 1
            municipalities.add(record["municipality_code"])
            bands.add(record["age_band"])
            # 合計は「総数×総数」の行だけを足します。年齢階級を全部足すと
            # 総数と二重になります。
            if record["sex"].startswith("0_") and record["age_order"] == 0:
                totals["night"] += record["night_population"] or 0
                totals["daytime"] += record["daytime_population"] or 0
        if not rows:
            raise AcquisitionError(
                ERROR_EMPTY,
                f"{artifact.name}: 市区町村の行が1つも読めませんでした。"
                f"config/sources.yaml の column_positions と first_data_row "
                f"（現在 {self._first_data_row()}）を確認してください")

        facts: dict[str, Any] = {
            "row_count": rows,
            "municipalities": len(municipalities),
            "age_bands": len(bands),
            "night_population_total": totals["night"],
            "daytime_population_total": totals["daytime"],
        }
        # 全国の昼間人口は夜間人口と一致します（国内で移動するだけなので）。
        # 大きくずれていたら、列か地域の絞り込みを間違えています。
        if totals["night"]:
            ratio = totals["daytime"] / totals["night"]
            facts["daytime_over_night"] = round(ratio, 4)
            if not 0.95 <= ratio <= 1.05:
                raise AcquisitionError(
                    ERROR_SCHEMA,
                    f"{artifact.name}: 全市区町村の合計で昼間人口/夜間人口が "
                    f"{ratio:.3f} です。国内の移動を合計すれば 1.0 前後になる"
                    "はずなので、列の対応か地域の絞り込みが間違っています")
        # 常識の範囲。日本の人口が1千万人未満や2億人超になることはありません。
        if not 10_000_000 < totals["night"] < 200_000_000:
            raise AcquisitionError(
                ERROR_SCHEMA,
                f"{artifact.name}: 夜間人口の合計が {totals['night']:,} 人です。"
                "市区町村以外の行（全国・都道府県・政令市計）が混ざっているか、"
                "列がずれています")
        return facts

    def transform(self, artifact: Path) -> Iterator[dict[str, Any]]:
        yield from self._records(artifact)

    def load(self, conn: psycopg.Connection,
             records: Iterable[dict[str, Any]]) -> int:
        rows = [rec | {"source_id": self.source_id} for rec in records]
        with conn.cursor() as cur:
            # 全国が1ファイルなので、都道府県では絞りません。source_id ごと
            # 置き換えます。
            cur.execute("DELETE FROM municipality_daytime WHERE source_id = %s",
                        (self.source_id,))
            return self.insert_many(
                cur,
                """
                INSERT INTO municipality_daytime (
                    source_id, municipality_code, municipality_name,
                    prefecture_code, age_band, age_order, sex,
                    night_population, daytime_population, source_date, last_updated
                ) VALUES (
                    %(source_id)s, %(municipality_code)s, %(municipality_name)s,
                    %(prefecture_code)s, %(age_band)s, %(age_order)s, %(sex)s,
                    %(night_population)s, %(daytime_population)s,
                    %(source_date)s, now()
                )
                ON CONFLICT (source_id, municipality_code, age_band, sex)
                DO UPDATE SET
                    night_population   = EXCLUDED.night_population,
                    daytime_population = EXCLUDED.daytime_population,
                    source_date        = EXCLUDED.source_date,
                    last_updated       = now()
                """,
                rows,
            )
