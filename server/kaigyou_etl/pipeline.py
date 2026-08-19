"""Runs one source through download -> validate -> transform -> load.

Each step is recorded separately, so a run that gets the file but chokes on a
renamed column is distinguishable from a run that never reached the publisher.
When a step fails the following steps are recorded as ``skipped`` and the
target tables are left untouched.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import psycopg

from kaigyou_core import config as cfg
from kaigyou_etl.acquisition import (
    AcquisitionError,
    AcquisitionLog,
    StepRecord,
    sha256_of,
)
from kaigyou_etl.adapters import AdapterContext, get_adapter


@dataclass
class RunResult:
    source_id: str
    ok: bool
    steps: dict[str, str] = field(default_factory=dict)
    record_count: int = 0
    error_type: str | None = None
    error_message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def upsert_source(conn: psycopg.Connection, row: Mapping[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO data_sources (id, name, publisher, dataset_kind, license,
                                      homepage_url, terms_url, notes)
            VALUES (%(id)s, %(name)s, %(publisher)s, %(dataset_kind)s, %(license)s,
                    %(homepage_url)s, %(terms_url)s, %(notes)s)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                publisher = EXCLUDED.publisher,
                dataset_kind = EXCLUDED.dataset_kind,
                license = EXCLUDED.license,
                homepage_url = EXCLUDED.homepage_url,
                terms_url = EXCLUDED.terms_url,
                notes = EXCLUDED.notes
            """,
            dict(row),
        )
    if not conn.autocommit:
        conn.commit()


def run_source(source_id: str, *, input_path: Path | None = None,
               offline: bool = False,
               prefecture_filter: str | None = None,
               baseline_path: Path | None = None) -> RunResult:
    """Execute the full pipeline for one configured source."""
    sources = cfg.sources_config()
    specs = sources.get("sources") or {}
    if source_id not in specs:
        raise KeyError(f"unknown source {source_id!r}; configured: {sorted(specs)}")
    spec = specs[source_id]
    defaults = sources.get("defaults") or {}

    ctx = AdapterContext(
        source_id=source_id,
        spec=spec,
        defaults=defaults,
        raw_dir=cfg.data_dir() / "raw" / source_id,
        input_path=input_path,
        offline=offline,
        prefecture_filter=prefecture_filter,
        baseline_path=baseline_path,
    )
    adapter = get_adapter(spec["adapter"])(ctx)

    from kaigyou_core.db import connect

    with connect(autocommit=True) as audit_conn, connect() as data_conn:
        log = AcquisitionLog(audit_conn)
        upsert_source(audit_conn, adapter.source_row())
        result = RunResult(source_id=source_id, ok=False)

        # ---------------------------------------------------------- download
        artifact: Path | None = None
        try:
            with log.step(source_id, "download",
                          target_url=spec.get("url"),
                          params={"input": str(input_path) if input_path else None,
                                  "offline": offline,
                                  "prefecture_filter": prefecture_filter,
                                  "baseline": str(baseline_path) if baseline_path else None,
                                  }) as rec:
                artifact = adapter.download()
                rec.artifact_path = str(artifact)
                rec.bytes = artifact.stat().st_size
                rec.sha256 = sha256_of(artifact)
            result.steps["download"] = "success"
        except AcquisitionError as exc:
            result.steps["download"] = "failed"
            result.error_type, result.error_message = exc.error_type, str(exc)
            _skip_rest(log, source_id, ("validate", "transform", "load"), result, exc)
            return result

        # ---------------------------------------------------------- validate
        try:
            with log.step(source_id, "validate", artifact_path=str(artifact)) as rec:
                facts = adapter.validate(artifact)
                rec.params = facts
                rec.record_count = facts.get("row_count") or facts.get("feature_count")
            result.steps["validate"] = "success"
            result.details["validate"] = facts
        except AcquisitionError as exc:
            result.steps["validate"] = "failed"
            result.error_type, result.error_message = exc.error_type, str(exc)
            _skip_rest(log, source_id, ("transform", "load"), result, exc)
            return result

        # ------------------------------------------------- transform + load
        try:
            with log.step(source_id, "transform", artifact_path=str(artifact)) as rec:
                records = list(adapter.transform(artifact))
                rec.record_count = len(records)
            result.steps["transform"] = "success"
        except AcquisitionError as exc:
            result.steps["transform"] = "failed"
            result.error_type, result.error_message = exc.error_type, str(exc)
            _skip_rest(log, source_id, ("load",), result, exc)
            return result

        try:
            with log.step(source_id, "load") as rec:
                count = adapter.load(data_conn, records)
                data_conn.commit()
                rec.record_count = count
            result.steps["load"] = "success"
            result.record_count = count
            result.ok = True
        except Exception as exc:  # noqa: BLE001
            data_conn.rollback()
            result.steps["load"] = "failed"
            result.error_type = getattr(exc, "error_type", "database_error")
            result.error_message = str(exc)
            return result

    return result


def _skip_rest(log: AcquisitionLog, source_id: str, steps: tuple[str, ...],
               result: RunResult, cause: AcquisitionError) -> None:
    now = datetime.now(timezone.utc)
    for step in steps:
        log.write(StepRecord(
            source_id=source_id, step=step, status="skipped",
            error_type=cause.error_type,
            error_message=f"skipped: previous step failed ({cause.error_type})",
            started_at=now, finished_at=now,
        ))
        result.steps[step] = "skipped"
