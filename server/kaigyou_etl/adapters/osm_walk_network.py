"""OpenStreetMap 道路データ（Geofabrik shapefile 抽出） -> walk_network.

Geofabrik publishes each region as a shapefile bundle alongside the PBF, and
the roads layer in it is all this needs. That matters practically: the project
already reads zipped shapefiles for S12 and N03, so this arrives through
machinery that exists, and nobody has to install osm2pgrouting or convert a
PBF on a Windows laptop to get a walking network.

What counts as walkable is configuration, not code. The published `fclass`
values are kept as they are, and ``config/sources.yaml`` says which of them a
person may walk along -- motorways and trunk roads are excluded because
pedestrians are prohibited on them, which is a rule about Japan rather than
about OpenStreetMap.

Attribution: OpenStreetMap data is ODbL. The source row carries the licence and
the attribution string, and the app displays it wherever the catchment is used,
like every other source here.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator

import psycopg

from kaigyou_core.db import column_exists
from kaigyou_etl.acquisition import (
    ERROR_EMPTY,
    ERROR_INPUT_MISSING,
    ERROR_SCHEMA,
    AcquisitionError,
)
from kaigyou_etl.adapters._util import read_shapefile
from kaigyou_etl.adapters.base import SourceAdapter


def _linestrings(shape: Any) -> Iterator[list[tuple[float, float]]]:
    """Yield each simple LineString in a pyshp polyline.

    pgRouting needs simple LineStrings: a MultiLineString has no single source
    and target, so a multi-part way is stored as one row per part.
    """
    geo = shape.__geo_interface__
    gtype, coords = geo.get("type"), geo.get("coordinates")
    if gtype == "LineString":
        parts = [coords]
    elif gtype == "MultiLineString":
        parts = list(coords)
    else:
        return
    for part in parts:
        points = [(float(x), float(y)) for x, y, *_ in part]
        # Two distinct points minimum, or it is not an edge.
        if len(points) >= 2 and len(set(points)) >= 2:
            yield points


def _wkt(points: list[tuple[float, float]]) -> str:
    return "LINESTRING(" + ", ".join(f"{x:.7f} {y:.7f}" for x, y in points) + ")"


class OSMWalkNetworkAdapter(SourceAdapter):
    target_tables = ("walk_network",)

    # ---------------------------------------------------------------- config
    def walkable_classes(self) -> set[str]:
        return {str(c).strip().lower() for c in (self.spec.get("walkable_classes") or [])}

    def bbox(self, conn: psycopg.Connection | None = None,
             ) -> tuple[float, float, float, float] | None:
        """切り取る範囲 (min_lng, min_lat, max_lng, max_lat)。

        地方の抽出ファイルは県よりずっと大きく、県の外の辺は経路探索の重しに
        しかなりません。

        優先順位は 2 つだけです。

        1. 設定に ``bbox`` が書いてあれば、それを使う（手で指定した意図を尊重）
        2. 書いていなければ、**取り込む県の市区町村境界から作る**

        既定を 2 にしたのは、1 を既定にしていたからです。東京23区の箱が
        書きっぱなしになっていて、静岡を入れると 1 本も残らないのに
        「成功」と表示されました。県ごとに手で書き換える設定は、いつか
        書き換え忘れます。

        どちらも無いときは止めます。切り取らずに地方全体を入れると、交差点の
        分割（いちばん遅い処理）が何十分も走ったうえで、使わない道路が
        経路探索を重くします。
        """
        if getattr(self, "_bbox_cache", "unset") != "unset":
            return self._bbox_cache  # type: ignore[return-value]

        raw = self.spec.get("bbox")
        if raw:
            self._bbox_cache = tuple(float(v) for v in raw)
            return self._bbox_cache  # type: ignore[return-value]

        margin = float(self.spec.get("bbox_margin_m") or 3000.0)
        try:
            if conn is not None:
                box = self._prefecture_bbox(conn, margin)
            else:
                # validate と transform は接続を持たずに呼ばれます。県の範囲は
                # DB にしかないので、ここだけ自分で開きます。
                from kaigyou_core.db import connect

                with connect() as own:
                    box = self._prefecture_bbox(own, margin)
        except Exception as exc:  # noqa: BLE001 - 理由を言ってから止める
            raise AcquisitionError(
                ERROR_SCHEMA,
                f"県の範囲を引けませんでした（{type(exc).__name__}: {exc}）。"
                "config/sources.yaml の bbox に範囲を書くか、DB に接続できる"
                "状態で実行してください。") from exc

        if box is None:
            raise AcquisitionError(
                ERROR_INPUT_MISSING,
                f"都道府県 {self.ctx.prefecture_code} の市区町村境界が入って"
                "いないため、切り取る範囲を作れません。先に "
                "`kaigyou-etl run mlit_municipalities --prefecture "
                f"{self.ctx.prefecture_code}` を実行するか、config/sources.yaml "
                "の bbox に範囲を書いてください。**範囲なしで地方全体を入れると、"
                "交差点の分割に何十分もかかったうえ、使わない道路が経路探索を"
                "重くします。**")
        self._bbox_cache = box
        return box

    def _prefecture_bbox(self, conn: psycopg.Connection, margin_m: float,
                         ) -> tuple[float, float, float, float] | None:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ST_XMin(e) AS x0, ST_YMin(e) AS y0,
                       ST_XMax(e) AS x1, ST_YMax(e) AS y1
                FROM (
                    -- ST_Extent は box2d を返すので、いちど geometry に
                    -- してから geography に渡します（box2d は直接キャスト
                    -- できず、握りつぶすと黙って設定の箱に落ちます）。
                    SELECT ST_Envelope(ST_Buffer(
                        ST_SetSRID(ST_Extent(geom)::geometry, 4326)::geography,
                        %s)::geometry) AS e
                    FROM municipalities WHERE prefecture_code = %s
                ) s
                """, (margin_m, self.ctx.prefecture_code))
            row = cur.fetchone()
        if not row or row["x0"] is None:
            return None
        return (float(row["x0"]), float(row["y0"]),
                float(row["x1"]), float(row["y1"]))

    def _rows(self, artifact: Path):
        fields, records = read_shapefile(
            artifact, member_prefix=self.spec.get("shapefile_member"), encoding="utf-8")
        if not records:
            raise AcquisitionError(ERROR_EMPTY, f"{artifact.name}: shapefile has no records")
        return fields, records

    # -------------------------------------------------------------- pipeline
    def validate(self, artifact: Path) -> dict[str, Any]:
        fields, records = self._rows(artifact)
        class_col = self.pick_column(fields, "road_class")
        walkable = self.walkable_classes()
        if not walkable:
            raise AcquisitionError(
                ERROR_SCHEMA,
                "walkable_classes is empty in sources.yaml; refusing to load a "
                "network with no idea which roads a person may walk along")

        box = self.bbox()
        counts: dict[str, int] = {}
        kept = edges = outside = 0
        for record in records:
            value = str(record.record[fields.index(class_col)] or "").strip().lower()
            counts[value] = counts.get(value, 0) + 1
            if value not in walkable:
                continue
            if box and not self._in_bbox(record.shape, box):
                outside += 1
                continue
            parts = list(_linestrings(record.shape))
            if parts:
                kept += 1
                edges += len(parts)

        facts: dict[str, Any] = {
            "feature_count": len(records),
            "fields": fields,
            "road_class_column": class_col,
            "walkable_features": kept,
            "edges": edges,
            "excluded_outside_bbox": outside,
            # The full histogram, so a release that renames a class shows up as
            # a large unfamiliar bucket rather than as a quietly smaller network.
            "road_class_counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
            "classes_not_walkable": sorted(set(counts) - walkable),
        }
        if kept == 0:
            raise AcquisitionError(
                ERROR_EMPTY,
                f"{artifact.name}: no feature matched walkable_classes "
                f"{sorted(walkable)}; the file has {sorted(counts)[:15]}")
        return facts

    @staticmethod
    def _in_bbox(shape: Any, box: tuple[float, float, float, float]) -> bool:
        min_lng, min_lat, max_lng, max_lat = box
        try:
            x0, y0, x1, y1 = shape.bbox
        except (AttributeError, ValueError):
            return True
        return not (x1 < min_lng or x0 > max_lng or y1 < min_lat or y0 > max_lat)

    def transform(self, artifact: Path) -> Iterator[dict[str, Any]]:
        fields, records = self._rows(artifact)
        idx = {name: fields.index(name) for name in fields}
        class_col = self.pick_column(fields, "road_class")
        id_col = self.pick_column(fields, "osm_id", required=False)
        name_col = self.pick_column(fields, "name", required=False)
        walkable = self.walkable_classes()
        box = self.bbox()
        source_date = self.source_date() or date.today()

        for record in records:
            values = record.record
            road_class = str(values[idx[class_col]] or "").strip().lower()
            if road_class not in walkable:
                continue
            if box and not self._in_bbox(record.shape, box):
                continue
            osm_id = str(values[idx[id_col]]).strip() if id_col else None
            name = (str(values[idx[name_col]]).strip() or None) if name_col else None
            for points in _linestrings(record.shape):
                yield {
                    "osm_id": osm_id,
                    "name": name,
                    "road_class": road_class,
                    "geom_wkt": _wkt(points),
                    "prefecture_code": self.ctx.prefecture_code,
                    "source_date": source_date,
                }

    def load(self, conn: psycopg.Connection, records: Iterable[dict[str, Any]]) -> int:
        # **列が無いまま入れると、2 つ目の県が 1 つ目を消します。** 取り込みは
        # source_id で全置換していて、source_id に県は入っていません。書く側は
        # 止まるのが正しい（読む側と逆です。docs/refactoring-multi-specialty.md）。
        if not column_exists(conn, "walk_network", "prefecture_code"):
            raise AcquisitionError(
                ERROR_SCHEMA,
                "walk_network に prefecture_code がありません。先に "
                "`kaigyou-etl migrate` を実行してください（マイグレーション 031）。"
                "県を記録せずに取り込むと、2 つ目の県を入れた時点で 1 つ目の"
                "道路網が消えます（しかも成功と表示されます）。")

        rows = [rec | {"source_id": self.source_id} for rec in records]
        with conn.cursor() as cur:
            # **県で絞って置き換えます。** 絞らないと、静岡を入れた時点で
            # 東京の道路網が消えます（メッシュ統計で同じことが起き、012 で
            # 直したのと同じ形です）。
            cur.execute("DELETE FROM walk_network "
                        "WHERE source_id = %s AND prefecture_code = %s",
                        (self.source_id, self.ctx.prefecture_code))
            count = self.insert_many(
                cur,
                """
                    INSERT INTO walk_network (
                        source_id, osm_id, name, road_class, geom, cost_m,
                        prefecture_code, source_date
                    ) VALUES (
                        %(source_id)s, %(osm_id)s, %(name)s, %(road_class)s,
                        ST_GeomFromText(%(geom_wkt)s, 4326),
                        ST_Length(ST_GeomFromText(%(geom_wkt)s, 4326)::geography),
                        %(prefecture_code)s, %(source_date)s
                    )
                """,
                rows,
            )
            # Zero-length edges would be nodes pretending to be streets.
            cur.execute("DELETE FROM walk_network WHERE cost_m <= 0")
        conn.commit()
        summary = build_topology(
            conn, tolerance_deg=float(self.spec.get("topology_tolerance_deg") or 0.00001),
            progress=print)
        # The connected share decides whether catchments are believable at
        # all; a low number means the tolerance is wrong for this extract.
        if summary.get("topology") == "built":
            print(f"  ノード {summary['nodes']:,} / 分割後エッジ "
                  f"{summary['noded_edges']:,} / 最大連結成分 "
                  f"{summary['largest_component_share']:.1%}")
            # 県ごとに独立した網なので、閾値も県の数で割ります。割らないと、
            # 2 県目を入れた瞬間に毎回この警告が出て、本物の分断が埋もれます。
            floor = 0.8 * (summary.get("expected_component_share") or 1.0)
            if (summary.get("largest_component_share") or 0) < floor:
                print(f"  警告: ネットワークが分断されています"
                      f"（最大成分 {summary['largest_component_share']:.1%}、"
                      f"{summary.get('prefectures', 1)} 県なら "
                      f"{floor:.1%} 以上が目安）。"
                      "topology_tolerance_deg の見直しが必要かもしれません。")
        return count


def build_topology(conn: psycopg.Connection, *, tolerance_deg: float = 0.00001,
                   progress: Any = None) -> dict[str, Any]:
    """Turn a pile of lines into a graph pgRouting can actually search.

    Two steps, and the first is the one that is easy to leave out.

    ``pgr_nodeNetwork`` splits every edge where another crosses it. Without it
    ``pgr_createTopology`` joins only lines that share an endpoint, and a
    crossing is not an endpoint -- so a grid of streets that all meet becomes a
    graph of disconnected fragments. Measured on the test fixture: 25 edges,
    largest connected component 4, and a 900m catchment covering 2% of the area
    it should. Real extracts are worse, not better, because one residential way
    runs through many junctions without being split at any.

    ``pgr_createTopology`` then fills source/target on the noded table by
    matching endpoints within a tolerance. Too small and streets that meet do
    not join; too large and a bridge welds itself to the road underneath. About
    a metre is the usual compromise.

    Skipped, with a note, where pgRouting is not installed: the network still
    loads and draws, and catchments fall back to circles.
    """
    say = progress or (lambda _msg: None)
    with conn.cursor() as cur:
        cur.execute("SELECT to_regproc('pgr_nodenetwork') AS fn")
        if cur.fetchone()["fn"] is None:
            return {"topology": "skipped", "reason": "pgrouting not installed"}

        cur.execute("SELECT count(*) AS n FROM walk_network")
        say(f"  交差点で分割しています（{cur.fetchone()['n']:,} 本）。"
            "数分〜数十分かかることがあります...")

        # Both, not just the edges. pgr_createTopology adds to an existing
        # vertices table rather than rebuilding it, so a second load leaves the
        # nodes of the first behind -- and kg_walk_catchment snaps to that
        # table, so a stale vertex can be chosen as the start of a route that
        # goes nowhere. It shows up as a connectivity share that halves on
        # every reload.
        cur.execute("DROP TABLE IF EXISTS walk_network_noded_vertices_pgr")
        cur.execute("DROP TABLE IF EXISTS walk_network_noded")
        cur.execute("SELECT pgr_nodeNetwork('walk_network', %s, 'id', 'geom')",
                    (tolerance_deg,))
        # pgr_nodeNetwork carries no cost; the split pieces need their own.
        cur.execute("ALTER TABLE walk_network_noded ADD COLUMN IF NOT EXISTS "
                    "cost_m double precision")
        cur.execute("UPDATE walk_network_noded "
                    "SET cost_m = ST_Length(geom::geography)")
        cur.execute("DELETE FROM walk_network_noded WHERE cost_m <= 0")
        cur.execute("CREATE INDEX IF NOT EXISTS walk_network_noded_geom_idx "
                    "ON walk_network_noded USING gist (geom)")
        say("  ノードを接続しています...")
        cur.execute("SELECT pgr_createTopology('walk_network_noded', %s, 'geom', 'id')",
                    (tolerance_deg,))
        cur.execute("CREATE INDEX IF NOT EXISTS walk_network_noded_source_idx "
                    "ON walk_network_noded (source)")
        cur.execute("CREATE INDEX IF NOT EXISTS walk_network_noded_target_idx "
                    "ON walk_network_noded (target)")

        cur.execute("SELECT count(*) AS n FROM walk_network_noded")
        edges = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM walk_network_noded_vertices_pgr")
        nodes = cur.fetchone()["n"]
        # How connected the result is decides whether catchments are believable.
        # A street network should be one dominant component; a low share here
        # means noding or the tolerance is wrong, and every catchment computed
        # afterwards will be quietly too small.
        #
        # pgr_connectedComponents returns one row per NODE, not per edge, so
        # this is a share of nodes and has to be compared against the node
        # count -- against the edge count it reads as a fraction of a network
        # that is in fact fully connected.
        cur.execute("""
            SELECT count(*) AS n
            FROM pgr_connectedComponents(
                'SELECT id, source, target, cost_m AS cost, cost_m AS reverse_cost
                   FROM walk_network_noded
                  WHERE source IS NOT NULL AND target IS NOT NULL')
            GROUP BY component ORDER BY n DESC LIMIT 1
        """)
        row = cur.fetchone()
        largest = row["n"] if row else 0

        # **県が 2 つ入っていれば、成分も 2 つあるのが正しい姿です。**
        # 東京と静岡の道は繋がっていません。全体に対する割合で見ると、
        # 2 県目を入れた瞬間に「ネットワークが分断されています」と警告が
        # 出ます。警告が当たり前になると、本物の分断を見落とします。
        #
        # 数えるのは、いちばん大きい成分が**入っている県の数**に対して
        # 妥当かどうか。県が n 個なら、最大成分は全体の 1/n 前後で正常です。
        cur.execute("SELECT count(DISTINCT prefecture_code) AS n FROM walk_network"
                    if column_exists(conn, "walk_network", "prefecture_code")
                    else "SELECT 1 AS n")
        prefectures = max(1, int((cur.fetchone() or {}).get("n") or 1))
    conn.commit()
    return {
        "topology": "built",
        "noded_edges": edges,
        "nodes": nodes,
        "prefectures": prefectures,
        "largest_component_nodes": largest,
        "largest_component_share": round(largest / nodes, 3) if nodes else None,
        # 県が n 個なら、最大成分は 1/n 前後が正常です。これを下回るときだけ
        # 分断を疑います。
        "expected_component_share": round(1.0 / prefectures, 3),
    }
