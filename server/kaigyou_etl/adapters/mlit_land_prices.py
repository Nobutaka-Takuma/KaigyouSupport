"""国土数値情報 L01（地価公示） -> land_prices.

The columns are named L01_001..L01_148 and nothing in the file says what any
of them mean, so the mapping lives in ``config/sources.yaml`` where a change of
release is a change of configuration. The ones this needs:

    L01_001  市区町村コード        L01_025  所在地
    L01_002  用途区分（000 住宅地 / 005 商業地 / 009 工業地 / 013 林地 …）
    L01_003  連番                 L01_028  利用の現況
    L01_007  価格時点の年          L01_048  最寄り駅
    L01_008  価格（円/m²）         L01_050  駅からの距離（m）
    L01_009  対前年変動率（%）      L01_051  用途地域

The use division is worth loading rather than collapsing: 住宅地 and 商業地 in
the same ward differ by a factor of three or more, so a single "land price
here" figure that mixes them describes neither.

What this is not: rent. The published figure is the price of a specific
surveyed parcel of land, per square metre, on 1 January. Turning it into what a
practice would pay needs the building, the floor and the contract, none of
which are published here -- and the requirements rule out predicting it. The
figures are loaded and shown as published; nothing derives a cost from them.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator

import psycopg

from kaigyou_etl.acquisition import ERROR_EMPTY, ERROR_SCHEMA, AcquisitionError
from kaigyou_etl.adapters._util import read_shapefile, to_int
from kaigyou_etl.adapters.base import SourceAdapter


def _to_float(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text or text == "_":
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _text(value: Any) -> str | None:
    """Published text, with the file's own placeholder for "not applicable"."""
    text = str(value if value is not None else "").strip()
    # L01 writes a bare underscore where a field does not apply. Storing that
    # would put "_" on screen as though it were an address.
    return text or None if text != "_" else None


class MLITLandPriceAdapter(SourceAdapter):
    target_tables = ("land_prices",)

    # ---------------------------------------------------------------- config
    def use_categories(self) -> dict[str, str]:
        return {str(k): str(v) for k, v in (self.spec.get("use_categories") or {}).items()}

    # -------------------------------------------------------------- pipeline
    def _rows(self, artifact: Path):
        fields, records = read_shapefile(
            artifact, member_prefix=self.spec.get("shapefile_member"),
            encoding=self.spec.get("encoding") or "cp932")
        if not records:
            raise AcquisitionError(ERROR_EMPTY, f"{artifact.name}: shapefile has no records")
        return fields, records

    def validate(self, artifact: Path) -> dict[str, Any]:
        # L01-26_13 names its prefecture the same way N03 does.
        named_prefecture = self.check_prefecture_matches_filename(artifact)
        fields, records = self._rows(artifact)
        col = {name: self.pick_column(fields, name,
                                      required=name in ("price", "lat_dummy_never"))
               for name in self.column_map()}

        price_col = col["price"]
        year_col = col.get("year")
        use_col = col.get("use_category")

        prices: list[int] = []
        years: dict[int, int] = {}
        by_use: dict[str, int] = {}
        for record in records:
            values = dict(zip(fields, record.record))
            price = to_int(values.get(price_col))
            if price:
                prices.append(price)
            if year_col:
                year = to_int(values.get(year_col))
                if year:
                    years[year] = years.get(year, 0) + 1
            if use_col:
                code = str(values.get(use_col) or "").strip()
                by_use[code] = by_use.get(code, 0) + 1

        if not prices:
            raise AcquisitionError(
                ERROR_SCHEMA,
                f"{artifact.name}: no prices in column {price_col!r}; check the "
                "column mapping in sources.yaml against this release")

        prices.sort()
        facts: dict[str, Any] = {
            "point_count": len(records),
            "priced_points": len(prices),
            "prefecture_from_filename": named_prefecture,
            "prefecture_code": self.ctx.prefecture_code,
            "survey_years": dict(sorted(years.items())),
            "price_yen_per_sqm": {
                "min": prices[0],
                "median": prices[len(prices) // 2],
                "max": prices[-1],
            },
            "points_by_use_code": dict(sorted(by_use.items(), key=lambda kv: -kv[1])),
            "use_codes_not_configured": sorted(set(by_use) - set(self.use_categories())),
        }

        # A price per square metre in yen is a five-to-eight digit number. A
        # release that switched to 千円/m² -- or a column read as the serial
        # number -- lands orders of magnitude away, and would be believed.
        if not (1_000 <= facts["price_yen_per_sqm"]["median"] <= 100_000_000):
            raise AcquisitionError(
                ERROR_SCHEMA,
                f"{artifact.name}: median price {facts['price_yen_per_sqm']['median']:,} "
                "円/m² is outside the plausible range; the price column is most "
                "likely not the one mapped in sources.yaml")
        return facts

    def transform(self, artifact: Path) -> Iterator[dict[str, Any]]:
        fields, records = self._rows(artifact)
        col = {name: self.pick_column(fields, name, required=(name == "price"))
               for name in self.column_map()}
        labels = self.use_categories()
        default_year = self.source_date().year if self.source_date() else date.today().year
        seen: set[tuple[str, int]] = set()

        for record in records:
            values = dict(zip(fields, record.record))
            price = to_int(values.get(col["price"]))
            if not price:
                continue

            geo = record.shape.__geo_interface__
            if geo.get("type") != "Point":
                continue
            lng, lat = (float(v) for v in geo["coordinates"][:2])

            municipality = _text(values.get(col.get("municipality_code", ""))) or ""
            use_code = str(values.get(col.get("use_category", "")) or "").strip()
            serial = str(values.get(col.get("serial", "")) or "").strip()
            # 標準地番号. Unique within a year, and stable between years, which
            # is what makes a later release land beside this one rather than
            # duplicating it.
            point_code = "-".join(p for p in (municipality, use_code, serial) if p)
            year = to_int(values.get(col.get("year", ""))) or default_year
            if not point_code or (point_code, year) in seen:
                continue
            seen.add((point_code, year))

            yield {
                "point_code": point_code,
                "survey_year": year,
                "prefecture_code": municipality[:2] or self.ctx.prefecture_code,
                "municipality_code": municipality or None,
                "municipality_name": _text(values.get(col.get("municipality_name", ""))),
                "address": _text(values.get(col.get("address", ""))),
                "lng": lng,
                "lat": lat,
                "price_yen_per_sqm": price,
                "change_rate_pct": _to_float(values.get(col.get("change_rate", ""))),
                "use_category": labels.get(use_code),
                "use_category_code": use_code or None,
                "current_use": _text(values.get(col.get("current_use", ""))),
                "zoning": _text(values.get(col.get("zoning", ""))),
                "building_coverage_pct": to_int(values.get(col.get("building_coverage", ""))),
                "floor_area_ratio_pct": to_int(values.get(col.get("floor_area_ratio", ""))),
                "nearest_station": _text(values.get(col.get("nearest_station", ""))),
                "station_distance_m": to_int(values.get(col.get("station_distance", ""))),
                "source_date": self.source_date() or date(year, 1, 1),
            }

    def load(self, conn: psycopg.Connection, records: Iterable[dict[str, Any]]) -> int:
        rows = [rec | {"source_id": self.source_id} for rec in records]
        if not rows:
            return 0
        # This prefecture and these years only -- one file is one prefecture,
        # and a later year must not remove the earlier one.
        prefectures = sorted({r["prefecture_code"] for r in rows})
        years = sorted({r["survey_year"] for r in rows})
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM land_prices WHERE source_id = %s "
                "AND prefecture_code = ANY(%s) AND survey_year = ANY(%s)",
                (self.source_id, prefectures, years))
            return self.insert_many(
                cur,
                """
                    INSERT INTO land_prices (
                        source_id, point_code, survey_year, prefecture_code,
                        municipality_code, municipality_name, address, geom,
                        price_yen_per_sqm, change_rate_pct, use_category,
                        use_category_code, current_use, zoning,
                        building_coverage_pct, floor_area_ratio_pct,
                        nearest_station, station_distance_m, source_date, last_updated
                    ) VALUES (
                        %(source_id)s, %(point_code)s, %(survey_year)s, %(prefecture_code)s,
                        %(municipality_code)s, %(municipality_name)s, %(address)s,
                        ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326),
                        %(price_yen_per_sqm)s, %(change_rate_pct)s, %(use_category)s,
                        %(use_category_code)s, %(current_use)s, %(zoning)s,
                        %(building_coverage_pct)s, %(floor_area_ratio_pct)s,
                        %(nearest_station)s, %(station_distance_m)s,
                        %(source_date)s, now()
                    )
                    ON CONFLICT (source_id, point_code, survey_year) DO UPDATE SET
                        price_yen_per_sqm = EXCLUDED.price_yen_per_sqm,
                        change_rate_pct = EXCLUDED.change_rate_pct,
                        use_category = EXCLUDED.use_category,
                        current_use = EXCLUDED.current_use,
                        zoning = EXCLUDED.zoning,
                        source_date = EXCLUDED.source_date,
                        last_updated = now()
                """,
                rows,
            )
