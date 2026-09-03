"""競合分析 STEP2：集計された競争環境を、言葉にする（指示書 §6）。

**数え上げは済んでいます。** ここでやるのは、その数字が何を意味するかを
言うことだけです。指示書 §8 の Fact → Analysis → Hypothesis のうち、
Hypothesis にあたります。

    Fact       … Web / GIS から確認できた事実（STEP1）
    Analysis   … 集計・比較した結果（kaigyou_core/competition.py、Python）
    Hypothesis … そこから考えられる市場機会（この段）

**新しい数字を作らせません。** 集計値は入力にあり、プロンプトが「数えるのは
済んでいます」と言います。
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from kaigyou_core import competition
from kaigyou_core import config as cfg
from kaigyou_core.analysis import DEFAULT_CATEGORY
from kaigyou_intel import client as llm
from kaigyou_intel.schemas import CompetitionSummary

STEP_NUMBER = 2


class StepFailed(RuntimeError):
    pass


def build_input(survey: Mapping[str, Any],
                category: str = DEFAULT_CATEGORY) -> dict[str, Any]:
    """集計とポジショニングマップ。**ここで数え終えます。**"""
    conf = cfg.competitors_config(category)
    competitors = list(survey.get("competitors") or [])
    near = int(((conf.get("survey") or {}).get("near_radius_m")) or 500)
    return {
        "label": conf.get("label") or "競合",
        "radius_m": survey.get("radius_m"),
        "tally": competition.tally(competitors, conf, near_radius_m=near),
        # **1 件ずつ、何を読んで何が分かったか。** これが無いと、読んだ人は
        # 同じことをゼロからやり直すことになります。
        "competitors": competitors,
        "positioning_map": competition.positioning_map(competitors, conf),
        # 調べられなかったぶん。**「この地域には少ない」と読ませないため。**
        "coverage": {
            "surveyed": survey.get("surveyed"),
            "requested": survey.get("requested"),
            "failed": survey.get("failed") or [],
            "not_surveyed": survey.get("not_surveyed", 0),
            # 時間切れで手を付けられなかった医院。上限で切ったのとは理由が
            # 違うので、分けて持ちます（やり直せば結果が変わりうるのは
            # こちらだけです）。
            "out_of_time": survey.get("out_of_time") or [],
            "total_in_radius": survey.get("total_in_radius"),
            # レポートは「半径◯m の中の件数」と書きます。半径を落とすと、
            # 件数だけが残って範囲が消えます。
            "radius_m": survey.get("radius_m"),
        },
    }


def run(payload: Mapping[str, Any], category: str = DEFAULT_CATEGORY,
        ) -> tuple[dict[str, Any], llm.Usage, list[dict[str, Any]]]:
    settings = llm.step_settings(STEP_NUMBER)
    if not (payload.get("tally") or {}).get("surveyed"):
        raise StepFailed("集計できる競合がありません。STEP1 の出力を確認してください。")

    result = llm.ask(
        step_number=STEP_NUMBER,
        system=cfg.prompt_text(settings["prompt"], category),
        user=("## 集計結果（**数え上げは済んでいます**）\n\n```json\n"
              + json.dumps(payload, ensure_ascii=False, indent=1) + "\n```"),
        schema=CompetitionSummary, web_search=False)

    summary: CompetitionSummary | None = result.parsed
    if summary is None:
        raise StepFailed("構造化出力を受け取れませんでした")

    # 集計値はそのまま持ち回します。**レポートは LLM の文ではなく、
    # この数字を出します。**
    return ({**summary.model_dump(),
             # 1 件ずつの中身は、要約とは別に持ち回します。**要約に溶かすと、
             # どの医院の話なのかが消えます。**
             "competitors": payload.get("competitors") or [],
             # 何を競合と呼ぶかは設定の語です。レポートで「競合」と書き直すと、
             # 画面（歯科医院）と文書（競合）で呼び方が食い違います。
             "label": payload.get("label"),
             "tally": payload.get("tally"),
             "positioning_map": payload.get("positioning_map"),
             "coverage": payload.get("coverage")},
            result.usage, [])
