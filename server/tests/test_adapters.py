"""Adapter transform logic.

The downloads themselves cannot be exercised here, so these tests pin the part
that is under our control: parsing the publishers' documented layouts. The
fixtures are hand-written files in those layouts (Shift-JIS, the units row
e-Stat inserts under the header, the suppression markers used for small
counts) -- they are test inputs, not data.
"""
from datetime import date
from pathlib import Path

import pytest

from kaigyou_core import config as cfg
from kaigyou_etl.acquisition import AcquisitionError
from kaigyou_etl.adapters import AdapterContext, get_adapter

FIXTURES = Path(__file__).parent / "fixtures"


def build(source_id: str, tmp_path: Path, overrides: dict | None = None):
    sources = cfg.sources_config()
    spec = dict(sources["sources"][source_id])
    spec.update(overrides or {})
    ctx = AdapterContext(
        source_id=source_id,
        spec=spec,
        defaults=sources.get("defaults", {}),
        raw_dir=tmp_path,
    )
    return get_adapter(spec["adapter"])(ctx)


# ------------------------------------------------------------------- clinics
CLINIC_FILE = FIXTURES / "mhlw_dental_facility_info.csv"


@pytest.fixture
def clinics_adapter(tmp_path):
    return build("mhlw_dental_clinics", tmp_path)


def test_real_schema_columns_are_resolved(clinics_adapter):
    facts = clinics_adapter.validate(CLINIC_FILE)
    assert facts["resolved_columns"]["facility_id"] == "ID"
    assert facts["resolved_columns"]["name"] == "正式名称"
    assert facts["resolved_columns"]["lat"] == "所在地座標（緯度）"
    assert facts["resolved_columns"]["prefecture_code"] == "都道府県コード"


def test_validate_reports_coordinate_quality(clinics_adapter):
    facts = clinics_adapter.validate(CLINIC_FILE)
    assert facts["row_count"] == 5
    assert facts["rows_usable"] == 4
    # The published file carries 0,0 for facilities it could not geocode.
    assert facts["rows_dropped_zero_coordinates"] == 1
    assert facts["rows_dropped_outside_japan"] == 0


def test_validate_reports_prefecture_coverage(clinics_adapter):
    facts = clinics_adapter.validate(CLINIC_FILE)
    assert facts["prefectures_in_file"] == 3
    assert facts["rows_by_prefecture"]["13"] == 3


def test_zero_coordinates_are_dropped_not_placed_at_the_equator(clinics_adapter):
    ids = {r["facility_id"] for r in clinics_adapter.transform(CLINIC_FILE)}
    assert "1331312334571" not in ids


def test_duplicate_ids_are_collapsed(clinics_adapter):
    records = list(clinics_adapter.transform(CLINIC_FILE))
    assert len(records) == len({r["facility_id"] for r in records})


def test_prefecture_comes_from_the_row_not_a_constant(clinics_adapter):
    by_id = {r["facility_id"]: r for r in clinics_adapter.transform(CLINIC_FILE)}
    assert by_id["1331310184150"]["prefecture_code"] == "13"
    assert by_id["1441171234567"]["prefecture_code"] == "14"
    assert by_id["0132010042450"]["prefecture_code"] == "01"


def test_municipality_code_is_completed_to_five_digits(clinics_adapter):
    """The file publishes only the 3-digit part, which is ambiguous alone."""
    by_id = {r["facility_id"]: r for r in clinics_adapter.transform(CLINIC_FILE)}
    assert by_id["1331310184150"]["municipality_code"] == "13101"   # 千代田区
    assert by_id["1441171234567"]["municipality_code"] == "14117"   # 横浜市青葉区
    assert by_id["0132010042450"]["municipality_code"] == "01405"


def test_prefecture_filter_restricts_the_load(tmp_path):
    adapter = build("mhlw_dental_clinics", tmp_path)
    adapter.ctx.prefecture_filter = "13"
    records = list(adapter.transform(CLINIC_FILE))
    assert {r["prefecture_code"] for r in records} == {"13"}
    assert len(records) == 1   # of the three Tokyo rows, one is 0,0 and one a duplicate


def test_no_filter_loads_every_prefecture(clinics_adapter):
    records = list(clinics_adapter.transform(CLINIC_FILE))
    assert {r["prefecture_code"] for r in records} == {"13", "14", "01"}


def test_extra_published_fields_are_kept_as_attributes(clinics_adapter):
    by_id = {r["facility_id"]: r for r in clinics_adapter.transform(CLINIC_FILE)}
    attrs = by_id["1331310184150"]["attributes"]
    assert attrs["homepage"] == "https://example.invalid/a"
    assert attrs["name_kana"] == "テストシカクリニック"
    assert attrs["closed_holiday"] == "1"
    # Blank cells are omitted rather than stored as empty strings.
    assert "short_name" not in by_id["1441171234567"]["attributes"]


def test_core_fields_are_normalised(clinics_adapter):
    by_id = {r["facility_id"]: r for r in clinics_adapter.transform(CLINIC_FILE)}
    row = by_id["1331310184150"]
    assert row["name"] == "テスト歯科クリニック"
    assert row["facility_category"] == "dental_clinic"
    assert row["lat"] == pytest.approx(35.700318)
    assert row["lng"] == pytest.approx(139.77518)
    assert row["address"].startswith("東京都千代田区")


def test_a_file_with_no_usable_coordinates_is_a_named_failure(tmp_path):
    broken = tmp_path / "broken.csv"
    text = CLINIC_FILE.read_text(encoding="utf-8-sig").splitlines()
    rows = [text[0]] + [
        line.replace(',35.700318,139.77518,', ',0.0,0.0,')
            .replace(',35.562479,139.478558,', ',0.0,0.0,')
            .replace(',43.295734,140.596965,', ',0.0,0.0,')
        for line in text[1:]
    ]
    broken.write_text("\n".join(rows), encoding="utf-8-sig")
    adapter = build("mhlw_dental_clinics", tmp_path)
    with pytest.raises(AcquisitionError) as exc:
        adapter.validate(broken)
    assert exc.value.error_type == "empty_dataset"


def test_renamed_columns_are_resolved_from_config(tmp_path):
    adapter = build("mhlw_dental_clinics", tmp_path,
                    {"columns": {"facility_id": ["施設ID"], "name": ["名称"],
                                 "lat": ["緯度"], "lng": ["経度"]}})
    with pytest.raises(AcquisitionError) as exc:
        adapter.validate(CLINIC_FILE)
    assert exc.value.error_type == "schema_mismatch"


# ---------------------------------------------------------------------- mesh
@pytest.fixture
def mesh_adapter(tmp_path):
    return build("estat_population_mesh", tmp_path)


def test_mesh_units_row_is_skipped(mesh_adapter):
    facts = mesh_adapter.validate(FIXTURES / "estat_mesh_sample.csv")
    assert facts["row_count"] == 4       # header + units row excluded
    assert facts["mesh_code_lengths"] == {8: 3}
    assert facts["invalid_mesh_codes_in_sample"] == 1


def test_mesh_geometry_is_derived_from_the_code(mesh_adapter):
    records = {r["mesh_code"]: r
               for r in mesh_adapter.transform(FIXTURES / "estat_mesh_sample.csv")}
    row = records["53394611"]
    assert row["mesh_size_m"] == 1000
    assert row["polygon_wkt"].startswith("POLYGON((")
    assert row["centroid_lat"] == pytest.approx(35.6792, abs=1e-3)
    assert row["population"] == 12540
    assert row["households"] == 6900


def test_suppressed_counts_become_null_not_zero(mesh_adapter):
    records = {r["mesh_code"]: r
               for r in mesh_adapter.transform(FIXTURES / "estat_mesh_sample.csv")}
    assert records["53394613"]["population"] is None
    assert records["53394613"]["households"] is None


def test_invalid_mesh_codes_are_skipped(mesh_adapter):
    codes = {r["mesh_code"]
             for r in mesh_adapter.transform(FIXTURES / "estat_mesh_sample.csv")}
    assert "bad-code" not in codes


def test_growth_is_null_without_a_baseline(mesh_adapter):
    records = list(mesh_adapter.transform(FIXTURES / "estat_mesh_sample.csv"))
    assert all(r["population_growth"] is None for r in records)


def test_growth_is_computed_when_the_baseline_is_present(tmp_path):
    baseline = tmp_path / "estat_mesh_baseline.csv"
    baseline.write_bytes((FIXTURES / "estat_mesh_baseline.csv").read_bytes())
    adapter = build("estat_population_mesh", tmp_path,
                    {"growth_baseline": {"path": str(baseline), "years": 5}})
    records = {r["mesh_code"]: r
               for r in adapter.transform(FIXTURES / "estat_mesh_sample.csv")}
    # 12540 vs 12000 over the interval
    assert records["53394611"]["population_growth"] == pytest.approx(0.045)
    assert records["53394612"]["population_growth"] == pytest.approx(-0.0235, abs=1e-3)


# ------------------------------------------------------------------ download
def test_missing_input_file_is_a_named_failure(tmp_path):
    adapter = build("mhlw_dental_clinics", tmp_path)
    adapter.ctx.input_path = tmp_path / "nope.csv"
    with pytest.raises(AcquisitionError) as exc:
        adapter.download()
    assert exc.value.error_type == "input_missing"


def test_offline_mode_refuses_to_reach_the_network(tmp_path):
    adapter = build("mhlw_dental_clinics", tmp_path)
    adapter.ctx.offline = True
    with pytest.raises(AcquisitionError) as exc:
        adapter.download()
    assert exc.value.error_type == "not_configured"


def test_a_local_input_is_preferred_over_downloading(tmp_path):
    adapter = build("mhlw_dental_clinics", tmp_path)
    adapter.ctx.input_path = CLINIC_FILE
    adapter.ctx.offline = True   # would fail if it tried the network
    artifact = adapter.download()
    assert artifact.exists()
    assert artifact.read_bytes() == (CLINIC_FILE).read_bytes()
