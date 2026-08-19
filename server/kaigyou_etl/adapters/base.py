"""Adapter contract.

One adapter per publisher. Each implements the four steps separately so a
change at the publisher's end -- a renamed column, a moved URL, a new archive
layout -- is contained to a single class and, wherever possible, to
``config/sources.yaml`` rather than to code.

    download  -> fetch bytes to data/raw/<source_id>/, or accept a local file
    validate  -> confirm the artefact is the shape we expect
    transform -> yield normalised records (plain dicts)
    load      -> write them to PostGIS in one transaction
"""
from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

import psycopg

from kaigyou_etl.acquisition import (
    ERROR_HTTP,
    ERROR_INPUT_MISSING,
    ERROR_NETWORK_BLOCKED,
    ERROR_NOT_CONFIGURED,
    ERROR_SCHEMA,
    ERROR_TIMEOUT,
    AcquisitionError,
)


@dataclass
class AdapterContext:
    """Everything an adapter needs that is not part of its own spec."""

    source_id: str
    spec: Mapping[str, Any]
    defaults: Mapping[str, Any]
    raw_dir: Path
    input_path: Path | None = None
    offline: bool = False
    #: Restrict a multi-prefecture source to one prefecture code. None loads all.
    prefecture_filter: str | None = None
    #: Prior-period artefact, for sources that derive a change rate from two rounds.
    baseline_path: Path | None = None

    @property
    def prefecture_code(self) -> str:
        return str(self.spec.get("prefecture_code")
                   or self.defaults.get("prefecture_code") or "13")

    @property
    def timeout(self) -> float:
        return float(self.spec.get("timeout_seconds")
                     or self.defaults.get("timeout_seconds") or 120)

    @property
    def user_agent(self) -> str:
        return str(self.defaults.get("user_agent") or "KaigyouSupport-ETL/0.1")


class SourceAdapter(ABC):
    """Base class for every external data source."""

    #: Tables this adapter replaces on load. Used by `status` and by --replace.
    target_tables: Sequence[str] = ()

    def __init__(self, ctx: AdapterContext):
        self.ctx = ctx
        self.spec = ctx.spec

    # ------------------------------------------------------------------ meta
    @property
    def source_id(self) -> str:
        return self.ctx.source_id

    def source_row(self) -> dict[str, Any]:
        """The ``data_sources`` row describing this source."""
        return {
            "id": self.source_id,
            "name": self.spec.get("name", self.source_id),
            "publisher": self.spec.get("publisher", "unknown"),
            "dataset_kind": self.spec.get("dataset_kind", "official"),
            "license": self.spec.get("license"),
            "homepage_url": self.spec.get("homepage_url"),
            "terms_url": self.spec.get("terms_url"),
            "notes": self.spec.get("notes"),
        }

    def source_date(self) -> date | None:
        value = self.spec.get("source_date")
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            return date.fromisoformat(value)
        return None

    # -------------------------------------------------------------- download
    def download(self) -> Path:
        """Fetch the artefact, or adopt a locally supplied one.

        A caller-supplied ``--input`` always wins: several of these datasets sit
        behind a click-through form, and a hand-downloaded file is a legitimate
        way to feed the pipeline.
        """
        if self.ctx.input_path is not None:
            src = self.ctx.input_path
            if not src.exists():
                raise AcquisitionError(
                    ERROR_INPUT_MISSING, f"--input file does not exist: {src}"
                )
            self.ctx.raw_dir.mkdir(parents=True, exist_ok=True)
            dest = self.ctx.raw_dir / src.name
            if src.resolve() != dest.resolve():
                shutil.copy2(src, dest)
            return dest

        url = self.spec.get("url")
        if not url:
            raise AcquisitionError(
                ERROR_NOT_CONFIGURED,
                f"no url configured for {self.source_id} and no --input given; "
                f"see config/sources.yaml",
            )
        if self.ctx.offline:
            raise AcquisitionError(
                ERROR_NOT_CONFIGURED,
                "offline mode: refusing to download; supply --input instead",
                url=url,
            )
        return self.fetch(url, self.spec.get("params") or {})

    def fetch(self, url: str, params: Mapping[str, Any] | None = None) -> Path:
        """HTTP GET with failure causes we can report honestly."""
        import httpx

        self.ctx.raw_dir.mkdir(parents=True, exist_ok=True)
        dest = self.ctx.raw_dir / self.filename_for(url)
        headers = {"User-Agent": self.ctx.user_agent}
        try:
            with httpx.Client(timeout=self.ctx.timeout, follow_redirects=True,
                              headers=headers) as client:
                with client.stream("GET", url, params=dict(params or {})) as resp:
                    if resp.status_code >= 400:
                        raise AcquisitionError(
                            ERROR_HTTP,
                            f"HTTP {resp.status_code} from {url}",
                            http_status=resp.status_code, url=url,
                        )
                    with dest.open("wb") as fh:
                        for chunk in resp.iter_bytes():
                            fh.write(chunk)
        except httpx.TimeoutException as exc:
            raise AcquisitionError(
                ERROR_TIMEOUT, f"timed out after {self.ctx.timeout}s: {exc}", url=url
            ) from exc
        except httpx.HTTPError as exc:
            # Proxy denials, DNS failures and TLS problems all land here. The
            # distinction that matters to the operator is "we never reached the
            # publisher", so report that rather than guessing further.
            raise AcquisitionError(
                ERROR_NETWORK_BLOCKED,
                f"could not reach {url}: {type(exc).__name__}: {exc}",
                url=url,
            ) from exc
        return dest

    def filename_for(self, url: str) -> str:
        tail = url.rstrip("/").split("/")[-1].split("?")[0]
        return tail or f"{self.source_id}.download"

    # ------------------------------------------------------- validate/transform
    @abstractmethod
    def validate(self, artifact: Path) -> dict[str, Any]:
        """Raise :class:`AcquisitionError` if the artefact is not usable.

        Returns a small dict of facts about it (row counts, detected columns)
        that gets stored on the validate step record.
        """

    @abstractmethod
    def transform(self, artifact: Path) -> Iterator[dict[str, Any]]:
        """Yield normalised records ready for :meth:`load`."""

    @abstractmethod
    def load(self, conn: psycopg.Connection, records: Iterable[dict[str, Any]]) -> int:
        """Write records, returning the number stored."""

    # ---------------------------------------------------------------- helpers
    def column_map(self) -> dict[str, list[str]]:
        raw = self.spec.get("columns") or {}
        return {k: (v if isinstance(v, list) else [v]) for k, v in raw.items()}

    def pick_column(self, available: Sequence[str], field: str,
                    required: bool = True) -> str | None:
        """Resolve a logical field to whichever published header is present."""
        candidates = self.column_map().get(field, [])
        lookup = {c.strip().lower(): c for c in available}
        for candidate in candidates:
            hit = lookup.get(str(candidate).strip().lower())
            if hit is not None:
                return hit
        if required:
            raise AcquisitionError(
                ERROR_SCHEMA,
                f"{self.source_id}: no column for {field!r}; tried {candidates}, "
                f"file has {list(available)[:40]}",
            )
        return None
