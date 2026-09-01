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

**Every headline figure knows where it stands** (schema 2.0). A number on its
own can only be restated: "0〜14歳が7,331人" rewrites to "there are 7,331
children". The same number carrying its benchmark, percentile, rank and change
-- 7,331, against a prefecture median of 3,137, top 6%, 327th of 5,448, +4.3%
where the median moved +1.2% -- can be reasoned about. ``measures`` holds every
figure in that form, and ``insight_metrics`` groups the ones that have to be
read together, listing what could *not* be established in ``gaps``. That last
part is the point: a reader that cannot tell "checked, none found" from "never
looked" will confidently write the wrong sentence.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Mapping

import psycopg

from kaigyou_core import provenance as prov
from kaigyou_core.analysis import (
    DEFAULT_CATCHMENT,
    DEFAULT_CATEGORY,
    analyze_point,
    catchment_geojson,
    default_prefecture,
    land_prices_near,
    resolve_distributions,
    prefecture_at,
    prefecture_name,
    resolve_mesh_size,
)
from kaigyou_core.db import column_exists, table_exists
from kaigyou_core.measures import (
    READING_GUIDE,
    build_insights,
    build_measures,
    scope_summary,
)
from kaigyou_core import specialties as vocab
from kaigyou_core.scoring import (
    ScoringModel,
    augment_specialty_metrics,
    competition_specialties,
)

#: Bumped when the shape changes in a way that would break a reader.
SCHEMA_VERSION = "2.0"

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
    "specialty_counts": {
        "unit": "件",
        "description": (
            "商圏内の歯科医院を標榜診療科目別に数えたもの。分母は診療科目データが"
            "ある医院（with_data）であり、商圏内の全医院ではない。"
            "declared_only=true の科目は自由記載欄からの抽出で、記載した医院しか"
            "数えられない。"),
        "source": "厚生労働省 医療機能情報提供制度（医療情報ネット）診療科目",
    },
    "specialty_population_per_clinic": {
        "unit": "人",
        "description": (
            "その科目を標榜する医院1件あたりの商圏人口。小児歯科は0〜14歳人口で"
            "算出する（scores の competition.population_metric を参照）。"),
        "source": "国勢調査 × 医療情報ネット",
    },
    "clinic_hours": {
        "unit": "件 / 時間",
        "description": (
            "商圏内の医院のうち土曜・日曜・祝日・夜間に診療枠がある件数と、"
            "週間診療時間の中央値。夜間は終了時刻18:30以降。重複する時間帯は"
            "結合してから合計している。"),
        "source": "厚生労働省 医療機能情報提供制度（医療情報ネット）診療時間",
    },
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
    "percentile": {
        "unit": "%",
        "description": (
            "同一半径・同一データで算出したメッシュ分布の中で、その値以下のメッシュの割合。"
            "30 なら「7割のメッシュより低い」。都道府県内と市区町村内の2つの尺度がある。"),
        "source": "mesh_scores（本アプリが算出したメッシュ集計）",
    },
    "nth_nearest_distance_m": {
        "unit": "m",
        "description": (
            "n番目に近い同種施設までの直線距離。件数が同じでも、この階段が短ければ密集、"
            "長ければ分散を意味する。"),
        "source": "厚生労働省 医療機能情報提供制度から算出",
    },
    "largest_mesh_share": {
        "unit": "比率",
        "description": (
            "商圏人口のうち、最大の1メッシュ（500m四方）が占める割合。0.4 なら4割が"
            "1メッシュに集中。合計値だけでは集合住宅1棟か一様な住宅地かを区別できない。"),
        "source": "国勢調査メッシュから算出",
    },
    "floor_area_ratio_pct": {
        "unit": "%",
        "description": (
            "容積率。敷地面積に対して建てられる延床面積の割合。テナントとしての診療所が"
            "入りうる床の量に効く。第一種低層住居専用地域は約100%、商業地域は約600%。"),
        "source": "国土数値情報 L01（地価公示の標準地の値）",
    },
    "building_coverage_pct": {
        "unit": "%", "description": "建蔽率。敷地面積に対する建築面積の割合。",
        "source": "国土数値情報 L01（地価公示の標準地の値）",
    },
    "zoning": {
        "unit": "区分",
        "description": (
            "都市計画の用途地域。regulation.land_price_survey にあるものは"
            "地価公示の標準地が置かれた地点の値で、候補地そのものの用途地域とは"
            "限らない。候補地の判定は regulation.city_plan（面データ）を見ること。"),
        "source": "国土数値情報 L01",
    },
    "city_plan_zone": {
        "unit": "区分",
        "description": (
            "候補地の座標が入っている都市計画の区域。用途地域は「何を建ててよいか」、"
            "区域区分（市街化区域／市街化調整区域）は「そもそも建てられるか」、"
            "立地適正化計画の誘導区域は「市町がそこに何を集めようとしているか」。"
            "面データによる判定なので、候補地そのものの値。"),
        "source": "国土数値情報 A55 都市計画決定情報",
    },
    "buildability": {
        "unit": "判定",
        "description": (
            "用途地域と区域区分の公表値だけによる、その業態の施設を建てられるかの"
            "一次判定。規模・接道・条例で変わり、決めるのは特定行政庁なので、"
            "建築確認や事前相談の代わりにはならない。判定の規則は業態ごとの"
            "設定ファイルにあり、診療所と病院では建てられる用途地域が異なる。"),
        "source": "国土数値情報 A55 と 建築基準法 別表第2（config/<業態>/city_planning.yaml）",
    },
    "score": {
        "unit": "0-100",
        "description": (
            "同一都道府県内のメッシュ分布に対する相対スコア。暫定モデルであり、"
            "実績データによる較正は行っていない。都道府県をまたぐ比較はできない。"),
        "source": "config/<業態>/scoring.yaml の重みによる算出",
    },
}


def _dataset_caveats() -> list[str]:
    """Known weaknesses, stated up front rather than discovered later."""
    return [
        "スコアは相対値であり、開業の成否・売上・患者数を予測するものではありません。",
        "スコアは同一都道府県内で正規化しています。都道府県をまたいだスコアの比較はできません。",
        "経済センサスの「従業者数」は昼間人口ではありません。**通学者が"
        "含まれません。** 大学や専門学校の門前では、そこにいる人の大半が"
        "従業者数に現れないことがあります。国勢調査の従業地・通学地メッシュ"
        "（demand.daytime.census_daytime）を取り込むと通学者まで数えられます。"
        "取り込んでいない地域では、そう表示されます。",
        "昼間人口を取り込んでいても、来街者（買い物・観光）は含まれません。"
        "繁華街の来街需要は、どちらの調査でも捕捉できていません。",
        "「地価」は土地の価格であり賃料ではありません。そこから賃料の目安を"
        "収益還元の考え方で機械的に換算していますが（cost.rent_estimate）、"
        "建物の状態・階数・契約条件・実際の募集事例を一切含まない粗い目安です。"
        "個別物件の賃料や初期投資額の"
        "代わりには使えません。",
        "歯科診療所は施設数と標榜診療科目・診療時間までです。"
        "規模・ユニット数・診療実績・経営状態・自費診療の比率は含まれません。",
        "標榜診療科目は届出値です。「小児歯科」を標榜していても小児を主に診ているとは限らず、"
        "標榜していなくても小児を診ている医院はあります。看板の数であって診療内容の数ではありません。",
        "インプラント・審美・訪問診療などは標榜診療科目ではなく自由記載欄にしかありません。"
        "記載率が低いため、これらの件数は実施医院数の下限です（東京都で"
        "インプラントの記載は1%台）。競合の少なさの根拠には使えません。",
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


def _neighbour_municipalities(conn: psycopg.Connection,
                              municipality_code: str | None) -> list[str]:
    """境界を接している市区町村の名前。

    開業地を探している人が実際に比べているのは、市の境界の内側ではなく
    **通える範囲**です。「裾野市内157商圏中2位」は、市がまるごと小さければ
    どこでも上位に入る数字で、意思決定には効きません。三島市・長泉町・
    御殿場市と並べて何位なのかが知りたいことです。

    半径ではなく**隣接**で決めます。半径10kmは既に ``nearby`` にあり、
    そちらは商圏の集合で、こちらは自治体の集合です。読み手が地図を持たなく
    ても、自治体の名前は知っています。

    同じ都道府県の中だけを見ます。スコアは都道府県内で正規化されているので、
    県をまたぐと同じ尺度になりません（要件の但し書きどおり）。
    """
    if not municipality_code or not table_exists(conn, "municipalities"):
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT n.name
            FROM municipalities me
            JOIN municipalities n
              ON n.municipality_code <> me.municipality_code
             AND n.prefecture_code = me.prefecture_code
             AND n.geom && ST_Expand(me.geom, %s)
             AND ST_Intersects(n.geom, ST_Expand(me.geom, %s))
            WHERE me.municipality_code = %s
            ORDER BY n.name
            """,
            (_ADJACENCY_TOLERANCE_DEG, _ADJACENCY_TOLERANCE_DEG, municipality_code))
        return [r["name"] for r in cur.fetchall()]


#: 隣接とみなす隙間。境界データの頂点は完全には一致しないので、厳密な
#: ST_Touches では隣どうしが落ちます。約100m。
_ADJACENCY_TOLERANCE_DEG = 0.001


def _clinic_vintage(conn: psycopg.Connection, lat: float, lng: float,
                    radius_m: int, category: str) -> dict[str, Any] | None:
    """商圏内の医院の開設年の分布。

    **なぜ要るか。** 「院長の世代交代で医院が減るかもしれない」は、歯科の
    開業で効く仮説のひとつです。20年後の競合の数は、いまの数ではなく
    「いまの院長があと何年やるか」で決まります。ところがこれを支える数字が
    どこにも出ていませんでした。年齢は公表されていませんが、**開設年は
    届出にあり、既に取り込んであります**（facilities.opening_date）。

    開設年は院長の年齢ではありません。承継で代替わりした医院も、法人が
    分院を出した医院も、開設は新しいままです。だから「40年前に開設」は
    「院長が高齢」ではなく、**そこを調べる価値がある**という意味です。
    仮説の代理指標であって、結論ではありません。

    取れなかった件数を必ず返します。3件しか分からない商圏で「中央値
    1998年」と書くと、10件ぶんの話に読めます。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*)                                    AS total,
                   count(opening_date)                         AS known,
                   min(EXTRACT(YEAR FROM opening_date))::int    AS oldest_year,
                   max(EXTRACT(YEAR FROM opening_date))::int    AS newest_year,
                   percentile_cont(0.5) WITHIN GROUP (
                       ORDER BY EXTRACT(YEAR FROM opening_date)) AS median_year,
                   count(*) FILTER (
                       WHERE opening_date <= (CURRENT_DATE - make_interval(years => %s))
                   ) AS opened_long_ago,
                   count(*) FILTER (
                       WHERE opening_date >= (CURRENT_DATE - make_interval(years => %s))
                   ) AS opened_recently
            FROM facilities
            WHERE facility_category = %s
              AND ST_DWithin(geom::geography,
                             ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
            """,
            (VINTAGE_LONG_AGO_YEARS, VINTAGE_RECENT_YEARS,
             category, lng, lat, radius_m))
        row = cur.fetchone()

    if not row or not row["total"]:
        return None
    if not row["known"]:
        # 「古い医院は0件」ではありません。**1件も分からなかった**のです。
        return {"available": False, "reason": "no_opening_dates",
                "total_clinics": int(row["total"]), "with_opening_date": 0,
                "note": "商圏内の医院について開設年月日が1件も取得できていません。"
                        "医療機能情報提供制度の配布ファイル（2026年6月版）には"
                        "開設年月日の列が無いためで、取り込みの失敗ではありません。"
                        "院長の世代交代による供給の増減は、この商圏について"
                        "手元のデータからは何も言えません。"}
    return {
        "available": True,
        "total_clinics": int(row["total"]),
        "with_opening_date": int(row["known"]),
        "coverage": round(row["known"] / row["total"], 3),
        "median_opening_year": (None if row["median_year"] is None
                                else int(row["median_year"])),
        "oldest_opening_year": row["oldest_year"],
        "newest_opening_year": row["newest_year"],
        "opened_over_years_ago": VINTAGE_LONG_AGO_YEARS,
        "opened_long_ago": int(row["opened_long_ago"]),
        "opened_within_years": VINTAGE_RECENT_YEARS,
        "opened_recently": int(row["opened_recently"]),
        "note": (f"開設年月日は医療機能情報提供制度の届出値。{row['known']}/"
                 f"{row['total']}件で取得できています。**開設年は院長の年齢では"
                 "ありません。** 承継で代替わりした医院も、法人の分院も、開設は"
                 "新しいままです。世代交代の可能性を調べる手がかりであって、"
                 "その証拠ではありません。"),
    }


#: 「古くからある医院」の線。開業から30年経っていれば、開設者がそのまま
#: 院長なら還暦前後です。決め打ちの閾値なので、意味づけではなく件数として
#: 出します。
VINTAGE_LONG_AGO_YEARS = 30
#: 「最近できた医院」の線。この商圏にいま参入が起きているかどうか。
VINTAGE_RECENT_YEARS = 5


#: 「若い通学者」の代理として見る年齢階級。大学・専門学校の在学年齢です。
#: 就業者も混じるので、これは**学生数ではありません**。そう名乗らせます。
_STUDENT_AGE_ORDERS = (2, 3)   # 15〜19歳, 20〜24歳


#: 「人数」の**数え方の基準**。ここを混ぜると、足してはいけないものを足します。
#:
#: 実測（早稲田駅・半径1km）で、同じ「働く人」がこうなります。
#:
#:     経済センサス 従業者数            52,688 人  ← 従業地基準（そこで働く人）
#:     国勢調査 当地に常住する就業者    22,322 人  ← 常住地基準（そこに住む働き手）
#:
#: 2.4 倍違いますが、これは調査の誤差ではありません。**別のものを数えて
#: います。** 前者は昼間そこにいる人、後者は夜そこにいる人です。「同じ
#: 国勢調査だから」という理由で後者に揃えると、昼間の人が半分以下になります。
#:
#: 在学者も同じです。T001108 の在学者は**そこに住んでいる学生**で、
#: 大学に通ってくる学生ではありません。従業者数に足しても昼間人口には
#: なりません。
MEASUREMENT_BASES = {
    "workplace": "従業地基準（そこで働いている人）",
    "residence": "常住地基準（そこに住んでいる人）",
    "workplace_or_school": "従業地・通学地基準（そこで働く人＋そこに通う人＝昼間人口）",
}


#: 通勤・通学の交通手段。**1人が複数を使うので合計は人数と一致しません。**
#: 比率で読むものなので、割合も一緒に返します。
_COMMUTE_MODES = (("commute_walk", "徒歩のみ"), ("commute_rail", "鉄道・電車"),
                  ("commute_bus", "乗合バス"), ("commute_car", "自家用車"),
                  ("commute_motorcycle", "オートバイ"),
                  ("commute_bicycle", "自転車"))


def resident_profile(conn: psycopg.Connection, lat: float, lng: float,
                     radius_m: int, mesh_size_m: int) -> dict[str, Any]:
    """そこに住んでいる人の性格（交通手段・居住期間・在学・雇用形態）。

    **昼間人口ではありません。常住地基準です。** 実測：大学・大学院在学者が
    いちばん多いメッシュでも 835 人、早稲田駅のメッシュで 393 人。通学地基準
    ならキャンパスに数万人が出ます。「そこに住んでいる学生」であって
    「そこに通ってくる学生」ではありません。

    歯科の判断を変えるものが 3 つ入っています。

    - **利用交通手段** … 駐車場が要るかどうかの代理。これまで「データが
      無いので現地で確認」としか書けませんでした
    - **居住期間** … かかりつけとリコールが回る街か。年齢構成では分かりません
    - **未就学者の内訳** … 0〜14歳より小児歯科の需要に近い

    重み付けは他のメッシュ集計と同じ面積按分です。
    """
    if not table_exists(conn, "mesh_resident_profile"):
        return {"available": False, "reason": "not_migrated",
                "note": "居住者プロファイルのテーブルがまだありません"
                        "（kaigyou-etl migrate）。"}
    fields = ([m for m, _l in _COMMUTE_MODES]
              + ["resident_under_1y", "resident_1_to_5y", "resident_20y_plus",
                 "preschool_total", "preschool_nursery", "students_high_school",
                 "students_university", "workers_living_here",
                 "students_living_here", "employees_regular",
                 "employees_part_time", "self_employed"])
    sums = ", ".join(f"SUM(p.{f} * s.w) AS {f}" for f in fields)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH buf AS (
                SELECT ST_Buffer(ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography,
                                 %(radius)s)::geometry AS g
            ),
            share AS (
                SELECT m.mesh_code,
                       LEAST(1.0, GREATEST(0.0,
                           ST_Area(ST_Intersection(m.geom, buf.g)::geography)
                           / NULLIF(ST_Area(m.geom::geography), 0))) AS w
                  FROM population_mesh m, buf
                 WHERE m.mesh_size_m = %(mesh)s
                   AND m.geom && buf.g AND ST_Intersects(m.geom, buf.g)
            )
            SELECT {sums}, count(*) AS mesh_count, MAX(p.source_date) AS source_date
              FROM mesh_resident_profile p
              JOIN share s ON s.mesh_code = p.mesh_code
             WHERE p.mesh_size_m = %(mesh)s
            """,
            {"lat": lat, "lng": lng, "radius": radius_m, "mesh": mesh_size_m})
        row = cur.fetchone()

    if not row or not row["mesh_count"]:
        return {"available": False, "reason": "not_loaded",
                "note": "この地域の就業状態等基本集計（メッシュ）は"
                        "取り込まれていません。来院手段の手がかり（通勤・通学の"
                        "交通手段）と、居住期間の長さは分かりません。"}

    values = {f: _round(row[f]) for f in fields}
    modes = [{"key": key, "label": label, "people": values.get(key)}
             for key, label in _COMMUTE_MODES]
    # 1人が複数の手段を使うので、割合の分母は合計です（人数ではありません）。
    denominator = sum(m["people"] or 0 for m in modes)
    for mode in modes:
        mode["share"] = (None if not denominator or mode["people"] is None
                         else round(mode["people"] / denominator, 3))
    settled = values.get("resident_20y_plus")
    recent = values.get("resident_under_1y")
    return {
        "available": True,
        "mesh_count": int(row["mesh_count"]),
        "source_date": row["source_date"],
        "commute_modes": modes,
        "car_share": next((m["share"] for m in modes
                           if m["key"] == "commute_car"), None),
        "residence": {
            "under_1_year": recent,
            "one_to_five_years": values.get("resident_1_to_5y"),
            "twenty_years_plus": settled,
            "note": "20年以上住んでいる人が多い街と、1年未満が多い街では、"
                    "かかりつけとリコール（定期管理）の回り方が違います。",
        },
        "schooling": {
            "preschool_total": values.get("preschool_total"),
            "preschool_nursery": values.get("preschool_nursery"),
            "high_school": values.get("students_high_school"),
            "university": values.get("students_university"),
            "note": "**常住地基準です。そこに住んでいる在学者であって、"
                    "そこに通ってくる在学者ではありません。** 大学の門前でも、"
                    "この数字は通学者の数になりません。",
        },
        "employment": {
            "regular": values.get("employees_regular"),
            "part_time": values.get("employees_part_time"),
            "self_employed": values.get("self_employed"),
            "workers_living_here": values.get("workers_living_here"),
            "students_living_here": values.get("students_living_here"),
        },
        "basis": "residence",
        "basis_label": MEASUREMENT_BASES["residence"],
        "definition": ("国勢調査 就業状態等基本集計を、現在人口と同じ面積按分で"
                       "商圏に切り出したもの。**常住地基準であり、昼間人口では"
                       "ありません。** 交通手段は通勤・通学の手段で、来院手段"
                       "そのものではありませんが、その地域で車が使われるか"
                       "どうかの手がかりになります。"),
    }


def municipality_daytime(conn: psycopg.Connection,
                         municipality_code: str | None) -> dict[str, Any]:
    """市区町村の昼夜間人口と、年齢階級ごとの膨らみ方。

    **商圏の数字ではありません。** 新宿区の昼間人口 793,528 人のうち何人が
    早稲田駅前の半径1km にいるかは、この表からは分かりません。歌舞伎町にも
    西新宿にもいます。**面積で按分するのは完全に間違いです。**

    では何のために出すのか。**文脈と、年齢の切り口です。** 新宿区の 20〜24歳は
    夜間 21,906 人に対して昼間 80,136 人（3.7倍）。「この街には昼間、若い
    通学者が大量に流入している」は、商圏の数字が無くても意思決定に効きます。
    そして年齢別は、メッシュ統計には無い切り口です。

    この数字は外部調査（Web検索）で自治体の PDF から拾っていました。一次
    データを手元に持てば、検索の枠が空き、値も正確になります。
    """
    if not municipality_code:
        return {"available": False, "reason": "no_municipality",
                "note": "市区町村が特定できていません（境界データが未取得）。"}
    if not table_exists(conn, "municipality_daytime"):
        return {"available": False, "reason": "not_migrated",
                "note": "市区町村の昼間人口のテーブルがまだありません"
                        "（kaigyou-etl migrate を実行してください）。"}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT age_band, age_order, night_population, daytime_population,
                   municipality_name, source_date
              FROM municipality_daytime
             WHERE municipality_code = %s AND sex LIKE '0!_%%' ESCAPE '!'
             ORDER BY age_order
            """, (municipality_code,))
        rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        return {"available": False, "reason": "not_loaded",
                "note": "この市区町村の従業地・通学地集計は取り込まれていません"
                        "（kaigyou-etl run estat_daytime_municipality）。"}

    def ratio(row: Mapping[str, Any]) -> float | None:
        night = row.get("night_population")
        day = row.get("daytime_population")
        return (None if not night or day is None
                else round(float(day) / float(night), 3))

    total = next((r for r in rows if r["age_order"] == 0), None)
    bands = [{"age_band": r["age_band"], "age_order": r["age_order"],
              "night_population": r["night_population"],
              "daytime_population": r["daytime_population"],
              "daytime_over_night": ratio(r)}
             for r in rows if r["age_order"] not in (None, 0)]
    young = [b for b in bands if b["age_order"] in _STUDENT_AGE_ORDERS]
    return {
        "available": True,
        "municipality_name": (total or rows[0]).get("municipality_name"),
        "night_population": (total or {}).get("night_population"),
        "daytime_population": (total or {}).get("daytime_population"),
        "daytime_over_night": ratio(total or {}),
        "by_age": bands,
        # 15〜24歳の膨らみ方。学生が流入している街ほど大きくなります。
        "young_inflow": {
            "age_bands": [b["age_band"] for b in young],
            "night_population": sum(b["night_population"] or 0 for b in young),
            "daytime_population": sum(b["daytime_population"] or 0 for b in young),
            "note": "15〜24歳は大学・専門学校の在学年齢ですが、**就業者も"
                    "含みます。学生数ではありません。** この層が昼間に大きく"
                    "膨らむ市区町村は、通学による流入が起きている可能性が"
                    "高い、というところまでが言えることです。",
        } if young else None,
        "source_date": (total or rows[0]).get("source_date"),
        "definition": ("国勢調査 従業地・通学地による人口（昼間人口）。"
                       "**市区町村全体の数字であって、この商圏の数字では"
                       "ありません。** 商圏に按分することはできません"
                       "（同じ区の中でも場所によってまったく違うため）。"
                       "商圏の昼間人口は demand.daytime.census_daytime を"
                       "見てください。"),
    }


def daytime_population(conn: psycopg.Connection, lat: float, lng: float,
                       radius_m: int, mesh_size_m: int) -> dict[str, Any]:
    """商圏の昼間人口（従業地・通学地による人口）。取れなければ、取れないと言う。

    **なぜ要るか。** 「昼間そこにいる人」を経済センサスの従業者数だけで
    測っていました。従業者は昼間人口の一部でしかなく、**通学者が丸ごと
    落ちます**。

    実測：早稲田駅前（半径1km）のレポートは、従業者数 52,688 人を昼間人口の
    代理として使い、大学生に一言も触れませんでした。早稲田大学の学生は
    従業者ではないので、経済センサスには 1 人も現れません。

    重み付けは他のメッシュ集計と**同じ面積按分**にします。別の数え方に
    すると、常住人口と並べたときに差が調査の差なのか数え方の差なのか
    分からなくなります。
    """
    if not table_exists(conn, "mesh_daytime_population"):
        return {"available": False, "reason": "not_migrated",
                "note": "昼間人口のテーブルがまだありません"
                        "（kaigyou-etl migrate を実行してください）。"}
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH buf AS (
                SELECT ST_Buffer(ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography,
                                 %(radius)s)::geometry AS g
            ),
            share AS (
                SELECT m.mesh_code,
                       LEAST(1.0, GREATEST(0.0,
                           ST_Area(ST_Intersection(m.geom, buf.g)::geography)
                           / NULLIF(ST_Area(m.geom::geography), 0))) AS w
                  FROM population_mesh m, buf
                 WHERE m.mesh_size_m = %(mesh)s
                   AND m.geom && buf.g AND ST_Intersects(m.geom, buf.g)
            )
            SELECT SUM(d.daytime_population * s.w) AS daytime,
                   SUM(d.workers_here       * s.w) AS workers,
                   SUM(d.students_here      * s.w) AS students,
                   SUM(d.night_population   * s.w) AS night,
                   count(*)                        AS mesh_count,
                   count(d.students_here)          AS meshes_with_students,
                   MAX(d.source_date)              AS source_date
              FROM mesh_daytime_population d
              JOIN share s ON s.mesh_code = d.mesh_code
             WHERE d.mesh_size_m = %(mesh)s
            """,
            {"lat": lat, "lng": lng, "radius": radius_m, "mesh": mesh_size_m})
        row = cur.fetchone()

    if not row or not row["mesh_count"]:
        return {"available": False, "reason": "not_loaded",
                "note": "この地域の昼間人口（国勢調査 従業地・通学地メッシュ）は"
                        "取り込まれていません。**昼間の人については、働いて"
                        "いる人しか数えられていません。** 通学者（大学生など）は"
                        "含まれていません。"}

    daytime = _round(row["daytime"])
    students = _round(row["students"])
    workers = _round(row["workers"])
    # 就業者でも通学者でもない昼間人口（在宅の常住者・乳幼児・高齢者など）。
    # 引き算なので、そう名乗ります。
    other = (None if daytime is None or workers is None or students is None
             else max(0, daytime - workers - students))
    return {
        "available": True,
        "population": daytime,
        "workers_here": workers,
        "students_here": students,
        "other_here": other,
        "night_population": _round(row["night"]),
        "mesh_count": int(row["mesh_count"]),
        # 通学者が取れなかったメッシュの数。0 と「分からない」を混ぜないため。
        "meshes_without_students": int(row["mesh_count"]) - int(row["meshes_with_students"]),
        "source_date": row["source_date"],
        "basis": "workplace_or_school",
        "basis_label": MEASUREMENT_BASES["workplace_or_school"],
        "definition": ("従業地・通学地による人口（昼間人口）を、現在人口と同じ"
                       "面積按分で商圏に切り出したもの。**経済センサスの"
                       "「従業者数」とは調査も定義も違うので、足さないで"
                       "ください。** 通学者はこちらにしか現れません。"),
    }


def _industry_mix(conn: psycopg.Connection, lat: float, lng: float, radius_m: int,
                  mesh_size_m: int,
                  total: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
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
    measured = {
        r["division"]: {"workers": round(r["workers"] or 0),
                        "establishments": round(r["establishments"] or 0)}
        for r in rows
    }
    return _industry_tree(measured, total)


#: 産業分類の親子関係。**これが無いと、表が MECE に見えません。**
#:
#: 実測：「第3次産業 49,203」「教育・学習支援 13,245」「第2次産業 3,474」
#: 「医療・福祉 5,978」…と並べていました。1行目に3行目以降が含まれている
#: のに、同じ字下げで並んでいます。読み手は足し算ができず、合計を取ると
#: 二重計上になります。
#:
#: 取り込む分類は config/sources.yaml の industry_columns で決まります。
#: ここは**その親子関係**だけを持ちます（どれを取り込むかは設定、
#: 取り込んだものがどう入れ子になっているかは統計の定義）。
INDUSTRY_TREE: tuple[tuple[str, str, str | None], ...] = (
    ("primary", "第1次産業", None),
    ("secondary", "第2次産業", None),
    ("tertiary", "第3次産業", None),
    ("wholesale_retail", "卸売・小売", "tertiary"),
    ("accommodation_food", "宿泊・飲食", "tertiary"),
    ("education", "教育・学習支援", "tertiary"),
    ("health_welfare", "医療・福祉", "tertiary"),
)


def _industry_tree(measured: Mapping[str, Mapping[str, Any]],
                   total: Mapping[str, Any] | None) -> dict[str, Any]:
    """測った分類を、足し合わせられる形に組み直す。

    **残差を明示します。** 「第3次産業 49,203」のうち名前の付いた4つを
    引くと 21,382 残ります。その行を出さないと、読み手は「4つで全部」と
    読むか、足りない分を探すかのどちらかになります。どちらも間違いです。

    残差は測った値ではなく引き算の結果なので、``derived`` で区別します。
    按分の丸めで負になりうるので 0 で止め、止めたことも残します。
    """
    def value(key: str, field: str) -> float:
        return float((measured.get(key) or {}).get(field) or 0)

    rows: list[dict[str, Any]] = []
    children: dict[str, list[str]] = {}
    for key, _label, parent in INDUSTRY_TREE:
        if parent:
            children.setdefault(parent, []).append(key)

    labels = {key: label for key, label, _p in INDUSTRY_TREE}
    for key, label, parent in INDUSTRY_TREE:
        if parent is not None or key not in measured:
            continue
        rows.append({"key": key, "label": label, "parent": None,
                     "derived": False, **measured[key]})
        named = [c for c in children.get(key, []) if c in measured]
        for child in named:
            rows.append({"key": child, "label": labels[child], "parent": key,
                         "derived": False, **measured[child]})
        if not named:
            continue
        # 残差は内訳の**いちばん最後**に置きます。名前の付いたものの前に
        # 出すと、「その他」が何の残りなのか読み取れません。
        rest = {field: value(key, field) - sum(value(c, field) for c in named)
                for field in ("workers", "establishments")}
        clamped = any(v < 0 for v in rest.values())
        rows.append({
            "key": f"{key}_other", "label": f"その他の{label}", "parent": key,
            "derived": True,
            "workers": max(0, round(rest["workers"])),
            "establishments": max(0, round(rest["establishments"])),
            "note": ("按分の丸めで内訳の合計が親を超えたため 0 にしました"
                     if clamped else None),
        })

    top = [k for k, _l, parent in INDUSTRY_TREE if parent is None and k in measured]
    unclassified = None
    if total and total.get("workers") is not None:
        rest = {field: float(total.get(field) or 0)
                - sum(value(k, field) for k in top)
                for field in ("workers", "establishments")}
        # 全産業から第1次〜第3次を引いた残り。分類不能の事業所と、取り込んで
        # いない分類（config で選んでいないもの）がここに落ちます。0 でも
        # 行を出すのは、「合計が合う」ことを読み手が確かめられるようにする
        # ためです。
        unclassified = {
            "key": "unclassified", "label": "分類不能・未取得（差分）",
            "parent": None, "derived": True,
            "workers": max(0, round(rest["workers"])),
            "establishments": max(0, round(rest["establishments"])),
        }

    return {
        "total": dict(total or {}),
        "divisions": rows + ([unclassified] if unclassified else []),
        "basis": "workplace",
        "basis_label": MEASUREMENT_BASES["workplace"],
        "note": ("親子の関係があります。第1次・第2次・第3次が全産業の内訳で、"
                 "卸売・小売などはさらに第3次産業の内訳です。**足し合わせる"
                 "ときは同じ段のものだけを足してください。** 「その他」と"
                 "「差分」は測った値ではなく、親から名前の付いた内訳を"
                 "引いた残りです。取り込む分類は config/sources.yaml の "
                 "industry_columns で決まります。"),
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

        # 標榜科目と診療時間を一緒に引きます。「近くに 50 件ある」より
        # 「近い順に 50 件、それぞれ何を標榜していて土日は開いているか」のほうが、
        # 読み手が自分で数えなおせるぶん情報量があります。
        cur.execute(
            """
            SELECT f.name, f.address, f.clinic_types, f.founder_type, f.opening_date,
                   f.attributes,
                   ff.specialty_keys, ff.declared_specialties, ff.weekly_open_hours,
                   ff.open_days, ff.latest_close, ff.opens_saturday, ff.opens_sunday,
                   ff.opens_holiday, ff.opens_evening,
                   ST_Distance(f.geom::geography,
                               ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) AS distance_m,
                   ST_Y(f.geom) AS lat, ST_X(f.geom) AS lng
            FROM facilities f
            LEFT JOIN facility_features ff ON ff.facility_id = f.facility_id
            WHERE f.facility_category = %s
              AND ST_DWithin(f.geom::geography,
                             ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
            ORDER BY f.geom::geography <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
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
            "specialties": ([
                {"key": key, "label": vocab.label(key),
                 "declared_only": vocab.is_free_text(key)}
                for key in sorted(row["specialty_keys"] or [], key=vocab.sort_key)
            ] or None),
            "declared_specialty_names": row["declared_specialties"] or None,
            "hours": (None if row["weekly_open_hours"] is None else {
                "weekly_hours": row["weekly_open_hours"],
                "open_days": row["open_days"],
                "latest_close": (row["latest_close"].isoformat()
                                 if row["latest_close"] else None),
                "saturday": row["opens_saturday"],
                "sunday": row["opens_sunday"],
                "holiday": row["opens_holiday"],
                "evening": row["opens_evening"],
            }),
        }
        items.append(item)
    return {"count": total, "listed": len(items), "truncated": total > len(items),
            "items": items}


#: 16 方位。8 方位だと「南南東」が「南」に丸まり、南口か東口かの手掛かりが
#: 1 段落ちます。北から時計回り。
_COMPASS = ("北", "北北東", "北東", "東北東", "東", "東南東", "南東", "南南東",
            "南", "南南西", "南西", "西南西", "西", "西北西", "北西", "北北西")


def _bearing(from_lat: float, from_lng: float,
             to_lat: float, to_lng: float) -> float:
    """真北からの方位角（度）。原点から見て相手がどちらにあるか。"""
    phi1, phi2 = math.radians(from_lat), math.radians(to_lat)
    dl = math.radians(to_lng - from_lng)
    y = math.sin(dl) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2)
         - math.sin(phi1) * math.cos(phi2) * math.cos(dl))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _direction_from_station(conn: psycopg.Connection, lat: float, lng: float,
                            name: str | None) -> dict[str, Any] | None:
    """最寄り駅から見て、候補地はどちらの方角か。

    **これは「南口か北口か」の手掛かりです。** 駅の座標は S12 で手元にあり、
    候補地の座標は利用者が置いたピンそのものなので、方位は引き算で出ます。
    実測（沼津駅・35.101942,138.861033）で「南南東 132m」。レポートは
    「南口側か北口側かは基礎データからは特定できていない」と書いていましたが、
    **特定できていなかったのは計算していなかったからです。**

    ただし**出口の名前まで断定はしません。** 駅の出口の呼び名は駅ごとに決まって
    いて、方角と一致しない駅があります（「南口」が西側にある駅は実在します）。
    ここが言えるのは「駅のどちら側か」までで、出口名は外部で確かめる話です。
    """
    if not name:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ST_Y(geom) AS lat, ST_X(geom) AS lng,
                   ST_Distance(geom::geography,
                               ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) AS distance_m
            FROM stations
            WHERE name = %s
            ORDER BY geom::geography <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
            LIMIT 1
            """, (lng, lat, name, lng, lat))
        row = cur.fetchone()
    if not row:
        return None
    degrees = _bearing(row["lat"], row["lng"], lat, lng)
    compass = _COMPASS[int((degrees + 11.25) % 360 // 22.5)]
    return {
        "bearing_deg": round(degrees, 1),
        "compass": compass,
        "statement": f"候補地は{name}駅の{compass}、直線 {round(row['distance_m']):,}m",
        "note": ("駅の座標と候補地の座標から計算した方位です。**駅のどちら側か**"
                 "までが言えることで、**出口の名前は別の話です。** 出口の呼び名は"
                 "駅ごとに決まっていて、方角と一致しない駅があります。"
                 "「南口」と書くなら、その駅の出口名を外部で確かめてください。"),
    }


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



#: Which competitor to measure the distance to. The total inside a radius says
#: how many; this says how close, which is a different fact and often the
#: sharper one -- twenty clinics inside 158m is a description no total gives.
_COMPETITOR_RANKS = (1, 3, 5, 10, 20)


def _competitor_distances(conn: psycopg.Connection, lat: float, lng: float,
                          category: str, radius_m: int) -> dict[str, Any]:
    """How far to the 1st, 3rd, 5th, 10th and 20th nearest competitor.

    A count inside a radius and a distance ladder answer different questions.
    "186 clinics within 1km" is a density; "the 20th is 158m away" is what it
    feels like on the street, and it is the version that separates a crowded
    high street from a wide catchment with the same total.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT n, distance_m FROM (
                SELECT row_number() OVER (
                           ORDER BY geom::geography <->
                                    ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) AS n,
                       ST_Distance(geom::geography,
                                   ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography)
                           AS distance_m
                FROM facilities
                WHERE facility_category = %s
                ORDER BY geom::geography <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
                LIMIT %s
            ) ranked
            WHERE n = ANY(%s)
            """,
            (lng, lat, lng, lat, category, lng, lat, max(_COMPETITOR_RANKS),
             list(_COMPETITOR_RANKS)))
        rows = cur.fetchall()

    ladder = {str(r["n"]): round(r["distance_m"]) for r in rows}
    area_km2 = 3.14159 * (radius_m / 1000) ** 2
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*)::int AS n FROM facilities
            WHERE facility_category = %s
              AND ST_DWithin(geom::geography,
                             ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
            """, (category, lng, lat, radius_m))
        inside = cur.fetchone()["n"]
    return {
        "nth_nearest_distance_m": ladder,
        "per_km2": round(inside / area_km2, 1) if area_km2 else None,
        "definition": ("n番目に近い同種施設までの直線距離（m）。件数が同じでも、"
                       "この階段が短ければ密集、長ければ分散を意味する。"),
    }



def _outlook_with_actual(outlook: dict[str, Any],
                         residents: Mapping[str, Any]) -> dict[str, Any]:
    """推計の基準年に、国勢調査の実績を並べる。

    **なぜ要るか。** 将来推計の公表値は、基準年（2020）については総人口しか
    ありません。年齢内訳は 2025 年以降だけです。表にすると基準年の列が
    「—」だらけになり、読み手には「分からない」に見えます。ところが 2020 年の
    年齢内訳は国勢調査にあり、この商圏について既に集計済みです。

    **合わせて 1 つの数にはしません。** 推計値と実績値は別の集合を別の方法で
    数えたもので、実測でも総人口が 66,965（推計の基準年）と 66,817（国勢調査）
    のように少し違います。足したり置き換えたりすると、どちらの数字を見て
    いるのか分からなくなります。列を分けて、違う理由を書きます。
    """
    if not outlook.get("available") or not residents:
        return outlook
    population = residents.get("population")
    elderly = residents.get("age_65_plus")
    return {**outlook, "actual": {
        "year": outlook.get("base_year"),
        "source": "総務省統計局 国勢調査（e-Stat 統計GIS）",
        "population": population,
        "age_0_14": residents.get("age_0_14"),
        "age_15_64": residents.get("age_15_64"),
        "age_65_plus": elderly,
        "households": residents.get("households"),
        "elderly_share": (None if not population or elderly is None
                          else round(float(elderly) / float(population), 3)),
        # 75歳以上は国勢調査メッシュの取り込み対象に入っていません
        #（population_mesh に列がありません）。**0 ではなく、無いのです。**
        "age_75_plus": None,
        "late_elderly_share": None,
        "note": ("基準年の年齢内訳は将来推計の公表値には含まれないため、"
                 "同じ商圏の国勢調査の集計を並べています。**推計値とは"
                 "別の数え方**なので、総人口が推計の基準年と少し違います。"
                 "75歳以上は国勢調査メッシュの取り込み対象に入っていないため"
                 "空欄です（0 ではありません）。"),
    }}


def population_outlook(conn: psycopg.Connection, lat: float, lng: float,
                       radius_m: int, mesh_size_m: int) -> dict[str, Any]:
    """商圏の将来推計人口。取れなければ、取れないと言う。

    総合スコアの「成長」は 2015→2020 の実績で決まっています。過去 5 年です。
    歯科の開業は 20〜30 年の意思決定なので、いちばん重い問いにいちばん弱い
    指標で答えていることになります。ここはその埋め合わせです。

    重み付けは既存のメッシュ集計と**同じ面積按分**にします。別の数え方に
    すると、現在人口と将来人口を並べたときに、差が推計の差なのか数え方の
    差なのか分からなくなります。ジオメトリは population_mesh のものを
    mesh_code で借ります（同じ格子なので複製する理由がありません）。

    データが無い環境では ``{"available": False, ...}`` を返します。無いものを
    埋めません（要件 §3）。
    """
    from kaigyou_core.db import table_exists

    if not table_exists(conn, "mesh_population_projection"):
        return {"available": False,
                "reason": "not_migrated",
                "note": "将来推計人口のテーブルがありません（kaigyou-etl migrate）。"}

    with conn.cursor() as cur:
        cur.execute(
            """
            WITH buf AS (
                SELECT ST_Buffer(ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)::geography,
                                 %(radius)s)::geometry AS g
            ),
            share AS (
                SELECT m.mesh_code,
                       LEAST(1.0, GREATEST(0.0,
                           ST_Area(ST_Intersection(m.geom, buf.g)::geography)
                           / NULLIF(ST_Area(m.geom::geography), 0))) AS w
                  FROM population_mesh m, buf
                 WHERE m.mesh_size_m = %(mesh)s
                   AND m.geom && buf.g AND ST_Intersects(m.geom, buf.g)
            )
            SELECT p.projection_year        AS year,
                   MIN(p.base_year)         AS base_year,
                   MIN(p.estimate_label)    AS estimate_label,
                   MAX(p.source_date)       AS source_date,
                   SUM(p.population  * s.w) AS population,
                   SUM(p.age_0_14    * s.w) AS age_0_14,
                   SUM(p.age_15_64   * s.w) AS age_15_64,
                   SUM(p.age_65_plus * s.w) AS age_65_plus,
                   SUM(p.age_75_plus * s.w) AS age_75_plus,
                   count(*)                 AS mesh_count
              FROM mesh_population_projection p
              JOIN share s ON s.mesh_code = p.mesh_code
             WHERE p.mesh_size_m = %(mesh)s
             GROUP BY p.projection_year
             ORDER BY p.projection_year
            """,
            {"lat": lat, "lng": lng, "radius": radius_m, "mesh": mesh_size_m})
        rows = cur.fetchall()

    if not rows:
        return {"available": False,
                "reason": "not_loaded",
                "note": "この地域の将来推計人口は取り込まれていません。"
                        "成長の評価は 2015→2020 の実績のみに基づきます。"}

    years = [{
        "year": int(r["year"]),
        "population": _round(r["population"]),
        "age_0_14": _round(r["age_0_14"]),
        "age_15_64": _round(r["age_15_64"]),
        "age_65_plus": _round(r["age_65_plus"]),
        # 75歳以上を分けて持つのは、通院と訪問で開業方針が変わるからです。
        "age_75_plus": _round(r["age_75_plus"]),
        # `or 0` を書いてはいけないところ。年齢内訳が無い年（公表データの
        # 2020 は総人口のみ）に 0 を入れると、画面に「65歳以上 0.0%」と出ます。
        # **「分からない」を「いない」と言い換えたことになります。** 実際に
        # そう表示され、指摘を受けました。無いものは None のまま渡します。
        "elderly_share": (None if not r["population"] or r["age_65_plus"] is None
                          else round(float(r["age_65_plus"])
                                     / float(r["population"]), 3)),
        "late_elderly_share": (None if not r["population"] or r["age_75_plus"] is None
                               else round(float(r["age_75_plus"])
                                          / float(r["population"]), 3)),
        "mesh_count": int(r["mesh_count"]),
    } for r in rows]

    # 基準年に対する比。読み手が欲しいのは「いま比べて何割か」です。
    base_year = rows[0]["base_year"]
    base = next((y for y in years if y["year"] == base_year), years[0])
    for y in years:
        y["index_vs_base"] = (None if not base["population"]
                              else round(y["population"] / base["population"], 3))

    return {
        "available": True,
        "base_year": (int(base_year) if base_year is not None else base["year"]),
        "estimate_label": rows[0]["estimate_label"],
        "source_date": rows[0]["source_date"],
        "years": years,
        "definition": ("500m メッシュ別の将来推計人口を、現在人口と同じ面積按分で"
                       "商圏に切り出したもの。推計であって予測ではなく、"
                       "出生・死亡・移動の仮定の上に成り立つ。"),
    }


def _catchment_shape(conn: psycopg.Connection, lat: float, lng: float,
                     radius_m: int, mesh_size_m: int) -> dict[str, Any] | None:
    """How the catchment's population is spread across the meshes inside it.

    A total hides its own shape. 20,000 residents can be one tower block beside
    an empty park, or forty streets of houses, and the two are different
    businesses. The largest mesh's share is the quickest tell.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH buf AS (
                SELECT ST_Buffer(ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                                 %s)::geometry AS g
            ),
            inside AS (
                SELECT COALESCE(m.population, 0) * LEAST(1.0, GREATEST(0.0,
                           ST_Area(ST_Intersection(m.geom, buf.g)::geography)
                           / NULLIF(ST_Area(m.geom::geography), 0))) AS population
                FROM population_mesh m, buf
                WHERE m.mesh_size_m = %s AND m.geom && buf.g
                  AND ST_Intersects(m.geom, buf.g)
            )
            SELECT count(*)::int AS meshes,
                   count(*) FILTER (WHERE population < 1)::int AS empty_meshes,
                   sum(population) AS total,
                   max(population) AS largest,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY population) AS median
            FROM inside
            """, (lng, lat, radius_m, mesh_size_m))
        row = cur.fetchone()

    if not row or not row["meshes"]:
        return None
    total = float(row["total"] or 0)
    return {
        "meshes": row["meshes"],
        "meshes_with_no_residents": row["empty_meshes"],
        "population_median_per_mesh": _round(row["median"]),
        "population_largest_mesh": _round(row["largest"]),
        "largest_mesh_share": (round(float(row["largest"]) / total, 3)
                               if total > 0 and row["largest"] is not None else None),
        "definition": ("商圏内のメッシュごとの人口の散らばり。largest_mesh_share が"
                       "0.4 なら、商圏人口の4割が1メッシュ（500m四方）に集中している。"
                       "合計値だけでは、集合住宅1棟なのか一様な住宅地なのか区別できない。"),
    }


def _city_planning(conn: psycopg.Connection, lat: float, lng: float,
                   radius_m: int, category: str = DEFAULT_CATEGORY,
                   ) -> dict[str, Any] | None:
    """候補地に掛かっている都市計画。**何が建てられるかを決めているのはここです。**

    人口も競合も「そこに何人いるか」の話です。この節だけが「そこに何を建てて
    よいか」を答えます。市街化調整区域なら他の数字を読む意味がほとんど無く、
    工業専用地域なら診療所は建てられません——**それを知らずに商圏人口を
    論じても仕方がありません。**

    地価公示から採る :func:`_zoning` との違いは、点か面かです。地価公示は
    静岡県で 3,221 点しかなく、近くに標準地が無ければ不明になり、あっても
    用途地域の境目は道 1 本で変わるので最寄り点の用途地域が候補地の用途地域
    とは限りません。A55 は面なので ST_Contains で決まります。

    可否の規則は ``config/<業態>/city_planning.yaml`` にあります。診療所と
    病院で建てられる用途地域が違うためで、コードには書きません。規則が
    無い環境では**可否を判定せず区域名だけ**を返します。
    """
    from kaigyou_core import config as cfg

    if not table_exists(conn, "city_planning_zones"):
        return None

    rules = cfg.city_planning_config(category)
    point = (lng, lat)
    with conn.cursor() as cur:
        # --- 候補地点そのものに掛かる区域 ---------------------------------
        # 公表データには同じ層で重なる面があるので（沼津市の調整区域など）、
        # 1 件だけ返る前提では書きません。
        cur.execute(
            """
            SELECT zone_kind, zone_kind_label, zone_type, zone_name,
                   far, bcr, municipality_name, decided_on
            FROM city_planning_zones
            WHERE geom && ST_SetSRID(ST_MakePoint(%s, %s), 4326)
              AND ST_Contains(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
            ORDER BY zone_kind_label, zone_type
            """, (*point, *point))
        here = cur.fetchall()

        # --- 商圏の用途地域の構成（面積按分） -----------------------------
        # 「商圏の何割が商業地域か」。合計人口と同じで、1 点の用途地域だけでは
        # 商店街なのか住宅地なのかが分かりません。
        cur.execute(
            """
            WITH buf AS (
                SELECT ST_Buffer(ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                                 %s)::geometry AS g
            )
            SELECT z.zone_type,
                   round(avg(z.far))::int AS floor_area_ratio_pct,
                   sum(ST_Area(ST_Intersection(z.geom, buf.g)::geography)) AS area_m2,
                   max(ST_Area(buf.g::geography)) AS buffer_m2
            FROM city_planning_zones z, buf
            WHERE z.zone_kind = 'youto'
              AND z.geom && buf.g AND ST_Intersects(z.geom, buf.g)
            GROUP BY z.zone_type
            ORDER BY 3 DESC
            """, (*point, radius_m))
        mix = cur.fetchall()

    if not here and not mix:
        return None

    at_site = [
        {
            "layer": row["zone_kind_label"],
            "zone": row["zone_type"],
            "name": row["zone_name"],
            "floor_area_ratio_pct": _round(row["far"]),
            "building_coverage_pct": _round(row["bcr"]),
            "decided_on": row["decided_on"],
        }
        for row in here
    ]
    zones_here = {row["zone_type"] for row in here if row["zone_type"]}

    buffer_m2 = float(mix[0]["buffer_m2"]) if mix else 0.0
    composition = [
        {
            "zone": row["zone_type"],
            "floor_area_ratio_pct": row["floor_area_ratio_pct"],
            "area_km2": _round(float(row["area_m2"]) / 1e6, 3),
            "share": (round(float(row["area_m2"]) / buffer_m2, 3)
                      if buffer_m2 > 0 else None),
        }
        for row in mix
    ]

    out: dict[str, Any] = {
        "at_site": at_site,
        "municipality": next((r["municipality_name"] for r in here
                              if r["municipality_name"]), None),
        "zoning_mix_in_radius": composition,
        "zoning_mix_note": (
            f"半径 {radius_m}m の円で切った用途地域の面積構成。"
            "徒歩圏で分析している場合でもこの構成は円で算出しています。"),
        "definition": (
            "国土数値情報 A55（都市計画決定情報）の面データ。候補地の座標が"
            "どの区域の中にあるかで判定している。用途地域は「何を建ててよいか」、"
            "区域区分（市街化区域／市街化調整区域）は「そもそも建てられるか」、"
            "立地適正化計画の誘導区域は「市町がそこに何を集めようとしているか」。"),
        "source": "国土数値情報 A55 都市計画決定情報",
    }

    if not rules:
        out["buildability"] = None
        out["note"] = ("この業態の用途地域規則（config/<業態>/city_planning.yaml）が"
                       "無いため、区域名のみを示し可否は判定していません。")
        return out

    out["buildability"] = _buildability(zones_here, rules)
    out["guidance_zones"] = [
        {"zone": zone, "meaning": meaning}
        for zone, meaning in (rules.get("guidance_notes") or {}).items()
        if zone in zones_here
    ]
    out["disclaimer"] = rules.get("disclaimer")
    return out


def _buildability(zones: set[str], rules: Mapping[str, Any]) -> dict[str, Any]:
    """区域名の集合を「建てられるか」に写す。

    **禁止が 1 つでもあれば禁止です。** 用途地域が可でも市街化調整区域なら
    建てられません。可否は and で畳み、理由は全部残します——「なぜ駄目か」が
    1 行しか無いと、読んだ人はもう一方の制約に気づかないままになります。
    """
    zone_rules = rules.get("zone_rules") or {}
    area_rules = rules.get("area_division_rules") or {}
    default = rules.get("default") or {}

    blocking: list[dict[str, str]] = []
    cautions: list[dict[str, str]] = []
    matched = False
    for zone in sorted(zones):
        rule = zone_rules.get(zone) or area_rules.get(zone)
        if rule is None:
            continue
        matched = True
        if rule.get("buildable") is False:
            blocking.append({"zone": zone, "reason": str(rule.get("note") or "")})
        elif rule.get("caution"):
            cautions.append({"zone": zone, "caution": str(rule["caution"])})

    if blocking:
        verdict = "not_permitted"
    elif matched or zones:
        verdict = "permitted_with_conditions" if cautions else "permitted"
    else:
        verdict = "unknown"

    return {
        "facility": rules.get("facility_label"),
        "verdict": verdict,
        "verdict_label": {
            "not_permitted": "原則として建てられない",
            "permitted_with_conditions": "建てられるが条件がある",
            "permitted": "用途地域・区域区分の上では制約なし",
            "unknown": "判定できない（区域が特定できていない）",
        }[verdict],
        "blocking": blocking,
        "cautions": cautions,
        "note": ("用途地域と区域区分の公表値だけによる一次判定。規模・接道・条例で"
                 "変わり、決めるのは特定行政庁。"
                 if default is not None else None),
    }


def _zoning(conn: psycopg.Connection, lat: float, lng: float,
            radius_m: int) -> dict[str, Any] | None:
    """用途地域・容積率・建蔽率。何をどれだけ建てられる場所か。

    Carried on every 地価公示 point and never surfaced. It is the constraint
    the other figures sit inside: the same "residential area" is 96% floor area
    ratio in 第一種低層住居専用 and 583% in 商業, which is the difference
    between a street of houses and a street of tenanted buildings -- and a
    practice is a tenant.

    Only where a surveyed parcel happens to be; this is not the zoning map
    (国土数値情報 A29 would be), and the response says so.
    """
    if not table_exists(conn, "land_prices"):
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT zoning, count(*)::int AS points,
                   -- ::int, or the numeric that round() returns arrives as a
                   -- Decimal and is serialised as a quoted string. A reader
                   -- comparing "769" with 800 gets a lexicographic answer.
                   round(avg(floor_area_ratio_pct))::int AS floor_area_ratio_pct,
                   round(avg(building_coverage_pct))::int AS building_coverage_pct
            FROM land_prices
            WHERE zoning IS NOT NULL
              AND ST_DWithin(geom::geography,
                             ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
            GROUP BY zoning ORDER BY 2 DESC
            """, (lng, lat, radius_m))
        rows = cur.fetchall()

        cur.execute(
            """
            SELECT zoning, floor_area_ratio_pct, building_coverage_pct, current_use,
                   ST_Distance(geom::geography,
                               ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) AS distance_m
            FROM land_prices
            WHERE zoning IS NOT NULL
            ORDER BY geom::geography <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
            LIMIT 1
            """, (lng, lat, lng, lat))
        nearest = cur.fetchone()

    if not rows and not nearest:
        return None
    return {
        "at_nearest_surveyed_point": ({
            "zoning": nearest["zoning"],
            "floor_area_ratio_pct": nearest["floor_area_ratio_pct"],
            "building_coverage_pct": nearest["building_coverage_pct"],
            "current_use": nearest["current_use"],
            "distance_m": round(nearest["distance_m"]),
        } if nearest else None),
        "in_radius": [dict(r) for r in rows],
        "definition": ("都市計画の用途地域と、その地点の容積率・建蔽率（%）。"
                       "地価公示の標準地が置かれた地点の値であり、用途地域図そのものではない。"
                       "容積率は「その敷地に建てられる延床面積の割合」で、"
                       "テナントとしての診療所が入りうる床の量に効く。"),
    }


def _round(value: Any, digits: int = 0) -> Any:
    if value is None:
        return None
    return round(float(value), digits) if digits else round(float(value))


def _by_specialty(metrics: dict[str, Any],
                  by_radius: dict[str, Any]) -> dict[str, Any]:
    """標榜科目別の競合。件数・占有率・1 件あたり人口を、半径ごとにも。

    分母（``with_data``）を必ず並べます。科目別の件数は「診療科目が分かって
    いる医院のうち何件か」であって、「商圏内に何件あるか」ではありません。
    今の抽出は東京都を全部含んでいますが、他県では欠けることがあります。

    自由記載の科目（インプラント・審美・訪問診療）は ``declared_only`` の印を
    付けます。書かなかった医院を「やっていない」と数えてしまうため、これらの
    件数は競合の少なさの根拠には使えません。
    """
    total = metrics.get("facility_count") or 0
    with_data = metrics.get("facilities_with_specialty_data") or 0
    counts = metrics.get("facility_specialty_counts") or {}
    population = metrics.get("population")

    detail = []
    for row in vocab.describe(counts):
        count = row["count"]
        detail.append({
            **row,
            "share_of_clinics_with_data": (
                None if not with_data else round(count / with_data, 3)),
            "population_per_clinic": (
                None if not count or population is None
                else round(float(population) / count)),
        })

    return {
        "total_clinics": total,
        "with_data": with_data,
        "coverage": None if not total else round(with_data / total, 3),
        "detail": detail,
        "by_radius": {r: vals.get("specialty_counts") or {}
                      for r, vals in by_radius.items()},
        "note": ("標榜診療科目は告示に基づく届出（歯科・小児歯科・矯正歯科・"
                 "歯科口腔外科・小児矯正歯科）。インプラント・審美・訪問診療などは"
                 "「その他」欄への自由記載のため、実施していても記載が無い医院が"
                 "多数あります。declared_only=true の件数は「そう記載した医院の数」"
                 "であり、実施医院数の下限にすぎません。"),
    }


def _hours_block(metrics: dict[str, Any]) -> dict[str, Any]:
    """商圏内の医院の診療時間。件数では見えない競合の形。

    土曜に開けている医院が 6 割の商圏と 9 割の商圏では、同じ件数でも空いている
    時間帯が違います。中央値の週間診療時間は、その商圏の「ふつう」の営業量。
    """
    counts = metrics.get("facility_hours_counts") or {}
    declared = counts.get("declared") or 0
    return {
        "declared": declared,
        "counts": [
            {"key": key, "label": label, "count": counts.get(key),
             "share": (None if not declared or counts.get(key) is None
                       else round(counts[key] / declared, 3))}
            for key, label in vocab.hours_labels().items()
        ],
        "weekly_hours_median": _round(metrics.get("facility_weekly_hours_median"), 1),
        "note": ("診療時間は医療機能情報提供制度の届出値。曜日ごとの開始・終了時刻から"
                 "算出しています。夜間診療は終了時刻が 18:30 以降の枠がある医院。"
                 "週間診療時間は重複する時間帯を結合してから合計しています。"),
    }


def _supplementary_definitions() -> dict[str, dict[str, str]]:
    """measures が自分で説明しない欄だけの定義。

    schema 2.0 から、各指標は自分の ``definition`` / ``unit`` / ``source`` /
    ``data_year`` を持ちます。同じ説明をここにも置くと、片方だけ直したときに
    2 つの説明が食い違い、どちらが正しいか読み手には分かりません。
    """
    from kaigyou_core.measures import MEASURE_SPECS

    covered = {spec["metric"] for spec in MEASURE_SPECS.values()} | set(MEASURE_SPECS)
    # 名前がずれているもの（データセット側の呼び名）も除きます。
    covered |= {"age_0_14", "age_15_64", "age_65_plus", "population_growth",
                "dental_clinics", "population_per_clinic"}
    return {key: value for key, value in DEFINITIONS.items() if key not in covered}


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

    scoring_config = scoring_config or cfg.scoring_config(category)
    model = ScoringModel(scoring_config, profile)

    # The point decides its own prefecture, as everywhere else: scoring a
    # Chiyoda click against another prefecture's normalisation is meaningless.
    prefecture_code = prefecture_code or prefecture_at(conn, lat, lng)
    prefecture_code = default_prefecture(conn, prefecture_code)
    mesh_size_m = resolve_mesh_size(conn, mesh_size_m, prefecture_code)

    # そのプロファイルが競合として数える標榜科目。指定するとメッシュ分布との
    # 比較（relative_position）もその科目の件数で行われます。
    specialty = (model.profile.get("competition") or {}).get("specialty")
    pairs = competition_specialties(scoring_config)

    radii = sorted({*model.radii, radius_m})
    #: 半径ごとの生の測定結果。**同じ半径を測り直さないために持ちます。**
    #: 円のときは 1 回 0.01 秒なので重複は見えませんでしたが、徒歩圏では
    #: 1 回 2 秒近くかかり、しかもここはジョブ作成の HTTP 応答の中です。
    measured: dict[int, dict[str, Any]] = {}
    by_radius: dict[str, Any] = {}
    for r in radii:
        m = measured[r] = analyze_point(
            conn, lat, lng, r, category, mesh_size_m or 1000, catchment, specialty)
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
            "specialty_counts": m.get("facility_specialty_counts") or {},
            "clinics_with_specialty_data": m.get("facilities_with_specialty_data"),
        }

    # radius_m は必ず radii に入っているので、上のループがもう測っています。
    metrics = measured[radius_m]
    augment_specialty_metrics(metrics, pairs)

    # The mesh distribution exists only at the radius the scoring ran at, so
    # the comparison is made there and says so. Comparing a 2km catchment
    # against meshes measured at 1km would be a different quantity wearing the
    # same name.
    comparison_radius = model.mesh_scoring_radius_m
    comparison_metrics = measured.get(comparison_radius) or analyze_point(
        conn, lat, lng, comparison_radius, category,
        mesh_size_m or 1000, catchment, specialty)
    scope, distributions = resolve_distributions(
        conn, mesh_size_m or 1000, radius_m, prefecture_code, scoring_config,
        category)

    scores = []
    for name in (scoring_config.get("profiles") or {}):
        alt = ScoringModel(scoring_config, name)
        result = alt.score(metrics, distributions)
        alt_specialty = (alt.profile.get("competition") or {}).get("specialty")
        scores.append({
            "profile": name,
            "label": alt.label,
            "competition_specialty": alt_specialty,
            "competition_specialty_label": (vocab.label(alt_specialty)
                                            if alt_specialty else None),
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
    neighbours = _neighbour_municipalities(
        conn, (municipality or {}).get("municipality_code"))
    land = land_prices_near(conn, lat, lng, radius_m, limit=MAX_LAND_POINTS)

    # 比較はメッシュ分布に対して行うので、分布が作られた半径で測ります。2km の
    # 商圏を 1km で測ったメッシュと比べると、同じ名前の別の量を比べることに
    # なります。どの半径で比べたかは measurement_basis に出します。
    insights_config = cfg.insights_config(category)
    measures, benchmark_notes, primary_benchmark = build_measures(
        conn, comparison_metrics,
        profile=model.profile_name, radius_m=comparison_radius,
        prefecture_code=prefecture_code,
        prefecture_label=prefecture_name(conn, prefecture_code),
        municipality=(municipality or {}).get("name"),
        neighbours=neighbours,
        lat=lat, lng=lng,
        # **比較相手を業態で絞ります。** 絞らないと、内科を入れたあとに
        # 歯科の順位を出すと全業態が混ざった母集団に対する順位になります。
        facility_category=category,
        specialty=specialty,
        specialty_label=vocab.label(specialty) if specialty else None,
        config=insights_config.get("benchmarks") or {})
    insights = build_insights(measures, insights_config)

    tables = ["population_mesh", "mesh_business", "facilities", "stations",
              # 都市計画が未取得の県では「建てられるか」を判定できません。
              # 黙って節が消えるのではなく、未取得として名前が出るようにします。
              "municipalities", "land_prices", "city_planning_zones"]
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
        # この文書の読み方と、この文書では答えられないこと。
        "reading_guide": READING_GUIDE,
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
            # 市区町村全体の昼夜間人口。**商圏の数字ではありません。**
            # location の下に置いているのは、そこが「この地点がどこにあるか」
            # の欄で、商圏の集計と混ざらないからです。
            "daytime": municipality_daytime(
                conn, (municipality or {}).get("municipality_code")),
            # 隣接市区町村。比較の母集団として使うだけでなく、外部調査の
            # 検索語にもなります（「三島市 歯科 インプラント」）。
            "neighbour_municipalities": neighbours or None,
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
        # 各統計を、比較対象・percentile・順位・増減つきで 1 件ずつ。
        # 読み手が最初に当たる場所。
        "measures": {
            "measurement_basis": {
                "radius_m": comparison_radius,
                "catchment": catchment,
                "profile": model.profile_name,
                "compared_against": "同一都道府県の採点済みメッシュ（同一半径・同一商圏形状）",
                "note": ("比較対象のメッシュ分布は半径 "
                         f"{comparison_radius}m で作られています。"
                         f"要求された半径（{radius_m}m）と異なる場合、実数は要求半径、"
                         "比較は上記半径のものです。"),
            },
            # どの母集団を代表に使ったか、なぜそれを選んだか。県全域が
            # 弁別力を失う県（山林が大半を占める県）では、ここが別の母集団に
            # 切り替わります。切り替えたことを黙っていると、東京の「上位6%」と
            # 静岡の「上位6%」が同じ意味に見えてしまいます。
            "primary_benchmark": primary_benchmark,
            # 比較対象の説明はここに 1 回だけ。各 benchmark は type で参照します。
            "benchmark_scopes": scope_summary(measures),
            "items": [m.as_dict() for m in measures],
        },
        # 同時に見るべき指標の組。結論は含まず、揃わなかったものを gaps に出します。
        "insight_metrics": insights,
        "demand": {
            # A total hides its own shape; this is how the residents are laid
            # out inside the catchment.
            "distribution": _catchment_shape(conn, lat, lng, radius_m,
                                             mesh_size_m or 1000),
            "residents": {
                "by_radius": {r: {
                    k: v for k, v in vals.items()
                    if k in ("population", "age_0_14", "age_15_64",
                             "age_65_plus", "households", "population_growth",
                             "mesh_count")}
                    for r, vals in by_radius.items()},
                # そこに住んでいる人の性格。交通手段は駐車場の判断に、
                # 居住期間はかかりつけが回るかの判断に効きます。年齢構成
                # からは出てきません。**常住地基準**なので residents の下です。
                "profile": resident_profile(conn, lat, lng, radius_m,
                                            mesh_size_m or 1000),
            },
            # 20〜30年の意思決定に、過去5年の増減だけで答えない。
            "outlook": _outlook_with_actual(
                population_outlook(conn, lat, lng, radius_m, mesh_size_m or 1000),
                by_radius.get(str(radius_m)) or {}),
            "daytime": {
                # **経済センサスの従業者数です。従業地基準。** 同じ商圏の
                # 「そこに住んでいる働き手」（国勢調査・常住地基準）とは別物で、
                # 実測では 52,688 対 22,322 と 2.4 倍違います。誤差ではなく、
                # 別のものを数えています。
                "basis": "workplace",
                "basis_label": MEASUREMENT_BASES["workplace"],
                "by_radius": {r: {k: vals[k] for k in ("workers", "establishments")}
                              for r, vals in by_radius.items()},
                # 経済センサスの従業者数では、通学者が丸ごと落ちます。
                # 大学の門前で「昼間人口 5 万人」と書きながら学生を数え落とす、
                # ということが実際に起きました。
                "census_daytime": daytime_population(
                    conn, lat, lng, radius_m, mesh_size_m or 1000),
                # 全産業の合計も渡します。渡さないと「分類不能・未取得」の
                # 差分が出せず、内訳を足しても合計に届かない理由が読み手に
                # 分かりません。
                "industry_mix": _industry_mix(
                    conn, lat, lng, radius_m, mesh_size_m or 1000,
                    total={k: (by_radius.get(str(radius_m)) or {}).get(k)
                           for k in ("workers", "establishments")}),
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
            # How close, not only how many.
            "proximity": _competitor_distances(conn, lat, lng, category, radius_m),
            # 標榜科目別の競合。「歯科医院 186 件」は、小児歯科をやるつもりの
            # 人にとっては 186 件ではありません。
            "by_specialty": _by_specialty(metrics, by_radius),
            # 診療時間。空いている曜日・時間帯は、件数には出てこない競合の形。
            "hours": _hours_block(metrics),
            # 開設年。20年後の競合の数は、いまの数ではなく「いまの院長が
            # あと何年やるか」で決まります。年齢は公表されていませんが、
            # 開設年は届出にあります。
            "vintage": _clinic_vintage(conn, lat, lng, radius_m, category),
        },
        "access": {
            "nearest_station": {
                "name": metrics.get("nearest_station"),
                "distance_m": _round(metrics.get("station_distance_m")),
                "daily_passengers": metrics.get("daily_passengers"),
                # 駅のどちら側か。**「南口か北口か」はここで決まります。**
                "direction": _direction_from_station(
                    conn, lat, lng, metrics.get("nearest_station")),
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
            "rent_estimate": rent_estimate(
                _round(metrics.get("land_price_yen_per_sqm")),
                insights_config.get("rent_estimate") or {}),
        },
        # 用途地域・容積率・建蔽率。**この節だけが「何を建ててよいか」を
        # 答えます。** 他は全部「そこに何人いるか」の話です。
        #
        # 2 つの出所を並べ、どちらがどちらか分かるようにしています。
        # city_plan は都市計画決定情報（面）で、候補地の座標がどの区域に
        # 入るかが決まります。land_price_survey は地価公示（点）で、
        # 標準地が置かれた場所の値です。**混ぜると、点の値が面の判定として
        # 読まれます。**
        "regulation": {
            "city_plan": _city_planning(conn, lat, lng, radius_m, category),
            "land_price_survey": _zoning(conn, lat, lng, radius_m),
            "note": ("city_plan は国土数値情報 A55（面）による候補地点の判定。"
                     "land_price_survey は地価公示の標準地（点）の値で、"
                     "候補地そのものの用途地域とは限らない。"
                     "食い違う場合は city_plan が候補地の値。"),
        },
        "scores": {
            "normalization_scope": scope,
            "normalization_reference": scope.rsplit(":", 1)[-1],
            "normalization_reference_note": (
                "得点の目盛りをどの集合から作ったか。with_clinics は「県内で歯科医院が"
                "実在する商圏」。県全域から作ると、農村が大半の県では市街地がどこでも"
                "上限に張り付きます。"),
            "by_profile": scores,
            "note": ("スコアは同一都道府県内のメッシュ分布に対する相対値です。"
                     "暫定モデルであり、実績データによる較正は行っていません。"),
        },
        "data_quality": {
            "unavailable_datasets": unavailable,
            "notes": notes,
            # 算出できなかった比較と、その理由。percentile が欠けている指標を
            # 「平凡だった」と読まれないために、欠けた理由を必ず添えます。
            "benchmark_notes": benchmark_notes,
            "caveats": _dataset_caveats(),
        },
        # measures に入らない欄（産業構成・標榜科目・診療時間など）の定義。
        # measures 側の各指標は自分の definition と source を持っているので、
        # 同じ説明を 2 か所に置きません。
        "definitions": _supplementary_definitions(),
        "provenance": provenance,
        "disclaimer": disclaimer,
        "score_disclaimer": score_disclaimer,
    }


#: 坪と平方メートル。
SQM_PER_TSUBO = 3.305785


def rent_estimate(land_price_yen_per_sqm: float | None,
                  config: Mapping[str, Any]) -> dict[str, Any] | None:
    """地価から、テナント賃料の**目安の幅**を機械的に換算する。

    プロジェクトの前提では家賃の「予測」を禁じています。ここでやるのも予測では
    ありません。**土地の価格を、想定利回りという 1 つの仮定で賃料の次元に
    置き換えているだけ**です。式も仮定も出力に載せるので、読み手は自分の
    利回り観で引き直せます。

        月額賃料（円/坪） = 地価（円/m²） × 3.305785 × 想定利回り ÷ 12

    利回りの幅は設定に置きます（config/<業態>/insights.yaml）。ここを 1 つの数字に
    決め打ちすると、出てきた数字が一人歩きします。幅で出すのは、幅があることが
    この換算の実態だからです。

    含まれないもの：建物の状態、階数、面積効率、共益費、契約条件、
    そして何より**実際の募集事例**。募集賃料のデータは取り込んでいません。
    """
    if not land_price_yen_per_sqm or land_price_yen_per_sqm <= 0:
        return None
    rates = config.get("yield_range") or [0.06, 0.10]
    low, high = float(min(rates)), float(max(rates))
    per_tsubo = land_price_yen_per_sqm * SQM_PER_TSUBO

    def monthly(rate: float) -> int:
        return int(round(per_tsubo * rate / 12))

    return {
        "monthly_yen_per_tsubo_low": monthly(low),
        "monthly_yen_per_tsubo_high": monthly(high),
        "monthly_yen_per_sqm_low": int(round(land_price_yen_per_sqm * low / 12)),
        "monthly_yen_per_sqm_high": int(round(land_price_yen_per_sqm * high / 12)),
        "assumed_yield_low": low,
        "assumed_yield_high": high,
        "formula": ("月額賃料（円/坪） = 地価（円/m²） × 3.305785 × 想定利回り ÷ 12"),
        "basis": "公示地価（中央値）からの収益還元による換算",
        "note": (
            "実際の募集賃料ではありません。地価を想定利回りで賃料の次元に"
            "置き換えた目安で、建物の状態・階数・面積効率・共益費・契約条件は"
            "含みません。募集事例のデータは取り込んでいないため、実勢との差は"
            "検証できていません。想定利回りは設定値"
            f"（{low:.0%}〜{high:.0%}）であり、地域や物件種別で変わります。"
            "**地価の高い商業地ほど実際の利回りは低くなる傾向があるため、"
            "都心の一等地ではこの換算は高めに出ます。**"
            "config/<業態>/insights.yaml の rent_estimate.yield_range で調整してください。"),
    }
