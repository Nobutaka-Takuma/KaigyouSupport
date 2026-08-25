"""キューを回す。

ワークステーションで動かします（ETL と同じ場所）。Vercel の関数には実行時間の
上限があり、Web検索を伴う 4 ステップはそこに収まりません。

worker は記憶を持ちません。状態は全部 DB にあります。途中で落としても、
次に起動したときに続きから拾えます。
"""
from __future__ import annotations

import time
import traceback
from typing import Any, Callable, Mapping

import psycopg

from kaigyou_intel import client as llm
from kaigyou_intel import jobs
from kaigyou_intel.steps import step1_features, step2_research, step3_demand

#: 実装済みのステップ。ここに無い番号に来たら止めます。「未実装なので飛ばす」
#: をやると、材料が無いまま最終レポートが書かれます。
#:
#: どれも受け取るのは**射影済みの入力**です。ステップ自身に作らせていた頃は、
#: worker が記録用に 1 回、ステップが実行用にもう 1 回作っていて、
#: 「記録した入力と実際に渡した入力が同じ」という保証がありませんでした。
RUNNERS: dict[int, Callable[[Any], Any]] = {
    1: step1_features.run,
    2: step2_research.run,
    3: step3_demand.run,
}


class StepNotImplemented(RuntimeError):
    pass


def run_step(conn: psycopg.Connection, job_id: str, number: int) -> dict[str, Any]:
    """1 ステップ実行して保存する。

    例外は握って DB に書きます。worker が落ちて理由がどこにも残らないのが
    いちばん困るので、失敗も結果として記録します。
    """
    job = jobs.get_job(conn, job_id, include_base_data=True)
    if job is None:
        raise ValueError(f"job {job_id} が見つかりません")

    settings = llm.step_settings(number)
    runner = RUNNERS.get(number)
    if runner is None:
        # 記録は残しません。未実装は「失敗」ではないからです。failed と書くと
        # 実行して壊れたように見えて、実装後に手で直さないと再開できません。
        # 何も書かなければ pending のままなので、RUNNERS に足した次の
        # `analyze --once` がそのまま続きから拾います。
        raise StepNotImplemented(
            f"STEP{number}（{settings['name']}）は未実装です。"
            "実装されるまでこのジョブは進みません。")

    payload = build_input(conn, job, number)

    jobs.start_step(conn, job_id, number, payload, settings)
    try:
        output, usage, sources = runner(payload)
    except Exception as exc:  # noqa: BLE001 - 失敗も結果として残す
        jobs.fail_step(conn, job_id, number,
                       f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}")
        raise

    jobs.finish_step(conn, job_id, number, output, {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "web_searches": usage.web_searches,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
    })
    if sources:
        jobs.save_sources(conn, job_id, number, None, sources)
    return output


def build_input(conn: psycopg.Connection, job: Mapping[str, Any],
                number: int) -> dict[str, Any]:
    """そのステップに渡すものを組み立てる。

    前のステップの出力が要るものは DB から読みます。worker が記憶を持たない
    ようにしてあるので、途中で落ちても続きから拾えます。
    """
    if number == 1:
        return step1_features.build_input(job["base_data"])
    if number == 2:
        step1_output = jobs.step_output(conn, job["id"], 1)
        if not step1_output:
            raise StepNotImplemented(
                "STEP1 の出力がありません。STEP2 は STEP1 の PATTERN を調べる段です。")
        return step2_research.build_input(step1_output, job["base_data"])
    if number == 3:
        step1_output = jobs.step_output(conn, job["id"], 1)
        step2_output = jobs.step_output(conn, job["id"], 2)
        if not step1_output or not step2_output:
            raise StepNotImplemented(
                "STEP1 と STEP2 の出力が揃っていません。STEP3 は両方を使う段です。")
        return step3_demand.build_input(step1_output, step2_output, job["base_data"])
    # pragma: no cover - STEP4 を足すときにここへ分岐を書きます
    raise StepNotImplemented(f"STEP{number} の入力の作り方が未定義です")


def run_job(conn: psycopg.Connection, job_id: str,
            progress: Callable[[str], None] | None = None) -> str:
    """1 ジョブを、進める限り進める。

    どの抜け方をしても最後に ``release_job`` を通します。通さないと Job は
    running のまま残り、``claim_job`` は queued しか見ないので二度と拾われません。
    """
    say = progress or (lambda _m: None)
    while True:
        number = jobs.next_step(conn, job_id)
        if number is None:
            say(f"  {job_id}: 全ステップ完了")
            jobs.release_job(conn, job_id, "completed")
            return "completed"
        settings = llm.step_settings(number)
        say(f"  {job_id}: STEP{number} {settings['name']} …")
        try:
            run_step(conn, job_id, number)
        except StepNotImplemented as exc:
            say(f"    停止: {exc}")
            # 失敗ではないので queued に戻します。そのステップを実装した
            # 次の実行が、そのまま続きから拾います。
            jobs.release_job(conn, job_id, "blocked", str(exc))
            return "blocked"
        except Exception as exc:  # noqa: BLE001
            say(f"    失敗: {type(exc).__name__}: {exc}")
            say(f"    直したら: python -m kaigyou_etl analyze --once --job {job_id}"
                f"  （STEP{number} からやり直します。済んだステップは残ります）")
            jobs.release_job(conn, job_id, "failed", f"STEP{number}: {exc}")
            return "failed"


def serve(connect: Callable[[], psycopg.Connection], *, once: bool = False,
          poll_seconds: float = 5.0, job_id: str | None = None,
          progress: Callable[[str], None] | None = None) -> int:
    """待っているジョブを拾って回し続ける。

    接続は関数で受け取ります。長時間動かすので、都度つなぎ直せるほうが
    ネットワークの切断に強い。

    ``job_id`` を指定すると、その 1 件だけを状態にかかわらず動かします。
    失敗した Job を自動で拾い直さないぶん、人が名指しで再開する道が要ります。
    """
    say = progress or (lambda _m: None)
    if not llm.is_configured():
        say("ANTHROPIC_API_KEY が未設定です。分析は実行できません。")
        return 1

    with connect() as conn:
        # 未実装で止まっていた Job を、実装済みになっていれば拾い直します。
        # 「実装したあとにどのジョブを再開するか」を人が覚えている必要は
        # 無いはずです。
        resumed = jobs.requeue_unblocked(conn, RUNNERS)
    if resumed:
        say(f"未実装で止まっていたジョブ {resumed} 件を再開します。")

    if job_id is not None:
        with connect() as conn:
            claimed = jobs.claim_specific(conn, job_id)
            if claimed is None:
                say(f"ジョブ {job_id} は見つからないか、取り下げ済みです。")
                return 0
            say(f"ジョブ {claimed} を開始します")
            run_job(conn, claimed, progress=say)
        return 1

    handled = 0
    while True:
        with connect() as conn:
            claimed = jobs.claim_job(conn)
            if claimed is not None:
                say(f"ジョブ {claimed} を開始します")
                run_job(conn, claimed, progress=say)
                handled += 1
        if once:
            return handled
        if claimed is None:
            time.sleep(poll_seconds)
