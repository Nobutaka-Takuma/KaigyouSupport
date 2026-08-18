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
@pytest.fixture
def clinics_adapter(tmp_path):
    return build("mhlw_dental_clinics", tmp_path)


def test_clinic_csv_is_read_as_shift_jis(clinics_adapter):
    facts = clinics_adapter.validate(FIXTURES / "mhlw_clinics_sample.csv")
    assert facts["row_count"] == 5
    assert facts["rows_without_coordinates"] == 1


def test_clinic_rows_without_usable_coordinates_are_dropped_not_guessed(clinics_adapter):
    records = list(clinics_adapter.transform(FIXTURES / "mhlw_clinics_sample.csv"))
    ids = {r["facility_id"] for r in records}
    assert "1310000003" not in ids   # blank lat/lng
    assert "1310000004" not in ids   # 0,0 -- outside Japan
    assert ids == {"1310000001", "1310000002"}


def test_duplicate_facility_ids_are_collapsed(clinics_adapter):
    records = list(clinics_adapter.transform(FIXTURES / "mhlw_clinics_sample.csv"))
    assert len(records) == len({r["facility_id"] for r in records})


def test_clinic_attributes_are_normalised(clinics_adapter):
    records = {r["facility_id"]: r
               for r in clinics_adapter.transform(FIXTURES / "mhlw_clinics_sample.csv")}
    first = records["1310000001"]
    assert first["name"] == "テスト歯科クリニック"
    assert first["lat"] == pytest.approx(35.681236)
    assert first["clinic_types"] == ["一般歯科", "小児歯科"]
    assert first["opening_date"] == date(2015, 4, 1)
    assert first["facility_category"] == "dental_clinic"
    # Slash-separated specialities and year-month dates are handled too.
    second = records["1310000002"]
    assert second["clinic_types"] == ["一般歯科", "矯正歯科"]
    assert second["opening_date"] == date(2020, 7, 1)


def test_renamed_columns_are_resolved_from_config(tmp_path):
    adapter = build("mhlw_dental_clinics", tmp_path,
                    {"columns": {"facility_id": ["施設ID"], "name": ["名称"],
                                 "lat": ["緯度"], "lng": ["経度"]}})
    with pytest.raises(AcquisitionError) as exc:
        adapter.validate(FIXTURES / "mhlw_clinics_sample.csv")
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
    adapter.ctx.input_path = FIXTURES / "mhlw_clinics_sample.csv"
    adapter.ctx.offline = True   # would fail if it tried the network
    artifact = adapter.download()
    assert artifact.exists()
    assert artifact.read_bytes() == (FIXTURES / "mhlw_clinics_sample.csv").read_bytes()
