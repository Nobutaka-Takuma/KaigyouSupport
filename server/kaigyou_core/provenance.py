"""Per-response data attribution.

Every API response that carries numbers also carries the sources those numbers
came from, their publication dates, and whether any of them is synthetic. The
UI renders this; it is not optional decoration.
"""
from __future__ import annotations

from typing import Any, Iterable

import psycopg

TABLE_LABELS = {
    "facilities": "歯科医院",
    "population_mesh": "人口メッシュ",
    "stations": "駅",
    "municipalities": "行政区域",
}


def for_tables(conn: psycopg.Connection, tables: Iterable[str]) -> dict[str, Any]:
    tables = list(tables)
    entries: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        for table in tables:
            cur.execute(
                f"""
                SELECT ds.id, ds.name, ds.publisher, ds.dataset_kind, ds.license,
                       ds.homepage_url, ds.terms_url,
                       count(t.*)  AS row_count,
                       max(t.source_date)  AS source_date,
                       max(t.last_updated) AS last_updated
                FROM {table} t
                JOIN data_sources ds ON ds.id = t.source_id
                GROUP BY ds.id, ds.name, ds.publisher, ds.dataset_kind, ds.license,
                         ds.homepage_url, ds.terms_url
                """
            )
            for row in cur.fetchall():
                entries.append({
                    "dataset": table,
                    "dataset_label": TABLE_LABELS.get(table, table),
                    "source_id": row["id"],
                    "name": row["name"],
                    "publisher": row["publisher"],
                    "dataset_kind": row["dataset_kind"],
                    "license": row["license"],
                    "homepage_url": row["homepage_url"],
                    "terms_url": row["terms_url"],
                    "row_count": row["row_count"],
                    "source_date": row["source_date"],
                    "last_updated": row["last_updated"],
                })

    missing = [t for t in tables if not any(e["dataset"] == t for e in entries)]
    return {
        "sources": entries,
        "contains_sample_data": any(e["dataset_kind"] == "sample" for e in entries),
        "datasets_unavailable": [
            {"dataset": t, "dataset_label": TABLE_LABELS.get(t, t)} for t in missing
        ],
    }
