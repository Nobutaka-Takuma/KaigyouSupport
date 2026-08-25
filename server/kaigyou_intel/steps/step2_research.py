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
from typing import Any, Mapping

from kaigyou_core import config as cfg
from kaigyou_intel import client as llm
from kaigyou_intel.projection import for_step2
from kaigyou_intel.schemas import Step2Output, normalize_url, verify_step2

STEP_NUMBER = 2


class StepFailed(RuntimeError):
    """このステップが結果を出せなかった。原因は message に。"""


def build_input(step1_output: Mapping[str, Any],
                dataset: Mapping[str, Any]) -> dict[str, Any]:
    """PATTERN と地点だけ。基礎データは渡しません（要件の Input からの意図的な差）。

    要件 §8 の Input は ``base_data + step1_output`` ですが、base_data を渡すと
    外部情報を調べずに手元の数字を言い換えたものが「外部事実」として返ってきます。
    STEP1 が既に読んだ数字をもう一度読ませる利得より、その害のほうが大きい。
    必要な文脈は PATTERN の ``evidence_summary`` に入っています。
    """
    limits = cfg.analysis_config().get("limits") or {}
    return for_step2(step1_output, dataset.get("location") or {}, limits)


def _prompts(limits: Mapping[str, Any]) -> tuple[str, str]:
    settings = llm.step_settings(STEP_NUMBER)
    if not settings.get("prompt_structure"):
        raise StepFailed("config/analysis.yaml の steps.2 に prompt_structure がありません")
    research = cfg.prompt_text(settings["prompt"]) \
        .replace("{searches_per_pattern}", str(limits.get("searches_per_pattern", 3))) \
        .replace("{max_searches_total}", str(limits.get("max_searches_total", 15)))
    return research, cfg.prompt_text(settings["prompt_structure"])


def run(payload: Mapping[str, Any]) -> tuple[dict[str, Any], llm.Usage, list[dict[str, Any]]]:
    """PATTERN を調べて、外部事実と仮説を返す。

    返り値は (出力, 使用量, 出典)。出典には pattern_id を付けて返します。
    どの PATTERN を調べていて出てきた URL かが分からないと、§25 の追跡が
    「この主張の出典は」で止まります。
    """
    limits = cfg.analysis_config().get("limits") or {}
    research_prompt, structure_prompt = _prompts(limits)

    asked = ("以下が STEP1 で見つかった商圏の特徴です。"
             "research_questions に答えてください。\n\n"
             "```json\n" + json.dumps(payload, ensure_ascii=False, indent=1) + "\n```")

    # 1 回目：調べる。
    research = llm.ask(step_number=STEP_NUMBER, system=research_prompt, user=asked)
    retrieved = [s for s in research.sources if s.get("url")]
    if not research.text.strip():
        raise StepFailed("調査の本文が空でした")
    errors = [s.get("error") for s in research.sources if s.get("error")]
    if errors and not retrieved:
        # 検索そのものが動かなかった。「外部情報が見つからなかった」ではないので、
        # そう記録します。取り違えると、次に読む人が調査済みだと思います。
        raise StepFailed("Web検索が実行できませんでした: " + ", ".join(map(str, errors)))

    # 2 回目：書き写す。取得した URL の一覧を明示して渡します。ここに無い URL を
    # 書けば下の検算で落ちるので、「一覧から選ぶ」ほうが易しい問題になります。
    catalogue = "\n".join(
        f"- {s['url']}  {s.get('title') or ''}" for s in retrieved) or "（なし）"
    structured = llm.ask(
        step_number=STEP_NUMBER, system=structure_prompt,
        user=("## 調査結果\n\n" + research.text
              + "\n\n## 今回の検索で取得した URL（source_url はこの中から選ぶこと）\n\n"
              + catalogue
              + "\n\n## 調べていた PATTERN\n\n```json\n"
              + json.dumps(payload.get("patterns") or [], ensure_ascii=False, indent=1)
              + "\n```"),
        schema=Step2Output, web_search=False)

    output: Step2Output | None = structured.parsed
    if output is None:
        raise StepFailed("構造化出力を受け取れませんでした")

    allowed = {p.get("id") for p in (payload.get("patterns") or []) if p.get("id")}
    problems = verify_step2(output, allowed, {s["url"] for s in retrieved})
    if problems:
        raise StepFailed(
            "参照が解決しませんでした: "
            + "; ".join(f"{p.where}: {p.problem}" for p in problems))

    usage = llm.Usage(
        input_tokens=research.usage.input_tokens + structured.usage.input_tokens,
        output_tokens=research.usage.output_tokens + structured.usage.output_tokens,
        web_searches=research.usage.web_searches + structured.usage.web_searches,
        cache_read_tokens=(research.usage.cache_read_tokens
                           + structured.usage.cache_read_tokens),
        cache_write_tokens=(research.usage.cache_write_tokens
                            + structured.usage.cache_write_tokens),
    )
    return output.model_dump(), usage, _sources_with_patterns(retrieved, output)


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
