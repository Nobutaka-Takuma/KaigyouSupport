"""What was obtained, and what was not.

The one query behind both ``kaigyou-etl status`` and ``GET /api/data-status``.
It reports each configured source's latest attempt at every step, the rows
currently attributed to it, and -- when the attempt failed -- the cause. A
source that is configured but has never been run appears too, as
``never_attempted``: silence is not the same as success.
"""
from __future__ import annotations

from typing import Any

import psycopg

from kaigyou_core import config as cfg

# Which table each adapter fills, for the row counts below.
ADAPTER_TABLES = {
    "mhlw_clinics": "facilities",
    "mhlw_specialties": "facility_specialties",
    "estat_mesh": "population_mesh",
    "estat_business_mesh": "mesh_business",
    "mlit_stations": "stations",
    "mlit_municipalities": "municipalities",
    "osm_walk_network": "walk_network",
    "mlit_land_prices": "land_prices",
}

_TABLES = ("facilities", "facility_specialties", "population_mesh",
           "mesh_business", "stations", "municipalities", "walk_network",
           "land_prices")

_TABLE_LABELS = {
    "facilities": "歯科医院",
    "facility_specialties": "診療科目・診療時間",
    "population_mesh": "人口メッシュ",
    "mesh_business": "事業所・従業者メッシュ",
    "stations": "駅",
    "municipalities": "行政区域",
    "walk_network": "街路ネットワーク",
    "land_prices": "地価公示",
}


def _row_counts(conn: psycopg.Connection) -> dict[str, dict[str, int]]:
    from kaigyou_core.db import table_exists

    counts: dict[str, dict[str, int]] = {}
    with conn.cursor() as cur:
        for table in _TABLES:
            if not table_exists(conn, table):
                continue
            cur.execute(f"SELECT source_id, count(*) AS n FROM {table} GROUP BY source_id")
            for row in cur.fetchall():
                counts.setdefault(row["source_id"], {})[table] = row["n"]
    return counts


def _latest_steps(conn: psycopg.Connection) -> dict[str, dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (source_id, step)
                   source_id, step, status, target_url, http_status, record_count,
                   error_type, error_message, finished_at
            FROM acquisition_runs
            ORDER BY source_id, step, started_at DESC
            """
        )
        out: dict[str, dict[str, Any]] = {}
        for row in cur.fetchall():
            out.setdefault(row["source_id"], {})[row["step"]] = {
                "status": row["status"],
                "target_url": row["target_url"],
                "http_status": row["http_status"],
                "record_count": row["record_count"],
                "error_type": row["error_type"],
                "error_message": row["error_message"],
                "finished_at": row["finished_at"],
            }
        return out


def _registered_sources(conn: psycopg.Connection) -> dict[str, dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM data_sources ORDER BY id")
        return {r["id"]: dict(r) for r in cur.fetchall()}


def data_status(conn: psycopg.Connection) -> dict[str, Any]:
    configured = (cfg.sources_config().get("sources") or {})
    registered = _registered_sources(conn)
    steps = _latest_steps(conn)
    counts = _row_counts(conn)

    sources: list[dict[str, Any]] = []
    for source_id in sorted(set(configured) | set(registered)):
        spec = configured.get(source_id, {})
        db_row = registered.get(source_id, {})
        source_steps = steps.get(source_id, {})
        rows = counts.get(source_id, {})
        total_rows = sum(rows.values())

        state, reason = _classify(source_steps, total_rows)
        sources.append({
            "source_id": source_id,
            "name": db_row.get("name") or spec.get("name") or source_id,
            "publisher": db_row.get("publisher") or spec.get("publisher"),
            "dataset_kind": db_row.get("dataset_kind") or spec.get("dataset_kind", "official"),
            "license": db_row.get("license") or spec.get("license"),
            "homepage_url": db_row.get("homepage_url") or spec.get("homepage_url"),
            "terms_url": db_row.get("terms_url") or spec.get("terms_url"),
            "configured_url": spec.get("url"),
            "target_table": ADAPTER_TABLES.get(spec.get("adapter")),
            "state": state,
            "reason": reason,
            "row_counts": rows,
            "row_total": total_rows,
            "steps": source_steps,
            "last_attempt_at": max(
                (s["finished_at"] for s in source_steps.values() if s.get("finished_at")),
                default=None,
            ),
        })

    official_loaded = [s for s in sources
                       if s["dataset_kind"] == "official" and s["row_total"] > 0]
    sample_loaded = [s for s in sources
                     if s["dataset_kind"] == "sample" and s["row_total"] > 0]

    # A table holding both real and synthetic rows double-counts: the analysis
    # queries by facility_category, not by source, so 8,000 real clinics plus
    # 2,000 generated ones look like 10,000 competitors. Surface it loudly --
    # this is a wrong-answer condition, not a cosmetic one.
    mixed = []
    for table in _TABLES:
        kinds = {
            (registered.get(sid, {}).get("dataset_kind") or "official")
            for sid, c in counts.items() if c.get(table)
        }
        if {"official", "sample"} <= kinds:
            mixed.append({
                "dataset": table,
                "dataset_label": _TABLE_LABELS.get(table, table),
                "message": (
                    f"{_TABLE_LABELS.get(table, table)}に実データと合成データが同時に存在します。"
                    f"集計が二重計上になります。`kaigyou-etl drop-sample` で合成データを削除してください。"
                ),
            })

    return {
        "sources": sources,
        "contains_sample_data": bool(sample_loaded),
        "mixed_datasets": mixed,
        "official_sources_loaded": len(official_loaded),
        "official_sources_configured": sum(
            1 for s in sources if s["dataset_kind"] == "official"
        ),
        "sample_sources_loaded": len(sample_loaded),
        "table_row_totals": {
            table: sum(c.get(table, 0) for c in counts.values()) for table in _TABLES
        },
        "table_row_totals_official": {
            table: sum(
                c.get(table, 0) for sid, c in counts.items()
                if (registered.get(sid, {}).get("dataset_kind") or "official") == "official"
            )
            for table in _TABLES
        },
    }


def _classify(steps: dict[str, dict[str, Any]], row_total: int) -> tuple[str, str | None]:
    if not steps:
        return "never_attempted", "この情報源はまだ取得を試行していません。"
    load = steps.get("load")
    if load and load["status"] == "success" and row_total > 0:
        return "loaded", None
    for step in ("download", "validate", "transform", "load"):
        info = steps.get(step)
        if info and info["status"] == "failed":
            return "failed", f"{step}: {info.get('error_type')}: {info.get('error_message')}"
    if load and load["status"] == "success" and row_total == 0:
        return "empty", "取り込みは成功しましたが、対象データが0件でした。"
    return "incomplete", "取得処理が完了していません。"
