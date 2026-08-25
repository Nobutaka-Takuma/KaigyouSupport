"""キューを回す。

ワークステーションで動かします（ETL と同じ場所）。Vercel の関数には実行時間の
上限があり、Web検索を伴う 4 ステップはそこに収まりません。

worker は記憶を持ちません。状態は全部 DB にあります。途中で落としても、
次に起動したときに続きから拾えます。
"""
from __future__ import annotations

import time
import traceback
from typing import Any, Callable

import psycopg

from kaigyou_intel import client as llm
from kaigyou_intel import jobs
from kaigyou_intel.steps import step1_features

#: 実装済みのステップ。ここに無い番号に来たら止めます。「未実装なので飛ばす」
#: をやると、材料が無いまま最終レポートが書かれます。
RUNNERS: dict[int, Callable[..., Any]] = {
    1: step1_features.run,
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
        message = (f"STEP{number}（{settings['name']}）は未実装です。"
                   "実装されるまでこのジョブは進みません。")
        jobs.fail_step(conn, job_id, number, message)
        raise StepNotImplemented(message)

    if number == 1:
        payload = step1_features.build_input(job["base_data"])
    else:  # pragma: no cover - STEP2 以降を足すときにここへ分岐を書きます
        raise StepNotImplemented(f"STEP{number} の入力の作り方が未定義です")

    jobs.start_step(conn, job_id, number, payload, settings)
    try:
        output, usage, sources = runner(job["base_data"])
    except Exception as exc:  # noqa: BLE001 - 失敗も結果として残す
        jobs.fail_step(conn, job_id, number,
                       f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}")
        raise

    jobs.finish_step(conn, job_id, number, output, {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "web_searches": usage.web_searches,
    })
    if sources:
        jobs.save_sources(conn, job_id, number, None, sources)
    return output


def run_job(conn: psycopg.Connection, job_id: str,
            progress: Callable[[str], None] | None = None) -> str:
    """1 ジョブを、進める限り進める。"""
    say = progress or (lambda _m: None)
    while True:
        number = jobs.next_step(conn, job_id)
        if number is None:
            say(f"  {job_id}: 全ステップ完了")
            return "completed"
        settings = llm.step_settings(number)
        say(f"  {job_id}: STEP{number} {settings['name']} …")
        try:
            run_step(conn, job_id, number)
        except StepNotImplemented as exc:
            say(f"    停止: {exc}")
            return "blocked"
        except Exception as exc:  # noqa: BLE001
            say(f"    失敗: {type(exc).__name__}: {exc}")
            return "failed"


def serve(connect: Callable[[], psycopg.Connection], *, once: bool = False,
          poll_seconds: float = 5.0,
          progress: Callable[[str], None] | None = None) -> int:
    """待っているジョブを拾って回し続ける。

    接続は関数で受け取ります。長時間動かすので、都度つなぎ直せるほうが
    ネットワークの切断に強い。
    """
    say = progress or (lambda _m: None)
    if not llm.is_configured():
        say("ANTHROPIC_API_KEY が未設定です。分析は実行できません。")
        return 1

    handled = 0
    while True:
        with connect() as conn:
            job_id = jobs.claim_job(conn)
            if job_id is not None:
                say(f"ジョブ {job_id} を開始します")
                run_job(conn, job_id, progress=say)
                handled += 1
        if once:
            return handled
        if job_id is None:
            time.sleep(poll_seconds)
