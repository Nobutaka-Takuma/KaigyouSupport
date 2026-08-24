"""厚生労働省「医療情報ネット」診療科目・診療時間 -> facility_specialties ほか.

施設ファイル（031）の相方です。座標も名称も持たず、1 行が
(施設ID, 診療科目, 診療時間帯) で、曜日別の診療時間と外来受付時間が横に並びます。
施設ファイルに 診療科目 の列は無いので、標榜科目はこのファイルにしかありません。

3 つのテーブルに書きます:

    facility_specialties  公表された科目そのまま＋正規化キー
    facility_hours        公表された曜日別の時間そのまま
    facility_features     施設ごとの要約（分析が読むのはこれだけ）

診療時間は科目ごとに書ける形式ですが、実際にはたいてい先頭の「歯科」にだけ
入っていて、残りの科目は空です。要約は施設単位で作り、同じ時間帯が複数の科目に
重複して書かれていても 1 回だけ数えます。

自由記載区分（08991）の扱いは kaigyou_core.specialties を参照。
"""
from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterable, Iterator

import psycopg

from kaigyou_core import specialties as vocab
from kaigyou_etl.acquisition import ERROR_EMPTY, ERROR_SCHEMA, AcquisitionError
from kaigyou_etl.adapters._util import read_text
from kaigyou_etl.adapters.base import SourceAdapter

_REQUIRED = ("facility_id", "specialty_code", "specialty_name")

#: 診療時間帯の番号。公表値は 1..3。
_BANDS = (1, 2, 3)


def _parse_time(value: str | None) -> time | None:
    """"09:30" を time に。空欄・"00:00" 埋め・壊れた値は None。"""
    text = (value or "").strip()
    if not text or text in {"-", "_"}:
        return None
    for fmt in ("%H:%M", "%H:%M:%S", "%H%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def _merged_hours(spans: Iterable[tuple[time, time]]) -> float:
    """重なりを畳んでから合計した時間数。

    素直に足すと、同じ日の同じ時間帯を複数の科目が少しずつ違う時刻で書いている
    医院で二重に数えます（週 226.5 時間という、1 週間に無い長さが出ました）。
    区間を結合してから測れば、何科目書かれていても開いている長さは 1 つです。
    """
    ranges = sorted((_minutes(a), _minutes(b)) for a, b in spans if _minutes(b) > _minutes(a))
    total = 0
    current_start = current_end = None
    for start, end in ranges:
        if current_end is None or start > current_end:
            if current_end is not None:
                total += current_end - current_start
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    if current_end is not None:
        total += current_end - current_start
    return round(total / 60, 2)


class MHLWSpecialtiesAdapter(SourceAdapter):
    target_tables = ("facility_specialties", "facility_hours", "facility_features")

    # ------------------------------------------------------------------ 読み
    def _rows(self, artifact: Path) -> tuple[list[str], list[dict[str, str]]]:
        text = read_text(artifact, self.spec.get("encoding"))
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise AcquisitionError(ERROR_SCHEMA, f"{artifact.name}: no CSV header")
        return list(reader.fieldnames), list(reader)

    def _weekdays(self) -> dict[int, str]:
        raw = self.spec.get("weekday_prefixes") or {}
        return {int(k): str(v) for k, v in raw.items()}

    def _hour_columns(self) -> dict[str, str]:
        return {str(k): str(v) for k, v in (self.spec.get("hour_columns") or {}).items()}

    def _evening_from(self) -> time:
        return _parse_time(str(self.spec.get("evening_from") or "18:30")) or time(18, 30)

    def _hour_header(self, field: str, weekday: int) -> str:
        return self._hour_columns()[field].format(day=self._weekdays()[weekday])

    # -------------------------------------------------------------- validate
    def validate(self, artifact: Path) -> dict[str, Any]:
        headers, rows = self._rows(artifact)
        if not rows:
            raise AcquisitionError(ERROR_EMPTY, f"{artifact.name} contains no data rows")

        resolved = {field: self.pick_column(headers, field, required=field in _REQUIRED)
                    for field in self.column_map()}

        present = set(headers)
        hour_headers = [self._hour_header(f, d)
                        for f in self._hour_columns() for d in self._weekdays()]
        missing_hours = [h for h in hour_headers if h not in present]
        if len(missing_hours) == len(hour_headers):
            raise AcquisitionError(
                ERROR_SCHEMA,
                f"{artifact.name}: 診療時間の列が 1 つも見つかりません"
                f"（例: {hour_headers[0]}）。config/sources.yaml の "
                f"hour_columns / weekday_prefixes を確認してください")

        facilities: set[str] = set()
        by_key: Counter[str] = Counter()
        free_text_names: Counter[str] = Counter()
        rows_with_hours = 0
        unmapped_codes: Counter[str] = Counter()
        known = set(vocab.code_map())
        for r in rows:
            fid = (r.get(resolved["facility_id"]) or "").strip()
            if fid:
                facilities.add(fid)
            code = (r.get(resolved["specialty_code"]) or "").strip()
            name = (r.get(resolved["specialty_name"]) or "").strip()
            key, free = vocab.classify(code, name)
            by_key[key] += 1
            if free:
                free_text_names[name] += 1
            if code not in known and code != vocab.free_text_code():
                unmapped_codes[code] += 1
            if any(_parse_time(r.get(self._hour_header("opens", d)))
                   for d in self._weekdays() if self._hour_header("opens", d) in present):
                rows_with_hours += 1

        if not facilities:
            raise AcquisitionError(
                ERROR_EMPTY, f"{artifact.name}: 施設IDのある行がありません")

        return {
            "row_count": len(rows),
            "facilities": len(facilities),
            "rows_with_hours": rows_with_hours,
            "rows_by_specialty_key": dict(by_key.most_common()),
            "free_text_names_top": dict(free_text_names.most_common(15)),
            "non_dental_codes": dict(unmapped_codes.most_common(10)),
            "hour_columns_missing": missing_hours[:10],
            "resolved_columns": {k: v for k, v in resolved.items() if v},
            "headers": headers[:12],
        }

    # ------------------------------------------------------------- transform
    def transform(self, artifact: Path) -> Iterator[dict[str, Any]]:
        headers, rows = self._rows(artifact)
        col = {f: self.pick_column(headers, f, required=f in _REQUIRED)
               for f in self.column_map()}
        present = set(headers)
        source_date = self.source_date() or date.today()
        band_col = col.get("time_band")
        evening_from = self._evening_from()

        specialties: dict[str, dict[tuple[str, str], tuple[str, bool]]] = defaultdict(dict)
        # (weekday, band, specialty_code) -> times. Keyed so a re-read of the
        # same row cannot double it, and so identical hours declared under
        # several specialties collapse when the summary is built.
        hours: dict[str, dict[tuple[int, int, str], dict[str, time | None]]] = defaultdict(dict)

        for row in rows:
            fid = (row.get(col["facility_id"]) or "").strip()
            if not fid:
                continue
            code = (row.get(col["specialty_code"]) or "").strip()
            name = (row.get(col["specialty_name"]) or "").strip()
            if code or name:
                specialties[fid][(code, name)] = vocab.classify(code, name)

            band = 1
            if band_col:
                try:
                    band = int((row.get(band_col) or "1").strip() or 1)
                except ValueError:
                    band = 1
            if band not in _BANDS:
                continue

            for weekday in self._weekdays():
                times = {
                    field: _parse_time(row.get(header))
                    for field in self._hour_columns()
                    if (header := self._hour_header(field, weekday)) in present
                }
                if any(times.values()):
                    hours[fid][(weekday, band, code)] = times

        for fid in sorted(set(specialties) | set(hours)):
            declared = specialties.get(fid, {})
            table = hours.get(fid, {})
            yield {
                "facility_id": fid,
                "source_date": source_date,
                "specialties": [
                    {"specialty_code": code, "specialty_name": name,
                     "specialty_key": key, "is_free_text": free}
                    for (code, name), (key, free) in declared.items()
                ],
                "hours": [
                    {"specialty_code": code, "time_band": band, "weekday": weekday,
                     **{f: t.get(f) for f in self._hour_columns()}}
                    for (weekday, band, code), t in sorted(table.items())
                ],
                "features": _features(declared, table, evening_from),
            }

    # ------------------------------------------------------------------ load
    def load(self, conn: psycopg.Connection, records: Iterable[dict[str, Any]]) -> int:
        fields = list(self._hour_columns())
        spec_rows: list[dict[str, Any]] = []
        hour_rows: list[list[Any]] = []
        feature_rows: list[dict[str, Any]] = []

        for rec in records:
            fid = rec["facility_id"]
            when = rec["source_date"]
            for s in rec["specialties"]:
                spec_rows.append({"source_id": self.source_id, "facility_id": fid,
                                  "source_date": when, **s})
            for h in rec["hours"]:
                hour_rows.append([self.source_id, fid, h["specialty_code"],
                                  h["time_band"], h["weekday"],
                                  *[h.get(f) for f in fields], when])
            feature_rows.append({"source_id": self.source_id, "facility_id": fid,
                                 "source_date": when, **rec["features"]})

        with conn.cursor() as cur:
            for table in ("facility_hours", "facility_specialties", "facility_features"):
                cur.execute(f"DELETE FROM {table} WHERE source_id = %s", (self.source_id,))

            self.insert_many(
                cur,
                """
                INSERT INTO facility_specialties (
                    source_id, facility_id, specialty_code, specialty_name,
                    specialty_key, is_free_text, source_date, last_updated
                ) VALUES (
                    %(source_id)s, %(facility_id)s, %(specialty_code)s,
                    %(specialty_name)s, %(specialty_key)s, %(is_free_text)s,
                    %(source_date)s, now()
                )
                ON CONFLICT (source_id, facility_id, specialty_code, specialty_name)
                DO UPDATE SET specialty_key = EXCLUDED.specialty_key,
                              is_free_text = EXCLUDED.is_free_text,
                              last_updated = now()
                """,
                spec_rows,
            )

            # 診療時間は全国で 100 万行を超えます。1 行ずつの INSERT では
            # 取り込みが分単位で伸びるので、ここだけ COPY を使います。
            if hour_rows:
                columns = ["source_id", "facility_id", "specialty_code", "time_band",
                           "weekday", *fields, "source_date"]
                with cur.copy(
                    f"COPY facility_hours ({', '.join(columns)}) FROM STDIN"
                ) as copy:
                    for values in hour_rows:
                        copy.write_row(values)

            self.insert_many(
                cur,
                """
                INSERT INTO facility_features (
                    facility_id, source_id, specialty_keys, declared_specialties,
                    open_days, weekly_open_hours, latest_close, opens_saturday,
                    opens_sunday, opens_holiday, opens_evening, source_date, last_updated
                ) VALUES (
                    %(facility_id)s, %(source_id)s, %(specialty_keys)s,
                    %(declared_specialties)s, %(open_days)s, %(weekly_open_hours)s,
                    %(latest_close)s, %(opens_saturday)s, %(opens_sunday)s,
                    %(opens_holiday)s, %(opens_evening)s, %(source_date)s, now()
                )
                ON CONFLICT (facility_id) DO UPDATE SET
                    source_id = EXCLUDED.source_id,
                    specialty_keys = EXCLUDED.specialty_keys,
                    declared_specialties = EXCLUDED.declared_specialties,
                    open_days = EXCLUDED.open_days,
                    weekly_open_hours = EXCLUDED.weekly_open_hours,
                    latest_close = EXCLUDED.latest_close,
                    opens_saturday = EXCLUDED.opens_saturday,
                    opens_sunday = EXCLUDED.opens_sunday,
                    opens_holiday = EXCLUDED.opens_holiday,
                    opens_evening = EXCLUDED.opens_evening,
                    source_date = EXCLUDED.source_date,
                    last_updated = now()
                """,
                feature_rows,
            )
        return len(feature_rows)


#: 週間診療時間に数える曜日。祝日（8）は毎週来るわけではないので外します。
_WEEKLY = (1, 2, 3, 4, 5, 6, 7)


def _features(declared: dict[tuple[str, str], tuple[str, bool]],
              table: dict[tuple[int, int, str], dict[str, time | None]],
              evening_from: time) -> dict[str, Any]:
    """施設 1 件の要約。

    同じ時間帯が複数の科目に重複して書かれていることがあるので、
    (曜日, 開始, 終了) で一意にしてから数えます。そうしないと 4 科目を
    標榜する医院の週間診療時間が 4 倍になります。
    """
    intervals: set[tuple[int, time, time]] = set()
    open_weekdays: set[int] = set()
    latest: time | None = None

    for (weekday, _band, _code), times in table.items():
        opens, closes = times.get("opens"), times.get("closes")
        if opens is None and closes is None:
            continue
        open_weekdays.add(weekday)
        if opens is not None and closes is not None:
            intervals.add((weekday, opens, closes))
            if latest is None or closes > latest:
                latest = closes

    by_weekday: dict[int, list[tuple[time, time]]] = defaultdict(list)
    for weekday, opens, closes in intervals:
        if weekday in _WEEKLY:
            by_weekday[weekday].append((opens, closes))
    weekly = sum(_merged_hours(spans) for spans in by_weekday.values())

    keys = sorted({key for key, _free in declared.values()})
    return {
        "specialty_keys": keys,
        "declared_specialties": sorted({name for _code, name in declared if name}),
        "open_days": len(open_weekdays) or None,
        "weekly_open_hours": round(weekly, 2) if intervals else None,
        "latest_close": latest,
        "opens_saturday": 6 in open_weekdays,
        "opens_sunday": 7 in open_weekdays,
        "opens_holiday": 8 in open_weekdays,
        "opens_evening": any(closes >= evening_from
                             for weekday, _opens, closes in intervals
                             if weekday in _WEEKLY),
    }
