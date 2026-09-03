"""地域競合分析（3C の Competitor）。

守りたいのは、この分析でいちばん起きやすい 3 つの誤読です。

**「1km 圏に 12 院」と読ませない。** 上限で切った 12 件なのか、本当に 12 院
しかないのかで、読み方が正反対になります。切った件数を落とすと、読み手には
区別が付きません。

**「サイトに書いていない」を「やっていない」と読ませない。** 集計は公開情報
から確認できたものの数です。0 件の行を消すと、この地域に無いのか調べ落とし
たのかが区別できなくなるので、**0 件も行として残します。**

**「競合が少ない」を「機会がある」と読ませない。** 少ない理由が「まだ誰も
やっていない」なのか「やってみて成立しなかった」なのかは、このデータでは
区別できません（開発指示書 §6）。

LLM は呼びません。数え上げと組み立ては全部 Python 側にあります——**それが
この設計の狙い**で、数えた結果は検算できますが、モデルが数えたと言った数は
検算できません。
"""
from __future__ import annotations

import pathlib

import pytest

from kaigyou_core import competition
from kaigyou_intel import client as llm


#: 業態に依存しない軸の設定。**呼び方は設定から来ます。**
CONFIG = {
    "label": "歯科医院",
    "products": ["一般歯科", "小児歯科", "インプラント", "訪問歯科"],
    "segments": ["小児", "成人", "高齢者"],
    "place_attributes": [{"key": "parking", "label": "駐車場"},
                         {"key": "weekend", "label": "土日診療"}],
    "positioning_map": {
        "x": {"key": "payment", "label": "診療の支払い",
              "low": "保険診療中心", "high": "自費診療中心"},
        "y": {"key": "scope", "label": "診療の幅",
              "low": "一般診療中心", "high": "専門診療中心"},
        "scale": [-2, -1, 0, 1, 2],
    },
}

COMPETITORS = [
    {"name": "A歯科", "distance_m": 220, "products": ["一般歯科", "小児歯科"],
     "target": ["小児"], "positioning": ["小児対応"],
     "place_confirmed": ["parking"],
     "map_placed": True, "map_x": -1, "map_y": -1, "map_basis": "保険中心・一般"},
    {"name": "B歯科", "distance_m": 640, "products": ["インプラント", "一般歯科"],
     "target": ["成人"], "positioning": ["インプラント"],
     "place_confirmed": [],
     "map_placed": True, "map_x": 2, "map_y": 2, "map_basis": "自費専門を掲げる"},
    {"name": "C歯科", "distance_m": 880, "products": ["一般歯科"],
     "target": ["高齢者"], "positioning": [],
     "place_confirmed": ["weekend"],
     # 判定できなかった。**0 ではなく map_placed=False で表します**——0 は
     # 「どちらとも言えない」という意味のある値なので、混ぜられません。
     "map_placed": False, "map_basis": "サイトに診療方針の記載なし"},
]


# ------------------------------------------------------------------ 集計
def test_a_kind_nobody_offers_still_gets_a_row():
    """**0 件の行を消しません。**

    「この地域に訪問歯科を掲げる医院は無い」は、行が無いことでは伝わりません。
    行が無いのと、数えて 0 だったのとを、読み手は区別できないからです。
    """
    tally = competition.tally(COMPETITORS, CONFIG)
    rows = {r["label"]: r["count"] for r in tally["products"]}
    assert rows["訪問歯科"] == 0
    assert rows["一般歯科"] == 3
    assert rows["インプラント"] == 1


def test_a_word_outside_the_vocabulary_is_kept_and_marked():
    """設定に無い語を捨てません。**捨てると語彙の抜けに気づけません。**"""
    extra = [*COMPETITORS, {"name": "D歯科", "products": ["ホワイトニング"]}]
    rows = {r["label"]: r for r in competition.tally(extra, CONFIG)["products"]}
    assert rows["ホワイトニング"]["count"] == 1
    assert rows["ホワイトニング"]["outside_vocabulary"] is True


def test_the_near_ring_is_counted_separately():
    """500m 圏と 1km 圏は別に数えます（指示書 §4）。"""
    tally = competition.tally(COMPETITORS, CONFIG, near_radius_m=500)
    assert tally["surveyed"] == 3
    assert tally["within_near"] == 1
    assert tally["near_radius_m"] == 500


def test_the_counts_say_they_are_only_what_the_web_confirmed():
    """**「書いていない」は「やっていない」ではありません。**

    この但し書きが本文から落ちると、集計表は「この地域の診療内容」に見えます。
    実際には「Web に書いてあった診療内容」です。
    """
    note = competition.tally(COMPETITORS, CONFIG)["note"]
    assert "扱っていないという意味ではありません" in note
    # 呼び方は設定から。「医院」をコードに埋めると飲食で読めなくなります。
    assert "各歯科医院のサイト" in note


# ------------------------------------------ ポジショニングマップ（§5）
def test_a_clinic_that_could_not_be_placed_is_not_placed():
    """**判定困難を無理に置きません。**

    置いてしまうと、図の上では他の点と同じ確かさに見えます。置けなかった
    ことは、理由とともに残ります。
    """
    pmap = competition.positioning_map(COMPETITORS, CONFIG)
    assert [p["name"] for p in pmap["placed"]] == ["A歯科", "B歯科"]
    assert [u["name"] for u in pmap["undecided"]] == ["C歯科"]
    assert pmap["undecided"][0]["why"] == "サイトに診療方針の記載なし"


def test_the_quadrants_are_named_from_the_config_not_from_dentistry():
    """区画の呼び方は**設定の軸から**作ります。

    「自費 × 専門」をコードに書くと、この関数は歯科でしか読めません。飲食にも
    学習塾にも転用する前提なので、枠だけをコードに置き、語は設定に置きます。
    """
    named = {q["label"]: q["count"]
             for q in competition.positioning_map(COMPETITORS, CONFIG)["quadrants"]}
    assert named["自費診療中心 × 専門診療中心"] == 1
    assert named["保険診療中心 × 一般診療中心"] == 1
    # 誰もいない区画も残します。**0 件は「機会」ではなく、数えた結果です。**
    assert named["自費診療中心 × 一般診療中心"] == 0

    other = {**CONFIG, "positioning_map": {
        "x": {"low": "低価格帯", "high": "高価格帯"},
        "y": {"low": "総合", "high": "専門店"},
        "scale": [-2, -1, 0, 1, 2]}}
    labels = {q["label"] for q in
              competition.positioning_map(COMPETITORS, other)["quadrants"]}
    assert "高価格帯 × 専門店" in labels
    assert not any("自費" in label for label in labels)


def test_a_clinic_on_the_axis_is_not_pushed_into_a_quadrant():
    """0 は「どちらとも言えない」で、判定できなかったのとは別です。"""
    middle = [{"name": "E歯科", "map_placed": True, "map_x": 0, "map_y": 1}]
    counts = {q["label"]: q["count"]
              for q in competition.positioning_map(middle, CONFIG)["quadrants"]}
    assert counts["どちらとも言えない（軸上）"] == 1
    assert sum(v for k, v in counts.items() if "×" in k) == 0


# ------------------------------------------------------- 段の組み立て
def test_the_survey_takes_the_nearest_and_says_how_many_it_left():
    """上限で切った件数を**黙って落としません**（指示書 §1）。"""
    from kaigyou_intel.steps import comp1_survey

    dataset = {"location": {"lat": 35.0, "lng": 139.0},
               "competition": {"clinics_in_radius": {"items": [
                   {"name": f"第{i}歯科", "distance_m": i * 50} for i in range(20)
               ]}}}
    payload = comp1_survey.build_input(dataset)
    limit = len(payload["competitors"])
    assert payload["total_in_radius"] == 20
    assert payload["not_surveyed"] == 20 - limit
    # 近い順。遠いほうから切ります。
    distances = [c["distance_m"] for c in payload["competitors"]]
    assert distances == sorted(distances)


def test_the_summary_is_handed_counted_values_not_asked_to_count():
    """STEP2 の入力には**集計済みの数字**が入っています（指示書 §8）。"""
    from kaigyou_intel.steps import comp2_summary

    survey = {"competitors": COMPETITORS, "surveyed": 3, "requested": 4,
              "failed": [{"name": "D歯科", "why": "サイトが見つからない"}],
              "not_surveyed": 5, "total_in_radius": 9, "radius_m": 1000}
    payload = comp2_summary.build_input(survey)
    assert payload["tally"]["surveyed"] == 3
    assert payload["positioning_map"]["placed"]
    # 調べられなかったぶんも一緒に渡します。**要約が「少ない」と言う前に。**
    assert payload["coverage"]["not_surveyed"] == 5
    assert payload["coverage"]["radius_m"] == 1000
    assert payload["coverage"]["failed"][0]["name"] == "D歯科"


def test_the_two_kinds_have_different_steps():
    """同じ「STEP1」でも、種類が違えば別の仕事です。"""
    from kaigyou_intel import jobs, worker

    assert jobs.steps_for("competitors") == jobs.COMPETITOR_STEP_NAMES
    assert jobs.steps_for("area") == jobs.STEP_NAMES
    # 知らない種類は周辺一般として扱います（黙って止まらせない）。
    assert jobs.steps_for("unknown") == jobs.STEP_NAMES
    assert sorted(worker.runners_for("competitors")) == [1, 2]
    assert sorted(worker.runners_for("area")) == sorted(jobs.STEP_NAMES)


def test_the_schemas_actually_sent_stay_under_the_grammar_limit():
    """競合分析のスキーマも、構文コンパイラの上限の内側に収まること。

    上限を超えると API が 400 を返します。**走らせてみるまで分かりません**
    ——しかも失敗するのは、検索を全部終えたあとの構造化のところです。
    """
    import json

    from kaigyou_intel.schemas import CompetitionSummary, Competitor

    for schema in (Competitor, CompetitionSummary):
        size = len(json.dumps(schema.model_json_schema(), ensure_ascii=False))
        assert size < 5_200, f"{schema.__name__} が {size} 文字"


# --------------------------------------------------------------- 文書
def _summary_output() -> dict:
    return {
        "label": "歯科医院",
        "landscape": "調査した3院のうち、自費を強く掲げるのは1院です。",
        "character": "この地域の歯科医院は保険・一般型が多い。",
        "crowded": ["一般歯科"],
        "sparse": ["訪問歯科"],
        "opportunities": [{"position": "訪問歯科", "why": "掲げる医院が0院",
                           "caveat": "在宅需要そのものが乏しい可能性がある"}],
        "not_determinable": ["各院の自費価格"],
        "tally": competition.tally(COMPETITORS, CONFIG),
        "positioning_map": competition.positioning_map(COMPETITORS, CONFIG),
        "coverage": {"surveyed": 3, "requested": 4, "not_surveyed": 5,
                     "total_in_radius": 9, "radius_m": 1000,
                     "failed": [{"name": "D歯科", "why": "サイトなし"}]},
    }


DATASET = {"location": {"lat": 35.1, "lng": 138.9, "name": "沼津駅南"},
           "query": {"radius_m": 1000}}


def test_the_document_says_what_it_did_not_survey_before_any_count():
    """**調べた範囲が、どの件数より先に来ること。**

    あとに置くと読まれません。上限で切った件数を、その地域に存在する件数と
    して読まれると、この文書の数字は全部ずれます。
    """
    from kaigyou_intel.competitor_report import to_markdown

    markdown = to_markdown(_summary_output(), DATASET)
    assert markdown.index("## この分析で調べた範囲") < markdown.index("## 競争環境")
    assert "調べていない：**5 件**" in markdown
    assert "その地域に存在しない" in markdown
    assert "D歯科：サイトなし" in markdown


def test_the_document_keeps_the_zero_rows():
    """0 件の行を落とさないこと。**調べ落としと区別が付かなくなります。**"""
    from kaigyou_intel.competitor_report import to_markdown

    markdown = to_markdown(_summary_output(), DATASET)
    assert "| 訪問歯科 | 0 件 |" in markdown


def test_no_opportunity_is_stated_without_what_would_make_it_wrong():
    """機会仮説には**必ず但し書き**（指示書 §6）。

    但し書きを任意にすると、書かれない回が出ます。書かれなかった回だけが
    断定に見えるので、無ければこちらで補います。
    """
    from kaigyou_intel.competitor_report import to_markdown

    output = _summary_output()
    output["opportunities"] = [{"position": "訪問歯科", "why": "0院", "caveat": ""}]
    markdown = to_markdown(output, DATASET)
    assert "**外れるとしたら**：この領域の競合が少ないのは" in markdown
    assert "そこに需要があることを意味しません" in markdown


def test_the_document_does_not_call_an_empty_quadrant_an_opportunity():
    """空いている区画を「機会」と読ませないこと。"""
    from kaigyou_intel.competitor_report import to_markdown

    markdown = to_markdown(_summary_output(), DATASET)
    assert "0 件の区画は、**そこに機会があるという意味ではありません。**" in markdown


def test_the_document_does_not_place_what_could_not_be_judged():
    """判定困難は表の外に、理由とともに。"""
    from kaigyou_intel.competitor_report import to_markdown

    markdown = to_markdown(_summary_output(), DATASET)
    body = markdown[markdown.index("### 各歯科医院の位置"):]
    assert "A歯科" in body and "B歯科" in body
    assert "位置を判定できなかった歯科医院" in markdown
    assert "C歯科：サイトに診療方針の記載なし" in markdown


def test_the_document_uses_the_word_from_the_config():
    """「競合」ではなく「歯科医院」と呼ぶこと。画面と文書で呼び方を揃えます。"""
    from kaigyou_intel.competitor_report import to_markdown

    markdown = to_markdown(_summary_output(), DATASET)
    assert "半径内の歯科医院" in markdown
    assert "比較的歯科医院が少ない領域" in markdown


def test_the_document_carries_its_own_disclaimer():
    """免責は省けません（プロジェクトの前提）。"""
    from kaigyou_intel.competitor_report import DISCLAIMER, to_markdown

    assert DISCLAIMER in to_markdown(_summary_output(), DATASET)


# ------------------------------------------------------------ 設定と枠
def test_the_dental_config_spends_the_search_budget_on_competitors():
    """検索の振り分け。**周辺一般から競合へ移した分がここにあります。**

    見るのは**設定ファイルの値**です。節約中は budget が上から重なって
    もっと小さくなりますが、それは一時的な措置で、この設定ファイルが持って
    いる意図とは別のものです。budget.mode を消したときに戻る先を見張ります。
    """
    from kaigyou_core import config as cfg

    survey = cfg.load_yaml(
        cfg.business_file("competitors.yaml", "dental_clinic"))["survey"]
    assert survey["radius_m"] == 1000
    assert survey["near_radius_m"] == 500
    # 1 医院あたり × 上限。周辺一般の合計 8 回より多くなければ、振り替えた
    # ことになりません。
    assert survey["searches_per_competitor"] * survey["max_competitors"] > 8


def test_the_budget_never_edits_the_business_config(budgeting):
    """節約設定は**設定ファイルを書き換えません。**

    書き換えてしまうと、budget.mode を消しても元に戻りません。「1 つの
    スイッチで戻せる」がこの仕組みの要点なので、そこを見張ります。
    """
    from kaigyou_core import config as cfg

    on_disk = cfg.load_yaml(
        cfg.business_file("competitors.yaml", "dental_clinic"))["survey"]
    effective = cfg.competitors_config("dental_clinic")["survey"]
    assert on_disk["max_competitors"] == 12          # ファイルは本来の値のまま
    assert effective["max_competitors"] == 2         # 効いている値は節約後
    # 節約設定に無い項目は素通しであること。
    assert effective["radius_m"] == on_disk["radius_m"]


def test_the_vocabulary_lives_in_config_not_in_code():
    """語彙をコードに埋めないこと。**業態を足すのは設定ファイル 1 枚**です。

    確かめ方はソースの grep ではなく**出力**です。コメントに「インプラント」と
    書いてあるのは構いません。困るのは、飲食の設定で動かしたときに歯科の語が
    出てくることです。
    """
    import json
    import pathlib

    ramen = {
        "label": "飲食店",
        "products": ["ラーメン", "定食", "カフェ"],
        "segments": ["学生", "勤務者"],
        "place_attributes": [{"key": "parking", "label": "駐車場"}],
        "positioning_map": {
            "x": {"label": "価格帯", "low": "低価格帯", "high": "高価格帯"},
            "y": {"label": "品揃え", "low": "総合", "high": "専門店"},
            "scale": [-2, -1, 0, 1, 2]},
    }
    shops = [{"name": "麺屋A", "distance_m": 120, "products": ["ラーメン"],
              "target": ["学生"], "map_placed": True, "map_x": -1, "map_y": 1,
              "map_basis": "券売機"}]
    out = json.dumps({"tally": competition.tally(shops, ramen),
                      "map": competition.positioning_map(shops, ramen)},
                     ensure_ascii=False)
    for dental in ("歯科", "インプラント", "自費", "保険", "診療", "医院"):
        assert dental not in out, dental

    conf = pathlib.Path(__file__).resolve().parents[2] / "config"
    assert (conf / "dental_clinic" / "competitors.yaml").exists()


# ------------------------------------------------------- 通しで動くか
@pytest.fixture
def conn():
    psycopg = pytest.importorskip("psycopg")
    from kaigyou_core.db import connect

    try:
        with connect() as c:
            with c.cursor() as cur:
                cur.execute("SELECT to_regclass('public.analysis_jobs') AS t")
                if cur.fetchone()["t"] is None:
                    pytest.skip("017_analysis_jobs.sql not applied")
                cur.execute("""
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'analysis_jobs'
                      AND column_name = 'analysis_kind'
                """)
                if cur.fetchone() is None:
                    pytest.skip("036_analysis_kind.sql not applied")
            yield c
            c.rollback()
    except psycopg.OperationalError as exc:
        pytest.skip(f"database unavailable: {exc}")


BASE_DATA = {
    "location": {"lat": 35.1, "lng": 138.9, "name": "沼津駅南"},
    "query": {"radius_m": 1000},
    "competition": {"clinics_in_radius": {"items": [
        {"name": c["name"], "distance_m": c["distance_m"], "address": "静岡県沼津市"}
        for c in COMPETITORS]}},
}


def test_a_competitor_job_gets_the_competitor_steps(conn):
    """種類ごとに段の枠が変わること。**4 段の枠は作られません。**"""
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.1, lng=138.9, radius_m=1000,
                             dataset=BASE_DATA, base_hash="c",
                             analysis_kind="competitors")
    steps = jobs.get_steps(conn, job_id)
    assert [s["step_number"] for s in steps] == [1, 2]
    assert steps[0]["step_name"] == "競合の調査"
    assert jobs.kind_of(conn, job_id) == "competitors"
    conn.rollback()


def test_the_worker_runs_the_competitor_steps_and_saves_that_report(conn, monkeypatch):
    """通しで 1 本。**LLM は呼ばず、段の割り当てと保存だけを見ます。**

    見たいのは、周辺一般の型で保存されないことです。落ちないので、間違えると
    見出しだけが並んだレポートが**成功として**残ります。
    """
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.1, lng=138.9, radius_m=1000,
                             dataset=BASE_DATA, base_hash="c",
                             analysis_kind="competitors")
    survey = {"competitors": COMPETITORS, "surveyed": 3, "requested": 3,
              "failed": [], "not_surveyed": 5, "total_in_radius": 8,
              "radius_m": 1000}
    summary = {**_summary_output()}

    monkeypatch.setattr(worker, "COMPETITOR_RUNNERS", {
        1: lambda _p, _c=None: (survey, llm.Usage(), []),
        2: lambda _p, _c=None: (summary, llm.Usage(), []),
    })
    jobs.claim_specific(conn, job_id)
    assert worker.advance(conn, job_id)["step"] == 1
    assert worker.advance(conn, job_id)["step"] == 2

    with conn.cursor() as cur:
        cur.execute("SELECT report_markdown FROM analysis_reports WHERE job_id = %s",
                    (job_id,))
        markdown = cur.fetchone()["report_markdown"]
    assert markdown.startswith("# 沼津駅南 周辺の競合分析")
    assert "## この分析で調べた範囲" in markdown
    # 周辺一般のレポートの見出しが混じっていないこと。
    assert "## この地域はどんな場所か" not in markdown
    conn.rollback()


def test_the_progress_shown_while_waiting_is_the_competitor_one(conn):
    """待っている間の表示。**「問い 0 件」と出さないこと。**

    問いを立てる段が無い分析に、周辺一般の数え方を当てると、立てなかった
    のではなく立てる段が無いだけなのに「0 件」と出ます。
    """
    from kaigyou_api.routers.intel import _competitor_progress

    steps = [{"step_number": 1, "status": "completed", "output_json": {
        "surveyed": 3, "requested": 4, "failed": [], "not_surveyed": 5,
        "total_in_radius": 8}}]
    first = _competitor_progress(steps)
    assert first["surveyed"] == 3 and first["not_surveyed"] == 5
    # STEP2 が済むまでは null。0 と書くと「近くに1件も無い」に見えます。
    assert first["within_near"] is None

    steps.append({"step_number": 2, "status": "completed",
                  "output_json": _summary_output()})
    second = _competitor_progress(steps)
    assert second["within_near"] == 1
    assert second["placed"] == 2 and second["undecided"] == 1


# --------------------------------------------------- 時間切れの扱い（実測）
def test_a_slow_competitor_does_not_take_the_whole_step_down(monkeypatch):
    """**区切りを越えたら、まだ始めていない医院には手を付けません。**

    実測：12 院の調査が関数の上限（800秒）を越えて殺され、調べ終えていた
    ぶんも一緒に消えました。やり直してまた同じところで殺されました。画面には
    「STEP1 実行中 12分11秒 ／ やり直し 1回」とだけ出ていて、止まっているのか
    進んでいるのか分かりません。

    8 院調べて「4 院は時間切れ」と書くほうが、12 院ぶんの費用を捨てるより
    良い、という判断です。
    """
    import time

    from kaigyou_intel.steps import comp1_survey

    clock = {"now": 1000.0}
    monkeypatch.setattr(comp1_survey.time, "monotonic", lambda: clock["now"])

    def slow(target, payload, system, per, category, deadline=None):
        if comp1_survey._out_of_time(deadline):
            return comp1_survey._Surveyed(str(target["name"]), skipped=True)
        clock["now"] += 40.0          # 1 院 40 秒かかることにする
        return comp1_survey._Surveyed(str(target["name"]),
                                      competitor=_competitor(target["name"]))

    monkeypatch.setattr(comp1_survey, "_survey_one", slow)
    targets = [{"name": f"第{i}歯科", "distance_m": i * 50} for i in range(6)]
    # 100 秒しかない → 2 院で打ち切り。
    results = comp1_survey._survey_all(targets, {}, "sys", 2, 1, "dental_clinic",
                                       deadline=1100.0)
    assert sum(1 for r in results if r.competitor) == 3
    assert [r.name for r in results if r.skipped] == ["第3歯科", "第4歯科", "第5歯科"]


def test_running_out_of_time_before_the_first_one_is_not_a_failure(monkeypatch):
    """1 件も始められなかったのは**失敗ではありません。**

    失敗として記録すると、やり直し回数を 1 つ食います。上限に当たれば打ち切り
    です。つまり**いちばん時間のかかる商圏が、いちばん試行回数を使えない**
    ことになります。
    """
    from kaigyou_intel.steps import comp1_survey

    monkeypatch.setattr(comp1_survey, "_survey_one",
                        lambda t, *a, **k: comp1_survey._Surveyed(
                            str(t["name"]), skipped=True))
    payload = {"competitors": [{"name": "A歯科", "distance_m": 100}],
               "radius_m": 1000, "vocabulary": {}, "_残り秒": 0.0}
    with pytest.raises(comp1_survey.OutOfTime):
        comp1_survey.run(payload)


def test_the_competitors_it_ran_out_of_time_on_are_named(monkeypatch):
    """時間切れは、上限で切ったのとは**理由が違います。**

    上限は決めた方針、時間切れは事故です。もう一度走らせれば結果が変わり
    うるのは後者だけなので、レポートで区別します。
    """
    from kaigyou_intel.competitor_report import to_markdown
    from kaigyou_intel.steps import comp1_survey

    done = {"A歯科"}

    def one(target, *a, **k):
        name = str(target["name"])
        return (comp1_survey._Surveyed(name, competitor=_competitor(name))
                if name in done else comp1_survey._Surveyed(name, skipped=True))

    monkeypatch.setattr(comp1_survey, "_survey_one", one)
    payload = {"competitors": [{"name": n, "distance_m": 100}
                               for n in ("A歯科", "B歯科", "C歯科")],
               "radius_m": 1000, "vocabulary": {}, "not_surveyed": 2,
               "total_in_radius": 7}
    output, _usage, _sources = comp1_survey.run(payload)
    assert output["surveyed"] == 1
    assert output["out_of_time"] == ["B歯科", "C歯科"]
    # 上限で切った 2 件に、時間切れの 2 件を足して 4 件。
    assert output["not_surveyed"] == 4

    markdown = to_markdown(
        {**_summary_output(),
         "coverage": {"surveyed": 1, "not_surveyed": 4, "total_in_radius": 7,
                      "radius_m": 1000, "out_of_time": ["B歯科", "C歯科"]}},
        DATASET)
    assert "時間切れで手を付けられなかった：**2 件**" in markdown
    assert "もう一度実行すると調べられることがあります" in markdown


def _competitor(name: str):
    from kaigyou_intel.schemas import Competitor

    return Competitor(name=name)


def test_every_call_has_a_ceiling_shorter_than_the_function_limit():
    """**SDK の既定に任せません。**

    anthropic SDK の既定は 600 秒 × やり直し 2 回で、1 回の呼び出しが最大
    1,800 秒かかりえます。関数の上限は 800 秒です。呼び出しが 1 本詰まる
    だけで、その段はまるごと失われます。
    """
    from kaigyou_core import config as cfg
    from kaigyou_intel.client import _request_limits

    timeout, retries = _request_limits()
    invocation = float((cfg.analysis_config().get("worker") or {})
                       .get("invocation_seconds", 800))
    assert timeout * (retries + 1) < invocation, (
        f"1 呼び出しの最悪 {timeout * (retries + 1):.0f} 秒 "
        f"≥ 関数の上限 {invocation:.0f} 秒")


def test_the_survey_fits_the_invocation_in_waves():
    """波の数 × 1 波の最悪が、関数の上限に収まること。

    12 院を 4 本ずつだと 3 波です。1 波の長さはその波のいちばん遅い医院で
    決まり、1 院あたり 2 呼び出しかかります。
    """
    import math

    from kaigyou_core import config as cfg
    from kaigyou_intel.client import _request_limits

    survey = cfg.competitors_config("dental_clinic")["survey"]
    worker = cfg.analysis_config().get("worker") or {}
    timeout, retries = _request_limits()
    waves = math.ceil(survey["max_competitors"] / survey["parallel"])
    worst = waves * 2 * timeout * (retries + 1)     # 1 院 = 調査 + 構造化
    assert worst < float(worker.get("invocation_seconds", 800)) * 3, (
        f"最悪 {worst:.0f} 秒。波 {waves} 回では、区切りが無いと収まりません")


# ------------------------------------------------------------ 使う量の上限
@pytest.fixture
def budgeting(monkeypatch):
    """この節のテストだけ、節約設定を**明示的に効かせます。**

    conftest がセッション全体で切っています（他のテストが見たいのは本来の
    設定なので）。ここで見たいのは切り替えの仕組みそのものなので、入れ直します。
    """
    from kaigyou_core import config as cfg

    monkeypatch.delenv("KAIGYOU_BUDGET_MODE", raising=False)
    cfg._CACHE.clear()
    yield cfg.budget_mode()
    cfg._CACHE.clear()


def test_the_budget_profile_actually_lowers_every_step(budgeting):
    """節約設定は**全段に効くこと。**

    段に `effort: high` と書いてあるので、モデル既定を下げるだけでは効きません
    ——段の指定が勝ちます。節約中は段の指定ごと外します。
    """
    from kaigyou_core import config as cfg
    from kaigyou_intel import client as llm

    assert cfg.budget_mode() == "mvp", "config/analysis.yaml の budget.mode"
    for kind, numbers in (("area", (1, 2, 3, 4)), ("competitors", (1, 2))):
        llm.use_kind(kind)
        for number in numbers:
            settings = llm.step_settings(number)
            assert settings["effort"] == "low", f"{kind} STEP{number}"
            assert settings["effort_structure"] == "low", f"{kind} STEP{number}"
            assert settings["max_tokens"] <= 8_000, f"{kind} STEP{number}"
    llm.use_kind("area")


def test_turning_the_budget_off_restores_the_real_settings(monkeypatch):
    """**mode を消せば元に戻ること。** 書き換えではなく重ねる作りの要点です。"""
    from kaigyou_core.config import _with_budget

    monkeypatch.delenv("KAIGYOU_BUDGET_MODE", raising=False)

    raw = {
        "model": {"id": "m", "effort": "high", "max_tokens": 64000},
        "limits": {"max_patterns": 4, "research_rounds": 2},
        "steps": {1: {"effort": "medium", "web_search": False},
                  2: {"effort": "high", "web_search": True}},
        "budget": {"mode": "mvp",
                   "mvp": {"effort": "low", "max_tokens": 6000,
                           "max_patterns": 2, "research_rounds": 1}},
    }
    saving = _with_budget(raw)
    assert saving["model"]["effort"] == "low"
    assert saving["limits"] == {"max_patterns": 2, "research_rounds": 1}
    assert "effort" not in saving["steps"][2]      # 段の指定ごと外す
    assert saving["steps"][2]["web_search"] is True   # 検索の有無は触らない

    # 環境変数でも切れること。**設定ファイルを編集させない口**が要ります。
    monkeypatch.setenv("KAIGYOU_BUDGET_MODE", "")
    assert _with_budget(raw)["model"]["effort"] == "high"
    monkeypatch.delenv("KAIGYOU_BUDGET_MODE")

    off = _with_budget({**raw, "budget": {**raw["budget"], "mode": None}})
    assert off["model"]["effort"] == "high"
    assert off["limits"]["max_patterns"] == 4
    assert off["steps"][1]["effort"] == "medium"


def test_a_typo_in_the_budget_profile_cannot_silently_do_nothing(budgeting):
    """**白紙委任にしません。**

    budget の下に何を書いても効く作りにすると、打ち間違えた項目が黙って
    無視され、節約したつもりで満額請求されます。効く項目は決めてあります。
    """
    from kaigyou_core import config as cfg

    assert not set(_config_budget_profile()) - cfg.BUDGET_KEYS

    # 打ち間違いは**黙って無視されず、起動時に分かる**こと。
    with pytest.raises(cfg.UnknownBudgetKey) as caught:
        cfg._with_budget({"budget": {"mode": "m", "m": {"efort": "low"}}})
    assert "efort" in str(caught.value)


def _config_budget_profile() -> dict:
    from kaigyou_core import config as cfg

    budget = cfg.load_yaml(cfg.config_dir() / "analysis.yaml").get("budget") or {}
    return budget.get(budget.get("mode")) or {}


def test_the_estimate_says_it_is_a_ceiling_not_a_measurement(budgeting):
    """見積もりを実測に見せないこと。**実際にはこれより安く済みます。**"""
    from kaigyou_intel import budget

    est = budget.estimate("dental_clinic")
    assert est["budget_mode"] == "mvp"
    assert "上限の見積もり" in est["note"]
    area, competitors = est["runs"]
    # 節約中は、どちらも 1 ドル未満に収まっていること。
    assert area["usd"] < 1.0 and competitors["usd"] < 1.0
    # 競合は 1 院 2 呼び出し。設定の院数と噛み合っていること。
    from kaigyou_core import config as cfg

    n = cfg.competitors_config("dental_clinic")["survey"]["max_competitors"]
    assert competitors["calls"] == 2 * n + 1


def test_the_screen_is_told_that_the_analysis_was_cheapened(budgeting):
    """**黙って質を落としません。**

    質を落とした結果を本来の結果として読まれるのが、いちばん困ります。
    """
    from kaigyou_core import config as cfg

    assert cfg.budget_mode() == "mvp"
    panel = (pathlib.Path(__file__).resolve().parents[2]
             / "web" / "src" / "components" / "AnalysisPanel.tsx").read_text()
    assert "budget_mode" in panel
    assert "本来のものより落ちます" in panel
