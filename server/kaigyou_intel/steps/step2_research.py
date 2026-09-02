"""STEP2：外部コンテクスト調査（要件 §8）。

STEP1 が見つけた PATTERN の**背景**を Web検索で調べます。ここが、このシステムで
唯一 外部情報に触れる段です（要件 §38）。

呼び出しは 2 回に分かれます。Web検索（サーバ側ツール）と構造化出力は同じ
呼び出しでは併用しないので、

    1 回目  検索する。出力は日本語の文章。出典 URL は API が返す
            web_search_tool_result ブロックから拾う（モデルの自己申告ではない）。
    2 回目  その文章を JSON に写す。検索は切る。

2 回目で検索を切るのは、切らないと 1 回目に無かった事実が増え、
「実際に取得した URL の集合」との照合が意味を失うからです。

出典の検算がこの段のいちばん重要な仕事です。モデルは実在しそうな URL を
書けます。実際に返ってきた URL の集合に無いものは、出典ではありません。
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from kaigyou_core import config as cfg
from kaigyou_core.analysis import DEFAULT_CATEGORY
from kaigyou_intel import client as llm
from kaigyou_intel.projection import for_step2
from kaigyou_intel.schemas import (
    Step2Output, UnansweredQuestion, normalize_url, verify_step2)

STEP_NUMBER = 2


class StepFailed(RuntimeError):
    """このステップが結果を出せなかった。原因は message に。"""


def build_input(step1_output: Mapping[str, Any],
                dataset: Mapping[str, Any]) -> dict[str, Any]:
    """PATTERN と地点と、近隣医院の名前だけ（要件の Input からの意図的な差）。

    要件 §8 の Input は ``base_data + step1_output`` ですが、base_data を渡すと
    外部情報を調べずに手元の数字を言い換えたものが「外部事実」として返ってきます。
    STEP1 が既に読んだ数字をもう一度読ませる利得より、その害のほうが大きい。
    必要な文脈は PATTERN の ``evidence_summary`` に入っています。

    医院の名前だけは渡します。インプラント・審美・訪問診療は標榜診療科目に
    無く、届出の自由記載欄にしかありません。つまり**手元のデータでは原理的に
    分からない**論点で、固有名詞が無ければ外部でも調べようがありません。
    """
    limits = cfg.analysis_config().get("limits") or {}
    return for_step2(step1_output, dataset, limits)


def _prompts(limits: Mapping[str, Any], available: Any = None,
             category: str = DEFAULT_CATEGORY) -> tuple[str, str]:
    settings = llm.step_settings(STEP_NUMBER)
    if not settings.get("prompt_structure"):
        raise StepFailed("config/analysis.yaml の steps.2 に prompt_structure がありません")
    total = llm.max_searches(limits)
    # 統計に載らない定性要因の枠は STEP1 と同じものを渡します。片方だけに
    # 書くと、STEP1 が立てた問いを STEP2 が別の枠で読むことになります。
    from kaigyou_intel.steps.step1_features import _factor_frame

    research = cfg.prompt_text(settings["prompt"], category) \
        .replace("{searches_per_pattern}", str(limits.get("searches_per_pattern", 3))) \
        .replace("{max_searches_total}", str(total)) \
        .replace("{qualitative_factors}",
                 _factor_frame(cfg.hypotheses_config(category), available))
    return research, cfg.prompt_text(settings["prompt_structure"], category)


@dataclass
class _Finding:
    """1 つの PATTERN について調べた結果。"""

    pattern_id: str
    text: str
    sources: list[dict[str, Any]]
    usage: llm.Usage
    error: str | None = None
    #: サーバ側ツールが返したエラー。**例外ではなく content の中身として
    #: HTTP 200 で返ります。** 空の結果として扱うと「調査済み・該当なし」が
    #: レポートに残ります。
    search_errors: list[str] = field(default_factory=list)


def _research_one(pattern: Mapping[str, Any], location: Mapping[str, Any],
                  surroundings: Mapping[str, Any] | None,
                  system: str, max_uses: int) -> _Finding:
    """PATTERN 1 つを調べる。**失敗しても例外を上げません。**

    4 本のうち 1 本が落ちただけで、通った 3 本ぶんの検索と時間を捨てるのは
    割に合いません。落ちたことは呼び出し側が unanswered に書き残します。

    ``surroundings`` は STEP1 が最初に調べた「その場所に何があるか」です。
    **再調査させないために渡します。** 無いと、各担当がめいめいにキャンパスや
    モールを調べ直し、限られた検索回数をそこで使い切ります。
    """
    pattern_id = str(pattern.get("id") or "P000")
    asked = ("以下が STEP1 で見つかった商圏の特徴のうち、**あなたが調べる 1 つ**です。"
             "この PATTERN の research_questions にだけ答えてください。"
             "他の PATTERN は別の担当が調べています。\n\n"
             "```json\n"
             + json.dumps({"location": location, "surroundings": surroundings,
                           "pattern": pattern},
                          ensure_ascii=False, indent=1)
             + "\n```")
    try:
        result = llm.ask(step_number=STEP_NUMBER, system=system, user=asked,
                         max_uses=max_uses)
    except Exception as exc:  # noqa: BLE001 - 1 本の失敗で全部を捨てない
        return _Finding(pattern_id, "", [], llm.Usage(),
                        error=f"{type(exc).__name__}: {exc}")
    return _Finding(
        pattern_id, result.text or "",
        [s for s in result.sources if s.get("url")], result.usage,
        error=None if (result.text or "").strip() else "本文が空でした",
        search_errors=[str(s["error"]) for s in result.sources if s.get("error")])


def run(payload: Mapping[str, Any], category: str = DEFAULT_CATEGORY,
        ) -> tuple[dict[str, Any], llm.Usage, list[dict[str, Any]]]:
    """PATTERN を調べて、外部事実と仮説を返す。

    **PATTERN ごとに呼び出しを分けて、同時に走らせます。** 1 本にまとめると、
    サーバ側の検索ループが 1 本の中で直列に回り、増えていく文脈を毎回読み
    直します。実測（沼津・4検索）で入力 794,572 トークン・5分36秒。レポート
    1 本 11 分のうち、この段だけで半分を使っていました。

    調べる中身は PATTERN ごとに独立しているので、分けても答えは変わりません。
    変わるのは待ち時間で、直列の合計から**いちばん遅い 1 本**になります。

    返り値は (出力, 使用量, 出典)。出典には pattern_id を付けて返します。
    どの PATTERN を調べていて出てきた URL かが分からないと、§25 の追跡が
    「この主張の出典は」で止まります。
    """
    limits = cfg.analysis_config().get("limits") or {}
    research_prompt, structure_prompt = _prompts(
        limits, payload.get("available_keys"), category)

    patterns = [p for p in (payload.get("patterns") or []) if p.get("id")]
    if not patterns:
        raise StepFailed("調べる PATTERN がありません。STEP1 の出力を確認してください。")
    location = payload.get("location") or {}
    per_pattern = max(1, int(limits.get("searches_per_pattern", 2)))
    # 全体の上限も守ります。PATTERN あたりの上限を掛けた数が全体を超える
    # なら、調べる PATTERN のほうを削ります（importance の高い順に並んで
    # いるので、削るのは後ろから）。
    budget = llm.max_searches(limits)
    keep = max(1, budget // per_pattern)
    skipped = [str(p["id"]) for p in patterns[keep:]]
    patterns = patterns[:keep]

    findings = _research_all(patterns, location, payload.get("surroundings"),
                             research_prompt, per_pattern)

    retrieved = [s for finding in findings for s in finding.sources]
    text = "\n\n".join(f"## {f.pattern_id}\n\n{f.text}"
                        for f in findings if f.text.strip())
    failed = [f for f in findings if f.error]

    # 検索そのものが動かなかった。「外部情報が見つからなかった」ではないので、
    # そう記録します。取り違えると、次に読む人が調査済みだと思います。
    search_errors = [e for f in findings for e in f.search_errors]
    if search_errors and not retrieved:
        raise StepFailed("Web検索が実行できませんでした: " + ", ".join(search_errors))

    if not text.strip():
        raise StepFailed(
            "調査の本文が空でした"
            + (f"（{len(failed)}件の呼び出しが失敗: "
               + "; ".join(f"{f.pattern_id} {f.error}" for f in failed) + "）"
               if failed else ""))

    # 2 回目：書き写す。取得した URL の一覧を明示して渡します。ここに無い URL を
    # 書けば下の検算で落ちるので、「一覧から選ぶ」ほうが易しい問題になります。
    catalogue = "\n".join(
        f"- {s['url']}  {s.get('title') or ''}" for s in retrieved) or "（なし）"
    structured = llm.ask(
        step_number=STEP_NUMBER, system=structure_prompt,
        # 書き写すだけの呼び出しです。考えさせると、調べていないことを補い
        # 始めます（そして下の出典の検算で落ちます）。
        effort=llm.step_settings(STEP_NUMBER)["effort_structure"],
        user=("## 調査結果\n\n" + text
              + "\n\n## 今回の検索で取得した URL（source_url はこの中から選ぶこと）\n\n"
              + catalogue
              + "\n\n## 調べていた PATTERN\n\n```json\n"
              + json.dumps(patterns, ensure_ascii=False, indent=1)
              + "\n```"),
        schema=Step2Output, web_search=False)

    output: Step2Output | None = structured.parsed
    if output is None:
        raise StepFailed("構造化出力を受け取れませんでした")

    allowed = {p["id"] for p in patterns}
    urls = {s["url"] for s in retrieved}
    dropped = _drop_unverifiable(output, urls)
    # 調べられなかったぶんは黙って消しません。「調べたが確認できなかった」と
    # 「そもそも調べていない」は別のことです。
    output.unanswered = list(output.unanswered) + [
        f"{f.pattern_id} は調査の呼び出しが失敗したため調べられていません: {f.error}"
        for f in failed] + [
        f"{pid} は検索回数の上限に達したため調べていません。" for pid in skipped]

    # **調べても出ないと分かっている問いは、検索せずに現地確認へ回します。**
    #
    # 実測：これを分けていなかったとき、外部調査の半分が「その統計は存在
    # しない」という報告になりました。「市内の歯科衛生士・歯科医師の年齢
    # 構成」「在宅療養支援歯科診療所の届出数」——**公表されていないことは
    # 分かっているのに、分析のたびに検索していました。** 空振りは検索の
    # 上限と費用を使い切り、そのぶんは答えの出る問いに回りません。
    #
    # 落とすのではありません。重要なのに調べられない問いは、そのまま
    # 「開業前に現地で確かめること」になります（指示書 §56）。
    _send_to_the_field(output, payload.get("questions_for_the_field") or [])

    # 問いが渡っていない古い入力では None（検算を掛けません）。掛けると、
    # 古い形の再実行が毎回「存在しない QUESTION」で埋まります。
    #
    # 現地へ回した問いも「実在する QUESTION」です。検算から漏らすと、
    # _send_to_the_field が足した open_questions が毎回落とされます。
    question_ids = {str(q.get("id")) for q in (payload.get("questions") or [])
                    if q.get("id")}
    question_ids |= {str(q.get("id"))
                     for q in (payload.get("questions_for_the_field") or [])
                     if q.get("id")}
    problems = verify_step2(output, allowed, urls, question_ids or None)
    if problems:
        raise StepFailed(
            "参照が解決しませんでした: "
            + "; ".join(f"{p.where}: {p.problem}" for p in problems))
    if dropped and not output.external_facts:
        raise StepFailed(
            "出典を確かめられた外部事実がひとつも残りませんでした"
            f"（{len(dropped)}件を除外）。")

    usage = llm.Usage(
        input_tokens=sum(f.usage.input_tokens for f in findings)
        + structured.usage.input_tokens,
        output_tokens=sum(f.usage.output_tokens for f in findings)
        + structured.usage.output_tokens,
        web_searches=sum(f.usage.web_searches for f in findings)
        + structured.usage.web_searches,
        cache_read_tokens=sum(f.usage.cache_read_tokens for f in findings)
        + structured.usage.cache_read_tokens,
        cache_write_tokens=sum(f.usage.cache_write_tokens for f in findings)
        + structured.usage.cache_write_tokens,
    )

    # 2 周目以降。**前の周が何かを残したときだけ**走ります。
    for round_number in range(2, int(limits.get("research_rounds", 1)) + 1):
        if not _should_iterate(output, limits):
            break
        before = len(output.external_facts)
        extra_usage, extra_sources = _another_round(
            output, patterns, location, payload, limits, category, round_number)
        usage = _add(usage, extra_usage)
        retrieved = retrieved + extra_sources
        # **何も足せなかった周の次は、走らせません。** 同じ問いに、同じ
        # 「公表されていない」が返ってくるだけです。増えるのは費用と時間で、
        # 分かることは増えません。
        if len(output.external_facts) == before:
            output.unanswered = list(output.unanswered) + [
                f"{round_number} 周目で新しい外部事実が得られなかったため、"
                "調べ直しをここで打ち切りました。"]
            break

    return output.model_dump(), usage, _sources_with_patterns(retrieved, output)


def _send_to_the_field(output: Step2Output,
                       questions: Sequence[Mapping[str, Any]]) -> None:
    """検索せずに、そのまま現地確認へ回す。

    **これは諦めではなく、判断です。** 公表されていないと分かっているものを
    検索して「その統計は存在しません」という報告を持ち帰るより、最初から
    現地確認に回すほうが、正確で、速く、安い。

    ``what_would_settle_it`` には STEP1 の ``what_would_answer_it`` を使います。
    問いを立てた段で調べ方まで決めさせてあるので、ここで考え直しません。
    """
    known = {str(q.question_id) for q in output.open_questions}
    for question in questions:
        qid = str(question.get("id") or "")
        if not qid or qid in known:
            continue
        output.open_questions.append(UnansweredQuestion(
            question_id=qid,
            why=str(question.get("why_not_searchable")
                    or "公表資料に無いことが分かっているため、検索していません。"),
            what_would_settle_it=str(question.get("what_would_answer_it") or "")
            or "現地確認または自治体への照会。"))


# --------------------------------------------------------------- 2 周目以降
#
# 1 周目は STEP1 が立てた問いをそのまま検索に持っていきます。実際の調査は
# そうではありません。**1 回目に何が出てこなかったかを見て、次に何を引くかが
# 決まります。**
#
# とくに CONTRADICTED——「調べたら違うと分かった」——は、**問いがまだ生きて
# いるのに 1 周目で止まると、そこで終わりになります。** 人がやるなら
# 「では何が本当の理由なのか」と続けるところです。
#
# 周の数は ``limits.research_rounds`` です。既定は 2。**3 以上にできますが、
# 既定を上げていません**——2 周目で決着しなかったものは、たいてい公表されて
# いないことだからです。上げてよいかは `analyze --questions` の「2 周目で
# 決着」の件数が貯まれば分かります。回してみるための口は開けてあります。


def _should_iterate(output: Step2Output, limits: Mapping[str, Any]) -> bool:
    """次の周を走らせるか。

    残っているものが無ければ走りません。**走らせても足すものがありません**し、
    1 件あたりの費用と時間はそのぶん増えます。
    """
    if int(limits.get("research_rounds", 1)) < 2:
        return False
    if max(1, int(limits.get("followup_searches", 0))) < 1:
        return False
    return bool(output.open_questions
                or any(h.status == "CONTRADICTED" for h in output.hypotheses))


def _another_round(output: Step2Output, patterns: Sequence[Mapping[str, Any]],
                   location: Mapping[str, Any], payload: Mapping[str, Any],
                   limits: Mapping[str, Any], category: str,
                   round_number: int = 2,
                   ) -> tuple[llm.Usage, list[dict[str, Any]]]:
    """答えの出なかった問いを、角度を変えて調べ直す。

    **失敗しても例外を上げません。** 前の周は既に払った検索と時間の上に
    立っています。この周がしくじったからといってそれを捨てるのは割に
    合いません。落ちたことは ``unanswered`` に残ります。

    ``output`` をその場で書き換えます（呼び出し側が model_dump します）。
    """
    settings = llm.step_settings(STEP_NUMBER)
    name = settings.get("prompt_followup")
    if not name:
        return llm.Usage(), []
    budget = max(1, int(limits.get("followup_searches", 3)))
    try:
        system = cfg.prompt_text(name, category).replace(
            "{followup_searches}", str(budget)).replace(
            "{round_number}", str(round_number))
        result = llm.ask(
            step_number=STEP_NUMBER, system=system, max_uses=budget,
            user=_followup_brief(output, patterns, location, payload))
    except Exception as exc:  # noqa: BLE001 - 1 周目を捨てない
        output.unanswered = list(output.unanswered) + [
            f"{round_number} 周目の調べ直しは呼び出しに失敗しました"
            f"（{type(exc).__name__}: {exc}）。ここまでの結果はそのままです。"]
        return llm.Usage(), []

    sources = [s for s in result.sources if s.get("url")]
    if not (result.text or "").strip():
        output.unanswered = list(output.unanswered) + [
            f"{round_number} 周目の調べ直しは本文が空でした。"
            "ここまでの結果はそのままです。"]
        return result.usage, sources

    try:
        merged = _structure_followup(result.text, sources, patterns, category)
    except Exception as exc:  # noqa: BLE001
        output.unanswered = list(output.unanswered) + [
            f"{round_number} 周目の結果を JSON に写せませんでした"
            f"（{type(exc).__name__}: {exc}）。ここまでの結果はそのままです。"]
        return result.usage, sources

    _merge(output, merged, {s["url"] for s in sources},
           {str(p["id"]) for p in patterns},
           {str(q.get("id")) for q in (payload.get("questions") or []) if q.get("id")},
           round_number)
    total = llm.Usage(
        input_tokens=result.usage.input_tokens + merged.usage.input_tokens,
        output_tokens=result.usage.output_tokens + merged.usage.output_tokens,
        web_searches=result.usage.web_searches + merged.usage.web_searches,
        cache_read_tokens=result.usage.cache_read_tokens
        + merged.usage.cache_read_tokens,
        cache_write_tokens=result.usage.cache_write_tokens
        + merged.usage.cache_write_tokens)
    return total, sources


def _followup_brief(output: Step2Output, patterns: Sequence[Mapping[str, Any]],
                    location: Mapping[str, Any],
                    payload: Mapping[str, Any]) -> str:
    """2 周目に渡すもの。**1 周目の本文は渡しません。**

    渡すと入力が倍になり、そのぶん時間と金がかかります。角度を変えるのに
    必要なのは「何を調べて何が出なかったか」であって、出た事実の全文では
    ありません。
    """
    questions = {str(q.get("id")): q for q in (payload.get("questions") or [])}
    unsettled = []
    for item in output.open_questions:
        asked = questions.get(str(item.question_id)) or {}
        unsettled.append({
            "question_id": item.question_id,
            "question": asked.get("question"),
            "1周目に答えが出なかった理由": item.why,
            "決着させるには": item.what_would_settle_it,
        })
    dead_ends = [
        {"question_id": h.question_id, "pattern_id": h.pattern_id,
         "1周目に消えた説明": h.statement, "なぜ違うと分かったか": h.reasoning}
        for h in output.hypotheses if h.status == "CONTRADICTED"]
    return (
        "## 地点\n\n```json\n"
        + json.dumps(location, ensure_ascii=False, indent=1)
        + "\n```\n\n## まだ答えが出ていない問い\n\n```json\n"
        + json.dumps(unsettled, ensure_ascii=False, indent=1)
        + "\n```\n\n## 消えた説明（問いはまだ生きています）\n\n```json\n"
        + json.dumps(dead_ends, ensure_ascii=False, indent=1)
        + "\n```\n\n## 調べていた PATTERN\n\n```json\n"
        + json.dumps(list(patterns), ensure_ascii=False, indent=1)
        + "\n```\n\n## ここまでに確認できた事実（重複して調べないため）\n\n"
        + ("\n".join(f"- {f.statement}" for f in output.external_facts)
           or "（なし）"))


def _structure_followup(text: str, sources: Sequence[Mapping[str, Any]],
                        patterns: Sequence[Mapping[str, Any]],
                        category: str) -> Any:
    """2 周目の本文を JSON に写す。1 周目と同じプロンプトを使います。"""
    settings = llm.step_settings(STEP_NUMBER)
    catalogue = "\n".join(
        f"- {s['url']}  {s.get('title') or ''}" for s in sources) or "（なし）"
    return llm.ask(
        step_number=STEP_NUMBER,
        system=cfg.prompt_text(settings["prompt_structure"], category),
        effort=settings["effort_structure"],
        user=("## 調査結果（2 周目）\n\n" + text
              + "\n\n## 今回の検索で取得した URL（source_url はこの中から選ぶこと）"
                "\n\n" + catalogue
              + "\n\n## 調べていた PATTERN\n\n```json\n"
              + json.dumps(list(patterns), ensure_ascii=False, indent=1)
              + "\n```"),
        schema=Step2Output, web_search=False)


def _merge(first: Step2Output, second: Any, urls: set[str],
           allowed_patterns: set[str], allowed_questions: set[str],
           round_number: int = 2) -> None:
    """2 周目の結果を 1 周目に足す。**id は振り直します。**

    どちらの周も C001 から採番します。そのまま足すと、2 周目の C001 が
    1 周目の C001 を指しているように読め、根拠の追跡が静かに壊れます。
    **見た目には何も起きません**——レポートには「C001 による」と出て、
    別の事実が引かれているだけです。

    検算は 1 周目とまったく同じものを掛けます。ただし**落ちても例外は上げず、
    2 周目のほうを捨てます。** 検算を緩めると 2 周目だけ規律が下がり、
    「支持された」と書いてあるのに支持の根拠が無い仮説が混ざります。
    かといって落として例外を上げると、既に払った 1 周目まで消えます。
    """
    output: Step2Output | None = getattr(second, "parsed", None)
    if output is None:
        first.unanswered = list(first.unanswered) + [
            f"{round_number} 周目の構造化出力を受け取れませんでした。"
            "ここまでの結果はそのままです。"]
        return

    _drop_unverifiable(output, urls)
    if not output.external_facts and not output.open_questions:
        return

    problems = verify_step2(output, allowed_patterns, urls,
                            allowed_questions or None)
    if problems:
        first.unanswered = list(first.unanswered) + [
            f"{round_number} 周目の結果は参照が解決しなかったため採用しません"
            "でした（"
            + "; ".join(f"{p.where}: {p.problem}" for p in problems)
            + "）。ここまでの結果はそのままです。"]
        return

    fact_id = {}
    for offset, fact in enumerate(output.external_facts, 1):
        fact_id[fact.id] = f"C{len(first.external_facts) + offset:03d}"
    for fact in output.external_facts:
        fact.id = fact_id[fact.id]
        fact.round = round_number
    hyp_id = {}
    for offset, hypothesis in enumerate(output.hypotheses, 1):
        hyp_id[hypothesis.id] = f"H{len(first.hypotheses) + offset:03d}"
    for hypothesis in output.hypotheses:
        hypothesis.id = hyp_id[hypothesis.id]
        hypothesis.round = round_number
        hypothesis.evidence = [fact_id.get(e, e) for e in hypothesis.evidence]
        for link in hypothesis.evidence_links:
            link.fact_id = fact_id.get(link.fact_id, link.fact_id)

    first.external_facts = list(first.external_facts) + list(output.external_facts)
    first.hypotheses = list(first.hypotheses) + list(output.hypotheses)
    # 2 周目で答えが出た問いは、未確認から外します。**残すと、答えが出たのに
    # 「開業前に現地で確かめること」に並び続けます。**
    settled = {str(h.question_id) for h in output.hypotheses
               if h.question_id and h.status != "UNCERTAIN"}
    first.open_questions = [q for q in first.open_questions
                            if str(q.question_id) not in settled]
    # 2 周目でも出なかったものは、2 周目の言い分で置き換えます。1 周目の
    # 「資料が見つからなかった」より、2 周目の「別の角度でも出なかった」の
    # ほうが、次に何をすればよいかを言えています。
    known = {str(q.question_id) for q in first.open_questions}
    first.open_questions = list(first.open_questions) + [
        q for q in output.open_questions if str(q.question_id) not in known
        and str(q.question_id) not in settled]
    first.unanswered = list(first.unanswered) + list(output.unanswered)


def _add(a: llm.Usage, b: llm.Usage) -> llm.Usage:
    return llm.Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        web_searches=a.web_searches + b.web_searches,
        cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
        cache_write_tokens=a.cache_write_tokens + b.cache_write_tokens)


def _research_all(patterns: Sequence[Mapping[str, Any]],
                  location: Mapping[str, Any],
                  surroundings: Mapping[str, Any] | None, system: str,
                  per_pattern: int) -> list[_Finding]:
    """PATTERN ごとの調査を同時に走らせる。

    順序は入力どおりに揃えます。並行実行の完了順のまま返すと、同じ入力から
    毎回違う本文が組み上がり、プロンプトを直したときの比較ができません。

    同時実行数は設定で上げ下げできます。1 にすれば直列に戻ります
    （API のレート制限に当たったときの逃げ道）。
    """
    limit = max(1, int((cfg.analysis_config().get("limits") or {})
                       .get("parallel_research", 4)))
    if limit == 1 or len(patterns) == 1:
        return [_research_one(p, location, surroundings, system, per_pattern)
                for p in patterns]
    with ThreadPoolExecutor(max_workers=min(limit, len(patterns))) as pool:
        futures = [pool.submit(_research_one, p, location, surroundings, system,
                               per_pattern)
                   for p in patterns]
        return [f.result() for f in futures]


def _drop_unverifiable(output: Step2Output, urls: set[str]) -> list[str]:
    """出典を確かめられなかった外部事実を落とす。**黙って落としません。**

    以前はここでステップごと失敗させていました。出典が実在しないレポートは
    出典が無いレポートより悪い、という判断は変えていません。ですが 12 件中
    1 件の URL のために、通った 11 件と $1 の実行を捨てるのは割に合いません
    （実測：e-stat の検索画面の URL を1つ書いたために STEP2 が丸ごと落ちました）。

    落としたことは ``unanswered`` に書き残します。そこが「調べたが確認できな
    かった」の置き場所で、後続のステップも読みます。数が残らないと、
    「外部情報で確認できた」と読まれてしまいます。
    """
    known = {normalize_url(u) for u in urls if u}
    kept, dropped = [], []
    for fact in output.external_facts:
        if normalize_url(fact.source_url) in known:
            kept.append(fact)
        else:
            dropped.append(fact.id)
    if not dropped:
        return []

    output.external_facts = kept
    # 根拠が全部消えた仮説も落とします。残すと、根拠の無い判定になります。
    surviving = {f.id for f in kept}
    output.hypotheses = [h for h in output.hypotheses
                         if any(ref in surviving for ref in h.evidence)]
    output.unanswered = list(output.unanswered) + [
        f"外部事実 {len(dropped)}件（{', '.join(dropped)}）は、引用された URL が"
        "今回の検索結果に含まれていなかったため除外しました。"
        "内容の当否ではなく、出典を確かめられなかったことによる除外です。"]
    return dropped


def _sources_with_patterns(retrieved: list[dict[str, Any]],
                           output: Step2Output) -> list[dict[str, Any]]:
    """取得した URL に、それを引用した PATTERN の id を添える。

    引用されなかった URL も残します。「調べたが使わなかった」も記録で、
    同じ地点を調べ直すときに何を見たかが分かります。
    """
    cited: dict[str, str] = {}
    for fact in output.external_facts:
        cited.setdefault(normalize_url(fact.source_url), fact.pattern_id)
    return [{**source, "pattern_id": cited.get(normalize_url(source["url"]))}
            for source in retrieved]
