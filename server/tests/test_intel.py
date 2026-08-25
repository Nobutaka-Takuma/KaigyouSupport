"""商圏インテリジェンス・エンジン（Phase 1〜2）。

ここで守りたいのは、要件定義書がいちばん強く禁じていることです。

**入力に無い数字を出さない**（§3 原則2）。LLM にパーセンタイルを作らせない
仕組みになっているか、出力の数字が入力に実在するかを見ます。

**根拠が辿れる**（§25）。FACT が実在の指標を、PATTERN が実在の FACT を
指しているか。スキーマは形しか保証しないので、参照はこちらで確かめます。

**ステップが独立している**（§32）。Step2 が落ちても Step1 は残り、Step2 から
やり直せるか。

LLM は呼びません（API キーが要るうえ、呼ぶたびに結果が変わるとテストに
なりません）。呼び出しの境界を差し替えて、その前後を検証します。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kaigyou_core import config as cfg
from kaigyou_intel import client as llm
from kaigyou_intel.projection import (
    allowed_numbers,
    base_data_hash,
    for_step1,
    for_step2,
    for_step4,
    to_jsonable,
)
from kaigyou_intel.schemas import Fact, Pattern, Step1Output, verify_step1

REPO_ROOT = Path(__file__).resolve().parents[2]


# ------------------------------------------------------------------ 設定
def test_every_step_is_configured_with_its_prompt():
    """実装済みのステップには、プロンプトの実体があること。

    未実装のぶんは名前だけ先に置いてあります。実装した番号を RUNNERS に足すと、
    このテストがその番号のプロンプトを要求します。
    """
    from kaigyou_intel.jobs import STEP_NAMES
    from kaigyou_intel.worker import RUNNERS

    config = cfg.analysis_config()
    assert set(config["steps"]) == set(STEP_NAMES)
    for number, step in config["steps"].items():
        assert step["prompt_version"], f"step{number} に prompt_version がありません"
        if number not in RUNNERS:
            continue
        assert (cfg.config_dir() / "prompts" / step["prompt"]).is_file(), (
            f"step{number} のプロンプトが見つかりません: {step['prompt']}")


def test_the_searching_step_has_a_second_prompt_for_writing_it_down():
    """Web検索と構造化出力は同じ呼び出しでは併用しないので、STEP2 は 2 本必要。"""
    step = cfg.analysis_config()["steps"][2]
    assert (cfg.config_dir() / "prompts" / step["prompt_structure"]).is_file()
    assert llm.step_settings(2)["prompt_structure"] == step["prompt_structure"]


def test_only_step2_may_search_the_web():
    """要件 §38：外部コンテクスト調査を STEP2 に限定する。

    STEP1 で外部情報が混ざると、FACT と EXTERNAL FACT の区別が最初の段階で
    壊れます。STEP4 で足せると、§16 の「新しい外部事実を追加しない」が破れます。
    """
    steps = cfg.analysis_config()["steps"]
    assert steps[2]["web_search"] is True
    for number in (1, 3, 4, 5):
        assert steps[number].get("web_search") is False, (
            f"STEP{number} で Web 検索が有効になっています")


def test_the_search_source_priority_matches_the_requirement():
    """要件 §9 の優先順位が、設定として存在すること。"""
    types = [s["type"] for s in cfg.analysis_config()["search"]["source_types"]]
    assert types[:5] == ["government", "statistics", "prefecture",
                         "municipality", "public_body"]


def test_source_type_is_decided_by_the_url_not_by_the_model():
    """機械的に決まることを LLM に判定させない。"""
    from kaigyou_intel.jobs import classify_source

    assert classify_source("https://www.mlit.go.jp/a") == "government"
    assert classify_source("https://www.pref.shizuoka.jp/b") == "prefecture"
    assert classify_source("https://www.city.susono.shizuoka.jp/c") == "municipality"
    assert classify_source("https://www.u-tokyo.ac.jp/d") == "academic"
    assert classify_source("https://example.com/e") == "other"


# ------------------------------------------------------------------ 射影
@pytest.fixture(scope="module")
def dataset():
    psycopg = pytest.importorskip("psycopg")
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app
    from kaigyou_core.db import connect

    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM mesh_scores")
            if cur.fetchone()["n"] == 0:
                pytest.skip("mesh scores not computed here")
    except psycopg.OperationalError as exc:
        pytest.skip(f"database unavailable: {exc}")

    response = TestClient(app).get("/api/dataset", params={
        "lat": 35.6717, "lng": 139.7650, "radius": 1000,
        "profile": "pediatric", "max_clinics": 0})
    assert response.status_code == 200
    return response.json()


def test_step1_is_given_the_benchmarks_rather_than_asked_to_compute_them(dataset):
    """§3 原則2 を仕組みで守る。

    パーセンタイル・順位・position_label を入力に含めます。含めなければ LLM は
    自分で作るしかなく、作ったものは入力に無い数字です。
    """
    payload = for_step1(dataset)
    measures = {m["key"]: m for m in payload["measures"]}
    placed = [m for m in measures.values() if m.get("value") is not None]
    assert placed
    for measure in placed:
        assert "unit" in measure and "source" in measure
        if measure.get("rank") is not None:
            assert measure.get("position_label"), f"{measure['key']} に position_label が無い"
            assert measure.get("benchmark_type")


def test_step1_is_not_given_the_raw_percentile(dataset):
    """端で逆の意味に読める数字は渡さない。

    銀座の人口増減率は中央区内で最下位で、percentile は 0.0 です。それを
    渡したところ、モデルは position_label（「下位0.4%」）ではなく percentile から
    文を作り、「下位0%」と書きました。0% の集合には誰も入れません。

    プロンプトには「percentile から作るな」と既に書いてありました。書いてある
    ことより、無いことのほうが強い。
    """
    payload = for_step1(dataset, {"all_benchmarks": True})
    for measure in payload["measures"]:
        assert "percentile" not in measure
        assert "top_share_pct" not in measure
        for benchmark in measure.get("benchmarks") or []:
            assert "percentile" not in benchmark, measure["key"]
            assert "top_share_pct" not in benchmark, measure["key"]
        # 位置そのものは落としません。落とすと今度は何も言えなくなります。
        if measure.get("rank") is not None:
            assert measure.get("position_label") and measure.get("of")


def test_step1_keeps_every_reference_class(dataset):
    """母集団6つの比較は削らない。

    最初の実装は「大きいものを削る」で代表1つに間引いていました。ですがそこは
    いちばん削ってはいけない場所でした。銀座の 0〜14歳人口は、県内では
    下位24.3%（typical）なのに、近隣・同一区・同規模の商圏と比べると
    下位2.4〜4.5%（very_low）です。**食い違いこそがパターン**で、代表1つでは
    見えません。

    削る基準は「大きいもの」ではなく「発見に寄与しないもの」です。
    """
    payload = for_step1(dataset, {"all_benchmarks": True})
    placed = [m for m in payload["measures"]
              if m.get("value") is not None and m.get("rank") is not None]
    assert placed
    for measure in placed:
        assert measure.get("benchmarks"), f"{measure['key']} の比較が落ちています"
    # 母集団が複数あることが、この設計の要点。
    assert max(len(m["benchmarks"]) for m in placed) >= 3


def test_step1_still_drops_what_does_not_help(dataset):
    """要件 §34：元JSONを毎回丸ごと渡さない。

    医院20件の名前と住所は14KBありますが、パターン発見には効きません
    （効くのは by_specialty と hours の集計のほう）。
    """
    whole = len(json.dumps(to_jsonable(dataset), ensure_ascii=False).encode())
    projected = len(json.dumps(
        for_step1(dataset, {"all_benchmarks": True}), ensure_ascii=False).encode())
    assert projected < whole, "何も削れていません"
    assert "clinics" not in for_step1(dataset, {})["competition"]


def test_the_whole_dataset_can_be_sent_when_asked(dataset):
    """比較実験のための逃げ道。既定ではありません。"""
    payload = for_step1(dataset, {"full_dataset": True})
    assert set(payload) == set(to_jsonable(dataset))


def test_turning_off_all_benchmarks_actually_turns_them_off(dataset):
    """設定の欄が読まれずに落ちていないこと。

    all_benchmarks は読み取ってはいたものの、_measure に渡し忘れていました。
    既定が True なので出力は正しく、設定だけが黙って効かない状態でした。
    """
    trimmed = for_step1(dataset, {"all_benchmarks": False})
    assert all("benchmarks" not in m for m in trimmed["measures"])
    # 代表1つ（平坦な benchmark_*）は残ります。
    assert any(m.get("position_label") for m in trimmed["measures"])
    full = for_step1(dataset, {"all_benchmarks": True})
    assert any(m.get("benchmarks") for m in full["measures"])


def test_what_step1_sees_is_configuration_not_code():
    """何を渡すかは config/analysis.yaml で変えられること。"""
    projection = cfg.analysis_config().get("projection") or {}
    assert projection.get("all_benchmarks") is True
    assert projection.get("clinic_list") is False
    assert projection.get("full_dataset") is False


def test_the_clinic_list_is_not_sent_to_step1(dataset):
    """50件の医院名と住所は 34KB あって、パターン発見には寄与しません。"""
    payload = for_step1(dataset, {})
    assert "clinics" not in payload["competition"]
    assert payload["competition"]["by_specialty"] is not None
    assert payload["competition"]["hours"] is not None


def test_step1_keeps_the_reasons_a_comparison_was_withheld(dataset):
    """significance が無い理由を落とさない。

    落とすと「平凡だった」と読まれ、裾野のような地域で誤った断定が出ます。
    """
    payload = for_step1(dataset)
    for measure in payload["measures"]:
        if measure.get("value") is not None and measure.get("rank") is None:
            assert measure.get("benchmark_unavailable_reason")


def test_step1_keeps_the_gaps(dataset):
    """gaps は「確認できていないこと」。ここが唯一「未確認」と言える場所。"""
    payload = for_step1(dataset)
    for insight in payload["insight_metrics"]:
        assert "gaps" in insight and "complete" in insight
        # 成分の値は measures にあるので再掲しない。
        assert "components" not in insight
        assert "component_keys" in insight


def test_step2_is_not_given_the_base_data(dataset):
    """渡さなかったものについては何も言えない。

    base_data を渡すと、外部情報を調べずに手元の数字を言い換えたものが
    「外部事実」として返ってきます。
    """
    step1 = {"patterns": [{"id": "P001", "title": "t", "evidence_summary": "s",
                           "importance": "high", "research_questions": ["q"]}]}
    payload = for_step2(step1, dataset["location"], {"max_patterns": 5})
    assert "measures" not in payload
    assert "competition" not in payload
    assert payload["patterns"][0]["id"] == "P001"


def test_step2_respects_the_pattern_limit(dataset):
    """要件 §34：PATTERN 最大5個。"""
    step1 = {"patterns": [{"id": f"P{i:03d}", "title": "t", "evidence_summary": "s",
                           "importance": "high", "research_questions": ["q"]}
                          for i in range(1, 12)]}
    payload = for_step2(step1, dataset["location"], {"max_patterns": 5})
    assert len(payload["patterns"]) == 5


def test_step4_gets_conclusions_not_raw_material(dataset):
    """要件 §16：STEP4 で新しい外部事実を足さない。

    足せないようにするには、足せるだけの材料を渡さないのが確実です。
    """
    payload = for_step4({"facts": []}, {"external_facts": []}, {"insights": []}, dataset)
    assert "items" not in payload["competition"]
    assert payload["step2"] == {"external_facts": []}


def test_the_hash_ignores_the_timestamp(dataset):
    """同一地点・同一データの再実行を見分けるため（§34 Cache）。

    generated_at を含めると毎回別物になり、キャッシュが永久に当たりません。
    """
    a = dict(dataset, generated_at="2026-01-01T00:00:00Z")
    b = dict(dataset, generated_at="2026-12-31T23:59:59Z")
    assert base_data_hash(a) == base_data_hash(b)
    c = dict(dataset)
    c["location"] = dict(c["location"], lat=99.0)
    assert base_data_hash(c) != base_data_hash(a)


def test_numbers_in_the_input_can_be_enumerated(dataset):
    """出力の検算に使う集合。入力に無い数字を見つけるための土台。"""
    numbers = allowed_numbers(for_step1(dataset))
    assert len(numbers) > 50
    population = next(m for m in for_step1(dataset)["measures"]
                      if m["key"] == "population")
    from kaigyou_intel.projection import _canonical
    assert _canonical(population["value"]) in numbers


# ------------------------------------------------- スキーマと根拠の追跡
def _output(**kwargs) -> Step1Output:
    base = dict(
        facts=[Fact(id="F001", statement="0〜14歳人口は1,088人", value=1088.6,
                    unit="人", measure_key="child_population",
                    position_label="下位24.3%", benchmark_type="prefecture"),
               Fact(id="F002", statement="構成比は8.2%", measure_key="child_share")],
        patterns=[Pattern(id="P001", title="絶対数も構成比も周辺並み",
                          evidence=["F001", "F002"], evidence_summary="…",
                          importance="medium", research_questions=["なぜか"])])
    base.update(kwargs)
    return Step1Output(**base)


def test_a_pattern_must_rest_on_at_least_two_facts():
    """単一の事実の言い換えは PATTERN ではありません（要件 §6）。"""
    with pytest.raises(Exception):
        Pattern(id="P001", title="t", evidence=["F001"], evidence_summary="s",
                importance="high", research_questions=["q"])


def test_a_fact_pointing_at_a_measure_that_does_not_exist_is_caught():
    """LLM が指標名を作ってしまった場合。スキーマは通ってしまう。"""
    output = _output(facts=[Fact(id="F001", statement="…",
                                 measure_key="median_household_income")])
    output.patterns[0].evidence = ["F001", "F001"]
    problems = verify_step1(output, {"child_population", "child_share"})
    assert any("median_household_income" in p.problem for p in problems)


def test_a_pattern_pointing_at_a_fact_that_does_not_exist_is_caught():
    """§25 の追跡は、参照が全部解決して初めて成立します。"""
    output = _output()
    output.patterns[0].evidence = ["F001", "F999"]
    problems = verify_step1(output, {"child_population", "child_share"})
    assert any("F999" in p.problem for p in problems)


def test_a_clean_output_has_no_problems():
    assert verify_step1(_output(), {"child_population", "child_share"}) == []


def test_the_step1_schema_does_not_ask_for_benchmarks():
    """要件 §7 からの変更点。ここが変わったら気づけるように。

    パーセンタイルは /api/dataset が算出済みで、FACT は measure_key で参照
    します。スキーマに benchmarks を戻すと、LLM が数字を作り始めます。
    """
    assert set(Step1Output.model_fields) == {"facts", "patterns", "not_determinable"}
    assert "measure_key" in Fact.model_fields


# ------------------------------------------------------------------ Job
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
            yield c
            c.rollback()
    except psycopg.OperationalError as exc:
        pytest.skip(f"database unavailable: {exc}")


def test_a_new_job_has_every_step_pending(conn, dataset):
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    steps = jobs.get_steps(conn, job_id)
    assert [s["step_number"] for s in steps] == sorted(jobs.STEP_NAMES)
    assert all(s["status"] == "pending" for s in steps)
    assert jobs.next_step(conn, job_id) == 1
    conn.rollback()


def test_a_failed_step_does_not_erase_the_completed_ones(conn, dataset):
    """要件 §32：Step2 から再実行できること。

    最初からやり直させると、Web検索の費用も待ち時間ももう一度かかります。
    """
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    jobs.start_step(conn, job_id, 1, {"in": 1}, {"prompt_version": "v", "model": "m"})
    jobs.finish_step(conn, job_id, 1, {"facts": []},
                     {"input_tokens": 10, "output_tokens": 5, "web_searches": 0})
    jobs.fail_step(conn, job_id, 2, "検索に失敗しました")

    steps = {s["step_number"]: s for s in jobs.get_steps(conn, job_id)}
    assert steps[1]["status"] == "completed"
    assert steps[1]["output_json"] == {"facts": []}
    assert steps[2]["status"] == "failed"
    assert jobs.next_step(conn, job_id) == 2
    conn.rollback()


def test_an_unimplemented_step_is_left_pending_not_marked_failed(
        conn, dataset, monkeypatch):
    """未実装は失敗ではない。

    最初の実装は STEP2 を failed で記録していました。画面には赤い「失敗」と
    エラー本文が出るので、実行して壊れたように見えます。実際には一度も
    呼ばれていません。pending のままにしておけば、RUNNERS に足した次の
    `analyze --once` がそのまま続きから拾います。
    """
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    unimplemented = _pretend_the_last_step_is_unimplemented(monkeypatch)
    _complete_the_steps_before(conn, job_id, unimplemented)

    with pytest.raises(worker.StepNotImplemented):
        worker.run_step(conn, job_id, unimplemented)

    steps = {s["step_number"]: s for s in jobs.get_steps(conn, job_id)}
    assert steps[unimplemented]["status"] == "pending"
    assert not steps[unimplemented]["error_message"]
    assert jobs.next_step(conn, job_id) == unimplemented
    conn.rollback()


def test_retrying_a_step_also_clears_the_steps_after_it(conn, dataset):
    """後続だけ残すと、古い前提の上に新しい結論が乗ります。"""
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    for number in (1, 2, 3):
        jobs.start_step(conn, job_id, number, {}, {"prompt_version": "v", "model": "m"})
        jobs.finish_step(conn, job_id, number, {"n": number}, {})

    jobs.reset_step(conn, job_id, 2)
    steps = {s["step_number"]: s for s in jobs.get_steps(conn, job_id)}
    assert steps[1]["status"] == "completed", "STEP1 まで消してはいけない"
    assert steps[2]["status"] == "pending"
    assert steps[3]["status"] == "pending" and steps[3]["output_json"] is None
    assert jobs.next_step(conn, job_id) == 2
    conn.rollback()


def test_two_workers_do_not_claim_the_same_job(conn, dataset):
    """同じジョブを2回分析すると、費用が2倍で結果は1つしか残りません。"""
    from kaigyou_core.db import connect
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="claimtest")
    conn.commit()
    try:
        with connect() as a, connect() as b:
            first = jobs.claim_job(a)
            second = jobs.claim_job(b)
            assert first is not None
            assert second != first
    finally:
        with connect() as cleanup, cleanup.cursor() as cur:
            cur.execute("DELETE FROM analysis_jobs WHERE id = %s", (job_id,))
            cleanup.commit()


# ------------------------------------------------------ LLM を差し替えて通す
def test_step1_runs_end_to_end_with_a_stubbed_model(dataset, monkeypatch):
    """API キー無しで、呼び出しの前後を通す。

    プロンプトの組み立て・射影・構造化出力の検証・参照の追跡まで、LLM 以外の
    全部がここを通ります。
    """
    from kaigyou_intel.steps import step1_features

    captured: dict[str, object] = {}

    def fake_ask(*, step_number, system, user, schema=None, tools=None):
        captured.update(step_number=step_number, system=system, user=user,
                        schema=schema, tools=tools)
        return llm.Result(parsed=_output(), text="",
                          usage=llm.Usage(input_tokens=1000, output_tokens=200),
                          model="claude-opus-5")

    monkeypatch.setattr(llm, "ask", fake_ask)
    output, usage, sources = step1_features.run(step1_features.build_input(dataset))

    assert captured["step_number"] == 1
    assert captured["tools"] is None, "STEP1 に道具を渡してはいけない（Web検索禁止）"
    assert captured["schema"] is Step1Output
    # プロンプトに上限が埋め込まれていること。
    assert "最大 5 個" in captured["system"]
    assert "{max_patterns}" not in captured["system"]
    assert usage.input_tokens == 1000
    assert output["facts"][0]["measure_key"] == "child_population"
    assert sources == []


def test_step1_refuses_an_output_whose_references_do_not_resolve(dataset, monkeypatch):
    """参照が切れた出力は保存しない。レポートの末尾まで残ると追跡が切れます。"""
    from kaigyou_intel.steps import step1_features

    broken = _output()
    broken.patterns[0].evidence = ["F001", "F404"]
    monkeypatch.setattr(llm, "ask", lambda **kw: llm.Result(
        parsed=broken, usage=llm.Usage(), model="m"))

    with pytest.raises(step1_features.StepFailed, match="F404"):
        step1_features.run(step1_features.build_input(dataset))


# ----------------------------------------------- PowerShell から使えること
def test_a_job_can_be_created_without_an_http_client():
    """ジョブ作成が CLI にもあること。

    PowerShell の `curl` は Invoke-WebRequest の別名で -X を受け付けません。
    このプロジェクトの操作は全部 `python -m kaigyou_etl` で済むので、
    ジョブ作成だけ HTTP クライアントを要求すると、そこが最初の障害になります。
    """
    import argparse

    from kaigyou_etl.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["new-analysis", "--lat", "35.6717", "--lng", "139.765",
                              "--name", "銀座4丁目", "--profile", "pediatric"])
    assert isinstance(args, argparse.Namespace)
    assert (args.lat, args.lng, args.radius) == (35.6717, 139.765, 1000)
    assert args.func.__name__ == "cmd_new_analysis"


def test_the_dry_run_can_target_one_job():
    """作った直後のジョブを見たいことがある。worker の順番とは別。"""
    from kaigyou_etl.cli import build_parser

    args = build_parser().parse_args(
        ["analyze", "--dry-run", "step1.txt", "--job", "abc-123"])
    assert args.dry_run == "step1.txt" and args.job == "abc-123"


# ------------------------------------------------------------ モデルの契約
#
# Sonnet 5 / Opus 5 系では、以前よく書かれていた指定のいくつかが**削除**され、
# 送ると 400 で落ちます。「前はこう書いた」で戻ってこないように固定します。

def test_the_configured_model_is_a_current_one():
    """モデルIDは実在するものだけ。日付サフィックスは付けない。"""
    known = {"claude-sonnet-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
             "claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5",
             "claude-fable-5"}
    config = cfg.analysis_config()
    assert config["model"]["id"] in known, f"未知のモデル: {config['model']['id']}"
    for number, step in config["steps"].items():
        if step.get("model"):
            assert step["model"] in known, f"step{number} のモデルが未知です"


def test_effort_is_one_of_the_documented_levels():
    config = cfg.analysis_config()
    levels = {"low", "medium", "high", "xhigh", "max"}
    assert config["model"]["effort"] in levels


def test_we_never_send_the_parameters_that_were_removed():
    """budget_tokens / temperature などは 400 になります。

    思考の深さは adaptive thinking と effort で決めます。固定のトークン予算と
    いう考え方はもうありません。
    """
    from kaigyou_intel import client as llm

    source = Path(llm.__file__).read_text(encoding="utf-8")
    request_block = source[source.index('request: dict[str, Any] = {'):
                            source.index('declared = list(tools or [])')]
    for parameter in llm.REMOVED_PARAMETERS:
        assert f'"{parameter}"' not in request_block, (
            f"{parameter} を送っています。このモデルでは 400 になります")


def test_thinking_is_adaptive():
    """このモデル系での唯一の有効な指定。"""
    from kaigyou_intel import client as llm

    source = Path(llm.__file__).read_text(encoding="utf-8")
    assert '"thinking": {"type": "adaptive"}' in source


def test_a_refusal_is_reported_rather_than_read_as_an_empty_answer(monkeypatch):
    """拒否は例外ではなく HTTP 200 で返る。

    content を読む前に stop_reason を確かめないと、空の応答を
    「モデルが何も見つけなかった」と取り違えます。
    """
    from kaigyou_intel import client as llm

    class _Details:
        category, explanation = "cyber", "declined"

    class _Message:
        stop_reason, stop_details, content = "refusal", _Details(), []
        usage = type("U", (), {"input_tokens": 10, "output_tokens": 0})()
        model = "claude-sonnet-5"

    message = _Message()
    message.parsed_output = None
    monkeypatch.setattr(llm, "_client", lambda: _stub_client(message))
    with pytest.raises(llm.Refused, match="cyber"):
        llm.ask(step_number=1, system="s", user="u", schema=Step1Output)


# --------------------------------------------- 送信する本体そのものを検算する
#
# 最初の実装は Pydantic の**クラス**を output_config.format に入れていて、
# 送信の瞬間に `Object of type ModelMetaclass is not JSON serializable` で
# 落ちました。呼び出しごとスタブしたテストでは、その形を一度も見ていません
# でした。以下は API キー無しで、本体の形だけを確かめます。

def test_the_request_body_can_actually_be_serialised():
    """これが通らなければ、キーがあっても送信で落ちます。"""
    from kaigyou_intel.client import build_request

    for step_number in (1, 2, 3, 4):
        body = build_request(step_number, "system", "user")
        json.dumps(body, ensure_ascii=False)   # 落ちたらそこが原因


def test_the_schema_never_goes_into_output_config():
    """スキーマは output_format に渡し、SDK が output_config へマージします。

    自分で output_config.format に入れると、型がそのまま送信されます。
    """
    from kaigyou_intel.client import build_request

    body = build_request(1, "system", "user")
    assert "format" not in body["output_config"]
    assert body["output_config"]["effort"] in {"low", "medium", "high", "xhigh", "max"}


def test_output_format_must_be_a_type_not_a_dict():
    """SDK の契約。ここを取り違えたのが今回の原因なので、明示的に置きます。"""
    anthropic = pytest.importorskip("anthropic")
    import inspect

    source = inspect.getsource(anthropic.resources.messages.messages.Messages.parse)
    assert "`output_format` must be a type" in source, (
        "SDK の契約が変わった可能性があります。client.ask の呼び方を確認してください")


def test_step1_sends_no_tools_and_step2_sends_web_search():
    """要件 §38：外部コンテクスト調査を STEP2 に限定する。

    設定だけでなく、組み立てた本体でも確かめます。設定が正しくても
    組み立てが無視していたら意味がありません。
    """
    from kaigyou_intel.client import build_request

    assert "tools" not in build_request(1, "s", "u")
    assert [t["type"] for t in build_request(2, "s", "u")["tools"]] == \
        ["web_search_20260209"]
    for step_number in (3, 4):
        assert "tools" not in build_request(step_number, "s", "u")


def _stub_client(message, seen: dict | None = None):
    """SDK の呼び出し口だけを模した最小のクライアント。

    ストリームの context manager までは真似ます。ここを省いて ``ask`` ごと
    差し替えたせいで、送信する本体を一度も見ないまま
    ``ModelMetaclass is not JSON serializable`` を出荷しました。
    """
    import contextlib

    class _Stream:
        @staticmethod
        def get_final_message():
            return message

    class _Client:
        class messages:
            @staticmethod
            @contextlib.contextmanager
            def stream(**kwargs):
                if seen is not None:
                    seen.update(kwargs)
                yield _Stream()

    return _Client()


def test_the_structured_call_passes_the_schema_as_output_format(monkeypatch):
    """呼び方を固定する。

    スキーマは型のまま ``output_format`` へ。``output_config.format`` に
    自分で入れると、型がそのまま送信されて落ちます。
    """
    from kaigyou_intel import client as llm

    seen: dict[str, object] = {}
    message = type("M", (), {})()
    message.parsed_output = _output()
    message.content, message.stop_reason = [], "end_turn"
    message.usage = type("U", (), {"input_tokens": 1, "output_tokens": 1})()
    message.model = "claude-sonnet-5"

    monkeypatch.setattr(llm, "_client", lambda: _stub_client(message, seen))
    llm.ask(step_number=1, system="s", user="u", schema=Step1Output)

    assert seen["output_format"] is Step1Output
    assert "format" not in seen["output_config"]
    json.dumps({k: v for k, v in seen.items() if k != "output_format"},
               ensure_ascii=False)


def test_every_call_is_streamed(monkeypatch):
    """SDK は「10 分を超えうる操作」を非ストリームで呼ぶと送信前に落とします。

    その境目は max_tokens で決まります。実測：16,000 → 24,000 に上げた途端、
    ValueError: Streaming is required for operations that may take longer than
    10 minutes。ストリームなら上限を気にせず上げられます。
    """
    from kaigyou_intel import client as llm

    message = type("M", (), {})()
    message.parsed_output = None
    message.content, message.stop_reason = [], "end_turn"
    message.usage = type("U", (), {"input_tokens": 1, "output_tokens": 1})()
    message.model = "claude-sonnet-5"

    client = _stub_client(message)
    assert not hasattr(client.messages, "parse"), (
        "parse() を使うと max_tokens しだいで送信前に落ちます")
    monkeypatch.setattr(llm, "_client", lambda: client)
    # スキーマ有り・無しのどちらもストリームで通ること。
    llm.ask(step_number=1, system="s", user="u", schema=Step1Output)
    llm.ask(step_number=2, system="s", user="u")


def test_the_step_output_can_be_read_without_an_http_client():
    """STEP の出力を読む手段があること。

    プロンプトを直すかどうかの判断はここを読んでするので、JSON を
    そのまま出すのでは足りません。
    """
    from kaigyou_etl.cli import build_parser

    args = build_parser().parse_args(["analyze", "--show"])
    assert args.show == ""          # 省略時は最新のジョブ
    args = build_parser().parse_args(["analyze", "--show", "abc-123"])
    assert args.show == "abc-123"


def test_the_cost_is_computed_from_the_recorded_usage():
    """使用量は保存してあるので、後から数えられること（要件 §34）。"""
    from kaigyou_etl.cli import _step_cost

    cost = _step_cost({"model": "claude-sonnet-5",
                       "input_tokens": 43102, "output_tokens": 3201})
    assert cost == pytest.approx(43102 / 1e6 * 2 + 3201 / 1e6 * 10)
    # 知らないモデルなら黙って 0 を返さない。
    assert _step_cost({"model": "unknown", "input_tokens": 1}) is None


# ------------------------------------------------------------------ STEP2
def _step1_for_step2() -> dict:
    return {"patterns": [
        {"id": "P001", "title": "子ども人口は薄いが小児歯科は多い",
         "evidence_summary": "0〜14歳は下位3.1%、小児歯科標榜は上位9%",
         "importance": "high",
         "research_questions": ["この地区で2015年以降に大規模な住宅供給があったか"]},
    ]}


def _step2_output(**overrides) -> "Step2Output":
    from kaigyou_intel.schemas import ExternalFact, Hypothesis, Step2Output

    data = {
        "external_facts": [ExternalFact(
            id="C001", pattern_id="P001",
            statement="中央区の年少人口は2015年から2020年に増加した",
            source_url="https://www.city.chuo.lg.jp/toukei/jinkou.html",
            source_title="中央区 人口統計", confidence="high")],
        "hypotheses": [Hypothesis(
            id="H001", pattern_id="P001",
            statement="タワーマンション供給が子育て世帯を呼び込んだ",
            status="SUPPORTED", evidence=["C001"],
            reasoning="区の統計で年少人口の増加が確認できる", confidence="medium")],
        "unanswered": ["小児歯科の開設年は確認できなかった"],
    }
    data.update(overrides)
    return Step2Output(**data)


def test_a_fabricated_source_url_is_caught():
    """モデルは実在しそうな URL を書けます。実在するかは検索結果で決まります。

    ここが STEP2 でいちばん重要な検算です。出典が実在しないレポートは、
    出典が無いレポートより悪い。読む人が確かめたつもりになるからです。
    """
    from kaigyou_intel.schemas import verify_step2

    output = _step2_output()
    problems = verify_step2(output, {"P001"},
                            {"https://www.city.chuo.lg.jp/kurashi/betsu.html"})
    assert any("検索結果に無い URL" in p.problem for p in problems)


def test_a_real_source_url_survives_the_usual_url_differences():
    """末尾のスラッシュや www の有無で本物の出典を落とさない。"""
    from kaigyou_intel.schemas import verify_step2

    output = _step2_output()
    assert verify_step2(output, {"P001"},
                        {"http://city.chuo.lg.jp/toukei/jinkou.html/"}) == []


def test_step2_references_must_resolve():
    from kaigyou_intel.schemas import Hypothesis, verify_step2

    urls = {"https://www.city.chuo.lg.jp/toukei/jinkou.html"}
    stray = _step2_output(hypotheses=[Hypothesis(
        id="H001", pattern_id="P404", statement="s", status="UNSUPPORTED",
        evidence=["C404"], reasoning="r", confidence="low")])
    problems = verify_step2(stray, {"P001"}, urls)
    assert any("P404" in p.problem for p in problems)
    assert any("C404" in p.problem for p in problems)


def test_an_unsupported_hypothesis_is_kept():
    """要件 §11：否定された仮説も保存する。

    「調べたが違った」は、調べていないのとは別の情報です。落とすと STEP3 が
    同じ筋を追い直します。
    """
    from kaigyou_intel.schemas import Hypothesis, verify_step2

    output = _step2_output(hypotheses=[Hypothesis(
        id="H001", pattern_id="P001", statement="再開発が原因である",
        status="UNSUPPORTED", evidence=["C001"],
        reasoning="区の資料では当該期間の大規模開発は確認できなかった",
        confidence="medium")])
    assert verify_step2(output, {"P001"},
                        {"https://www.city.chuo.lg.jp/toukei/jinkou.html"}) == []
    assert output.model_dump()["hypotheses"][0]["status"] == "UNSUPPORTED"


def test_step2_searches_once_and_then_writes_it_down(monkeypatch):
    """検索する呼び出しと、JSON に写す呼び出しを分ける。

    Web検索（サーバ側ツール）と構造化出力は同じ呼び出しでは併用しません。
    2 回目に検索を残すと、1 回目に無かった事実が増えて、出典の照合が
    意味を失います。
    """
    from kaigyou_intel.steps import step2_research

    calls: list[dict] = []

    def fake_ask(*, step_number, system, user, schema=None, tools=None,
                 web_search=None):
        calls.append({"system": system, "user": user, "schema": schema,
                      "web_search": web_search})
        if schema is None:
            return llm.Result(
                parsed=None, text="中央区の年少人口は増加していました。",
                usage=llm.Usage(input_tokens=500, output_tokens=800, web_searches=3),
                model="claude-sonnet-5",
                sources=[{"url": "https://www.city.chuo.lg.jp/toukei/jinkou.html",
                          "title": "中央区 人口統計", "page_age": None},
                         {"url": "https://www.e-stat.go.jp/x", "title": "e-Stat",
                          "page_age": None}])
        return llm.Result(parsed=_step2_output(),
                          usage=llm.Usage(input_tokens=700, output_tokens=400),
                          model="claude-sonnet-5")

    monkeypatch.setattr(llm, "ask", fake_ask)
    payload = step2_research.build_input(_step1_for_step2(), {"location": {"name": "銀座"}})
    output, usage, sources = step2_research.run(payload)

    assert len(calls) == 2
    assert calls[0]["schema"] is None and calls[0]["web_search"] is None
    assert calls[1]["schema"] is not None
    assert calls[1]["web_search"] is False, "2 回目で検索を切っていない"
    # 取得した URL の一覧を 2 回目に見せること。無いものを書かせないため。
    assert "https://www.e-stat.go.jp/x" in calls[1]["user"]
    # 上限がプロンプトに埋まっていること。
    assert "{searches_per_pattern}" not in calls[0]["system"]

    assert usage.input_tokens == 1200 and usage.output_tokens == 1200
    assert usage.web_searches == 3
    assert output["hypotheses"][0]["status"] == "SUPPORTED"

    # 出典は「どの PATTERN を調べていて出てきたか」まで残す（§25）。
    cited = {s["url"]: s["pattern_id"] for s in sources}
    assert cited["https://www.city.chuo.lg.jp/toukei/jinkou.html"] == "P001"
    assert cited["https://www.e-stat.go.jp/x"] is None, "引用されなかった出典も残す"


def test_step2_refuses_an_output_citing_a_url_it_never_retrieved(monkeypatch):
    from kaigyou_intel.steps import step2_research

    def fake_ask(*, step_number, system, user, schema=None, tools=None,
                 web_search=None):
        if schema is None:
            return llm.Result(parsed=None, text="調べました。", usage=llm.Usage(),
                              model="m", sources=[{"url": "https://example.com/a"}])
        return llm.Result(parsed=_step2_output(), usage=llm.Usage(), model="m")

    monkeypatch.setattr(llm, "ask", fake_ask)
    payload = step2_research.build_input(_step1_for_step2(), {"location": {}})
    with pytest.raises(step2_research.StepFailed, match="検索結果に無い URL"):
        step2_research.run(payload)


def test_the_search_call_declares_the_web_search_tool_and_the_write_up_does_not():
    """設定で web_search を切り替えられること。送信する本体で確かめます。"""
    tooled = llm.build_request(2, "s", "u")
    assert tooled["tools"][0]["type"] == llm.WEB_SEARCH_TOOL_TYPE
    assert tooled["tools"][0]["max_uses"] == cfg.analysis_config()["limits"][
        "max_searches_total"]
    assert "tools" not in llm.build_request(2, "s", "u", web_search=False)
    # 本体はそのまま JSON にできること（ModelMetaclass 事件の再発防止）。
    json.dumps(tooled)


def test_step2_input_is_built_from_the_stored_step1_output(conn, dataset):
    """worker は記憶を持たない。前段の出力は DB から読む。"""
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    jobs.start_step(conn, job_id, 1, {}, {"prompt_version": "v", "model": "m"})
    jobs.finish_step(conn, job_id, 1, _step1_for_step2(), {})

    job = jobs.get_job(conn, job_id, include_base_data=True)
    payload = worker.build_input(conn, job, 2)
    assert payload["patterns"][0]["id"] == "P001"
    assert "measures" not in payload, "STEP2 に基礎データを渡してはいけない"
    conn.rollback()


def test_step2_will_not_start_before_step1_has_produced_patterns(conn, dataset):
    """PATTERN が無い状態で検索させると、地域紹介が返ってきます。"""
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    job = jobs.get_job(conn, job_id, include_base_data=True)
    with pytest.raises(worker.StepNotImplemented, match="STEP1 の出力"):
        worker.build_input(conn, job, 2)
    conn.rollback()


def test_a_search_that_could_not_run_is_not_reported_as_nothing_found(monkeypatch):
    """検索が動かなかったことと、調べて見つからなかったことは別。

    サーバ側ツールのエラーは例外ではなく content の中身として HTTP 200 で
    返ります。空の結果として扱うと、「調査済み・該当なし」がレポートに残ります。
    """
    from kaigyou_intel.steps import step2_research

    monkeypatch.setattr(llm, "ask", lambda **kw: llm.Result(
        parsed=None, text="検索できませんでした。", usage=llm.Usage(), model="m",
        sources=[{"error": "max_uses_exceeded"}]))
    payload = step2_research.build_input(_step1_for_step2(), {"location": {}})
    with pytest.raises(step2_research.StepFailed, match="Web検索が実行できません"):
        step2_research.run(payload)


# --------------------------------------------------- 止まったジョブを再開できる
def test_a_job_that_stops_does_not_stay_running_forever(conn, dataset, monkeypatch):
    """実測：銀座のジョブが running のまま残り、0 件が出続けました。

    claim_job は queued しか見ません。走り終わった Job の状態を戻していないと、
    二度と拾われないまま「待っているジョブはありません」になります。
    """
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    unimplemented = _pretend_the_last_step_is_unimplemented(monkeypatch)
    _complete_the_steps_before(conn, job_id, unimplemented)
    assert jobs.claim_specific(conn, job_id) == job_id  # ここで running になる

    try:
        assert worker.run_job(conn, job_id) == "blocked"
        # running のままにしない。ただし queued にも戻しません。戻すと worker が
        # 同じ Job を拾っては同じところで止まる、を繰り返します。
        # claim_job は queued しか見ないので、blocked は拾われません。
        assert jobs.get_job(conn, job_id)["status"] == "blocked"

        # 止まっていたステップを実装した体で、自動的に待ち行列へ戻ること。
        blocked_at = jobs.next_step(conn, job_id)
        assert jobs.requeue_unblocked(conn, set(worker.RUNNERS)) == 0, "まだ実装していない"
        assert jobs.requeue_unblocked(conn, {blocked_at}) >= 1
        assert jobs.get_job(conn, job_id)["status"] == "queued"
    finally:
        _drop_job(job_id)


def test_a_finished_job_is_marked_completed(conn, dataset, monkeypatch):
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    for number in sorted(jobs.STEP_NAMES):
        jobs.start_step(conn, job_id, number, {}, {"prompt_version": "v", "model": "m"})
        jobs.finish_step(conn, job_id, number, {"n": number}, {})
    jobs.claim_specific(conn, job_id)

    try:
        assert worker.run_job(conn, job_id) == "completed"
        job = jobs.get_job(conn, job_id)
        assert job["status"] == "completed" and job["completed_at"] is not None
    finally:
        _drop_job(job_id)


def test_a_failed_job_is_not_picked_up_again_on_its_own(conn, dataset, monkeypatch):
    """壊れたまま自動で拾い直すと、同じ失敗に何度も課金されます。"""
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    jobs.claim_specific(conn, job_id)
    monkeypatch.setattr(worker, "RUNNERS", {1: _boom})

    try:
        assert worker.run_job(conn, job_id) == "failed"
        # failed は claim_job の対象外。壊れたまま自動で拾い直すと、同じ失敗に
        # 何度も課金されます。
        assert jobs.get_job(conn, job_id)["status"] == "failed"
        # 人が名指しすれば再開できること。ここが無いと詰みます。
        assert jobs.claim_specific(conn, job_id) == job_id
    finally:
        _drop_job(job_id)


def _boom(_payload):
    raise RuntimeError("模擬的な失敗")


def _pretend_the_last_step_is_unimplemented(monkeypatch) -> int:
    """最後のステップを未実装ということにして、その番号を返す。

    4 段とも実装済みになったので、未実装の扱いは実物では作れません。ですが
    「未実装で止まった Job をどうするか」は仕組みの話で、次に段を足すときにも
    要ります。RUNNERS を差し替えて、その状況だけを作ります。
    """
    from kaigyou_intel import worker

    number = max(worker.RUNNERS)
    monkeypatch.setattr(
        worker, "RUNNERS", {k: v for k, v in worker.RUNNERS.items() if k != number})
    return number


def _complete_the_steps_before(conn, job_id: str, number: int) -> None:
    from kaigyou_intel import jobs

    for step in range(1, number):
        jobs.start_step(conn, job_id, step, {},
                        {"prompt_version": "v", "model": "m"})
        jobs.finish_step(conn, job_id, step, _step1_for_step2(), {})


def _drop_job(job_id: str) -> None:
    """このテストが commit した行を片づける。

    jobs の関数は自分で commit します（worker が落ちても状態が残るように）。
    そのぶん、テスト側は rollback では消せません。
    """
    from kaigyou_core.db import connect

    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM analysis_jobs WHERE id = %s", (job_id,))
        conn.commit()


def test_retrying_from_a_step_is_available_from_the_command_line():
    """PowerShell から止まったジョブを再開できること。"""
    from kaigyou_etl.cli import build_parser

    args = build_parser().parse_args(
        ["analyze", "--job", "abc", "--retry", "1"])
    assert (args.job, args.retry) == ("abc", 1)


# ------------------------------------- 空振りを「事実」として記録させない
def test_the_prompts_say_where_a_missing_source_belongs():
    """実測：EXTERNAL FACT 12件のうち6件が「その資料には載っていない」でした。

    調べた記録としては要りますが、商圏についての事実ではありません。混ぜると
    後続が「外部情報で12件確認できた」と読み、半分が空振りだったことが
    見えなくなります。
    """
    text = cfg.prompt_text("step2_structure.md")
    assert "unanswered" in text
    assert "EXTERNAL FACT ではありません" in text
    # 「何も見つからなかった」は反証ではない、と書いてあること。
    assert "UNSUPPORTED ではありません" in text


def test_step1_is_told_which_questions_have_public_answers():
    """答えの無い質問を作らせない。

    医院の経営方針・患者の居住地別内訳・自由診療比率は、どこにも公表されて
    いません。それを尋ねると、STEP2 の検索上限と費用が空振りに消えます。
    """
    text = cfg.prompt_text("step1_features.md")
    assert "公表資料で答えが出る質問だけを書く" in text
    assert "自由診療比率" in text


def test_the_request_carries_a_cache_breakpoint():
    """STEP2 は 1 回の呼び出しの中で検索ループが回り、文脈を読み直します。

    実測 307,754 トークン。変わらない前置きを読み直さずに済めばそのぶん安い。
    効いたかどうかは usage.cache_read_input_tokens で確かめます。
    """
    body = llm.build_request(2, "s", "u")
    assert body["cache_control"] == {"type": "ephemeral"}
    json.dumps(body)


def test_cache_tokens_are_recorded_and_priced(conn, dataset):
    """要件 §34：1レポートいくらかかったかを後から数えられること。

    単価が違うので、入力トークンとまとめてしまうと概算が合いません。
    """
    from kaigyou_etl.cli import _step_cost
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    jobs.start_step(conn, job_id, 1, {}, {"prompt_version": "v",
                                          "model": "claude-sonnet-5"})
    jobs.finish_step(conn, job_id, 1, {"facts": []}, {
        "input_tokens": 1_000_000, "output_tokens": 0, "web_searches": 0,
        "cache_read_tokens": 1_000_000, "cache_write_tokens": 1_000_000})

    step = {s["step_number"]: s for s in jobs.get_steps(conn, job_id)}[1]
    assert step["cache_read_tokens"] == 1_000_000
    # 入力 $2 + 読み出し $0.2 + 書き込み $2.5
    assert _step_cost(step) == pytest.approx(4.7)
    conn.rollback()


# ------------------------------------------------------------------ STEP3
def _step3_output(**overrides) -> "Step3Output":
    from kaigyou_intel.schemas import (
        DemandInsight, DemandMechanism, PatientSegment, Step3Output)

    data = {
        "demand_mechanisms": [DemandMechanism(
            id="M001", title="昼間人口が常住人口を大きく上回る",
            chain=["昼間従業者数が常住人口を大きく上回る",
                   "就業者は平日日中をこの地区で過ごす",
                   "受診も勤務地周辺で行われる",
                   "平日日中の歯科受診需要が常住人口の規模を超えて発生する"],
            evidence=["F001", "C002"], confidence="medium")],
        "patient_segments": [PatientSegment(
            id="S001", name="周辺勤務者", evidence=["F001"], mechanism_id="M001",
            importance="high", confidence="medium", note=None)],
        "insights": [DemandInsight(
            id="I001", statement="常住人口の規模で需要を測ると過小評価になる",
            evidence=["M001", "S001"])],
        "not_supported": ["広域流入患者：来院範囲を示すデータが無い"],
    }
    data.update(overrides)
    return Step3Output(**data)


def test_a_segment_without_a_mechanism_is_rejected():
    """要件 §14：患者属性を並べる作業ではありません。

    どの筋道でも説明できない層は、観察ではなく思いつきです。
    """
    from kaigyou_intel.schemas import PatientSegment, verify_step3

    output = _step3_output(patient_segments=[PatientSegment(
        id="S001", name="高齢者", evidence=["F001"], mechanism_id="M404",
        importance="high", confidence="low")])
    problems = verify_step3(output, {"F001"}, {"C002"})
    assert any("M404" in p.problem for p in problems)


def test_step3_evidence_must_come_from_the_earlier_steps():
    from kaigyou_intel.schemas import verify_step3

    output = _step3_output()
    problems = verify_step3(output, {"F001"}, set())
    assert any("C002" in p.problem for p in problems)
    assert verify_step3(output, {"F001"}, {"C002"}) == []


def test_a_mechanism_must_be_a_chain_not_a_restatement():
    """「駅前だから患者が来る」を書けなくする。

    段を 3 つ以上必須にしているので、一足飛びの説明はスキーマで落ちます。
    空文字で段を埋めるほうも塞ぎます。
    """
    from pydantic import ValidationError

    from kaigyou_intel.schemas import DemandMechanism, verify_step3

    with pytest.raises(ValidationError):
        DemandMechanism(id="M001", title="駅前だから患者が来る",
                        chain=["駅前である", "患者が来る"],
                        evidence=["F001", "F002"], confidence="high")

    hollow = _step3_output(demand_mechanisms=[DemandMechanism(
        id="M001", title="t", chain=["駅前である", "  ", "患者が来る"],
        evidence=["F001", "C002"], confidence="high")])
    problems = verify_step3(hollow, {"F001"}, {"C002"})
    assert any("空の段" in p.problem for p in problems)


def test_step3_runs_end_to_end_with_a_stubbed_model(dataset, monkeypatch):
    from kaigyou_intel.steps import step3_demand

    captured: dict[str, object] = {}

    def fake_ask(*, step_number, system, user, schema=None, tools=None,
                 web_search=None):
        captured.update(step_number=step_number, schema=schema, tools=tools,
                        system=system)
        return llm.Result(parsed=_step3_output(),
                          usage=llm.Usage(input_tokens=9, output_tokens=9), model="m")

    monkeypatch.setattr(llm, "ask", fake_ask)
    payload = step3_demand.build_input(
        {"facts": [{"id": "F001"}], "patterns": []},
        {"external_facts": [{"id": "C002"}], "hypotheses": []},
        dataset)
    output, usage, sources = step3_demand.run(payload)

    assert captured["tools"] is None, "STEP3 で外部検索をさせてはいけない（要件 §38）"
    assert output["patient_segments"][0]["mechanism_id"] == "M001"
    assert sources == []


def test_step3_refuses_evidence_it_was_never_given(dataset, monkeypatch):
    from kaigyou_intel.steps import step3_demand

    monkeypatch.setattr(llm, "ask", lambda **kw: llm.Result(
        parsed=_step3_output(), usage=llm.Usage(), model="m"))
    payload = step3_demand.build_input(
        {"facts": [{"id": "F001"}], "patterns": []},
        {"external_facts": [], "hypotheses": []}, dataset)
    with pytest.raises(step3_demand.StepFailed, match="C002"):
        step3_demand.run(payload)


def test_step3_input_needs_both_earlier_steps(conn, dataset):
    """STEP2 の結論が無いまま需要分析をさせると、外部事実のない推論になります。"""
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    jobs.start_step(conn, job_id, 1, {}, {"prompt_version": "v", "model": "m"})
    jobs.finish_step(conn, job_id, 1, _step1_for_step2(), {})

    job = jobs.get_job(conn, job_id, include_base_data=True)
    with pytest.raises(worker.StepNotImplemented, match="STEP1 と STEP2"):
        worker.build_input(conn, job, 3)
    conn.rollback()


def test_step3_is_not_asked_to_predict():
    """禁止事項。売上・患者数・成功確率の予測はこのシステムの目的外です。"""
    text = cfg.prompt_text("step3_demand.md")
    assert "売上・患者数・成功確率の予測" in text
    assert "UNSUPPORTED" in text, "反証された仮説を根拠に使わせない指示が要ります"


# ------------------------------------------------------------------ STEP4
def _evidenced(text="s", refs=("F001",)):
    from kaigyou_intel.schemas import Evidenced

    return Evidenced(statement=text, evidence=list(refs))


def _step4_output(**overrides) -> "Step4Output":
    from kaigyou_intel.schemas import (
        REPORT_SECTIONS, BusinessDecision, ReportBlock, ReportSection, Step4Output)

    data = {
        "executive_summary": "常住人口では説明できない供給を、勤務者需要が支えている。",
        "decision": BusinessDecision(
            primary_patients=_evidenced("周辺勤務者", ["S001"]),
            secondary_patients=_evidenced("居住小児は主要に置かない", ["F005"]),
            avoid_competing_on=_evidenced("小児歯科の標榜数では競わない", ["F014"]),
            acquisition_area=_evidenced("銀座駅から徒歩圏の通勤動線", ["M001"]),
            reason_to_visit=_evidenced("勤務時間の前後に通える診療時間", ["F015"]),
            clinic_model=_evidenced("平日夜間まで開ける成人中心の医院", ["M001"]),
            advantages=[_evidenced("昼間人口が常住人口を大きく上回る", ["F010"])],
            risks=[_evidenced("地価が都内上位0.2%", ["F017"])],
            confidence="medium"),
        "sections": [
            ReportSection(number=i + 1, title=title,
                          blocks=[ReportBlock(tag="FACT", text="t",
                                              evidence=["F001"])])
            for i, title in enumerate(REPORT_SECTIONS)],
        "actions": [_evidenced("平日夜間の診療体制を決める", ["M001"])],
    }
    data.update(overrides)
    return Step4Output(**data)


_STEP4_IDS = {"F001", "F005", "F010", "F014", "F015", "F017", "S001", "M001"}


def test_a_clean_report_passes():
    from kaigyou_intel.schemas import verify_step4

    assert verify_step4(_step4_output(), _STEP4_IDS) == []


def test_the_report_keeps_the_required_chapters():
    """要件 §18：章立てはモデルに決めさせません。

    毎回違う章立てで出てくると、2地点を並べて読めなくなります。
    """
    from kaigyou_intel.schemas import verify_step4

    output = _step4_output()
    output.sections = output.sections[:5]
    assert any("§18" in p.problem for p in verify_step4(output, _STEP4_IDS))


def test_a_chapter_that_puts_the_conclusion_before_the_evidence_is_caught():
    """守らせる価値があるのは、結論を先に書かないことだけ（要件 §19）。"""
    from kaigyou_intel.schemas import ReportBlock, verify_step4

    output = _step4_output()
    output.sections[6].blocks = [
        ReportBlock(tag="ACTION", text="夜間診療を検討する"),
        ReportBlock(tag="FACT", text="昼間人口が多い", evidence=["F010"]),
    ]
    assert any("§19" in p.problem for p in verify_step4(output, _STEP4_IDS))


def test_facts_and_benchmarks_may_interleave():
    """実測：この章立てでレポート1本を落としていました。

      FACT → FACT → BENCHMARK → FACT → BENCHMARK → PATTERN → WHY → INSIGHT
        → IMPLICATION

    事実ひとつに比較ひとつを添えて書けば当然こうなります。§22 の
    「値 + 比較 + 意味」はむしろそう書くことを求めています。書式の理由で
    レポート1本ぶんの費用を捨てていました。
    """
    from kaigyou_intel.schemas import ReportBlock, verify_step4

    output = _step4_output()
    output.sections[1].blocks = [
        ReportBlock(tag=tag, text="t", evidence=["F001"] if tag == "FACT" else [])
        for tag in ("FACT", "FACT", "BENCHMARK", "FACT", "BENCHMARK",
                    "PATTERN", "WHY", "INSIGHT", "IMPLICATION")
    ]
    assert verify_step4(output, _STEP4_IDS) == []


def test_a_chapter_may_make_more_than_one_argument():
    """章の中で筋を2本立てるなら、PATTERN と WHY は繰り返せます。"""
    from kaigyou_intel.schemas import ReportBlock, verify_step4

    output = _step4_output()
    output.sections[6].blocks = [
        ReportBlock(tag=tag, text="t")
        for tag in ("PATTERN", "WHY", "PATTERN", "WHY", "INSIGHT", "ACTION")
    ]
    assert verify_step4(output, _STEP4_IDS) == []


def test_a_chapter_may_skip_tags():
    """単なる事実の章は FACT だけでよい（要件 §19）。"""
    from kaigyou_intel.schemas import ReportBlock, verify_step4

    output = _step4_output()
    output.sections[5].blocks = [
        ReportBlock(tag="FACT", text="地価は14,300,000円/m2", evidence=["F017"]),
        ReportBlock(tag="IMPLICATION", text="賃料負担は初期条件を強く縛る"),
    ]
    assert verify_step4(output, _STEP4_IDS) == []


def test_the_report_cannot_cite_an_id_no_step_produced():
    """§25：INSIGHT から FACT、そして出典まで辿れること。"""
    from kaigyou_intel.schemas import verify_step4

    output = _step4_output()
    output.actions = [_evidenced("何かする", ["Z999"])]
    assert any("Z999" in p.problem for p in verify_step4(output, _STEP4_IDS))


@pytest.mark.parametrize("phrase", ["年商", "成功確率", "投資回収", "売上予測"])
def test_a_report_that_predicts_revenue_is_refused(phrase):
    """開業成功確率・売上・患者数の予測は、このシステムの目的外です。

    プロンプトで禁じたうえで、出力でも落とします。お願いだけで守られることに
    賭けない。
    """
    from kaigyou_intel.schemas import verify_step4

    output = _step4_output()
    output.executive_summary = f"この立地の{phrase}は良好と見込まれる。"
    assert any(phrase in p.problem for p in verify_step4(output, _STEP4_IDS))


def test_the_decision_has_a_place_for_who_not_to_compete_with():
    """要件 §17。散文に溶かすと、抜けても気づけません。

    欄として持つので、埋まっていなければスキーマが通しません。
    """
    from pydantic import ValidationError

    from kaigyou_intel.schemas import BusinessDecision

    with pytest.raises(ValidationError):
        BusinessDecision(
            primary_patients=_evidenced(), secondary_patients=_evidenced(),
            acquisition_area=_evidenced(), reason_to_visit=_evidenced(),
            clinic_model=_evidenced(), advantages=[_evidenced()],
            risks=[_evidenced()], confidence="low")  # avoid_competing_on 欠落


def test_the_report_markdown_always_carries_the_disclaimer_and_provenance(dataset):
    """免責・出典・データ時点は LLM に書かせません。書き忘れの起きる場所に
    置かないためです。"""
    from kaigyou_intel.report import to_markdown

    markdown = to_markdown(_step4_output().model_dump(), to_jsonable(dataset))
    assert "## 免責" in markdown
    assert "## データの出典と時点" in markdown
    assert dataset["disclaimer"][:20] in markdown
    # §17 の答えが表として載ること。
    assert "競争しない領域" in markdown and "主要に置かない層" in markdown


def test_the_report_lists_its_external_sources_in_priority_order(dataset):
    """要件 §9 の優先順位で並べる。読む人が上から見て一次資料に当たれるように。"""
    from kaigyou_intel.report import to_markdown

    markdown = to_markdown(_step4_output().model_dump(), to_jsonable(dataset), [
        {"url": "https://example.com/a", "title": "企業サイト",
         "source_type": "company", "pattern_id": "P001"},
        {"url": "https://www.mhlw.go.jp/b", "title": "厚労省",
         "source_type": "government", "pattern_id": "P001"},
    ])
    assert markdown.index("厚労省") < markdown.index("企業サイト")


def test_the_source_list_is_what_the_report_cited_not_what_it_opened(dataset):
    """実測：銀座のレポートの出典が 230 件になりました。

    同じ URL が最大6回、医院のホームページや情報サイトも混ざっていて、
    出典一覧として使えません。調べた記録は analysis_sources に残っているので、
    レポートに載せるのは本文が引用したものだけにします。
    """
    from kaigyou_intel.report import to_markdown

    markdown = to_markdown(_step4_output().model_dump(), to_jsonable(dataset), [
        {"url": "https://www.city.chuo.lg.jp/x", "title": "中央区 人口統計",
         "source_type": "municipality", "pattern_id": "P001"},
        {"url": "https://www.city.chuo.lg.jp/x/", "title": "中央区 人口統計",
         "source_type": "municipality", "pattern_id": "P002"},
        {"url": "https://example-clinic.com/", "title": "銀座◯◯歯科",
         "source_type": "company", "pattern_id": None},
    ])
    block = markdown.split("## 出典（外部情報）", 1)[1].split("\n## ", 1)[0]
    assert block.count("city.chuo.lg.jp") == 1, "同じ URL を重ねて出しています"
    assert "銀座◯◯歯科" not in block, "引用していない資料を出典に載せています"
    assert "このほか 2 件" in block


def test_a_very_long_source_title_is_trimmed(dataset):
    """e-Stat の表題は 481 文字ありました。"""
    from kaigyou_intel.report import to_markdown

    long_title = ("国勢調査 平成27年国勢調査 従業地・通学地による集計" + "あ" * 200
                  + " | 統計表・グラフ表示 | 政府統計の総合窓口")
    markdown = to_markdown(_step4_output().model_dump(), to_jsonable(dataset), [
        {"url": "https://www.e-stat.go.jp/x", "title": long_title,
         "source_type": "statistics", "pattern_id": "P001"}])
    line = [ln for ln in markdown.splitlines() if ln.startswith("- [政府統計]")][0]
    assert len(line) < 130
    assert "政府統計の総合窓口" not in line, "どの出典にも付く後半は落とす"


def test_a_report_with_no_cited_source_says_so(dataset):
    """外部情報を使えなかったことを黙らない。"""
    from kaigyou_intel.report import to_markdown

    markdown = to_markdown(_step4_output().model_dump(), to_jsonable(dataset), [
        {"url": "https://example.com/a", "title": "t", "source_type": "other",
         "pattern_id": None}])
    assert "本文が引用した外部資料はありません（1 件を参照）" in markdown


def test_step4_needs_all_three_earlier_steps(conn, dataset):
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    jobs.start_step(conn, job_id, 1, {}, {"prompt_version": "v", "model": "m"})
    jobs.finish_step(conn, job_id, 1, _step1_for_step2(), {})

    job = jobs.get_job(conn, job_id, include_base_data=True)
    with pytest.raises(worker.StepNotImplemented, match="STEP2・STEP3"):
        worker.build_input(conn, job, 4)
    conn.rollback()


def test_the_report_is_saved_and_readable(conn, dataset):
    from kaigyou_intel import jobs, report

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    try:
        report.save(conn, job_id, _step4_output().model_dump(), to_jsonable(dataset))
        markdown = report.markdown_for(conn, job_id)
        assert markdown and "# 商圏分析レポート" in markdown
        # 二度目は上書き（やり直しても行が増えない）。
        report.save(conn, job_id, _step4_output().model_dump(), to_jsonable(dataset))
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM analysis_reports WHERE job_id = %s",
                        (job_id,))
            assert cur.fetchone()["n"] == 1
    finally:
        _drop_job(job_id)


def test_the_report_can_be_read_from_the_command_line():
    from kaigyou_etl.cli import build_parser

    args = build_parser().parse_args(["analyze", "--report", "--out", "report.md"])
    assert args.report == "" and args.out == "report.md"


def test_the_report_carries_the_caveats_that_change_how_it_reads(dataset):
    """「標榜診療科目は届出値であって診療内容ではない」は、落とすと誤読になります。

    競合の数え方の但し書きが本文から消えると、標榜数をそのまま診療実態として
    読まれます。データ側が持っている注意書きは、レポートに必ず出します。
    """
    from kaigyou_intel.report import to_markdown

    markdown = to_markdown(_step4_output().model_dump(), to_jsonable(dataset))
    assert "## 読むときの注意" in markdown
    assert "標榜" in markdown
    # 出典と年次が指標から拾えていること（空欄のまま出さない）。
    assert "## データの出典と時点" in markdown
    body = markdown.split("## データの出典と時点", 1)[1].split("##", 1)[0]
    assert body.strip(), "出典が空のまま出しています"


def test_the_cost_line_does_not_look_like_it_lost_the_input(capsys, conn, dataset):
    """実測：STEP3 が「入力 2 tok」と出ました。

    キャッシュに入ったぶんは input_tokens から抜けます。そこを足さずに出すと、
    数え損ねたように見えます（費用の計算は合っているのに）。
    """
    from kaigyou_etl.cli import _print_step_cost_line

    _print_step_cost_line({"input_tokens": 2, "output_tokens": 5093,
                           "cache_read_tokens": 0, "cache_write_tokens": 42000,
                           "model": "claude-sonnet-5"})
    printed = capsys.readouterr().out
    assert "入力 42,002 tok" in printed
    assert "キャッシュ書 42,000" in printed


# ------------------------------------------------------------ API と UI
def test_starting_an_analysis_is_refused_on_a_public_host_without_a_secret(monkeypatch):
    """分析1件でLLMの課金が発生します。公開URLに認証なしで置けません。

    警告を出して通すのでは、気づいたときには請求が来ています。
    """
    from fastapi import HTTPException

    from kaigyou_api.routers.intel import _authorise

    monkeypatch.delenv("KAIGYOU_ANALYSIS_TOKEN", raising=False)
    monkeypatch.setenv("VERCEL", "1")
    with pytest.raises(HTTPException) as caught:
        _authorise(None)
    assert caught.value.status_code == 503

    monkeypatch.setenv("KAIGYOU_ANALYSIS_TOKEN", "s3cret")
    with pytest.raises(HTTPException) as caught:
        _authorise("wrong")
    assert caught.value.status_code == 401
    _authorise("s3cret")  # 一致すれば通る


def test_the_local_machine_does_not_need_a_secret(monkeypatch):
    """手元では設定なしで動かせること。開発のたびに秘密を作らせない。"""
    from kaigyou_api.routers.intel import _authorise

    for var in ("VERCEL", "AWS_LAMBDA_FUNCTION_NAME", "K_SERVICE",
                "KAIGYOU_ANALYSIS_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    _authorise(None)


def test_the_progress_endpoint_reports_what_it_cost(conn, dataset):
    """要件 §34。画面に出す金額と端末に出す金額を同じ表から出します。"""
    from fastapi.testclient import TestClient

    from kaigyou_api.main import app
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    try:
        jobs.start_step(conn, job_id, 1, {}, {"prompt_version": "v",
                                              "model": "claude-sonnet-5"})
        jobs.finish_step(conn, job_id, 1, {"facts": []}, {
            "input_tokens": 2, "output_tokens": 1_000_000, "web_searches": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 1_000_000})

        body = TestClient(app).get(f"/api/analysis/{job_id}").json()
        # キャッシュに入ったぶんを引いたまま出すと「入力 2」になります。
        assert body["usage"]["input_tokens"] == 1_000_002
        assert body["usage"]["cache_write_tokens"] == 1_000_000
        # 入力 $2.5（書き込み1.25倍）+ 出力 $10
        assert body["usage"]["estimated_cost_usd"] == pytest.approx(12.5, abs=0.01)
        assert body["status_note"], "待っている人に、何を待っているのかを言う"
    finally:
        _drop_job(job_id)


def test_the_cost_estimate_is_not_understated_when_a_model_is_unknown():
    """一部だけの合計を総額として見せない。実際より安いと思われます。"""
    from kaigyou_intel.pricing import total_cost

    assert total_cost([]) == 0.0
    assert total_cost([{"model": "claude-sonnet-5", "input_tokens": 1_000_000}]) == 2.0
    assert total_cost([
        {"model": "claude-sonnet-5", "input_tokens": 1_000_000},
        {"model": "未知のモデル", "input_tokens": 1_000_000},
    ]) is None


def test_retrying_restarts_the_clock(conn, dataset):
    """やり直した直後の画面に「経過 25秒」と出さない。

    数えているのが前回の開始からだと、待っている人が見たいものと違います。
    """
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    jobs.claim_specific(conn, job_id)
    assert jobs.get_job(conn, job_id)["started_at"] is not None
    jobs.reset_step(conn, job_id, 1)
    assert jobs.get_job(conn, job_id)["started_at"] is None
    conn.rollback()


def test_a_billing_failure_tells_the_user_what_to_do():
    """「失敗しました」だけでは動けません。

    実測：残高不足で止まったとき、画面には400のJSONとスタックトレースだけが
    出ていました。読んでも次に何をすればいいのか分かりません。
    """
    from pathlib import Path

    panel = (Path(__file__).resolve().parents[2] / "web" / "src" / "components"
             / "AnalysisPanel.tsx").read_text(encoding="utf-8")
    assert "credit balance is too low" in panel
    assert "Plans & Billing" in panel
    # やり直しは新しいジョブを作るのではなく、失敗したステップから再開する。
    assert "retryFrom" in panel and "からやり直す" in panel


def test_the_report_is_written_to_a_file_without_another_command(conn, dataset, tmp_path):
    """DB の中にあることと、手元にファイルがあることは違います。

    追加のコマンドを打たないと現物が手に入らないのでは、「レポートを作る道具」
    として不完全です。
    """
    from kaigyou_intel import jobs, report

    job_id = jobs.create_job(conn, lat=35.6717, lng=139.765, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x",
                             location_name="銀座4丁目")
    try:
        report.save(conn, job_id, _step4_output().model_dump(), to_jsonable(dataset))
        path = report.write_file(conn, job_id, directory=str(tmp_path))
        assert path is not None and path.exists()
        assert "銀座4丁目" in path.name, "人が見て分かる名前にする"
        assert path.read_text(encoding="utf-8").startswith("# 商圏分析レポート")

        # やり直しても同じ名前に上書きする。日付ごとに増やすと最新が分からない。
        again = report.write_file(conn, job_id, directory=str(tmp_path))
        assert again == path
        assert len(list(tmp_path.glob("*.md"))) == 1
    finally:
        _drop_job(job_id)


def test_a_file_name_survives_windows(conn, dataset, tmp_path):
    """`/` や `:` が入ると Windows では保存できません。"""
    from kaigyou_intel import jobs, report

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x",
                             location_name='A/B:C*D?"E<F>G|H')
    try:
        report.save(conn, job_id, _step4_output().model_dump(), to_jsonable(dataset))
        path = report.write_file(conn, job_id, directory=str(tmp_path))
        assert path is not None and path.exists()
        assert not set(path.name) & set('\\/:*?"<>|')
    finally:
        _drop_job(job_id)


def test_the_report_carries_the_numbers_it_did_not_quote(dataset):
    """本文が引用しなかった数値も、同じ文書の中で確かめられること。

    本文の数字は LLM が選びます。選ばれなかった数字がどこにも残らないと、
    読んだ人は「その数字はどこから来たのか」を別の画面で探すことになります。
    付録はデータセットからそのまま出すので、桁の取り違えも起きません。
    """
    from kaigyou_intel.report import to_markdown

    markdown = to_markdown(_step4_output().model_dump(), to_jsonable(dataset))
    assert "## 付録：商圏の基礎数値" in markdown
    for heading in ("### 常住人口", "### 昼間（従業者・事業所）",
                    "### 競合（歯科医院）", "### 交通アクセス", "### 地価（公示地価）"):
        assert heading in markdown, heading
    # 半径3段の比較が並ぶこと（1kmだけでは商圏の広がりが分からない）。
    assert "| 指標 | 500m | 1km | 2km |" in markdown
    # 標榜科目と診療時間は、競合の読み方を変える情報なので落とさない。
    assert "#### 標榜診療科目（商圏内）" in markdown
    assert "#### 診療時間（商圏内）" in markdown


def test_a_truncated_answer_is_not_reported_as_broken_json(monkeypatch):
    """実測：STEP4 が

      Invalid JSON: EOF while parsing a string at line 1 column 18741

    で落ちました。モデルが間違った JSON を書いたのではなく、**書き終わる前に
    止められた**という意味です。この 2 つは直し方がまったく違います
    （前者はプロンプト、後者は max_tokens）。

    SDK は content_block_stop の時点で本文を解析するので、stop_reason が
    max_tokens だと分かる前に例外が出ます。だから壊れ方のほうを見ます。
    """
    from pydantic_core import ValidationError as CoreValidationError

    from kaigyou_intel import client as llm

    boom = Exception(
        "1 validation error for Step4Output\n  Invalid JSON: EOF while parsing "
        "a string at line 1 column 18741 [type=json_invalid, input_value='{...']")
    assert llm._looks_truncated(boom)
    # 本物の書き間違いは別扱い（やり直しても同じところで落ちるとは限らない）。
    assert not llm._looks_truncated(Exception("Field required [type=missing]"))
    assert CoreValidationError is not None  # import できることの確認


def test_the_truncation_message_says_what_to_change(monkeypatch):
    import contextlib

    from kaigyou_intel import client as llm

    class _Stream:
        @staticmethod
        def get_final_message():
            raise Exception("Invalid JSON: EOF while parsing a string "
                            "[type=json_invalid, input_value='{']")

    class _Client:
        class messages:
            @staticmethod
            @contextlib.contextmanager
            def stream(**kwargs):
                yield _Stream()

    monkeypatch.setattr(llm, "_client", lambda: _Client())
    with pytest.raises(llm.Truncated, match="max_tokens"):
        llm.ask(step_number=4, system="s", user="u", schema=Step1Output)


def test_the_output_ceiling_leaves_room_for_the_whole_report():
    """10章＋開業方針の JSON に、思考のぶんを足しても収まる上限にしておく。

    24,000 では書き終わる前に切れました。全部ストリームで受けているので
    HTTP タイムアウトの心配はなく、上限は余裕を持たせられます。払うのは
    実際に出た分だけなので、上げても高くなりません。
    """
    assert cfg.analysis_config()["model"]["max_tokens"] >= 48000


# ------------------------------------------------------------------ STEP5
def _step5_output(**overrides) -> "Step5Output":
    from kaigyou_intel.schemas import (
        Judgement, NarrativeSection, Step5Output, SupportItem)

    data = {
        "title": "銀座4丁目 商圏分析レポート",
        "summary": "常住人口13,268人に対し歯科医院は186院あり、"
                   "住民だけを相手にする立地ではありません。",
        "verdict": Judgement(
            label="条件付きで有望",
            statement="昼間人口が常住人口を大きく上回るため、勤務者を主に据えれば"
                      "条件は揃っています。",
            basis=["M001", "F010"],
            counterpoint="勤務者の受診が勤務地周辺で行われていない場合、"
                         "この前提が崩れます。"),
        "why_here": "約49万人が昼間この地区で働いており、駅までは101mです。",
        "sections": [
            NarrativeSection(heading="住民と就業者", body="本文。", takeaway="要点",
                             evidence=["F001"]),
            NarrativeSection(heading="競合の厚み", body="本文。", evidence=["F010"]),
            NarrativeSection(heading="立地の条件", body="本文。"),
        ],
        "support_needed": [SupportItem(
            item="平日夜間まで回せる人員体制",
            why="勤務者を主に据えると、受診は勤務前後に寄ります。",
            evidence=["M001"], category="人員")],
        "questions_for_the_client": ["想定している診療時間の上限は何時までか"],
        "judgement_note": "数値は公的統計です。「条件が揃っている」という評価は"
                          "本レポートの判断であり、開業の成否を示すものではありません。",
    }
    data.update(overrides)
    return Step5Output(**data)


_STEP5_IDS = {"F001", "F010", "M001"}
_STEP5_NUMBERS = {"13268", "186", "494517", "101"}


def test_a_readable_report_passes():
    from kaigyou_intel.schemas import verify_step5

    assert verify_step5(_step5_output(), _STEP5_IDS, _STEP5_NUMBERS) == []


def test_rounding_for_readability_is_allowed():
    """「494,517人」と書けとは言えません。読み物として渡す文書です。

    「約49万人」は正しい書き方で、落としてはいけない。
    """
    from kaigyou_intel.schemas import invented_numbers

    known = {"494517", "13268", "0.279", "71.43"}
    assert invented_numbers("約49万人が働き、住民は約1.3万人です。", known) == []
    assert invented_numbers("構成比は27.9%、1院あたり71.4人。", known) == []
    assert invented_numbers("2020年から2025年、3つの理由で", known) == []


def test_a_number_that_was_never_in_the_data_is_caught():
    """散文にすると、数字はいくらでも滑らかに増やせます。

    「約5万人」は、元が13,268でも494,517でも文としては通ります。だから
    照合します。
    """
    from kaigyou_intel.schemas import invented_numbers

    known = {"494517", "13268"}
    assert invented_numbers("約5万人が働いています。", known) == ["5万"]
    assert invented_numbers("年間3,200人の来院が見込めます。", known) == ["3,200"]


def test_step5_refuses_a_number_it_was_not_given():
    from kaigyou_intel.schemas import verify_step5

    output = _step5_output(why_here="約5万人がこの地区で働いています。")
    problems = verify_step5(output, _STEP5_IDS, _STEP5_NUMBERS)
    assert any("5万" in p.problem for p in problems)


def test_step5_still_may_not_predict():
    """評価（「条件が揃っている」）と予測（「儲かる」）は別のものです。

    価値判断は書けますが、売上・患者数・成功確率は書けません。
    """
    from kaigyou_intel.schemas import verify_step5

    output = _step5_output(summary="この立地の年商は良好と見込まれます。")
    assert any("年商" in p.problem
               for p in verify_step5(output, _STEP5_IDS, _STEP5_NUMBERS))


def test_the_judgement_is_marked_as_a_judgement():
    """価値判断を許すなら、どこからが判断かを読み手に見せる必要があります。"""
    from pydantic import ValidationError

    from kaigyou_intel.schemas import Judgement

    # counterpoint が無い判断は通しません。書けないなら根拠が薄いということです。
    with pytest.raises(ValidationError):
        Judgement(label="有望", statement="良い立地です", basis=["F001"])
    # judgement_note も必須。
    with pytest.raises(ValidationError):
        _step5_output(judgement_note=None)


def test_the_client_report_reads_as_prose_not_as_tagged_facts(dataset):
    """[FACT] が20個並んだ文書は、読み手に「自分で要約してください」と
    言っているのと同じです。"""
    from kaigyou_intel.report import to_markdown

    markdown = to_markdown(_step5_output().model_dump(), to_jsonable(dataset))
    assert "**[FACT]**" not in markdown
    assert "## なぜこの立地か" in markdown
    assert "## この立地で開業するために必要なこと" in markdown
    assert "### 人員" in markdown, "支援の要件は分類して出す"
    assert "## 面談で確認したいこと" in markdown
    assert "## このレポートにおける評価の位置づけ" in markdown
    # 根拠の id は残す。読み飛ばせる形で本文の末尾に。
    assert "〔M001, F010〕" in markdown
    # 付録と免責はこれまでどおり。
    assert "## 付録：商圏の基礎数値" in markdown and "## 免責" in markdown


def test_the_working_format_is_still_rendered_when_there_is_no_client_report(dataset):
    """STEP5 が無いジョブ（古いもの）も読めること。"""
    from kaigyou_intel.report import to_markdown

    markdown = to_markdown(_step4_output().model_dump(), to_jsonable(dataset))
    assert "**[FACT]**" in markdown


def test_step5_needs_the_two_steps_before_it(conn, dataset):
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    job = jobs.get_job(conn, job_id, include_base_data=True)
    with pytest.raises(worker.StepNotImplemented, match="STEP3・STEP4"):
        worker.build_input(conn, job, 5)
    conn.rollback()


def test_an_existing_job_gains_the_new_step(conn, dataset):
    """段を増やしたとき、既にある Job には行がありません。

    行が無いと next_step は「全部終わった」と読み、増やした段が黙って
    飛ばされます。作り直さずに続きから流せること。
    """
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM analysis_steps WHERE job_id = %s AND step_number = 5",
                    (job_id,))
    for number in (1, 2, 3, 4):
        jobs.start_step(conn, job_id, number, {}, {"prompt_version": "v", "model": "m"})
        jobs.finish_step(conn, job_id, number, {"n": number}, {})
    # 4 段だったころに完走した Job の状態。
    jobs.release_job(conn, job_id, "completed")
    try:
        assert jobs.next_step(conn, job_id) is None, "行が無いので終わったように見える"
        assert jobs.ensure_steps(conn, job_id) == 1
        assert jobs.next_step(conn, job_id) == 5
        assert jobs.get_job(conn, job_id)["status"] == "queued"
    finally:
        _drop_job(job_id)


def test_the_client_report_keeps_its_own_title(dataset):
    """顧客に渡す文書なので、こちらが決めた定型より、その商圏について
    書かれた見出しのほうが読み手に向いています。"""
    from kaigyou_intel.report import to_markdown

    markdown = to_markdown(_step5_output().model_dump(), to_jsonable(dataset))
    assert markdown.startswith("# 銀座4丁目 商圏分析レポート")
    # 表題が無い（STEP4 止まり）ときは定型に戻ること。
    plain = to_markdown(_step4_output().model_dump(), to_jsonable(dataset))
    assert plain.startswith("# 商圏分析レポート：")
