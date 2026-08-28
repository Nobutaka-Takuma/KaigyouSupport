"""標榜診療科目と診療時間: 医療情報ネット 032 の取り込みと、その使われ方。

このデータで壊れやすいのは解析ではありません。読み手が件数を取り違えることです。

危ないのは 2 か所あります。ひとつは自由記載欄。インプラント・審美・訪問診療は
標榜診療科目ではなく「その他」欄への自由記載にしかなく、東京都で
インプラントと書いているのは歯科医院の 1.2% だけです。実施している医院は
それよりずっと多い。この件数を競合数として使うと、いちばん混んでいる場所が
いちばん空いて見えます。

もうひとつは分母。科目別の件数は「診療科目が分かっている医院のうち何件か」で
あって「商圏内に何件あるか」ではありません。抽出に載っていない医院は
「標榜していない」のではなく「分からない」です。

下のテストは、数字が合っていることと、この 2 つの区別が消えないことの両方を
見ています。
"""
from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest

from kaigyou_core import config as cfg
from kaigyou_core import specialties as vocab
from kaigyou_etl.adapters import AdapterContext, get_adapter

FIXTURE = Path(__file__).parent / "fixtures" / "mhlw_dental_specialties.csv"
REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ID = "__specialty_test__"


def _adapter(tmp_path: Path):
    sources = cfg.sources_config()
    spec = dict(sources["sources"]["mhlw_dental_specialties"])
    ctx = AdapterContext(source_id=SOURCE_ID, spec=spec,
                         defaults=sources.get("defaults", {}), raw_dir=tmp_path)
    return get_adapter(spec["adapter"])(ctx)


def _by_id(tmp_path: Path) -> dict[str, dict]:
    return {r["facility_id"]: r for r in _adapter(tmp_path).transform(FIXTURE)}


# ------------------------------------------------------------------ 正規化
def test_the_notified_specialty_codes_map_by_code_not_by_name():
    """コードが答え。名称は表記ゆれがあるので当てにしない。"""
    assert vocab.classify("08001", "歯科") == ("general", False)
    assert vocab.classify("08004", "小児歯科") == ("pediatric", False)
    assert vocab.classify("08002", "矯正歯科") == ("orthodontics", False)
    assert vocab.classify("08003", "歯科口腔外科") == ("oral_surgery", False)
    assert vocab.classify("08005", "小児矯正歯科") == ("pediatric_orthodontics", False)


def test_the_free_text_division_is_marked_as_free_text():
    """08991 から来たキーには、必ず自由記載の印が付く。"""
    for name in ("インプラント", "インプラント治療"):
        key, free = vocab.classify("08991", name)
        assert (key, free) == ("implant", True)
    assert vocab.classify("08991", "ホワイトニング") == ("cosmetic", True)
    assert vocab.classify("08991", "訪問歯科診療") == ("home_visit", True)
    # どのキーワードにも当たらない自由記載も、捨てずに other へ入れる。
    assert vocab.classify("08991", "なし") == ("other", True)


def test_is_free_text_marks_only_the_free_text_keys():
    """この印が競合数として使ってよいかどうかを分ける。逆になっていたら困る。"""
    assert vocab.is_free_text("implant")
    assert vocab.is_free_text("cosmetic")
    assert vocab.is_free_text("home_visit")
    assert not vocab.is_free_text("pediatric")
    assert not vocab.is_free_text("orthodontics")
    assert not vocab.is_free_text("general")


def test_non_dental_specialties_do_not_become_dental_ones():
    """病院の歯科口腔外科に併記された内科を、歯科の競合に混ぜない。"""
    assert vocab.classify("01001", "内科") == ("other_medical", False)
    assert vocab.classify("06001", "皮膚科") == ("other_medical", False)


# ------------------------------------------------------------------ 取り込み
def test_validate_reports_what_the_file_contains(tmp_path):
    facts = _adapter(tmp_path).validate(FIXTURE)
    assert facts["facilities"] == 5
    assert facts["rows_by_specialty_key"]["general"] > 0
    assert facts["rows_by_specialty_key"]["implant"] == 3
    # 自由記載の中身は、そのまま見えるようにしておく。正規化キーだけだと
    # キーワードの当て漏れに気づけない。
    assert "インプラント" in facts["free_text_names_top"]


def test_a_facility_keeps_every_specialty_it_declares(tmp_path):
    rows = _by_id(tmp_path)
    keys = set(rows["A0000000001"]["features"]["specialty_keys"])
    assert keys == {"general", "pediatric", "orthodontics", "oral_surgery"}
    # 公表された名称も残す。正規化は集計のためであって、表示のためではない。
    assert "小児歯科" in rows["A0000000001"]["features"]["declared_specialties"]


def test_hours_declared_under_one_specialty_describe_the_facility(tmp_path):
    """実ファイルでは診療時間はたいてい先頭の「歯科」にだけ入っている。

    それを科目ごとの時間として扱うと、小児歯科は「時間の記載なし」になる。
    要約は施設単位で作る。
    """
    f = _by_id(tmp_path)["A0000000001"]["features"]
    assert f["opens_saturday"] is True
    assert f["opens_sunday"] is False
    assert f["opens_holiday"] is False
    # 金曜だけ 19:30 まで。夜間診療の判定は設定の evening_from（18:30）。
    assert f["opens_evening"] is True
    assert f["latest_close"] == time(19, 30)
    # 平日 4 日 (4+3.5) + 金 (4+5) + 土 4 = 43.0
    assert f["weekly_open_hours"] == pytest.approx(43.0)


def test_sunday_and_holiday_opening_are_separate_facts(tmp_path):
    f = _by_id(tmp_path)["B0000000002"]["features"]
    assert (f["opens_sunday"], f["opens_holiday"]) == (True, True)
    assert f["opens_saturday"] is False
    assert f["opens_evening"] is False
    # 祝日は毎週来るわけではないので、週間診療時間には数えない。
    # 月火水日の 7 時間ずつで 28。
    assert f["weekly_open_hours"] == pytest.approx(28.0)


def test_overlapping_hours_are_merged_before_they_are_added_up(tmp_path):
    """同じ時間帯が複数の科目に書かれていても、開いている長さは 1 つ。

    素直に足すと 9 時間の日が 14 時間になり、実データでは週 226.5 時間という
    1 週間に存在しない長さが出た。
    """
    f = _by_id(tmp_path)["D0000000004"]["features"]
    assert f["weekly_open_hours"] == pytest.approx(9.0)


def test_a_facility_with_no_declared_hours_reports_none_not_zero(tmp_path):
    """時間の記載が無い医院は「0 時間開いている」ではない。"""
    f = _by_id(tmp_path)["C0000000003"]["features"]
    assert f["weekly_open_hours"] is None
    assert f["open_days"] is None
    assert f["opens_saturday"] is False
    # 科目の記載だけはあるので、そちらは残る。
    assert "implant" in f["specialty_keys"]


def test_the_hours_rows_keep_what_was_published(tmp_path):
    rows = _by_id(tmp_path)["A0000000001"]["hours"]
    monday_first = [h for h in rows if h["weekday"] == 1 and h["time_band"] == 1]
    assert len(monday_first) == 1
    assert monday_first[0]["opens"] == time(9, 0)
    assert monday_first[0]["closes"] == time(13, 0)
    assert monday_first[0]["reception_opens"] == time(9, 0)


# ------------------------------------------------------- 設定としてのモデル
def test_no_shipped_profile_scores_competition_on_a_free_text_specialty():
    """自由記載の科目で競合を数えるプロファイルを出荷しない。

    書いた医院しか数えられないので、数え落としが一方向にしか起きない。
    「インプラント 1 件」の商圏は、インプラントの競合が 1 件の商圏ではなく、
    1 件だけがそう書いた商圏。順位を付けると、記載率の低さがそのまま
    「競合が少ない」に化ける。
    """
    from kaigyou_core.scoring import competition_specialties

    for _population_metric, specialty in competition_specialties(cfg.scoring_config()):
        assert not vocab.is_free_text(specialty), (
            f"プロファイルが自由記載の科目 {specialty!r} で競合を数えています")


def test_the_specialty_scoped_profiles_use_their_own_distribution():
    """科目で絞った比率は、全科目の比率とは別の指標として集計される。

    同じ名前で扱うと、競合を半分に絞ったぶん大きくなった比率を、絞る前の
    分布に当てて評価することになる。どこもかしこも高得点になる。
    """
    from kaigyou_core.scoring import derived_metrics

    metrics = derived_metrics(cfg.scoring_config())
    assert "age_0_14_per_facility@pediatric" in metrics
    assert "population_per_facility@orthodontics" in metrics


def test_specialty_competition_is_withheld_when_coverage_is_thin():
    """科目が分からない医院が多い商圏では、科目別の競合を採点しない。

    載っていない医院は「標榜していない」ではなく「分からない」。そのまま
    数えると、データの薄い場所が競合の少ない場所に見える。
    """
    from kaigyou_core.scoring import ScoringModel, augment_specialty_metrics

    model = ScoringModel(cfg.scoring_config(), "pediatric")
    metrics = {
        "population": 20000, "age_0_14": 2400, "facility_count": 20,
        # 20 件中 4 件しか科目が分からない。
        "facilities_with_specialty_data": 4,
        "facility_specialty_counts": {"pediatric": 1},
    }
    augment_specialty_metrics(metrics, [("age_0_14", "pediatric")])
    result = model.competition(metrics, {})
    assert result.value is None
    assert "20%" in (result.note or "")


def test_specialty_competition_counts_only_that_specialty():
    """絞ったときの分母と分子が、両方とも切り替わっていること。"""
    from kaigyou_core.scoring import augment_specialty_metrics, specialty_ratio_metric

    metrics = {
        "population": 20000, "age_0_14": 2400, "facility_count": 20,
        "facilities_with_specialty_data": 20,
        "facility_specialty_counts": {"general": 19, "pediatric": 6},
    }
    augment_specialty_metrics(metrics, [("age_0_14", "pediatric")])
    assert metrics["facility_count@pediatric"] == 6
    # 全科目なら 20000/20 = 1000。小児歯科なら 2400/6 = 400。
    assert metrics[specialty_ratio_metric("age_0_14", "pediatric")] == pytest.approx(400.0)


def test_a_specialty_absent_from_the_catchment_counts_as_zero_not_missing():
    """商圏内の医院を全部見て 1 件も無かった、は欠測ではない。"""
    from kaigyou_core.scoring import augment_specialty_metrics

    metrics = {"population": 8000, "age_0_14": 900, "facility_count": 3,
               "facilities_with_specialty_data": 3,
               "facility_specialty_counts": {"general": 3}}
    augment_specialty_metrics(metrics, [("age_0_14", "pediatric")])
    assert metrics["facility_count@pediatric"] == 0
    # 件数 0 の比率は算出できない。競合スコア側が zero_facility_score を当てる。
    assert metrics["age_0_14_per_facility@pediatric"] is None


# ------------------------------------------------------------------ 見せ方
def test_the_breakdown_keeps_the_free_text_marker_for_the_reader():
    rows = vocab.describe({"pediatric": 12, "implant": 1, "general": 30})
    marks = {r["key"]: r["declared_only"] for r in rows}
    assert marks == {"general": False, "pediatric": False, "implant": True}
    # 表示順は標榜科目が先。自由記載を上に出すと、そちらが本筋に見える。
    assert [r["key"] for r in rows] == ["general", "pediatric", "implant"]


def test_the_ui_does_not_offer_free_text_specialties_as_a_map_filter():
    """地図の絞り込みに自由記載を出さない。

    「インプラント」で絞ると地図がほぼ空になり、それを見た人は
    「この辺にインプラントの医院は無い」と読む。実際には書いていないだけ。
    """
    page = (REPO_ROOT / "web" / "src" / "pages" / "MapPage.tsx").read_text(encoding="utf-8")
    assert "declared_only" in page and "filter" in page


def test_the_free_text_caveat_reaches_the_dataset():
    from kaigyou_core.dataset import _dataset_caveats

    joined = "\n".join(_dataset_caveats())
    assert "自由記載" in joined
    assert "下限" in joined


# --------------------------------------------------------- PostGIS 側の検算
# データベースがあるときだけ動きます。自前のフィクスチャを入れて
# ロールバックするので、読み込み済みのデータには触りません。
psycopg = pytest.importorskip("psycopg")

_TEST_SOURCE = "__specialty_geo_test__"
# 読み込み済みの東京から遠い 1km メッシュ。
_MESH_CODE = "50302030"


@pytest.fixture
def conn():
    from kaigyou_core.db import connect

    try:
        with connect() as c:
            with c.cursor() as cur:
                cur.execute("SELECT to_regclass('public.facility_features') AS t")
                if cur.fetchone()["t"] is None:
                    pytest.skip("015_specialties.sql not applied")
            yield c
            c.rollback()
    except psycopg.OperationalError as exc:
        pytest.skip(f"database unavailable: {exc}")


@pytest.fixture
def catchment(conn):
    """人口 10,000 の 1 メッシュと、その中心のまわりの 4 医院。

    歯科 4 件、うち小児歯科 2 件、うち 1 件は診療科目データなし。
    「全 4 件・科目が分かるのは 3 件・小児歯科は 2 件」という、分母と分子が
    全部違う状況をひとつ作っておきます。
    """
    from kaigyou_core import mesh as meshlib

    lng, lat = meshlib.centroid(_MESH_CODE)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO data_sources (id, name, publisher, dataset_kind) "
            "VALUES (%s, 'test fixture', 'test', 'sample') ON CONFLICT (id) DO NOTHING",
            (_TEST_SOURCE,))
        cur.execute(
            """
            INSERT INTO population_mesh (
                source_id, mesh_code, mesh_size_m, prefecture_code, geom, centroid,
                population, age_0_14, age_15_64, age_65_plus, households, source_date
            ) VALUES (
                %s, %s, 1000, '99', ST_GeomFromText(%s, 4326),
                ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                10000, 1200, 6800, 2000, 5000, current_date
            )
            """,
            (_TEST_SOURCE, _MESH_CODE, meshlib.to_polygon_wkt(_MESH_CODE), lng, lat))

        clinics = [
            ("F1", ["general", "pediatric"], True, False),
            ("F2", ["general", "pediatric", "orthodontics"], True, True),
            ("F3", ["general"], False, False),
            ("F4", None, None, None),          # 科目データなし
        ]
        for i, (fid, keys, saturday, evening) in enumerate(clinics):
            cur.execute(
                """
                INSERT INTO facilities (
                    source_id, facility_id, facility_category, name, prefecture_code,
                    geom, source_date
                ) VALUES (%s, %s, 'dental_clinic', %s, '99',
                          ST_SetSRID(ST_MakePoint(%s, %s), 4326), current_date)
                """,
                (_TEST_SOURCE, fid, f"テスト歯科{i}", lng + 0.0005 * i, lat))
            if keys is None:
                continue
            cur.execute(
                """
                INSERT INTO facility_features (
                    facility_id, source_id, specialty_keys, declared_specialties,
                    open_days, weekly_open_hours, opens_saturday, opens_sunday,
                    opens_holiday, opens_evening, source_date
                ) VALUES (%s, %s, %s, '{}', 5, 40, %s, false, false, %s, current_date)
                """,
                (fid, _TEST_SOURCE, keys, saturday, evening))
    return {"lat": lat, "lng": lng}


def test_the_analysis_counts_specialties_and_reports_its_denominator(conn, catchment):
    from kaigyou_core.analysis import analyze_point

    m = analyze_point(conn, catchment["lat"], catchment["lng"], 1000,
                      mesh_size_m=1000, specialty="pediatric")
    assert m["facility_count"] == 4
    # 分母。この 1 件の差が「小児歯科は 2 件しかない」と
    # 「小児歯科は 2 件しか分かっていない」を分ける。
    assert m["facilities_with_specialty_data"] == 3
    assert m["facility_specialty_count"] == 2
    assert m["facility_specialty_counts"] == {"general": 3, "pediatric": 2,
                                              "orthodontics": 1}


def test_the_analysis_summarises_the_opening_hours_in_the_catchment(conn, catchment):
    from kaigyou_core.analysis import analyze_point

    m = analyze_point(conn, catchment["lat"], catchment["lng"], 1000, mesh_size_m=1000)
    hours = m["facility_hours_counts"]
    assert hours["declared"] == 3       # F4 は時間の記載なし
    assert hours["saturday"] == 2
    assert hours["evening"] == 1
    assert hours["sunday"] == 0
    assert m["facility_weekly_hours_median"] == 40


def test_the_specialty_filter_reaches_the_radius_counts(conn, catchment):
    from kaigyou_core.analysis import facility_counts

    everyone = facility_counts(conn, catchment["lat"], catchment["lng"], [1000])
    children = facility_counts(conn, catchment["lat"], catchment["lng"], [1000],
                               specialty="pediatric")
    assert everyone[1000] == 4
    assert children[1000] == 2


def test_a_catchment_without_specialty_data_still_analyses(conn, catchment):
    """科目データを消しても、これまでの数字はそのまま出る。

    新しい情報源が入らない環境で分析全体が止まるのがいちばん困る。
    """
    from kaigyou_core.analysis import analyze_point

    with conn.cursor() as cur:
        cur.execute("DELETE FROM facility_features WHERE source_id = %s", (_TEST_SOURCE,))
    m = analyze_point(conn, catchment["lat"], catchment["lng"], 1000, mesh_size_m=1000)
    assert m["facility_count"] == 4
    assert m["population"] == pytest.approx(10000, rel=0.01)
    assert m["facilities_with_specialty_data"] == 0
    assert m["facility_specialty_counts"] == {}


# ------------------------------------------------------- 語彙は設定にあること
#
# 医科への拡張の 3 段目。コード表は YAML、表示名と並び順は Python の dict、
# という割れた状態でした。告示が科目を増やすたびに 2 か所を直すことになり、
# 片方だけ直すと「キーはあるのに表示名が英字のまま」になります。
# 詳細は docs/refactoring-multi-specialty.md。

def test_the_vocabulary_comes_from_the_configuration():
    """表示名も並び順も設定から来ること。**コードに戻すと 2 か所になります。**"""
    import ast
    from pathlib import Path

    module = Path(vocab.__file__)
    tree = ast.parse(module.read_text(encoding="utf-8"))
    literals = {
        node.targets[0].id
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, (ast.Dict, ast.Tuple, ast.List))
    }
    assert not literals, (
        f"語彙がコードに戻っています: {sorted(literals)}。"
        "config/sources.yaml の specialty_labels / specialty_order へ")

    # 中身は設定から引けていること（空の dict を返して素通りしない）。
    assert vocab.labels()["pediatric"] == "小児歯科"
    assert vocab.order()[0] == "general"
    assert vocab.hours_labels()["holiday"] == "祝日診療"


def test_moving_the_vocabulary_did_not_change_a_single_label():
    """**歯科版は商談で使われています。** 移し替えで表示が変わってはいけない。

    ここに書いてあるのは、移す前に Python の dict にあったものそのままです。
    """
    assert vocab.labels() == {
        "general": "一般歯科",
        "pediatric": "小児歯科",
        "orthodontics": "矯正歯科",
        "pediatric_orthodontics": "小児矯正歯科",
        "oral_surgery": "歯科口腔外科",
        "implant": "インプラント",
        "cosmetic": "審美・ホワイトニング",
        "home_visit": "訪問歯科診療",
        "periodontal": "歯周病",
        "preventive": "予防歯科",
        "special_needs": "障害者歯科",
        "dental_anesthesia": "歯科麻酔",
        "prosthodontics": "補綴・義歯",
        "endodontics": "歯内療法",
        "sleep_apnea": "睡眠時無呼吸",
        "other": "その他（歯科）",
        "other_medical": "歯科以外の標榜科",
    }
    assert vocab.order() == (
        "general", "pediatric", "orthodontics", "pediatric_orthodontics",
        "oral_surgery", "implant", "cosmetic", "home_visit", "periodontal",
        "preventive", "special_needs", "prosthodontics", "endodontics",
        "dental_anesthesia", "sleep_apnea", "other", "other_medical")
    assert vocab.hours_labels() == {
        "saturday": "土曜診療", "sunday": "日曜診療",
        "holiday": "祝日診療", "evening": "夜間診療"}


def test_the_vocabulary_is_chosen_by_business_type():
    """歯科と医科では科目の体系そのものが別です。1 つの表に混ぜられません。

    **見つからないときに他業態の語彙で代用しません。** 歯科の科目名で内科を
    分類したものは、間違っていてもそれらしく見えます。
    """
    from kaigyou_core.analysis import DEFAULT_CATEGORY

    assert vocab.spec(DEFAULT_CATEGORY).get("specialty_codes"), \
        "歯科の語彙は業態から引けること"
    assert vocab.spec("medical_clinic") == {}, \
        "まだ無い業態に、歯科の語彙を返さないこと"
    assert vocab.labels("medical_clinic") == {}
    assert vocab.label("pediatric", "medical_clinic") == "pediatric", \
        "語彙が無ければキーをそのまま返す（歯科の表示名を借りない）"
