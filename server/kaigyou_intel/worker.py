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
from kaigyou_intel import report
from kaigyou_intel.steps import (
    step1_features, step2_research, step3_demand, step4_strategy, step5_client)

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
    4: step4_strategy.run,
    5: step5_client.run,
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
    if number == max(RUNNERS):
        # 保存するのは最終段の出力です。STEP4 の形は根拠を辿るためのもので、
        # 顧客に渡す文書ではありません。
        # 最終段。レポートは Markdown にして保存します。免責とデータ時点は
        # ここで付けるので、LLM が書き忘れても落ちません。
        report.save(conn, job_id, output, job["base_data"])
        # ファイルにも書き出します。DB の中にあることと、手元にファイルが
        # あることは違います。追加のコマンドを要求しません。
        path = report.write_file(conn, job_id)
        if path is not None:
            output = {**output, "_report_file": str(path)}
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
    if number == 4:
        outputs = [jobs.step_output(conn, job["id"], n) for n in (1, 2, 3)]
        if not all(outputs):
            missing = [f"STEP{n}" for n, out in zip((1, 2, 3), outputs) if not out]
            raise StepNotImplemented(
                f"{'・'.join(missing)} の出力がありません。"
                "レポートは前 3 段の結論だけで書きます。")
        return step4_strategy.build_input(*outputs, job["base_data"])
    if number == 5:
        step3_output = jobs.step_output(conn, job["id"], 3)
        step4_output = jobs.step_output(conn, job["id"], 4)
        if not step3_output or not step4_output:
            raise StepNotImplemented(
                "STEP3・STEP4 の出力がありません。顧客向けレポートは、"
                "その結論を書き直す段です。")
        return step5_client.build_input(step3_output, step4_output, job["base_data"])
    raise StepNotImplemented(f"STEP{number} の入力の作り方が未定義です")


def advance(conn: psycopg.Connection, job_id: str,
            progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """1 ステップだけ進めて、Job を待ち行列に戻す。

    ``run_job`` は最後まで回します。手元で動かすならそれでよいのですが、
    ホスティングされた関数には実行時間の上限があり（Vercel の Hobby で 300 秒、
    Pro で 800 秒）、5 段を 1 回の呼び出しには収められません。

    そこで **1 呼び出し = 1 ステップ**にします。終わったら queued に戻すので、
    次の呼び出しが続きを拾います。running のまま置くより、こちらのほうが
    途中で関数が消えたときに強い。状態は全部 DB にあります。
    """
    say = progress or (lambda _m: None)
    number = jobs.next_step(conn, job_id)
    if number is None:
        jobs.release_job(conn, job_id, "completed")
        return {"job_id": job_id, "status": "completed", "step": None}

    settings = llm.step_settings(number)
    say(f"  {job_id}: STEP{number} {settings['name']} …")
    try:
        result = run_step(conn, job_id, number)
    except StepNotImplemented as exc:
        say(f"    停止: {exc}")
        jobs.release_job(conn, job_id, "blocked", str(exc))
        return {"job_id": job_id, "status": "blocked", "step": number,
                "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        say(f"    失敗: {type(exc).__name__}: {exc}")
        jobs.release_job(conn, job_id, "failed", f"STEP{number}: {exc}")
        return {"job_id": job_id, "status": "failed", "step": number,
                "error": f"{type(exc).__name__}: {exc}"}

    done = jobs.next_step(conn, job_id) is None
    jobs.release_job(conn, job_id, "completed" if done else "queued")
    if result.get("_report_file"):
        say(f"    レポートを書き出しました: {result['_report_file']}")
    return {"job_id": job_id, "status": "completed" if done else "queued",
            "step": number, "report_file": result.get("_report_file")}


def tick(conn: psycopg.Connection, *, stale_after_minutes: float = 20.0,
         progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """外から定期的に叩かれる入口。1 回につき 1 ステップ。

    Vercel Cron（Pro なら1分ごと）か、Supabase の pg_cron から呼びます。
    どちらから叩いても同じことをします。
    """
    say = progress or (lambda _m: None)
    recovered = jobs.recover_stale(conn, stale_after_minutes)
    if recovered:
        say(f"途中で止まっていたジョブ {len(recovered)} 件を待ち行列に戻しました。")

    job_id = jobs.claim_job(conn)
    if job_id is None:
        return {"claimed": None, "recovered": recovered, "status": "idle"}
    outcome = advance(conn, job_id, progress=say)
    return {**outcome, "claimed": job_id, "recovered": recovered}


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
            result = run_step(conn, job_id, number)
            if result.get("_report_file"):
                say(f"    レポートを書き出しました: {result['_report_file']}")
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
        # 段を増やしたとき、既にある Job には行がありません。足しておかないと
        # 「全部終わった」と読まれて、増やした段が黙って飛ばされます。
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM analysis_jobs "
                        "WHERE status IN ('queued','blocked','failed','completed')")
            for row in cur.fetchall():
                jobs.ensure_steps(conn, str(row["id"]))
        # 未実装で止まっていた Job を、実装済みになっていれば拾い直します。
        # 「実装したあとにどのジョブを再開するか」を人が覚えている必要は
        # 無いはずです。
        resumed = jobs.requeue_unblocked(conn, RUNNERS)
    if resumed:
        say(f"未実装で止まっていたジョブ {resumed} 件を再開します。")

    if job_id is not None:
        with connect() as conn:
            jobs.ensure_steps(conn, job_id)
            claimed = jobs.claim_specific(conn, job_id)
            if claimed is None:
                say(f"ジョブ {job_id} は見つからないか、取り下げ済みです。")
                return 0
            say(f"ジョブ {claimed} を開始します")
            run_job(conn, claimed, progress=say)
        return 1

    handled = 0
    told_why_idle = False
    while True:
        with connect() as conn:
            claimed = jobs.claim_job(conn)
            if claimed is not None:
                say(f"ジョブ {claimed} を開始します")
                run_job(conn, claimed, progress=say)
                handled += 1
                told_why_idle = False
            elif not once and not told_why_idle:
                # 何も言わずに待ち続けると、動いているのか壊れているのか
                # 分かりません。実測：失敗したジョブが3件あるのに worker が
                # 黙って待ち、「レポートが生成されない」となりました。
                for line in idle_reason(conn):
                    say(line)
                told_why_idle = True
        if once:
            return handled
        if claimed is None:
            time.sleep(poll_seconds)


def idle_reason(conn: psycopg.Connection) -> list[str]:
    """待っているものが無い理由。

    worker は queued しか拾いません（失敗した Job を自動で拾い直すと、壊れた
    まま何度も課金されるため）。拾えない Job があるなら、そのことと再開の
    仕方を言います。黙っているのがいちばん困る。
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT status, count(*) AS n,
                   (array_agg(id ORDER BY created_at DESC))[1] AS newest
            FROM analysis_jobs WHERE status IN ('failed', 'running', 'blocked')
            GROUP BY status
        """)
        rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        return ["待っているジョブはありません。地図から分析を開始してください。"]

    label = {"failed": "失敗したまま", "running": "実行中のまま",
             "blocked": "未実装のステップで待機中"}
    lines = []
    for row in rows:
        lines.append(f"{label[row['status']]}のジョブが {row['n']} 件あります"
                     "（自動では拾いません）。")
        lines.append(f"  再開: python -m kaigyou_etl analyze --job {row['newest']}")
    lines.append("  一覧: python -m kaigyou_etl analyze --list")
    return lines
