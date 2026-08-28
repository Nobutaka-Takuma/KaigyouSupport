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
from kaigyou_intel import client as llm
from kaigyou_intel.projection import for_step2
from kaigyou_intel.schemas import Step2Output, normalize_url, verify_step2

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


def _prompts(limits: Mapping[str, Any],
             available: Any = None) -> tuple[str, str]:
    settings = llm.step_settings(STEP_NUMBER)
    if not settings.get("prompt_structure"):
        raise StepFailed("config/analysis.yaml の steps.2 に prompt_structure がありません")
    total = llm.max_searches(limits)
    # 統計に載らない定性要因の枠は STEP1 と同じものを渡します。片方だけに
    # 書くと、STEP1 が立てた問いを STEP2 が別の枠で読むことになります。
    from kaigyou_intel.steps.step1_features import _factor_frame

    research = cfg.prompt_text(settings["prompt"]) \
        .replace("{searches_per_pattern}", str(limits.get("searches_per_pattern", 3))) \
        .replace("{max_searches_total}", str(total)) \
        .replace("{qualitative_factors}",
                 _factor_frame(cfg.hypotheses_config(), available))
    return research, cfg.prompt_text(settings["prompt_structure"])


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
                  system: str, max_uses: int) -> _Finding:
    """PATTERN 1 つを調べる。**失敗しても例外を上げません。**

    4 本のうち 1 本が落ちただけで、通った 3 本ぶんの検索と時間を捨てるのは
    割に合いません。落ちたことは呼び出し側が unanswered に書き残します。
    """
    pattern_id = str(pattern.get("id") or "P000")
    asked = ("以下が STEP1 で見つかった商圏の特徴のうち、**あなたが調べる 1 つ**です。"
             "この PATTERN の research_questions にだけ答えてください。"
             "他の PATTERN は別の担当が調べています。\n\n"
             "```json\n"
             + json.dumps({"location": location, "pattern": pattern},
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


def run(payload: Mapping[str, Any]) -> tuple[dict[str, Any], llm.Usage, list[dict[str, Any]]]:
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
        limits, payload.get("available_keys"))

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

    findings = _research_all(patterns, location, research_prompt, per_pattern)

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

    problems = verify_step2(output, allowed, urls)
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
    return output.model_dump(), usage, _sources_with_patterns(retrieved, output)


def _research_all(patterns: Sequence[Mapping[str, Any]],
                  location: Mapping[str, Any], system: str,
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
        return [_research_one(p, location, system, per_pattern) for p in patterns]
    with ThreadPoolExecutor(max_workers=min(limit, len(patterns))) as pool:
        futures = [pool.submit(_research_one, p, location, system, per_pattern)
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
