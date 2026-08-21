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

from kaigyou_etl.acquisition import ERROR_EMPTY, ERROR_SCHEMA, AcquisitionError
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

    def bbox(self) -> tuple[float, float, float, float] | None:
        """Optional clip, as (min_lng, min_lat, max_lng, max_lat).

        A Kanto extract is far larger than the prefecture being analysed, and
        every edge outside it is dead weight in the routing graph.
        """
        box = self.spec.get("bbox")
        return tuple(float(v) for v in box) if box else None  # type: ignore[return-value]

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
                    "source_date": source_date,
                }

    def load(self, conn: psycopg.Connection, records: Iterable[dict[str, Any]]) -> int:
        rows = [rec | {"source_id": self.source_id} for rec in records]
        with conn.cursor() as cur:
            cur.execute("DELETE FROM walk_network WHERE source_id = %s", (self.source_id,))
            count = self.insert_many(
                cur,
                """
                    INSERT INTO walk_network (
                        source_id, osm_id, name, road_class, geom, cost_m, source_date
                    ) VALUES (
                        %(source_id)s, %(osm_id)s, %(name)s, %(road_class)s,
                        ST_GeomFromText(%(geom_wkt)s, 4326),
                        ST_Length(ST_GeomFromText(%(geom_wkt)s, 4326)::geography),
                        %(source_date)s
                    )
                """,
                rows,
            )
            # Zero-length edges would be nodes pretending to be streets.
            cur.execute("DELETE FROM walk_network WHERE cost_m <= 0")
        conn.commit()
        build_topology(conn, tolerance_deg=float(self.spec.get("topology_tolerance_deg") or 0.00001))
        return count


def build_topology(conn: psycopg.Connection, *, tolerance_deg: float = 0.00001) -> dict[str, Any]:
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
    with conn.cursor() as cur:
        cur.execute("SELECT to_regproc('pgr_nodenetwork') AS fn")
        if cur.fetchone()["fn"] is None:
            return {"topology": "skipped", "reason": "pgrouting not installed"}

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
    conn.commit()
    return {
        "topology": "built",
        "noded_edges": edges,
        "nodes": nodes,
        "largest_component_nodes": largest,
        "largest_component_share": round(largest / nodes, 3) if nodes else None,
    }
