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
MESH_2020 = FIXTURES / "estat_mesh_2020.txt"
MESH_2015 = FIXTURES / "estat_mesh_2015.txt"


@pytest.fixture
def mesh_adapter(tmp_path):
    return build("estat_population_mesh", tmp_path)


@pytest.fixture
def mesh_adapter_with_baseline(tmp_path):
    adapter = build("estat_population_mesh", tmp_path)
    adapter.ctx.baseline_path = MESH_2015
    return adapter


def test_japanese_label_row_is_skipped(mesh_adapter):
    """e-Stat inserts a row of column captions under the header."""
    facts = mesh_adapter.validate(MESH_2020)
    assert facts["row_count"] == 6      # 7 lines minus the caption row


def test_real_schema_columns_are_resolved(mesh_adapter):
    facts = mesh_adapter.validate(MESH_2020)
    assert facts["resolved_columns"]["population"] == "T001141001"
    assert facts["resolved_columns"]["age_65_plus"] == "T001141019"
    assert facts["resolved_columns"]["households"] == "T001141034"


def test_mesh_resolution_comes_from_the_code_length(mesh_adapter):
    facts = mesh_adapter.validate(MESH_2020)
    assert facts["mesh_size_m"] == [500]      # 9-digit codes
    assert facts["mesh_code_lengths"] == {9: 5}   # bad-code is not counted


def test_validate_reports_the_population_total(mesh_adapter):
    """The one figure that can be checked against the published headline.

    It counts only rows that will actually load, so an unusable row cannot
    inflate the total into agreeing with the official figure by accident.
    """
    facts = mesh_adapter.validate(MESH_2020)
    assert facts["population_total"] == 12540 + 8300 + 317 + 1   # bad-code excluded
    assert facts["loadable_rows"] == 5
    assert facts["invalid_mesh_codes"] == 1


def test_validate_reports_suppression_extent(mesh_adapter):
    facts = mesh_adapter.validate(MESH_2020)
    assert facts["suppression_flag_counts"] == {"0": 3, "1": 1, "2": 1}
    assert facts["suppressed_rows"] == 2   # the merge target and the merged cell


def test_geometry_is_derived_from_the_mesh_code(mesh_adapter):
    records = {r["mesh_code"]: r for r in mesh_adapter.transform(MESH_2020)}
    row = records["533946113"]
    assert row["mesh_size_m"] == 500
    assert row["polygon_wkt"].startswith("POLYGON((")
    assert row["population"] == 12540
    assert row["age_65_plus"] == 2960
    assert row["households"] == 6900


def test_unpublished_counts_become_null_not_zero(mesh_adapter):
    records = {r["mesh_code"]: r for r in mesh_adapter.transform(MESH_2020)}
    assert records["533946121"]["population"] is None
    assert records["533946121"]["households"] is None


def test_invalid_mesh_codes_are_skipped(mesh_adapter):
    codes = {r["mesh_code"] for r in mesh_adapter.transform(MESH_2020)}
    assert "bad-code" not in codes


def test_suppressed_rows_are_loaded_as_published(mesh_adapter):
    """Reversing the disclosure control would mean inventing numbers."""
    records = {r["mesh_code"]: r for r in mesh_adapter.transform(MESH_2020)}
    assert records["533946111"]["population"] == 317   # merge target
    assert records["533946112"]["population"] == 1     # suppressed cell


# --------------------------------------------------------------- growth rate
def test_growth_is_null_without_a_baseline(mesh_adapter):
    records = list(mesh_adapter.transform(MESH_2020))
    assert all(r["population_growth"] is None for r in records)


def test_baseline_uses_its_own_column_ids(mesh_adapter_with_baseline):
    """2015 calls total population T000847001, 2020 calls it T001141001."""
    _, facts = mesh_adapter_with_baseline._baseline_population()
    assert facts["baseline_columns"]["population"] == "T000847001"
    assert facts["baseline_rows"] == 3


def test_growth_is_computed_against_the_prior_round(mesh_adapter_with_baseline):
    records = {r["mesh_code"]: r
               for r in mesh_adapter_with_baseline.transform(MESH_2020)}
    assert records["533946113"]["population_growth"] == pytest.approx(
        (12540 - 12000) / 12000
    )
    assert records["533946114"]["population_growth"] == pytest.approx(
        (8300 - 8500) / 8500
    )


def test_meshes_absent_from_the_baseline_have_no_growth(mesh_adapter_with_baseline):
    records = {r["mesh_code"]: r
               for r in mesh_adapter_with_baseline.transform(MESH_2020)}
    assert records["533946112"]["population_growth"] is None


def test_validate_reports_baseline_coverage(mesh_adapter_with_baseline):
    facts = mesh_adapter_with_baseline.validate(MESH_2020)
    assert facts["baseline_supplied"] is True
    assert facts["baseline_matched_meshes"] == 3
    assert facts["meshes_without_baseline"] == 2   # 112 and 121; bad-code is excluded


def test_a_missing_baseline_file_is_a_named_failure(tmp_path):
    adapter = build("estat_population_mesh", tmp_path)
    adapter.ctx.baseline_path = tmp_path / "nope.txt"
    with pytest.raises(AcquisitionError) as exc:
        adapter.validate(MESH_2020)
    assert exc.value.error_type == "input_missing"


# ------------------------------------------------------------------ stations
STATION_FILE = FIXTURES / "mlit_s12_stations.zip"


@pytest.fixture
def stations_adapter(tmp_path):
    return build("mlit_stations", tmp_path)


def stations_by_name(adapter):
    return {r["name"]: r for r in adapter.transform(STATION_FILE)}


def test_year_columns_are_derived_from_config(stations_adapter):
    facts = stations_adapter.validate(STATION_FILE)
    assert facts["latest_year"] == 2024
    assert facts["years_available"] == list(range(2011, 2025))


def test_operators_of_one_station_are_combined(stations_adapter):
    """Shinjuku is eleven published rows but one station."""
    rows = stations_by_name(stations_adapter)
    hub = rows["大結節"]
    assert hub["daily_passengers"] == 1_500_000     # 1,000,000 + 500,000
    assert hub["attributes"]["record_count"] == 3
    assert set(hub["attributes"]["operators"]) == {"東日本旅客鉄道", "京王電鉄"}
    assert "ほか" in hub["operator"]


def test_duplicate_rows_contribute_zero_not_missing_data(stations_adapter):
    """A row flagged 2 is published as 0 because it is counted elsewhere.

    Treating it as "no data for 2024" would silently fall back to an older
    year and report a stale figure.
    """
    hub = stations_by_name(stations_adapter)["大結節"]
    assert hub["passengers_year"] == 2024


def test_the_newest_year_with_data_is_used(stations_adapter):
    row = stations_by_name(stations_adapter)["旧年"]
    assert row["daily_passengers"] == 12345
    assert row["passengers_year"] == 2018


def test_a_station_with_no_published_figure_stays_null(stations_adapter):
    row = stations_by_name(stations_adapter)["無数値"]
    assert row["daily_passengers"] is None
    assert row["passengers_year"] is None


def test_blank_group_codes_do_not_merge_unrelated_stations(stations_adapter):
    """Two stations 65km apart share an empty group code in the real file."""
    rows = stations_by_name(stations_adapter)
    assert rows["空グループ北"]["daily_passengers"] == 700
    assert rows["空グループ南"]["daily_passengers"] == 800


def test_a_group_spanning_an_implausible_distance_is_split(stations_adapter):
    rows = stations_by_name(stations_adapter)
    assert rows["散在A"]["daily_passengers"] == 111
    assert rows["散在B"]["daily_passengers"] == 222


def test_line_geometry_is_reduced_to_a_point(stations_adapter):
    hub = stations_by_name(stations_adapter)["大結節"]
    # midpoint of the two counted platforms' segments
    assert hub["lng"] == pytest.approx(139.70055, abs=1e-4)
    assert hub["lat"] == pytest.approx(35.69030, abs=1e-4)


def test_prefecture_is_left_unset_because_s12_does_not_publish_one(stations_adapter):
    assert all(r["prefecture_code"] is None
               for r in stations_adapter.transform(STATION_FILE))


def test_validate_counts_stations_not_rows(stations_adapter):
    facts = stations_adapter.validate(STATION_FILE)
    assert facts["feature_count"] == 9
    assert facts["station_count"] == 7       # the three-operator hub collapses
    assert facts["stations_without_passengers"] == 1


def test_encoding_variant_is_selected_by_config(tmp_path):
    """The archive ships the layer twice; reading the wrong copy mangles names."""
    adapter = build("mlit_stations", tmp_path,
                    {"archive_variants": [{"path": "Shift-JIS", "encoding": "cp932"}]})
    assert "大結節" in stations_by_name(adapter)

    adapter = build("mlit_stations", tmp_path,
                    {"archive_variants": [{"path": "UTF-8", "encoding": "utf-8"}]})
    assert "大結節" in stations_by_name(adapter)


def test_a_missing_variant_directory_is_a_named_failure(tmp_path):
    adapter = build("mlit_stations", tmp_path,
                    {"archive_variants": [{"path": "NoSuchDir", "encoding": "utf-8"}]})
    with pytest.raises(AcquisitionError) as exc:
        adapter.validate(STATION_FILE)
    assert exc.value.error_type == "schema_mismatch"


def test_an_edition_without_the_configured_years_is_reported(tmp_path):
    adapter = build("mlit_stations", tmp_path,
                    {"passenger_series": {"first_year": 2100, "last_year": 2101,
                                          "column_template": "S12_{index:03d}",
                                          "duplicate_start": 900,
                                          "passengers_start": 903, "stride": 4}})
    with pytest.raises(AcquisitionError) as exc:
        adapter.validate(STATION_FILE)
    assert exc.value.error_type == "schema_mismatch"


# ------------------------------------------------------------ municipalities
BOUNDARY_FILE = FIXTURES / "mlit_n03_municipalities.zip"


@pytest.fixture
def municipalities_adapter(tmp_path):
    return build("mlit_municipalities", tmp_path)


def municipalities_by_name(adapter):
    return {r["name"]: r for r in adapter.transform(BOUNDARY_FILE)}


def test_encoding_comes_from_the_cpg_declaration(municipalities_adapter):
    """N03 ships flat and UTF-8 while older layers are Shift-JIS.

    The adapter's own default is cp932, so reading these names correctly is
    only possible by honouring the publisher's .cpg sidecar.
    """
    names = municipalities_by_name(municipalities_adapter)
    assert "千代田区" in names
    assert "所属未定地" not in names


def test_parts_of_one_municipality_are_collected(municipalities_adapter):
    """Ogasawara is 4,812 published parts but one municipality."""
    islands = municipalities_by_name(municipalities_adapter)["島村"]
    assert len(islands["parts"]) == 3
    assert islands["municipality_code"] == "13421"


def test_one_row_per_municipality(municipalities_adapter):
    records = list(municipalities_adapter.transform(BOUNDARY_FILE))
    codes = [r["municipality_code"] for r in records]
    assert len(codes) == len(set(codes))
    assert set(codes) == {"13101", "13102", "13421", "13303"}


def test_the_prefecture_level_catch_all_is_excluded(municipalities_adapter):
    """13000 所属未定地 is reclaimed land, not a municipality.

    Letting it through would label meshes with an area name that does not
    exist as a place.
    """
    codes = {r["municipality_code"] for r in municipalities_adapter.transform(BOUNDARY_FILE)}
    assert "13000" not in codes


def test_validate_reports_what_was_excluded(municipalities_adapter):
    facts = municipalities_adapter.validate(BOUNDARY_FILE)
    assert facts["feature_count"] == 9
    assert facts["municipality_count"] == 4
    assert facts["excluded_features"] == {"13000 所属未定地": 2}
    assert facts["most_fragmented"]["13421"] == 3


def test_prefecture_code_is_taken_from_the_municipality_code(municipalities_adapter):
    assert all(r["prefecture_code"] == "13"
               for r in municipalities_adapter.transform(BOUNDARY_FILE))


def test_prefecture_name_is_carried_through(municipalities_adapter):
    assert municipalities_by_name(municipalities_adapter)["千代田区"]["prefecture_name"] == "東京都"


def test_a_district_on_only_some_parts_does_not_split_the_municipality(municipalities_adapter):
    """N03 fills 郡 on a handful of rows and leaves it blank on the rest."""
    records = [r for r in municipalities_adapter.transform(BOUNDARY_FILE)
               if r["municipality_code"] == "13303"]
    assert len(records) == 1
    assert len(records[0]["parts"]) == 2


def test_geometry_is_emitted_as_polygon_wkt(municipalities_adapter):
    part = municipalities_by_name(municipalities_adapter)["千代田区"]["parts"][0]
    assert part.startswith(("POLYGON((", "MULTIPOLYGON("))


def test_prefecture_filter_restricts_the_load(tmp_path):
    adapter = build("mlit_municipalities", tmp_path)
    adapter.ctx.prefecture_filter = "27"
    assert list(adapter.transform(BOUNDARY_FILE)) == []


def test_a_file_with_only_excluded_codes_is_a_named_failure(tmp_path):
    adapter = build("mlit_municipalities", tmp_path,
                    {"exclude_code_suffixes": ["0", "1", "2", "3"]})
    with pytest.raises(AcquisitionError) as exc:
        adapter.validate(BOUNDARY_FILE)
    assert exc.value.error_type == "empty_dataset"


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


# ------------------------------------------- 経済センサス メッシュ（従業者数）
BUSINESS_FIXTURE = FIXTURES / "estat_business_mesh.txt"


def business_adapter(tmp_path, overrides=None):
    return build("estat_business_mesh", tmp_path, overrides)


def test_workers_and_establishments_come_from_the_configured_columns(tmp_path):
    """The two blocks are indistinguishable by label; only the column id says.

    The published table repeats the same 21 industry names twice -- first
    counting establishments, then the people in them. Reading the wrong block
    silently turns 400 shops into 400 workers.
    """
    adapter = business_adapter(tmp_path)
    records = {r["mesh_code"]: r for r in adapter.transform(BUSINESS_FIXTURE)}

    office = records["533946113"]
    assert office["establishments"] == 400
    assert office["workers"] == 6000
    assert office["industry_workers"]["wholesale_retail"] == 1500
    assert office["industry_establishments"]["wholesale_retail"] == 100


def test_the_resolution_comes_from_the_code_not_a_constant(tmp_path):
    adapter = business_adapter(tmp_path)
    sizes = {r["mesh_code"]: r["mesh_size_m"] for r in adapter.transform(BUSINESS_FIXTURE)}
    assert sizes["533946113"] == 500   # 9 digits
    assert sizes["53394611"] == 1000   # 8 digits


def test_validate_reports_the_totals_that_will_load(tmp_path):
    """Totals cover loadable rows only, so they compare against published ones."""
    facts = business_adapter(tmp_path).validate(BUSINESS_FIXTURE)
    assert facts["workers_total"] == 6000 + 900 + 700
    assert facts["establishments_total"] == 400 + 120 + 50
    assert facts["invalid_mesh_codes"] == 1        # BAD_CODE
    assert facts["loadable_rows"] == 3


def test_swapped_columns_are_refused_rather_than_loaded(tmp_path):
    """Establishments read as workers gives under one person per workplace.

    This is the failure the file invites, and it is invisible afterwards: the
    map still draws, the numbers are just wrong by a factor of fifteen.
    """
    swapped = {"columns": {"mesh_code": ["KEY_CODE"],
                           "establishments": ["T001147022"],   # <- the worker column
                           "workers": ["T001147001"]}}         # <- the establishment one
    with pytest.raises(AcquisitionError) as exc:
        business_adapter(tmp_path, swapped).validate(BUSINESS_FIXTURE)
    assert "swapped" in str(exc.value)


def test_a_column_id_that_is_not_in_the_file_is_named(tmp_path):
    """A release that renumbers its columns should say so, not load zeroes."""
    with pytest.raises(AcquisitionError) as exc:
        business_adapter(tmp_path, {"columns": {
            "mesh_code": ["KEY_CODE"], "workers": ["T999999999"]}}).validate(BUSINESS_FIXTURE)
    assert "T999999999" in str(exc.value)


def test_industries_missing_from_the_file_are_reported_not_fatal(tmp_path):
    """Divisions vary between releases; their absence is worth noting only."""
    facts = business_adapter(tmp_path, {
        "industry_columns": {"nonexistent": ["T001147900", "T001147901"]}}).validate(
            BUSINESS_FIXTURE)
    assert facts["industries_not_in_file"] == ["nonexistent"]


# ------------------------------------------------ OSM 徒歩ネットワーク
WALK_FIXTURE = FIXTURES / "osm_roads_river.shp.zip"


def walk_adapter(tmp_path, overrides=None):
    return build("osm_walk_network", tmp_path, {"bbox": None, **(overrides or {})})


def test_roads_pedestrians_may_not_use_are_excluded(tmp_path):
    """Motorways are in the extract and are not walkable in Japan.

    Routing through one would invent a catchment nobody can reach on foot.
    """
    facts = walk_adapter(tmp_path).validate(WALK_FIXTURE)
    assert "motorway" in facts["classes_not_walkable"]
    assert facts["walkable_features"] == facts["feature_count"] - 1

    classes = {r["road_class"] for r in walk_adapter(tmp_path).transform(WALK_FIXTURE)}
    assert classes == {"residential"}


def test_a_release_that_renames_its_classes_fails_loudly(tmp_path):
    """Silently loading nothing would look like a network with no streets."""
    with pytest.raises(AcquisitionError) as exc:
        walk_adapter(tmp_path, {"walkable_classes": ["no_such_class"]}).validate(WALK_FIXTURE)
    assert "no feature matched" in str(exc.value)


def test_refuses_to_load_without_knowing_what_is_walkable(tmp_path):
    with pytest.raises(AcquisitionError) as exc:
        walk_adapter(tmp_path, {"walkable_classes": []}).validate(WALK_FIXTURE)
    assert "walkable_classes" in str(exc.value)


def test_the_bbox_clips_the_network(tmp_path):
    """A Kanto extract is several times larger than the area being analysed."""
    far_away = {"bbox": [130.0, 33.0, 131.0, 34.0]}
    with pytest.raises(AcquisitionError):
        walk_adapter(tmp_path, far_away).validate(WALK_FIXTURE)


def test_every_edge_is_a_simple_linestring(tmp_path):
    """pgRouting needs one source and one target per row."""
    for record in walk_adapter(tmp_path).transform(WALK_FIXTURE):
        assert record["geom_wkt"].startswith("LINESTRING(")


# ----------------------------------------------------- 就業状態等基本集計メッシュ
#: T001108 の実ファイルと同じ形（ヘッダ行＋ラベル行、Shift-JIS）。値は
#: 手書きで、データではありません。
_PROFILE_COLUMNS = {
    "T001108004": "employees_regular", "T001108010": "employees_part_time",
    "T001108013": "self_employed",
    "T001108019": "preschool_total", "T001108025": "preschool_nursery",
    "T001108034": "students_total", "T001108037": "students_primary",
    "T001108040": "students_high_school", "T001108043": "students_junior",
    "T001108046": "students_university", "T001108049": "graduates",
    "T001108067": "resident_under_1y", "T001108070": "resident_1_to_5y",
    "T001108079": "resident_20y_plus",
    "T001108087": "workers_living_here", "T001108088": "students_living_here",
    "T001108089": "commute_walk", "T001108090": "commute_rail",
    "T001108091": "commute_bus", "T001108092": "commute_car",
    "T001108093": "commute_motorcycle", "T001108094": "commute_bicycle",
}


def _profile_file(tmp_path: Path, rows: list[dict], *, drop: tuple = ()) -> Path:
    ids = ["KEY_CODE"] + [c for c in _PROFILE_COLUMNS if _PROFILE_COLUMNS[c] not in drop]
    lines = [",".join(ids), ",".join([""] * len(ids))]
    for row in rows:
        lines.append(",".join(
            [row["mesh_code"]]
            + [str(row.get(_PROFILE_COLUMNS[c], 0)) for c in ids[1:]]))
    path = tmp_path / "tblT001108H13.txt"
    path.write_text("\n".join(lines) + "\n", encoding="cp932")
    return path


def _profile_rows() -> list[dict]:
    """内訳が総数に収まり、居住期間も入っている、まっとうな2メッシュ。"""
    return [
        {"mesh_code": "533945572", "students_total": 927,
         "students_primary": 150, "students_high_school": 108,
         "students_junior": 0, "students_university": 669,
         "graduates": 4084, "resident_under_1y": 210,
         "resident_1_to_5y": 640, "resident_20y_plus": 980,
         "commute_car": 53, "commute_rail": 1279},
        {"mesh_code": "533945463", "students_total": 518,
         "students_primary": 240, "students_high_school": 109,
         "students_junior": 0, "students_university": 169,
         "graduates": 2776, "resident_under_1y": 180,
         "resident_1_to_5y": 500, "resident_20y_plus": 720,
         "commute_car": 41, "commute_rail": 902},
    ]


def test_resident_profile_reads_the_columns_that_change_the_decision(tmp_path):
    adapter = build("estat_resident_profile", tmp_path)
    facts = adapter.validate(_profile_file(tmp_path, _profile_rows()))
    assert facts["loadable_rows"] == 2
    assert facts["totals"]["commute_car"] == 94
    assert facts["totals"]["resident_20y_plus"] == 1700
    assert facts["columns_not_in_file"] == []


def test_resident_profile_refuses_a_table_without_residence_duration(tmp_path):
    """通学地基準かどうかは、人数の大小では分かりません。**定義で分けます。**

    居住期間は常住地にしかない項目なので、無ければこの表ではありません。
    以前ここは「1メッシュの大学生が1万人を超えたら落とす」でしたが、
    通学地基準でも1万を超えるメッシュはほとんど無いので効きませんでした。
    """
    rows = [dict(r, resident_under_1y=0, resident_1_to_5y=0,
                 resident_20y_plus=0) for r in _profile_rows()]
    adapter = build("estat_resident_profile", tmp_path)
    with pytest.raises(AcquisitionError) as excinfo:
        adapter.validate(_profile_file(tmp_path, rows))
    assert "常住地基準" in str(excinfo.value)
    assert "estat_daytime_mesh" in str(excinfo.value)


def test_resident_profile_catches_a_column_id_that_moved(tmp_path):
    """列 ID を設定に置いた以上、版が変われば静かにずれます。在学者の内訳が
    総数を超えたら、その列は別の項目を指しています。"""
    rows = [dict(r, students_university=r["students_total"] * 2)
            for r in _profile_rows()]
    adapter = build("estat_resident_profile", tmp_path)
    with pytest.raises(AcquisitionError) as excinfo:
        adapter.validate(_profile_file(tmp_path, rows))
    assert "列 ID" in str(excinfo.value)


def test_resident_profile_records_how_students_compare_with_adults(tmp_path):
    """落とすための閾値ではなく、次に読む人のための目盛りです。実測（令和2年・
    東京都、分母50人以上）の最大は 5.36 倍。"""
    adapter = build("estat_resident_profile", tmp_path)
    facts = adapter.validate(_profile_file(tmp_path, _profile_rows()))
    assert facts["students_university_peak_mesh"] == 669
    assert facts["students_per_adult_peak"] == round(669 / 4084, 3)
    assert facts["residence_duration_columns"]
