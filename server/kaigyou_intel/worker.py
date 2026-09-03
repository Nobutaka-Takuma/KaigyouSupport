"""キューを回す。

ワークステーションで動かします（ETL と同じ場所）。Vercel の関数には実行時間の
上限があり、Web検索を伴う 4 ステップはそこに収まりません。

worker は記憶を持ちません。状態は全部 DB にあります。途中で落としても、
次に起動したときに続きから拾えます。
"""
from __future__ import annotations

import os
import time
import traceback
from typing import Any, Callable, Mapping

import psycopg

from kaigyou_core import config as cfg
from kaigyou_intel import client as llm
from kaigyou_intel import failures
from kaigyou_intel import jobs
from kaigyou_intel import report
from kaigyou_intel.steps import (
    comp1_survey,
    comp2_summary,
    dd_facts,
    dd_write,
    step1_features, step2_research, step3_demand, step4_report)

#: 実装済みのステップ。ここに無い番号に来たら止めます。「未実装なので飛ばす」
#: をやると、材料が無いまま最終レポートが書かれます。
#:
#: どれも受け取るのは**射影済みの入力**です。ステップ自身に作らせていた頃は、
#: worker が記録用に 1 回、ステップが実行用にもう 1 回作っていて、
#: 「記録した入力と実際に渡した入力が同じ」という保証がありませんでした。
#: 受け取るのは (射影済みの入力, 業態) です。業態はジョブの business_type で、
#: プロンプトと KSF の枠をどの業態のものから読むかを決めます。渡さないと、
#: 医科のジョブが歯科のプロンプトで書かれます——**しかも成功と表示されます。**
RUNNERS: dict[int, Callable[[Any, str], Any]] = {
    1: dd_facts.run,
    2: dd_write.run,
}

#: 旧・4段の探索型。**消していません**——既定から外しただけです。
RESEARCH_RUNNERS: dict[int, Callable[[Any, str], Any]] = {
    1: step1_features.run,
    2: step2_research.run,
    3: step3_demand.run,
    4: step4_report.run,
}

#: 競合分析の段。**同じ「STEP1」でも、種類が違えば別の仕事です。**
COMPETITOR_RUNNERS: dict[int, Callable[[Any, str], Any]] = {
    1: comp1_survey.run,
    2: comp2_summary.run,
}

#: 種類ごとの段の表。**モジュール属性を都度読み直します。**
#: 表を辞書に畳んで持つと、RUNNERS を差し替えても畳んだ側は古い辞書を
#: 指したままで、差し替えたつもりの段が動きません（テストが差し替えます）。
_RUNNER_ATTR_BY_KIND = {"area": "RUNNERS", "competitors": "COMPETITOR_RUNNERS",
                        "research": "RESEARCH_RUNNERS"}


def runners_for(kind: str | None) -> dict[int, Callable[[Any, str], Any]]:
    attr = _RUNNER_ATTR_BY_KIND.get(kind or jobs.DEFAULT_KIND, "RUNNERS")
    return globals()[attr]


def all_runner_steps() -> list[int]:
    """種類をまたいだ、実装済みの段番号。"""
    return sorted({n for kind in _RUNNER_ATTR_BY_KIND
                   for n in runners_for(kind)})


class StepNotImplemented(RuntimeError):
    pass


#: この呼び出しが始まった時刻。tick が入れます。手元の worker では None。
_INVOCATION_STARTED: float | None = None


def _seconds_left() -> float | None:
    """この呼び出しに残っている秒数。上限の無い環境では None。

    段そのものに区切りを渡すためのものです。段と段の間で見るだけでは、
    **1 つの段が上限を越えるとき**に何もできません。
    """
    if _INVOCATION_STARTED is None:
        return None
    budget, _reserve = time_budget()
    if budget == float("inf"):
        return None
    # 保存と後片付けのぶんを残します。ぎりぎりまで使うと、調べ終えた結果を
    # 書き込む前に殺されます。
    return max(0.0, budget - (time.monotonic() - _INVOCATION_STARTED) - 60.0)


def _business_type(job: Mapping[str, Any]) -> str:
    """このジョブがどの業態のものか。**設定を読み分ける鍵です。**

    ジョブは業態を持って作られています（``analysis_jobs.business_type``）。
    古いジョブや、まだ列が空のものは既定（歯科）に落とします。
    """
    from kaigyou_core.analysis import DEFAULT_CATEGORY

    return str(job.get("business_type") or DEFAULT_CATEGORY)


def run_step(conn: psycopg.Connection, job_id: str, number: int) -> dict[str, Any]:
    """1 ステップ実行して保存する。

    例外は握って DB に書きます。worker が落ちて理由がどこにも残らないのが
    いちばん困るので、失敗も結果として記録します。
    """
    job = jobs.get_job(conn, job_id, include_base_data=True)
    if job is None:
        raise ValueError(f"job {job_id} が見つかりません")

    kind = jobs.kind_of(conn, job_id)
    # **段のプロンプトは種類ごとに違います。** 同じ「STEP1」でも、周辺一般は
    # 商圏特徴抽出、競合分析は競合の調査です。伝え忘れると、黙って別の
    # プロンプトが使われます。
    #
    # 抜けたら元に戻します。設定しっぱなしにすると、この段が終わったあとも
    # 残り、次に来た別の種類のジョブが——`run_step` を通らない経路で——
    # 前のジョブのプロンプト設定を読みます。
    with llm.for_kind(kind):
        return _run_step(conn, job, job_id, number, kind)


def _run_step(conn: psycopg.Connection, job: Mapping[str, Any], job_id: str,
              number: int, kind: str) -> dict[str, Any]:
    settings = llm.step_settings(number)
    runner = runners_for(kind).get(number)
    if runner is None:
        # 記録は残しません。未実装は「失敗」ではないからです。failed と書くと
        # 実行して壊れたように見えて、実装後に手で直さないと再開できません。
        # 何も書かなければ pending のままなので、RUNNERS に足した次の
        # `analyze --once` がそのまま続きから拾います。
        raise StepNotImplemented(
            f"STEP{number}（{settings['name']}）は未実装です。"
            "実装されるまでこのジョブは進みません。")

    payload = build_input(conn, job, number)

    # 前回落ちているなら、その理由を一緒に渡します。payload はそのまま
    # JSON として user メッセージに載るので、どのステップでも同じように届きます。
    #
    # これが無いと、やり直しは**同じプロンプトの引き直し**でしかありません。
    # 揺らぎで直る失敗（切れた JSON）は直りますが、決定的な失敗は何度でも
    # 同じところで落ちます。実測：STEP5 が検算に落ち、2回とも同じ数字を
    # 書いて2回とも落ちました。
    previous = jobs.last_error(conn, job_id, number)
    if previous:
        payload = {**payload, "_前回の失敗": {
            "内容": previous,
            "指示": "同じ失敗を繰り返さないでください。指摘された箇所だけを直し、"
                    "それ以外はこれまでどおりの方針で書いてください。"
                    "数値は入力にあるものだけを使い、無いものは書かないでください。",
        }}

    jobs.start_step(conn, job_id, number, payload, settings)
    # 例外はここでは握りません。やり直す価値があるかを判定してから記録します
    # （_handle_failure）。ここで failed と書いてしまうと、やり直せる失敗まで
    # 人がボタンを押しに行くことになります。
    output, usage, sources = runner(payload, _business_type(job))

    jobs.finish_step(conn, job_id, number, output, {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "web_searches": usage.web_searches,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
    })
    if sources:
        jobs.save_sources(conn, job_id, number, None, sources)
    # 中間 JSON（指示書 §21）。DB に入っていることと、手元でファイルとして
    # 読めることは違います。プロンプトを直したあと「なぜこの PATTERN が
    # 出たのか」を追うのに、DB クライアントを開かせません。
    # **便宜なので、書けなくても段は成功のままです。**
    try:
        report.write_step_file(conn, job_id, number, payload, output)
    except OSError:
        pass
    if number == max(runners_for(kind)):
        # 最終段。レポートは Markdown にして保存します。免責とデータ時点は
        # ここで付けるので、LLM が書き忘れても落ちません。
        report.save(conn, job_id, output, job["base_data"])
        # ファイルにも書き出します。DB の中にあることと、手元にファイルが
        # あることは違います。追加のコマンドを要求しません。
        #
        # ただし**これは成果物ではなく便宜**です。ここで落ちても、上の save で
        # レポートは DB に入っています。書けない環境（読み取り専用の関数）で
        # 例外を上げると、仕上がったレポートを持ったままステップが失敗し、
        # やり直しにもう $1 かかります。実測でそうなりました。
        try:
            path = report.write_file(conn, job_id)
        except OSError:
            path = None
        if path is not None:
            output = {**output, "_report_file": str(path)}
    return output


def _competitor_input(conn: psycopg.Connection, job: Mapping[str, Any],
                      number: int) -> dict[str, Any]:
    """競合分析の段に渡すもの（開発指示書 §1・§4）。

    STEP1 は GIS が確定した競合一覧、STEP2 は**集計済み**の競争環境です。
    数え上げは Python が済ませてあるので、LLM には数えさせません。
    """
    category = _business_type(job)
    if number == 1:
        payload = comp1_survey.build_input(job["base_data"], category)
        # **この呼び出しに残っている秒数を渡します。**
        #
        # 競合の調査は医院ごとに Web を引くので、商圏によって所要時間が
        # 何倍も違います。関数の上限を越えると段まるごとが失われ、調べ終えて
        # いたぶんも一緒に消えます。実測：12 院の調査が上限（800秒）で殺され、
        # やり直してまた同じところで殺されました。**8 院調べて「4 院は時間
        # 切れ」と書くほうが、12 院ぶんの費用を捨てるより良い。**
        remaining = _seconds_left()
        if remaining is not None:
            payload = {**payload, "_残り秒": remaining}
        return payload
    if number == 2:
        survey = jobs.step_output(conn, job["id"], 1)
        if not survey:
            raise StepNotImplemented(
                "STEP1 の出力がありません。STEP2 は競合の調査結果を集計する段です。")
        return comp2_summary.build_input(survey, category)
    raise StepNotImplemented(f"競合分析に STEP{number} はありません。")


def build_input(conn: psycopg.Connection, job: Mapping[str, Any],
                number: int) -> dict[str, Any]:
    """そのステップに渡すものを組み立てる。

    前のステップの出力が要るものは DB から読みます。worker が記憶を持たない
    ようにしてあるので、途中で落ちても続きから拾えます。
    """
    kind = job.get("analysis_kind") or jobs.DEFAULT_KIND
    if kind == "competitors":
        return _competitor_input(conn, job, number)
    if kind == "research":
        return _research_input(conn, job, number)
    if number == 1:
        # **DB のデータを数えるだけ。** 競合の中身は動的なので、別に走らせた
        # 「周辺の競合を分析する」の結果があれば取り込みます。
        return dd_facts.build_input(job["base_data"],
                                    _competitor_survey(conn, job))
    if number == 2:
        pack = jobs.step_output(conn, job["id"], 1)
        if not pack:
            raise StepNotImplemented(
                "STEP1 の出力がありません。STEP2 は確定した事実を読む段です。")
        return dd_write.build_input(pack, _business_type(job))
    raise StepNotImplemented(f"STEP{number} の入力の作り方が未定義です")


def _research_input(conn: psycopg.Connection, job: Mapping[str, Any],
                    number: int) -> dict[str, Any]:
    """旧・4 段の探索型に渡すもの。**既定からは外しましたが、動きます。**"""
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
        return step4_report.build_input(*outputs, job["base_data"])
    raise StepNotImplemented(f"探索型に STEP{number} はありません。")


def _competitor_survey(conn: psycopg.Connection,
                       job: Mapping[str, Any]) -> dict[str, Any] | None:
    """同じ地点で走らせた競合分析があれば、その結果を借りる。

    **無くても成立します。** 競合の件数と距離は施設データベースから出ますが、
    各院が何を掲げているかは Web を見ないと分かりません。見ていなければ、
    レポートは「調べていない」と書きます。
    """
    def latest(step: int) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.output_json
                FROM analysis_jobs j
                JOIN analysis_steps s ON s.job_id = j.id
                WHERE j.analysis_kind = 'competitors'
                  AND s.step_number = %s AND s.status = 'completed'
                  AND abs(j.latitude - %s) < 0.0005
                  AND abs(j.longitude - %s) < 0.0005
                ORDER BY j.created_at DESC LIMIT 1
            """, (step, job["latitude"], job["longitude"]))
            row = cur.fetchone()
        return dict(row["output_json"] or {}) if row else None

    survey = latest(1)
    if survey is None:
        return None
    summary = latest(2) or {}
    if summary.get("tally"):
        survey["tally"] = summary["tally"]
    return survey


def advance(conn: psycopg.Connection, job_id: str,
            progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """1 ステップだけ進めて、Job を待ち行列に戻す。

    ``run_job`` は最後まで回します。手元で動かすならそれでよいのですが、
    ホスティングされた関数には実行時間の上限があり（Vercel の Hobby で 300 秒、
    Pro で 800 秒）、収まらないことがあります。

    そこで **1 呼び出し = 1 ステップ**にします。終わったら queued に戻すので、
    次の呼び出しが続きを拾います。running のまま置くより、こちらのほうが
    途中で関数が消えたときに強い。状態は全部 DB にあります。
    """
    say = progress or (lambda _m: None)
    number = jobs.next_step(conn, job_id)
    if number is None:
        jobs.release_job(conn, job_id, "completed")
        return {"job_id": job_id, "status": "completed", "step": None}

    # **段の名前も種類ごとに違います。** ここで種類を渡さないと、競合分析の
    # ジョブの進捗に「商圏特徴抽出」と出ます。動きはしますが、読む人には
    # どちらの分析が走っているのか分かりません。
    settings = llm.step_settings(number, kind=jobs.kind_of(conn, job_id))

    # 走らせる前に回数を見ます。_handle_failure は例外を捕まえたときにしか
    # 通らないので、関数が強制終了された場合そこを通りません。ここで見ないと、
    # 上限に収まらないステップが永久に回り続けます（費用だけが増えます）。
    limit = _retry_limit()
    attempts = jobs.attempts_for(conn, job_id, number)
    if attempts >= limit:
        reason = jobs.last_error(conn, job_id, number) or "原因は記録されていません。"
        detail = f"STEP{number} を {attempts} 回試みましたが完了しませんでした: {reason}"
        say(f"    打ち切り: {detail}")
        jobs.fail_step(conn, job_id, number, detail)
        jobs.release_job(conn, job_id, "failed", detail)
        return {"job_id": job_id, "status": "failed", "step": number,
                "attempt": attempts, "error": detail}

    say(f"  {job_id}: STEP{number} {settings['name']} …")
    try:
        result = run_step(conn, job_id, number)
    except StepNotImplemented as exc:
        say(f"    停止: {exc}")
        jobs.release_job(conn, job_id, "blocked", str(exc))
        return {"job_id": job_id, "status": "blocked", "step": number,
                "error": str(exc)}
    except comp1_survey.OutOfTime as exc:
        # **失敗ではありません。** この呼び出しに時間が残っていなかっただけ
        # です。やり直し回数を食わせると、いちばん時間のかかる商圏が、
        # いちばん試行回数を使えないことになります。
        say(f"    時間切れ: {exc}")
        jobs.reset_step(conn, job_id, number)
        jobs.release_job(conn, job_id, "queued")
        return {"job_id": job_id, "status": "queued", "step": None,
                "waiting": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return _handle_failure(conn, job_id, number, exc, say)

    done = jobs.next_step(conn, job_id) is None
    jobs.release_job(conn, job_id, "completed" if done else "queued")
    if result.get("_report_file"):
        say(f"    レポートを書き出しました: {result['_report_file']}")
    return {"job_id": job_id, "status": "completed" if done else "queued",
            "step": number, "report_file": result.get("_report_file")}


def _handle_failure(conn: psycopg.Connection, job_id: str, number: int,
                    exc: BaseException,
                    say: Callable[[str], None]) -> dict[str, Any]:
    """失敗を、やり直す価値があるかで分ける。

    モデルの言い間違い（存在しない出典・参照の取り違え・長さの上限で切れた JSON）は
    一定の確率で起きます。1 回で止めると、そのたびに人がボタンを押しに行くことに
    なります。数回は黙って直させます。

    ただし**何度やっても直らないもの**（残高不足・キー不正・安全性の拒否）を
    繰り返すと、費用だけが増えます。そこは 1 回で止めます。
    """
    limit = _retry_limit()
    detail = f"{type(exc).__name__}: {exc}"
    attempts = jobs.attempts_for(conn, job_id, number)

    if failures.is_retryable(exc) and attempts + 1 < limit:
        count = jobs.retry_step(conn, job_id, number, detail)
        say(f"    やり直します（{count}/{limit} 回目）: {exc}")
        # queued のまま。次の呼び出しが同じステップを拾い直します。
        jobs.release_job(conn, job_id, "queued")
        return {"job_id": job_id, "status": "retrying", "step": number,
                "attempt": count, "error": detail}

    say(f"    失敗: {detail}")
    say(f"    {failures.describe(exc, attempts + 1, limit)}")
    jobs.fail_step(conn, job_id, number, f"{detail}\n"
                   f"{traceback.format_exc(limit=3)}")
    jobs.release_job(conn, job_id, "failed", f"STEP{number}: {exc}")
    return {"job_id": job_id, "status": "failed", "step": number,
            "attempt": attempts + 1, "error": detail}


def _retry_limit() -> int:
    settings = cfg.analysis_config().get("worker") or {}
    return max(1, int(settings.get("max_attempts", 3)))


def hosted() -> bool:
    """ホスティングされた関数の中か。

    実行時間の上限があるかどうかで、待ち方を変えます。手元の worker は
    好きなだけ動けるので、消えたと判断するまで長く待ってよい。関数は
    上限で必ず殺されるので、上限より長く待つのはただの空白です。
    """
    return any(os.getenv(v) for v in ("VERCEL", "AWS_LAMBDA_FUNCTION_NAME", "K_SERVICE"))


def stale_after() -> float:
    """消えたとみなすまでの分数。環境で変えます。"""
    settings = cfg.analysis_config().get("worker") or {}
    if hosted():
        return float(settings.get("hosted_stale_after_minutes", 6))
    return float(settings.get("stale_after_minutes", 20))


def time_budget() -> tuple[float, float]:
    """(1回の呼び出しに使える秒数, 1ステップの最長の見込み)。

    手元の worker には上限が無いので、無限として扱います。
    """
    settings = cfg.analysis_config().get("worker") or {}
    if not hosted():
        return (float("inf"), 0.0)
    return (float(settings.get("invocation_seconds", 800)),
            float(settings.get("reserve_seconds", 420)))


def tick(conn: psycopg.Connection, *, stale_after_minutes: float | None = None,
         progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """外から定期的に叩かれる入口。時間が許すかぎり続けて進めます。

    Vercel Cron（Pro なら1分ごと）か、Supabase の pg_cron から呼びます。
    どちらから叩いても同じことをします。

    **1 呼び出し 1 ステップだと、ステップの間に cron の間隔がまるごと空きます。**
    4 段で最大4分、平均2分。ただ待っているだけの時間です。関数の上限が
    800秒 になったので、残り時間が足りるうちは続けて回します。

    判断は「足りるか分からないなら進まない」。次の段に必要な時間は事前には
    分からないので、設定に置いた**1ステップの最長の見込み**（reserve_seconds）
    を使い、それが残っていなければ queued に戻して次の呼び出しに任せます。
    途中で殺されるとそのステップは丸ごとやり直しで、時間も費用も倍かかります。
    早めに手を引くほうが安い。
    """
    say = progress or (lambda _m: None)
    if stale_after_minutes is None:
        stale_after_minutes = stale_after()
    recovered = jobs.recover_stale(conn, stale_after_minutes)
    if recovered:
        say(f"途中で止まっていたジョブ {len(recovered)} 件を待ち行列に戻しました。")

    job_id = jobs.claim_job(conn)
    if job_id is None:
        return {"claimed": None, "recovered": recovered, "status": "idle"}

    budget, reserve = time_budget()
    started = time.monotonic()
    # 段そのものに区切りを渡せるようにします。段と段の間で見るだけでは、
    # **1 つの段が上限を越えるとき**に手の打ちようがありません。
    global _INVOCATION_STARTED
    _INVOCATION_STARTED = started
    steps_done: list[int] = []
    outcome: dict[str, Any] = {}

    while True:
        outcome = advance(conn, job_id, progress=say)
        if outcome.get("step") is not None:
            steps_done.append(int(outcome["step"]))
        # 続きがあるときだけ queued に戻ります。それ以外（完了・失敗・
        # 待ち・やり直し待ち）は、この呼び出しでは何もしません。
        if outcome.get("status") != "queued":
            break
        spent = time.monotonic() - started
        if spent + reserve >= budget:
            say(f"  残り時間が足りないので、ここまで（{spent:.0f}秒使用）。"
                "続きは次の呼び出しが拾います。")
            break
        # 続けて拾い直します。claim は running を立てるので、同時に走る
        # 別の呼び出しがこの Job を横取りすることはありません。
        if jobs.claim_specific(conn, job_id) is None:
            break

    _INVOCATION_STARTED = None
    return {**outcome, "claimed": job_id, "recovered": recovered,
            "steps_completed": steps_done}


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
            # やり直す価値があるかは 1 か所で判定します。手元の worker と
            # 関数の worker で、同じ失敗の扱いが違うと追えなくなります。
            outcome = _handle_failure(conn, job_id, number, exc, say)
            if outcome["status"] != "retrying":
                say(f"    直したら: python -m kaigyou_etl analyze --once --job {job_id}"
                    f"  （STEP{number} からやり直します。済んだステップは残ります）")
                return "failed"
            # queued に戻っているので、このまま次の周回で同じステップを拾い直します。
            jobs.claim_specific(conn, job_id)


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
        # 種類をまたいで、実装済みの段番号すべて。
        resumed = jobs.requeue_unblocked(conn, all_runner_steps())
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
