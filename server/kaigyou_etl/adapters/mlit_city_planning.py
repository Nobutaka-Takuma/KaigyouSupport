"""国土数値情報「都市計画決定情報」(A55) -> city_planning_zones.

**この層が答えるのは「そこに何人いるか」ではなく「そこに何が建てられるか」です。**

用途地域・容積率・建蔽率はすでに地価公示が持っていますが、地価公示は点で、
静岡県で 3,221 点しかありません。候補地の近くに点が無ければ「不明」になり、
点があっても用途地域の境目は道 1 本で変わるので、最寄りの点の用途地域が候補地の
用途地域だとは限りません。A55 は面なので、候補地がどの区域に入るかが決まります。

A55 は 1 県のアーカイブが**市区町村ごとのフォルダに分かれ、その中に層ごとの
shapefile が入っています**。静岡県で 32 フォルダ・233 ファイル。1 つ読んで
終わりにすると、1 市の 1 層だけを取り込んで成功と表示することになります。
:func:`read_shapefiles` で全部読みます。

どの層を取り込むかと、層ごとの列名は ``config/sources.yaml`` の ``layers`` に
あります。A55 は層ごとに列名が違う（``YoutoName`` / ``AreaType`` / ``DistType``
/ ``ParkName`` / ``TokeiType``）ので、コードに層名を書くと 1 層増えるたびに
コードを直すことになります。

面だけを取り込みます。都市計画道路（``douro``）は線で、面積按分にも
点の内外判定にも使えないので、この表には入れません——**取り込めなかったのでは
なく、この表の形に合わないので入れていません。** 取り込みの要約にそう出ます。

**同じ層の中で面が重なっていることがあります。** 公表データがそうなっています
（沼津市の区域区分には 52.96 km² と 52.52 km² の市街化調整区域が別々の行として
入っていて、大部分が重なります）。取り込みは公表どおりに入れ、重なりを解消
しません——勝手にどちらかを捨てると、捨てたほうが正しかったときに気づけない
ためです。**1 点が同じ層で 2 つの区分に当たることがある**ので、読む側は
1 件だけ返る前提で書かないでください。
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

import psycopg

from kaigyou_etl.acquisition import ERROR_EMPTY, ERROR_SCHEMA, AcquisitionError
from kaigyou_etl.adapters._util import read_shapefiles, shape_to_wkt, to_float
from kaigyou_etl.adapters.base import SourceAdapter

#: A55 が告示日に使う書き方。市町によって混在します（同じ県の中でも）。
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d")


def _as_date(value: Any) -> date | None:
    """告示日を日付にする。読めないものは NULL。

    A55 の FNDate には日付でないものが混ざります（図面番号らしき ``JT226_1``
    など）。**推測して日付を作らない**ほうがよいので、読めなければ捨てます。
    """
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _percent(value: Any) -> float | None:
    """容積率・建蔽率。空文字は 0 ではなく NULL。

    「定めが無い」と「まだ取り込んでいない」を混ぜないためです。0% の
    容積率は存在しないので、0 で埋めると「建てられない土地」に見えます。
    """
    if value is None or str(value).strip() == "":
        return None
    return to_float(value)


class MLITCityPlanningAdapter(SourceAdapter):
    target_tables = ("city_planning_zones",)

    # ---------------------------------------------------------------- config
    def layers(self) -> Mapping[str, Mapping[str, Any]]:
        raw = self.spec.get("layers") or {}
        return {str(k): (v or {}) for k, v in raw.items()}

    def _match(self):
        suffixes = tuple(f"_{name}.shp" for name in self.layers())
        return lambda member: member.endswith(suffixes)

    # ------------------------------------------------------------------ read
    def _rows(self, artifact: Path) -> Iterator[tuple[str, Mapping[str, Any], Any]]:
        """(層の鍵, 属性の辞書, 図形) を 1 件ずつ。

        層の鍵はファイル名の末尾から決めます（``22100_youto.shp`` -> ``youto``）。
        フォルダ名でも属性でもなくファイル名なのは、A55 でそこだけが必ず層を
        表しているからです。
        """
        layers = self.layers()
        for member, fields, records in read_shapefiles(
                artifact, self._match(), encoding="utf-8"):
            key = next((name for name in layers
                        if member.endswith(f"_{name}.shp")), None)
            if key is None:                       # _match が通したのに引けない
                continue
            for rec in records:
                yield key, dict(zip(fields, rec.record)), rec.shape

    # -------------------------------------------------------------- pipeline
    def validate(self, artifact: Path) -> dict[str, Any]:
        layers = self.layers()
        if not layers:
            raise AcquisitionError(
                ERROR_SCHEMA,
                "sources.yaml の layers が空です。どの層を取り込むのか決まって"
                "いないまま取り込むと、0 件で成功と表示されます。")

        by_layer: Counter[str] = Counter()
        by_type: Counter[str] = Counter()
        municipalities: set[str] = set()
        no_geometry = 0
        with_far = 0
        for key, row, shape in self._rows(artifact):
            spec = layers[key]
            by_layer[key] += 1
            code = str(row.get("Citycode") or "").strip()
            if code:
                municipalities.add(code)
            zone_type = str(row.get(spec.get("type_field") or "") or "").strip()
            if zone_type:
                by_type[f"{key}:{zone_type}"] += 1
            if spec.get("far_field") and _percent(row.get(spec["far_field"])) is not None:
                with_far += 1
            if shape_to_wkt(shape) is None:
                no_geometry += 1

        if not by_layer:
            raise AcquisitionError(
                ERROR_EMPTY,
                f"{artifact.name}: 取り込む層が 1 つも見つかりませんでした"
                f"（探した層: {sorted(layers)}）")

        return {
            "feature_count": sum(by_layer.values()),
            "features_by_layer": dict(by_layer.most_common()),
            "municipality_count": len(municipalities),
            "features_without_geometry": no_geometry,
            "features_with_floor_area_ratio": with_far,
            "zone_types": dict(by_type.most_common(40)),
            # **入れなかったものを名指しします。** 都市計画道路は線なので
            # この表には入りません。「取り込めなかった」と読まれないように。
            "not_loaded": {
                "douro": "都市計画道路（線）。この表は面だけを持ちます。",
            },
        }

    def transform(self, artifact: Path) -> Iterator[dict[str, Any]]:
        layers = self.layers()
        source_date = self.source_date() or date.today()
        fallback_pref = self.ctx.prefecture_code

        for key, row, shape in self._rows(artifact):
            wkt = shape_to_wkt(shape)
            if wkt is None:
                continue
            spec = layers[key]
            city = str(row.get("Citycode") or "").strip() or None
            # 県は市区町村コードの上 2 桁が正です。走らせるときに渡された県では
            # ありません——**取り違えたまま入ると、静岡の区域が東京の名前で
            # 引かれます。**
            prefecture = (city[:2] if city and len(city) >= 2 else fallback_pref)
            yield {
                "prefecture_code": prefecture,
                "municipality_code": city,
                "municipality_name": str(row.get("Cityname") or "").strip() or None,
                "zone_kind": key,
                "zone_kind_label": str(spec.get("label") or key),
                "zone_type": str(row.get(spec.get("type_field") or "") or "").strip() or None,
                "zone_code": _int(row.get(spec.get("code_field") or "")),
                "zone_name": str(row.get(spec.get("name_field") or "") or "").strip() or None,
                "far": _percent(row.get(spec["far_field"])) if spec.get("far_field") else None,
                "bcr": _percent(row.get(spec["bcr_field"])) if spec.get("bcr_field") else None,
                "decided_on": _as_date(row.get("FNDate")),
                "geom_wkt": wkt,
                "source_date": source_date,
            }

    def load(self, conn: psycopg.Connection, records: Iterable[dict[str, Any]]) -> int:
        rows = [rec | {"source_id": self.source_id} for rec in records]
        # **県で絞って置き換えます。** A55 は 1 県ごとに配られるので、絞らないと
        # 静岡を入れた時点で東京の区域が消えます（しかも成功と表示されます）。
        # 行から採るのは、市区町村コードの上 2 桁がこの表の県の権威だからです。
        incoming = sorted({r["prefecture_code"] for r in rows if r.get("prefecture_code")})
        with conn.cursor() as cur:
            if incoming:
                cur.execute("DELETE FROM city_planning_zones "
                            "WHERE source_id = %s AND prefecture_code = ANY(%s)",
                            (self.source_id, incoming))
            else:
                cur.execute("DELETE FROM city_planning_zones WHERE source_id = %s",
                            (self.source_id,))
            count = self.insert_many(
                cur,
                """
                    INSERT INTO city_planning_zones (
                        source_id, prefecture_code, municipality_code,
                        municipality_name, zone_kind, zone_kind_label, zone_type,
                        zone_code, zone_name, far, bcr, decided_on, geom,
                        source_date, last_updated
                    ) VALUES (
                        %(source_id)s, %(prefecture_code)s, %(municipality_code)s,
                        %(municipality_name)s, %(zone_kind)s, %(zone_kind_label)s,
                        %(zone_type)s, %(zone_code)s, %(zone_name)s,
                        %(far)s, %(bcr)s, %(decided_on)s,
                        -- ST_MakeValid: 公表データには自己交差する面が混ざります。
                        -- そのまま入れると ST_Contains が落ちるか、黙って偽を返します。
                        ST_Multi(ST_CollectionExtract(
                            ST_MakeValid(ST_GeomFromText(%(geom_wkt)s, 4326)), 3)),
                        %(source_date)s, now()
                    )
                """,
                rows,
            )
            # 面にならなかったもの（線や点だけの図形）は残しません。
            cur.execute("DELETE FROM city_planning_zones "
                        "WHERE source_id = %s AND ST_IsEmpty(geom)", (self.source_id,))
        conn.commit()
        return count


def _int(value: Any) -> int | None:
    text = str(value or "").strip()
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None
