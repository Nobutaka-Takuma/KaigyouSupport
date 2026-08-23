"""Work out which downloaded file belongs to which source.

Publishers rename their downloads between editions, and browsers add their own
suffixes, so matching on file names alone is unreliable -- the same S12 archive
arrives as ``S12-25_GML.zip`` or ``S1225_GML.zip`` depending on where it came
from. Every check here looks at the file's *contents* instead: the CSV header,
the statistics table's column ids, the members inside the archive.

Only the column candidates already declared in ``config/sources.yaml`` are
used, so a publisher's rename is still a one-line config change rather than a
code change.
"""
from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

DATA_SUFFIXES = (".csv", ".txt", ".zip")


@dataclass
class Discovery:
    """What was found in a directory, and what was not."""

    directory: Path
    clinics: Path | None = None
    mesh_current: Path | None = None
    mesh_baseline: Path | None = None
    mesh_business: Path | None = None
    walk_network: Path | None = None
    land_prices: Path | None = None
    stations: Path | None = None
    municipalities: Path | None = None
    unmatched: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def missing(self) -> list[str]:
        out = []
        if self.clinics is None:
            out.append("歯科診療所 CSV")
        if self.mesh_current is None:
            out.append("国勢調査メッシュ（最新年）")
        if self.stations is None:
            out.append("駅別乗降客数 S12 zip")
        if self.municipalities is None:
            out.append("行政区域 N03 zip")
        return out

    @property
    def optional_missing(self) -> list[str]:
        """Datasets that improve the analysis but are not required for it."""
        out = []
        if not self.mesh_business:
            out.append("経済センサス メッシュ（事業所・従業者数）")
        if not self.land_prices:
            out.append("地価公示 L01（参考情報として表示）")
        if not self.walk_network:
            out.append("OpenStreetMap 道路（徒歩圏の算出に使用）")
        return out

    def as_dict(self) -> dict[str, str | None]:
        return {
            "clinics": str(self.clinics) if self.clinics else None,
            "mesh_current": str(self.mesh_current) if self.mesh_current else None,
            "mesh_baseline": str(self.mesh_baseline) if self.mesh_baseline else None,
            "mesh_business": str(self.mesh_business) if self.mesh_business else None,
            "walk_network": str(self.walk_network) if self.walk_network else None,
            "land_prices": str(self.land_prices) if self.land_prices else None,
            "stations": str(self.stations) if self.stations else None,
            "municipalities": str(self.municipalities) if self.municipalities else None,
        }


def _candidates(spec: Mapping[str, Any], field_name: str) -> list[str]:
    raw = (spec.get("columns") or {}).get(field_name, [])
    values = raw if isinstance(raw, list) else [raw]
    return [str(v).strip().lower() for v in values]


def _header(path: Path) -> list[str]:
    """First CSV row, decoded with whichever of the usual encodings works."""
    raw = path.open("rb").readline()
    for encoding in ("utf-8-sig", "cp932", "utf-8"):
        try:
            line = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        return [c.strip().strip('"').lower() for c in next(csv.reader(io.StringIO(line)), [])]
    return []


def _zip_members(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as zf:
            return [n.lower() for n in zf.namelist()]
    except (zipfile.BadZipFile, OSError):
        return []


def discover(directory: Path, sources: Mapping[str, Any]) -> Discovery:
    """Classify every data file in ``directory`` by looking inside it."""
    found = Discovery(directory=directory)
    if not directory.is_dir():
        raise NotADirectoryError(directory)

    clinic_spec = sources.get("mhlw_dental_clinics", {})
    mesh_spec = sources.get("estat_population_mesh", {})
    business_spec = sources.get("estat_business_mesh", {})
    baseline_spec = (mesh_spec.get("growth_baseline") or {})

    clinic_keys = set(_candidates(clinic_spec, "facility_id")) | set(
        _candidates(clinic_spec, "lat"))
    mesh_code_keys = set(_candidates(mesh_spec, "mesh_code"))
    mesh_current_keys = set(_candidates(mesh_spec, "population"))
    mesh_baseline_keys = {
        str(v).strip().lower()
        for v in ((baseline_spec.get("columns") or {}).get("population") or [])
    }
    mesh_business_keys = set(_candidates(business_spec, "workers"))

    for path in sorted(p for p in directory.iterdir()
                       if p.is_file() and p.suffix.lower() in DATA_SUFFIXES):
        if path.suffix.lower() == ".zip":
            members = " ".join(_zip_members(path))
            if "gis_osm_roads" in members:
                _assign(found, "walk_network", path)
            elif "numberofpassengers" in members or "s12-" in members:
                _assign(found, "stations", path)
            elif "l01-" in members or "l01" in path.name.lower():
                _assign(found, "land_prices", path)
            elif "n03-" in members or "n03" in path.name.lower():
                _assign(found, "municipalities", path)
            else:
                found.unmatched.append(path)
            continue

        header = set(_header(path))
        if not header:
            found.unmatched.append(path)
            continue

        if header & mesh_code_keys:
            # Both census rounds share KEY_CODE; the statistics table id in the
            # column names is what separates them.
            # All the mesh tables share KEY_CODE; the statistics table id in
            # the column names is what separates population from workers, and
            # this census round from the previous one.
            if header & mesh_current_keys:
                _assign(found, "mesh_current", path)
            elif header & mesh_baseline_keys:
                _assign(found, "mesh_baseline", path)
            elif header & mesh_business_keys:
                _assign(found, "mesh_business", path)
            else:
                found.unmatched.append(path)
                found.notes.append(
                    f"{path.name}: メッシュ統計だが、設定済みの列IDと一致しません"
                    f"（config/sources.yaml の columns.population を確認してください）"
                )
        elif header & clinic_keys:
            _assign(found, "clinics", path)
        else:
            found.unmatched.append(path)

    if found.mesh_current is None and found.mesh_baseline is not None:
        found.notes.append(
            "メッシュファイルが基準年のものだけです。最新年のファイルも置いてください。"
        )
    if found.walk_network is None:
        found.notes.append(
            "OpenStreetMap の道路データがないため、商圏は円（直線距離）のみになります。"
            "徒歩圏（街路網に沿った距離）は選べません。"
        )
    if found.land_prices is None:
        found.notes.append(
            "地価公示（L01）のファイルがないため、地価は表示されません。"
            "分析・スコアには影響しません。"
        )
    if found.mesh_business is None:
        found.notes.append(
            "経済センサスのメッシュファイルがないため、従業者数（昼の需要）は"
            "算出されません。夜間人口のみの評価になります。"
        )
    if found.mesh_current is not None and found.mesh_baseline is None:
        found.notes.append(
            "基準年のメッシュファイルがないため、人口増減率は算出されません"
            "（成長スコアは「算出不可」になります）。"
        )
    return found


def _assign(found: Discovery, slot: str, path: Path) -> None:
    existing = getattr(found, slot)
    if existing is None:
        setattr(found, slot, path)
        return
    # Two files claim the same slot: keep the newer one and say so, rather
    # than silently picking whichever the filesystem listed first.
    keep, drop = ((path, existing) if path.stat().st_mtime > existing.stat().st_mtime
                  else (existing, path))
    setattr(found, slot, keep)
    found.unmatched.append(drop)
    found.notes.append(
        f"{slot}: 候補が複数あります。更新日時の新しい {keep.name} を使います"
        f"（{drop.name} は無視）。"
    )


def describe(found: Discovery) -> Iterable[str]:
    labels = {
        "clinics": "歯科診療所",
        "mesh_current": "人口メッシュ（最新年）",
        "mesh_baseline": "人口メッシュ（基準年）",
        "mesh_business": "事業所・従業者メッシュ",
        "walk_network": "街路ネットワーク（OSM）",
        "stations": "駅別乗降客数",
        "municipalities": "行政区域",
        "land_prices": "地価公示（L01）",
    }
    for slot, label in labels.items():
        path = getattr(found, slot)
        mark = "OK  " if path else "なし"
        yield f"  [{mark}] {label:<22} {path.name if path else '-'}"
