"""Recording what was and was not obtained.

Every download / validate / transform / load attempt writes one row to
``acquisition_runs``. Failures are first-class: they carry a machine-readable
``error_type`` and the message, and they are surfaced by ``kaigyou-etl status``
and by ``GET /api/data-status``.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import psycopg

STEPS = ("download", "validate", "transform", "load")

# Machine-readable failure causes. Kept short so the UI can group by them.
ERROR_NETWORK_BLOCKED = "network_blocked"
ERROR_HTTP = "http_error"
ERROR_TIMEOUT = "timeout"
ERROR_MISSING_CREDENTIALS = "missing_credentials"
ERROR_NOT_CONFIGURED = "not_configured"
ERROR_INPUT_MISSING = "input_missing"
ERROR_PARSE = "parse_error"
ERROR_SCHEMA = "schema_mismatch"
ERROR_EMPTY = "empty_dataset"
ERROR_DB = "database_error"
ERROR_UNKNOWN = "unknown_error"


class AcquisitionError(Exception):
    """A step failed for a reason we can name."""

    def __init__(self, error_type: str, message: str, *,
                 http_status: int | None = None, url: str | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.http_status = http_status
        self.url = url


@dataclass
class StepRecord:
    source_id: str
    step: str
    status: str = "failed"
    target_url: str | None = None
    artifact_path: str | None = None
    http_status: int | None = None
    bytes: int | None = None
    sha256: str | None = None
    record_count: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class AcquisitionLog:
    """Writes step records. Uses its own autocommit connection so a failure
    inside a data transaction never rolls the audit trail back."""

    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    @contextmanager
    def step(self, source_id: str, step: str, **kwargs: Any) -> Iterator[StepRecord]:
        record = StepRecord(source_id=source_id, step=step, **kwargs)
        try:
            yield record
        except AcquisitionError as exc:
            record.status = "failed"
            record.error_type = exc.error_type
            record.error_message = str(exc)
            if exc.http_status is not None:
                record.http_status = exc.http_status
            if exc.url and not record.target_url:
                record.target_url = exc.url
            self.write(record)
            raise
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            record.status = "failed"
            record.error_type = ERROR_UNKNOWN
            record.error_message = f"{type(exc).__name__}: {exc}"
            self.write(record)
            raise
        else:
            if record.status == "failed":
                record.status = "success"
            self.write(record)

    def write(self, record: StepRecord) -> None:
        record.finished_at = record.finished_at or datetime.now(timezone.utc)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO acquisition_runs (
                    source_id, step, status, target_url, artifact_path, http_status,
                    bytes, sha256, record_count, error_type, error_message, params,
                    started_at, finished_at
                ) VALUES (
                    %(source_id)s, %(step)s, %(status)s, %(target_url)s, %(artifact_path)s,
                    %(http_status)s, %(bytes)s, %(sha256)s, %(record_count)s,
                    %(error_type)s, %(error_message)s, %(params)s,
                    %(started_at)s, %(finished_at)s
                )
                """,
                {
                    "source_id": record.source_id,
                    "step": record.step,
                    "status": record.status,
                    "target_url": record.target_url,
                    "artifact_path": record.artifact_path,
                    "http_status": record.http_status,
                    "bytes": record.bytes,
                    "sha256": record.sha256,
                    "record_count": record.record_count,
                    "error_type": record.error_type,
                    "error_message": record.error_message,
                    "params": psycopg.types.json.Json(record.params),
                    "started_at": record.started_at,
                    "finished_at": record.finished_at,
                },
            )
        if not self.conn.autocommit:
            self.conn.commit()
