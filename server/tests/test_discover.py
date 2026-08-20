"""Matching downloaded files to sources by their contents.

Names are not dependable: the same S12 archive is published as
``S12-25_GML.zip`` and lands as ``S1225_GML.zip`` depending on where it came
from, and the census rounds differ only by a statistics-table id buried in the
column names. These tests use deliberately unhelpful file names so a
regression to name-matching fails here.
"""
import csv
import io
import zipfile
from pathlib import Path

import pytest

from kaigyou_core import config as cfg
from kaigyou_etl.discover import discover


@pytest.fixture
def sources():
    return cfg.sources_config().get("sources") or {}


def write_csv(path: Path, header: list[str], encoding: str = "utf-8-sig") -> Path:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerow(["x"] * len(header))
    path.write_bytes(buf.getvalue().encode(encoding))
    return path


def write_zip(path: Path, members: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for member in members:
            z.writestr(member, b"x")
    return path


@pytest.fixture
def downloads(tmp_path):
    """A folder of the five real files, all with unhelpful names."""
    write_csv(tmp_path / "a.csv",
              ["ID", "正式名称", "所在地", "所在地座標（緯度）", "所在地座標（経度）"])
    write_csv(tmp_path / "b.txt",
              ["KEY_CODE", "HTKSYORI", "T001141001", "T001141034"], encoding="cp932")
    write_csv(tmp_path / "c.txt",
              ["KEY_CODE", "HTKSYORI", "T000847001", "T000847025"], encoding="cp932")
    write_zip(tmp_path / "d.zip",
              ["S12-25_GML/UTF-8/S12-25_NumberOfPassengers.shp"])
    write_zip(tmp_path / "e.zip", ["N03-20240101_13.shp", "N03-20240101_13.cpg"])
    return tmp_path


# ------------------------------------------------------------------ matching
def test_every_source_is_matched_regardless_of_file_name(downloads, sources):
    found = discover(downloads, sources)
    assert found.clinics.name == "a.csv"
    assert found.mesh_current.name == "b.txt"
    assert found.mesh_baseline.name == "c.txt"
    assert found.stations.name == "d.zip"
    assert found.municipalities.name == "e.zip"
    assert found.missing == []


def test_census_rounds_are_told_apart_by_their_statistics_table_id(downloads, sources):
    """Both rounds carry KEY_CODE; only the column ids differ."""
    found = discover(downloads, sources)
    assert found.mesh_current != found.mesh_baseline


def test_hyphenless_archive_names_still_match(tmp_path, sources):
    """The browser drops the hyphen: S12-25_GML.zip arrives as S1225_GML.zip."""
    write_zip(tmp_path / "S1225_GML.zip",
              ["S12-25_GML/UTF-8/S12-25_NumberOfPassengers.dbf"])
    write_zip(tmp_path / "N0320240101_13_GML.zip", ["N03-20240101_13.dbf"])
    found = discover(tmp_path, sources)
    assert found.stations.name == "S1225_GML.zip"
    assert found.municipalities.name == "N0320240101_13_GML.zip"


def test_shift_jis_and_utf8_headers_are_both_read(tmp_path, sources):
    write_csv(tmp_path / "sjis.txt", ["KEY_CODE", "T001141001"], encoding="cp932")
    assert discover(tmp_path, sources).mesh_current is not None
    (tmp_path / "sjis.txt").unlink()
    write_csv(tmp_path / "utf8.txt", ["KEY_CODE", "T001141001"], encoding="utf-8-sig")
    assert discover(tmp_path, sources).mesh_current is not None


# ------------------------------------------------------------ what's missing
def test_missing_datasets_are_named(tmp_path, sources):
    write_csv(tmp_path / "only-clinics.csv",
              ["ID", "所在地座標（緯度）", "所在地座標（経度）"])
    found = discover(tmp_path, sources)
    assert found.clinics is not None
    assert "国勢調査メッシュ（最新年）" in found.missing
    assert "駅別乗降客数 S12 zip" in found.missing
    assert "行政区域 N03 zip" in found.missing


def test_a_missing_baseline_is_a_note_not_a_missing_dataset(tmp_path, sources):
    """Growth needs two rounds, but the rest of the analysis does not."""
    write_csv(tmp_path / "mesh.txt", ["KEY_CODE", "T001141001"], encoding="cp932")
    found = discover(tmp_path, sources)
    assert found.mesh_baseline is None
    assert "国勢調査メッシュ（最新年）" not in found.missing
    assert any("人口増減率" in n for n in found.notes)


def test_unrelated_files_are_listed_not_guessed_at(tmp_path, sources):
    write_csv(tmp_path / "memo.csv", ["名前", "メモ"])
    (tmp_path / "notes.txt").write_text("just a note", encoding="utf-8")
    found = discover(tmp_path, sources)
    assert {p.name for p in found.unmatched} == {"memo.csv", "notes.txt"}
    assert found.clinics is None


def test_a_zip_that_is_neither_source_is_not_forced_into_a_slot(tmp_path, sources):
    write_zip(tmp_path / "photos.zip", ["holiday/img001.jpg"])
    found = discover(tmp_path, sources)
    assert found.stations is None
    assert found.municipalities is None
    assert [p.name for p in found.unmatched] == ["photos.zip"]


def test_a_corrupt_zip_does_not_abort_the_scan(tmp_path, sources):
    (tmp_path / "broken.zip").write_bytes(b"not a zip at all")
    write_zip(tmp_path / "good.zip", ["N03-20240101_13.shp"])
    found = discover(tmp_path, sources)
    assert found.municipalities.name == "good.zip"
    assert [p.name for p in found.unmatched] == ["broken.zip"]


# ------------------------------------------------------------------ conflicts
def test_two_candidates_for_one_slot_keep_the_newer_and_say_so(tmp_path, sources):
    import os
    import time

    old = write_zip(tmp_path / "old.zip", ["N03-20200101_13.shp"])
    new = write_zip(tmp_path / "new.zip", ["N03-20240101_13.shp"])
    os.utime(old, (time.time() - 3600, time.time() - 3600))

    found = discover(tmp_path, sources)
    assert found.municipalities.name == "new.zip"
    assert [p.name for p in found.unmatched] == ["old.zip"]
    assert any("old.zip" in n for n in found.notes)


def test_a_missing_directory_is_reported(tmp_path, sources):
    with pytest.raises(NotADirectoryError):
        discover(tmp_path / "nope", sources)
