"""One point, everything known about it, in a shape a reader can reason over.

The rest of the API is shaped for the screen: each endpoint answers the
question one panel asks. This assembles the lot for a single location --
residents, daytime workers and their industry mix, competing clinics,
stations and their passenger counts, published land prices, the scores under
every configured model -- into one document.

Three things it does that a UI response does not have to:

**It says what every number means.** Every figure is accompanied by its unit
and its source in ``definitions``. A reader that has never seen this project
cannot otherwise tell 従業者数 from 昼間人口, or 円/m² of land from rent, and
both mistakes produce confident wrong answers.

**It distinguishes absent from zero.** ``null`` means not known; a zero means
a counted zero. Where a whole dataset is missing, ``data_quality.unavailable``
names it, so "no clinics nearby" cannot be read out of an unloaded table.

**It carries its own caveats.** The disclaimers and the known weaknesses of
each dataset travel with the data rather than living in a screen the reader
never sees. Anything that reads this and produces prose will have them.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg

from kaigyou_core import provenance as prov
from kaigyou_core.analysis import (
    DEFAULT_CATCHMENT,
    DEFAULT_CATEGORY,
    analyze_point,
    catchment_geojson,
    default_prefecture,
    land_prices_near,
    load_distributions,
    prefecture_at,
    prefecture_name,
    resolve_mesh_size,
)
from kaigyou_core.db import table_exists
from kaigyou_core.scoring import ScoringModel, scope_key

#: Bumped when the shape changes in a way that would break a reader.
SCHEMA_VERSION = "1.0"

#: Rows returned for the list sections. Enough to characterise a trade area,
#: bounded so that one request cannot return every clinic in Tokyo. The count
#: is always reported in full, so a truncated list never understates it.
MAX_CLINICS = 50
MAX_STATIONS = 20
MAX_LAND_POINTS = 10

#: What each figure is, in one line. Kept beside the data rather than in a
#: document elsewhere, because a reader that has to guess the unit will guess.
DEFINITIONS: dict[str, dict[str, str]] = {
    "population": {
        "unit": "人",
        "description": "常住人口（夜間人口）。国勢調査500mメッシュを商圏との面積按分で合算。",
        "source": "総務省統計局 国勢調査（e-Stat 統計GIS）",
    },
    "age_0_14": {"unit": "人", "description": "0〜14歳の常住人口。",
                 "source": "総務省統計局 国勢調査"},
    "age_15_64": {"unit": "人", "description": "15〜64歳の常住人口。",
                  "source": "総務省統計局 国勢調査"},
    "age_65_plus": {"unit": "人", "description": "65歳以上の常住人口。",
                    "source": "総務省統計局 国勢調査"},
    "households": {"unit": "世帯", "description": "世帯数。",
                   "source": "総務省統計局 国勢調査"},
    "population_growth": {
        "unit": "比率",
        "description": "2015年→2020年の人口増減率。0.05 なら +5%。人口で加重平均。",
        "source": "総務省統計局 国勢調査（2回の調査の差）",
    },
    "workers": {
        "unit": "人",
        "description": (
            "従業地ベースの従業者数。そこで働く人の数であり、昼間人口ではない"
            "（通学者・来街者は含まない）。常住人口とは別集計で、足し合わせると"
            "通勤者を二重に数えることになる。"),
        "source": "総務省統計局・経済産業省 経済センサス（e-Stat 統計GIS）",
    },
    "establishments": {"unit": "事業所", "description": "事業所数。",
                       "source": "経済センサス"},
    "industry_workers": {
        "unit": "人",
        "description": (
            "産業分類別の従業者数。secondary=第2次産業, tertiary=第3次産業, "
            "wholesale_retail=卸売・小売, accommodation_food=宿泊・飲食, "
            "education=教育・学習支援, health_welfare=医療・福祉。"
            "分類は重なるため合計は総数と一致しない。"),
        "source": "経済センサス",
    },
    "dental_clinics": {
        "unit": "件",
        "description": "商圏内の歯科診療所の数。施設数であり、規模・ユニット数・診療実績は含まない。",
        "source": "厚生労働省 医療機能情報提供制度（医療情報ネット）",
    },
    "population_per_clinic": {
        "unit": "人/件",
        "description": "常住人口 ÷ 歯科診療所数。多いほど1院あたりの人口が多い。",
        "source": "上記2つから算出",
    },
    "workers_per_clinic": {"unit": "人/件", "description": "従業者数 ÷ 歯科診療所数。",
                           "source": "上記2つから算出"},
    "daily_passengers": {
        "unit": "人/日",
        "description": "駅の1日あたり乗降客数。同一駅に複数事業者が乗り入れる場合は合算。",
        "source": "国土交通省 国土数値情報 S12（駅別乗降客数）",
    },
    "land_price_yen_per_sqm": {
        "unit": "円/m²",
        "description": (
            "地価公示の標準地の価格（毎年1月1日時点）の中央値。土地1m²の価格であり、"
            "賃料ではない（建物・階数・契約条件を含まない）。"),
        "source": "国土交通省 国土数値情報 L01（地価公示）",
    },
    "change_rate_pct": {"unit": "%", "description": "地価の対前年変動率。",
                        "source": "国土数値情報 L01"},
    "distance_m": {"unit": "m", "description": "指定地点からの直線距離。", "source": "算出"},
    "score": {
        "unit": "0-100",
        "description": (
            "同一都道府県内のメッシュ分布に対する相対スコア。暫定モデルであり、"
            "実績データによる較正は行っていない。都道府県をまたぐ比較はできない。"),
        "source": "config/scoring.yaml の重みによる算出",
    },
}


def _dataset_caveats() -> list[str]:
    """Known weaknesses, stated up front rather than discovered later."""
    return [
        "スコアは相対値であり、開業の成否・売上・患者数を予測するものではありません。",
        "スコアは同一都道府県内で正規化しています。都道府県をまたいだスコアの比較はできません。",
        "「従業者数」は昼間人口ではありません。通学者・来街者は含まれず、"
        "繁華街の来街需要は捕捉できていません。",
        "「地価」は土地の価格であり賃料ではありません。テナント賃料・初期投資額の"
        "代わりには使えません。",
        "歯科診療所は施設数のみです。規模・ユニット数・診療実績・経営状態は含まれません。",
        "国勢調査メッシュは秘匿処理により、小規模メッシュの値が隣接メッシュへ"
        "合算されています。合計は保たれますが局所的に1メッシュ分ずれることがあります。",
        "人口増減率は2015年→2020年の変化で、直近の動向とは異なる場合があります。",
    ]


def _municipality(conn: psycopg.Connection, lat: float, lng: float) -> dict[str, Any] | None:
    if not table_exists(conn, "municipalities"):
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT municipality_code, name, prefecture_code, prefecture_name
            FROM municipalities
            WHERE geom && ST_SetSRID(ST_MakePoint(%s, %s), 4326)
              AND ST_Contains(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
            LIMIT 1
            """, (lng, lat, lng, lat))
        row = cur.fetchone()
    return dict(row) if row else None


def _industry_mix(conn: psycopg.Connection, lat: float, lng: float, radius_m: int,
                  mesh_size_m: int) -> dict[str, Any] | None:
    """Workers by industry division, apportioned like every other mesh figure.

    Stored per mesh as jsonb and never surfaced until now. It is what separates
    "300,000 people work here" from "300,000 people work here, four fifths of
    them in offices" -- and a dental practice's day looks different in the two.
    """
    if not table_exists(conn, "mesh_business"):
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH buf AS (
                SELECT ST_Buffer(ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                                 %s)::geometry AS g
            ),
            shares AS (
                SELECT b.industry_workers, b.industry_establishments,
                       LEAST(1.0, GREATEST(0.0,
                           ST_Area(ST_Intersection(b.geom, buf.g)::geography)
                           / NULLIF(ST_Area(b.geom::geography), 0))) AS share
                FROM mesh_business b, buf
                WHERE b.mesh_size_m = %s AND b.geom && buf.g
                  AND ST_Intersects(b.geom, buf.g)
            )
            SELECT w.key AS division,
                   SUM((w.value)::numeric * shares.share)::double precision AS workers,
                   SUM(COALESCE((shares.industry_establishments ->> w.key)::numeric, 0)
                       * shares.share)::double precision AS establishments
            FROM shares, jsonb_each_text(shares.industry_workers) AS w
            GROUP BY w.key
            ORDER BY 2 DESC
            """, (lng, lat, radius_m, mesh_size_m))
        rows = cur.fetchall()
    if not rows:
        return None
    return {
        r["division"]: {"workers": round(r["workers"] or 0),
                        "establishments": round(r["establishments"] or 0)}
        for r in rows
    }


def _clinics(conn: psycopg.Connection, lat: float, lng: float, radius_m: int,
             category: str, limit: int = MAX_CLINICS) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*)::int AS n
            FROM facilities
            WHERE facility_category = %s
              AND ST_DWithin(geom::geography,
                             ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
            """, (category, lng, lat, radius_m))
        total = cur.fetchone()["n"]

        cur.execute(
            """
            SELECT name, address, clinic_types, founder_type, opening_date,
                   attributes,
                   ST_Distance(geom::geography,
                               ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) AS distance_m,
                   ST_Y(geom) AS lat, ST_X(geom) AS lng
            FROM facilities
            WHERE facility_category = %s
              AND ST_DWithin(geom::geography,
                             ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
            ORDER BY geom::geography <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
            LIMIT %s
            """, (lng, lat, category, lng, lat, radius_m, lng, lat, max(limit, 0)))
        rows = cur.fetchall() if limit > 0 else []

    items = []
    for row in rows:
        item = {
            "name": row["name"],
            "address": row["address"],
            "distance_m": round(row["distance_m"]),
            "lat": row["lat"], "lng": row["lng"],
            "clinic_types": row["clinic_types"] or None,
            "founder_type": row["founder_type"],
            "opening_date": row["opening_date"].isoformat() if row["opening_date"] else None,
            "homepage": (row["attributes"] or {}).get("homepage"),
        }
        items.append(item)
    return {"count": total, "listed": len(items), "truncated": total > len(items),
            "items": items}


def _stations(conn: psycopg.Connection, lat: float, lng: float,
              radius_m: int) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT name, operator, railway_line, daily_passengers, passengers_year,
                   ST_Distance(geom::geography,
                               ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) AS distance_m,
                   ST_Y(geom) AS lat, ST_X(geom) AS lng
            FROM stations
            WHERE ST_DWithin(geom::geography,
                             ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
            ORDER BY geom::geography <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
            LIMIT %s
            """, (lng, lat, lng, lat, radius_m, lng, lat, MAX_STATIONS))
        rows = cur.fetchall()
    return {
        "count": len(rows),
        "items": [{
            "name": r["name"], "operator": r["operator"], "railway_line": r["railway_line"],
            "daily_passengers": r["daily_passengers"],
            "passengers_year": r["passengers_year"],
            "distance_m": round(r["distance_m"]),
            "lat": r["lat"], "lng": r["lng"],
        } for r in rows],
    }


def _round(value: Any, digits: int = 0) -> Any:
    if value is None:
        return None
    return round(float(value), digits) if digits else round(float(value))


def build_dataset(conn: psycopg.Connection, lat: float, lng: float, radius_m: int, *,
                  catchment: str = DEFAULT_CATCHMENT,
                  category: str = DEFAULT_CATEGORY,
                  prefecture_code: str | None = None,
                  mesh_size_m: int | None = None,
                  profile: str | None = None,
                  scoring_config: dict[str, Any] | None = None,
                  include_geometry: bool = False,
                  max_clinics: int = MAX_CLINICS,
                  disclaimer: str = "", score_disclaimer: str = "") -> dict[str, Any]:
    """Everything this database knows about one point, in one document."""
    from kaigyou_core import config as cfg

    scoring_config = scoring_config or cfg.scoring_config()
    model = ScoringModel(scoring_config, profile)

    # The point decides its own prefecture, as everywhere else: scoring a
    # Chiyoda click against another prefecture's normalisation is meaningless.
    prefecture_code = prefecture_code or prefecture_at(conn, lat, lng)
    prefecture_code = default_prefecture(conn, prefecture_code)
    mesh_size_m = resolve_mesh_size(conn, mesh_size_m, prefecture_code)

    radii = sorted({*model.radii, radius_m})
    by_radius: dict[str, Any] = {}
    for r in radii:
        m = analyze_point(conn, lat, lng, r, category, mesh_size_m or 1000, catchment)
        by_radius[str(r)] = {
            "population": _round(m.get("population")),
            "age_0_14": _round(m.get("age_0_14")),
            "age_15_64": _round(m.get("age_15_64")),
            "age_65_plus": _round(m.get("age_65_plus")),
            "households": _round(m.get("households")),
            "population_growth": _round(m.get("population_growth"), 4),
            "workers": _round(m.get("workers")),
            "establishments": _round(m.get("establishments")),
            "dental_clinics": m.get("facility_count"),
            "population_per_clinic": _round(m.get("population_per_facility")),
            "workers_per_clinic": _round(m.get("workers_per_facility")),
            "land_price_yen_per_sqm": _round(m.get("land_price_yen_per_sqm")),
            "mesh_count": m.get("mesh_count"),
        }

    metrics = analyze_point(conn, lat, lng, radius_m, category,
                            mesh_size_m or 1000, catchment)
    scope = scope_key(mesh_size_m or 1000, radius_m, prefecture_code)
    distributions = load_distributions(conn, scope)

    scores = []
    for name in (scoring_config.get("profiles") or {}):
        alt = ScoringModel(scoring_config, name)
        result = alt.score(metrics, distributions)
        scores.append({
            "profile": name,
            "label": alt.label,
            "overall": result.get("overall"),
            "components": {
                "demand": result.get("demand"),
                "competition": result.get("competition"),
                "growth": result.get("growth"),
                "accessibility": result.get("accessibility"),
                "cost": result.get("cost"),
            },
            "weights": alt.profile.get("overall_weights", {}),
            "unavailable_components": result.get("unavailable_components", []),
            "missing_required_components": result.get("missing_required_components", []),
            "is_provisional": True,
        })

    municipality = _municipality(conn, lat, lng)
    land = land_prices_near(conn, lat, lng, radius_m, limit=MAX_LAND_POINTS)

    tables = ["population_mesh", "mesh_business", "facilities", "stations",
              "municipalities", "land_prices"]
    provenance = prov.for_tables(conn, tables)
    unavailable = [d["dataset_label"] for d in provenance.get("datasets_unavailable", [])]

    notes: list[str] = []
    if not distributions:
        notes.append(f"スコア基準（{scope}）が未計算のため、相対スコアは算出されていません。")
    if metrics.get("catchment_kind") != catchment:
        notes.append(f"商圏の形は要求 {catchment} に対して "
                     f"{metrics.get('catchment_kind')} で算出しています。")
    if not metrics.get("mesh_count"):
        notes.append("この地点の商圏に人口メッシュデータがありません。")
    # clinic_types is in the schema and empty in the published file; saying so
    # stops a reader concluding that no clinic offers 小児歯科.
    notes.append("歯科診療所の診療科目（clinic_types）は取り込み元のファイルに"
                 "収録されていないため、すべて空です。「該当なし」ではありません。")
    notes.append("歯科診療所の休診日は取り込み元の値をそのまま保持していますが、"
                 "実態と一致しない例が確認されているため、この項目は返していません。")

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "query": {
            "lat": lat, "lng": lng, "radius_m": radius_m,
            "catchment_requested": catchment,
            "facility_category": category,
            "mesh_size_m": mesh_size_m,
            "prefecture_code": prefecture_code,
            "active_profile": model.profile_name,
        },
        "location": {
            "lat": lat, "lng": lng,
            "prefecture_code": prefecture_code,
            "prefecture_name": prefecture_name(conn, prefecture_code),
            "municipality_code": (municipality or {}).get("municipality_code"),
            "municipality_name": (municipality or {}).get("name"),
        },
        "catchment": {
            "kind": metrics.get("catchment_kind"),
            "radius_m": radius_m,
            "area_km2": _round(metrics.get("catchment_area_km2"), 3),
            "description": ("円（直線距離）" if metrics.get("catchment_kind") == "circle"
                            else "徒歩圏（街路網に沿った距離）"),
            **({"geometry": (catchment_geojson(conn, lat, lng, radius_m, catchment)
                             or {}).get("geometry")} if include_geometry else {}),
        },
        "demand": {
            "residents": {"by_radius": {r: {
                k: v for k, v in vals.items()
                if k in ("population", "age_0_14", "age_15_64", "age_65_plus",
                         "households", "population_growth", "mesh_count")}
                for r, vals in by_radius.items()}},
            "daytime": {
                "by_radius": {r: {k: vals[k] for k in ("workers", "establishments")}
                              for r, vals in by_radius.items()},
                "industry_mix": _industry_mix(conn, lat, lng, radius_m,
                                              mesh_size_m or 1000),
            },
        },
        "competition": {
            "by_radius": {r: {k: vals[k] for k in
                              ("dental_clinics", "population_per_clinic",
                               "workers_per_clinic")}
                          for r, vals in by_radius.items()},
            "nearest": {
                "name": metrics.get("nearest_facility_name"),
                "distance_m": _round(metrics.get("nearest_facility_distance_m")),
            },
            "clinics_in_radius": _clinics(conn, lat, lng, radius_m, category, max_clinics),
        },
        "access": {
            "nearest_station": {
                "name": metrics.get("nearest_station"),
                "distance_m": _round(metrics.get("station_distance_m")),
                "daily_passengers": metrics.get("daily_passengers"),
            },
            "stations_in_radius": _stations(conn, lat, lng, radius_m),
        },
        "cost": {
            "land_price_yen_per_sqm": _round(metrics.get("land_price_yen_per_sqm")),
            "surveyed_points": metrics.get("land_price_points"),
            "basis": metrics.get("land_price_basis"),
            "by_use_division": (land or {}).get("by_use", []),
            "nearest_points": (land or {}).get("nearest", []),
            "note": (land or {}).get(
                "note", "地価公示が未取得のため、コストの情報はありません。"),
        },
        "scores": {
            "normalization_scope": scope,
            "by_profile": scores,
            "note": ("スコアは同一都道府県内のメッシュ分布に対する相対値です。"
                     "暫定モデルであり、実績データによる較正は行っていません。"),
        },
        "data_quality": {
            "unavailable_datasets": unavailable,
            "notes": notes,
            "caveats": _dataset_caveats(),
        },
        "definitions": DEFINITIONS,
        "provenance": provenance,
        "disclaimer": disclaimer,
        "score_disclaimer": score_disclaimer,
    }
