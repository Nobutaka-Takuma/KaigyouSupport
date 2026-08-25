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
def test_the_four_steps_are_configured_with_their_prompts():
    """実装済みのステップには、プロンプトの実体があること。

    未実装のぶんは名前だけ先に置いてあります。実装した番号を RUNNERS に足すと、
    このテストがその番号のプロンプトを要求します。
    """
    from kaigyou_intel.worker import RUNNERS

    config = cfg.analysis_config()
    assert set(config["steps"]) == {1, 2, 3, 4}
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
    for number in (1, 3, 4):
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


def test_a_new_job_has_all_four_steps_pending(conn, dataset):
    from kaigyou_intel import jobs

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    steps = jobs.get_steps(conn, job_id)
    assert [s["step_number"] for s in steps] == [1, 2, 3, 4]
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


def test_an_unimplemented_step_is_left_pending_not_marked_failed(conn, dataset):
    """未実装は失敗ではない。

    最初の実装は STEP2 を failed で記録していました。画面には赤い「失敗」と
    エラー本文が出るので、実行して壊れたように見えます。実際には一度も
    呼ばれていません。pending のままにしておけば、RUNNERS に足した次の
    `analyze --once` がそのまま続きから拾います。
    """
    from kaigyou_intel import jobs, worker

    job_id = jobs.create_job(conn, lat=35.0, lng=139.0, radius_m=1000,
                             dataset=to_jsonable(dataset), base_hash="x")
    jobs.start_step(conn, job_id, 1, {}, {"prompt_version": "v", "model": "m"})
    jobs.finish_step(conn, job_id, 1, {"facts": []}, {})

    jobs.start_step(conn, job_id, 2, {}, {"prompt_version": "v", "model": "m"})
    jobs.finish_step(conn, job_id, 2, {"external_facts": []}, {})

    unimplemented = min(n for n in (3, 4) if n not in worker.RUNNERS)
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

    class _Client:
        class messages:
            @staticmethod
            def parse(**kwargs):
                m = _Message()
                m.parsed_output = None
                return m

    monkeypatch.setattr(llm, "_client", lambda: _Client())
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


def test_the_structured_call_passes_the_schema_as_output_format(monkeypatch):
    """parse() の呼び方を固定する。"""
    from kaigyou_intel import client as llm

    seen: dict[str, object] = {}

    class _Client:
        class messages:
            @staticmethod
            def parse(**kwargs):
                seen.update(kwargs)
                message = type("M", (), {})()
                message.parsed_output = _output()
                message.content, message.stop_reason = [], "end_turn"
                message.usage = type("U", (), {"input_tokens": 1, "output_tokens": 1})()
                message.model = "claude-sonnet-5"
                return message

    monkeypatch.setattr(llm, "_client", lambda: _Client())
    llm.ask(step_number=1, system="s", user="u", schema=Step1Output)

    assert seen["output_format"] is Step1Output
    assert "format" not in seen["output_config"]
    json.dumps({k: v for k, v in seen.items() if k != "output_format"},
               ensure_ascii=False)


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
