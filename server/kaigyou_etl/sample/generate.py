"""Generate the synthetic development dataset.

The numbers are plausible in shape -- population falls off from the centre,
clinics cluster where people are, stations sit on a coarse grid -- because the
point is to exercise the analysis, not to look convincing. They are not
estimates of anything and must never be presented as such.
"""
from __future__ import annotations

import math
import random
from datetime import date, datetime, timezone
from typing import Any, Iterator

import psycopg

from kaigyou_core import mesh as meshlib
from kaigyou_etl.acquisition import AcquisitionLog, StepRecord
from kaigyou_etl.pipeline import upsert_source

SAMPLE_PREFIX = "【サンプル】"
PREFECTURE_CODE = "13"
MESH_SIZE_M = 1000
SEED = 20260818

# Bounding box loosely covering the Tokyo wards, used only to decide which
# mesh cells to generate.
BBOX = (139.56, 35.53, 139.93, 35.83)   # min_lng, min_lat, max_lng, max_lat
CENTRE = (139.767, 35.681)

SOURCES = {
    "sample_population_mesh": {
        "id": "sample_population_mesh",
        "name": f"{SAMPLE_PREFIX}1kmメッシュ人口（合成データ）",
        "publisher": "KaigyouSupport (synthetic)",
        "dataset_kind": "sample",
        "license": None,
        "homepage_url": None,
        "terms_url": None,
        "notes": "開発・動作確認用の合成データ。実在の統計ではありません。",
    },
    "sample_dental_clinics": {
        "id": "sample_dental_clinics",
        "name": f"{SAMPLE_PREFIX}歯科診療所（合成データ）",
        "publisher": "KaigyouSupport (synthetic)",
        "dataset_kind": "sample",
        "license": None,
        "homepage_url": None,
        "terms_url": None,
        "notes": "開発・動作確認用の合成データ。実在の医療機関ではありません。",
    },
    "sample_stations": {
        "id": "sample_stations",
        "name": f"{SAMPLE_PREFIX}駅・乗降客数（合成データ）",
        "publisher": "KaigyouSupport (synthetic)",
        "dataset_kind": "sample",
        "license": None,
        "homepage_url": None,
        "terms_url": None,
        "notes": "開発・動作確認用の合成データ。実在の駅ではありません。",
    },
    "sample_municipalities": {
        "id": "sample_municipalities",
        "name": f"{SAMPLE_PREFIX}行政区域（合成データ）",
        "publisher": "KaigyouSupport (synthetic)",
        "dataset_kind": "sample",
        "license": None,
        "homepage_url": None,
        "terms_url": None,
        "notes": "開発・動作確認用の合成グリッド。実在の市区町村ではありません。",
    },
}

CLINIC_TYPES = ["一般歯科", "小児歯科", "矯正歯科", "歯科口腔外科"]


def _density(lng: float, lat: float, rng: random.Random) -> float:
    """A smooth 0..1 field peaking near the centre, with a little texture."""
    dx = (lng - CENTRE[0]) * 90.0        # rough km at this latitude
    dy = (lat - CENTRE[1]) * 111.0
    dist = math.hypot(dx, dy)
    base = math.exp(-(dist ** 2) / (2 * 9.0 ** 2))
    ripple = 0.5 + 0.5 * math.sin(dx / 3.2) * math.cos(dy / 2.7)
    return max(0.02, min(1.0, 0.25 * base + 0.6 * base * ripple + 0.12 * rng.random()))


def mesh_codes() -> Iterator[str]:
    min_lng, min_lat, max_lng, max_lat = BBOX
    d_lat, d_lng = meshlib.cell_size_deg("53394611")
    lat = min_lat
    seen: set[str] = set()
    while lat <= max_lat:
        lng = min_lng
        while lng <= max_lng:
            code = meshlib.from_lat_lng(lat, lng, 8)
            if code not in seen:
                seen.add(code)
                yield code
            lng += d_lng / 2
        lat += d_lat / 2


def generate(conn: psycopg.Connection, audit: AcquisitionLog) -> dict[str, int]:
    rng = random.Random(SEED)
    today = date.today()
    counts: dict[str, int] = {}

    for spec in SOURCES.values():
        upsert_source(conn, spec)

    # ------------------------------------------------------------------ mesh
    meshes: list[dict[str, Any]] = []
    for code in mesh_codes():
        lng, lat = meshlib.centroid(code)
        density = _density(lng, lat, rng)
        population = int(density * 22000 + rng.uniform(-800, 800))
        if population < 50:
            continue
        elderly_share = 0.32 - 0.12 * density + rng.uniform(-0.03, 0.03)
        child_share = 0.08 + 0.06 * density + rng.uniform(-0.015, 0.015)
        age_65 = int(population * max(0.05, elderly_share))
        age_0_14 = int(population * max(0.03, child_share))
        meshes.append({
            "mesh_code": code,
            "population": population,
            "age_0_14": age_0_14,
            "age_65_plus": age_65,
            "age_15_64": population - age_0_14 - age_65,
            "households": int(population / rng.uniform(1.7, 2.4)),
            "population_growth": round(rng.gauss(0.012, 0.028), 4),
            "polygon_wkt": meshlib.to_polygon_wkt(code),
            "lng": lng, "lat": lat,
        })

    with conn.cursor() as cur:
        cur.execute("DELETE FROM population_mesh WHERE source_id = 'sample_population_mesh'")
        for m in meshes:
            cur.execute(
                """
                INSERT INTO population_mesh (
                    source_id, mesh_code, mesh_size_m, prefecture_code, geom, centroid,
                    population, age_0_14, age_15_64, age_65_plus, households,
                    population_growth, source_date, last_updated
                ) VALUES (
                    'sample_population_mesh', %(mesh_code)s, %(size)s, %(pref)s,
                    ST_GeomFromText(%(polygon_wkt)s, 4326),
                    ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326),
                    %(population)s, %(age_0_14)s, %(age_15_64)s, %(age_65_plus)s,
                    %(households)s, %(population_growth)s, %(source_date)s, now()
                )
                """,
                {**m, "size": MESH_SIZE_M, "pref": PREFECTURE_CODE, "source_date": today},
            )
    counts["population_mesh"] = len(meshes)

    # --------------------------------------------------------------- clinics
    clinics: list[dict[str, Any]] = []
    for m in meshes:
        expected = m["population"] / 3400.0
        n = int(expected) + (1 if rng.random() < (expected % 1) else 0)
        for i in range(n):
            clinics.append({
                "facility_id": f"SAMPLE-{m['mesh_code']}-{i:02d}",
                "name": f"{SAMPLE_PREFIX}歯科医院 {m['mesh_code']}-{i + 1}",
                "address": f"{SAMPLE_PREFIX}東京都（メッシュ {m['mesh_code']}）",
                "lng": m["lng"] + rng.uniform(-0.0055, 0.0055),
                "lat": m["lat"] + rng.uniform(-0.0040, 0.0040),
                "clinic_types": rng.sample(CLINIC_TYPES, rng.randint(1, 3)),
            })

    with conn.cursor() as cur:
        cur.execute("DELETE FROM facilities WHERE source_id = 'sample_dental_clinics'")
        for c in clinics:
            cur.execute(
                """
                INSERT INTO facilities (
                    source_id, facility_id, facility_category, name, address,
                    prefecture_code, geom, clinic_types, source_date, last_updated
                ) VALUES (
                    'sample_dental_clinics', %(facility_id)s, 'dental_clinic', %(name)s,
                    %(address)s, %(pref)s,
                    ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326),
                    %(clinic_types)s, %(source_date)s, now()
                )
                """,
                {**c, "pref": PREFECTURE_CODE, "source_date": today},
            )
    counts["facilities"] = len(clinics)

    # -------------------------------------------------------------- stations
    stations: list[dict[str, Any]] = []
    min_lng, min_lat, max_lng, max_lat = BBOX
    step_lng, step_lat = 0.0165, 0.0125
    idx = 0
    lat = min_lat + step_lat
    while lat < max_lat:
        lng = min_lng + step_lng
        while lng < max_lng:
            if rng.random() < 0.55:
                lng += step_lng
                continue
            idx += 1
            density = _density(lng, lat, rng)
            stations.append({
                "station_id": f"SAMPLE-ST-{idx:04d}",
                "name": f"{SAMPLE_PREFIX}第{idx}駅",
                "operator": f"{SAMPLE_PREFIX}鉄道",
                "railway_line": f"{SAMPLE_PREFIX}{chr(65 + idx % 6)}線",
                "lng": lng + rng.uniform(-0.004, 0.004),
                "lat": lat + rng.uniform(-0.003, 0.003),
                "daily_passengers": int(2000 + density * rng.uniform(20000, 260000)),
            })
            lng += step_lng
        lat += step_lat

    with conn.cursor() as cur:
        cur.execute("DELETE FROM stations WHERE source_id = 'sample_stations'")
        for s in stations:
            cur.execute(
                """
                INSERT INTO stations (
                    source_id, station_id, name, operator, railway_line,
                    prefecture_code, geom, daily_passengers, source_date, last_updated
                ) VALUES (
                    'sample_stations', %(station_id)s, %(name)s, %(operator)s,
                    %(railway_line)s, %(pref)s,
                    ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326),
                    %(daily_passengers)s, %(source_date)s, now()
                )
                """,
                {**s, "pref": PREFECTURE_CODE, "source_date": today},
            )
    counts["stations"] = len(stations)

    # ---------------------------------------------------------- areas (grid)
    # Not municipalities: a labelled grid so the ranking table has an area
    # column. The names are deliberately not those of real wards.
    areas = 0
    with conn.cursor() as cur:
        cur.execute("DELETE FROM municipalities WHERE source_id = 'sample_municipalities'")
        rows, cols = 5, 6
        for r in range(rows):
            for c in range(cols):
                lo_lng = min_lng + (max_lng - min_lng) * c / cols
                hi_lng = min_lng + (max_lng - min_lng) * (c + 1) / cols
                lo_lat = min_lat + (max_lat - min_lat) * r / rows
                hi_lat = min_lat + (max_lat - min_lat) * (r + 1) / rows
                wkt = (
                    f"MULTIPOLYGON((({lo_lng} {lo_lat}, {hi_lng} {lo_lat}, "
                    f"{hi_lng} {hi_lat}, {lo_lng} {hi_lat}, {lo_lng} {lo_lat})))"
                )
                areas += 1
                cur.execute(
                    """
                    INSERT INTO municipalities (
                        source_id, municipality_code, name, prefecture_code,
                        prefecture_name, geom, source_date, last_updated
                    ) VALUES (
                        'sample_municipalities', %(code)s, %(name)s, %(pref)s,
                        %(pref_name)s, ST_GeomFromText(%(wkt)s, 4326), %(source_date)s, now()
                    )
                    """,
                    {
                        "code": f"S{r}{c}",
                        "name": f"{SAMPLE_PREFIX}地区 {chr(65 + r)}-{c + 1}",
                        "pref": PREFECTURE_CODE,
                        "pref_name": f"{SAMPLE_PREFIX}東京都",
                        "wkt": wkt,
                        "source_date": today,
                    },
                )
    counts["municipalities"] = areas

    conn.commit()

    now = datetime.now(timezone.utc)
    for source_id, table in (
        ("sample_population_mesh", "population_mesh"),
        ("sample_dental_clinics", "facilities"),
        ("sample_stations", "stations"),
        ("sample_municipalities", "municipalities"),
    ):
        audit.write(StepRecord(
            source_id=source_id, step="load", status="success",
            record_count=counts[table], started_at=now, finished_at=now,
            params={"generated": True,
                    "warning": "synthetic development data, not real statistics"},
        ))
    return counts


def drop(conn: psycopg.Connection, only: list[str] | None = None) -> dict[str, int]:
    """Remove sample sources and the rows attributed to them.

    ``only`` limits the removal to named source ids, which is what you want
    after loading real data for one dataset while the others are still
    unavailable.
    """
    removed: dict[str, int] = {}
    with conn.cursor() as cur:
        if only:
            cur.execute(
                "SELECT id FROM data_sources WHERE dataset_kind = 'sample' AND id = ANY(%s)",
                (only,),
            )
        else:
            cur.execute("SELECT id FROM data_sources WHERE dataset_kind = 'sample'")
        ids = [r["id"] for r in cur.fetchall()]
        for source_id in ids:
            for table in ("facilities", "population_mesh", "stations", "municipalities"):
                cur.execute(f"DELETE FROM {table} WHERE source_id = %s", (source_id,))
                removed[table] = removed.get(table, 0) + cur.rowcount
            cur.execute("DELETE FROM acquisition_runs WHERE source_id = %s", (source_id,))
            cur.execute("DELETE FROM data_sources WHERE id = %s", (source_id,))
    conn.commit()
    return removed
