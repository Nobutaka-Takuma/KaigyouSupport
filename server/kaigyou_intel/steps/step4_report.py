"""STEP4：顧客提出用レポート。最終段です。

STEP3 までは根拠を辿れる形（タグと id）で材料を作ります。それは検算のための
形であって、人が読むための形ではありません。`[FACT]` が20個並んだ文書は、
読み手に「自分で要約してください」と言っているのと同じです。

ここで散文に起こし直します。**書き直しであって、書き足しではありません。**

以前はこの手前に「経営判断」の段があり、そこがタグ付きの10章レポートを書いて、
この段がそれを散文に書き直していました。読み手に届くのは散文だけなので、
タグ付きのほうは**捨てるために書いていた**ことになります。実測でレポート1本
32分のうち、その1本ぶんの生成が丸ごと無駄でした。判断は STEP3 に移しました。

散文にすると数字はいくらでも滑らかに増やせるので（「約5万人」は、元が
13,268 でも 494,517 でも文としては通ります）、本文の数値が前の段に実在した
ものかを機械的に照合します。読みやすさのための丸めは通し、別の数になった
ものだけを落とします。
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from kaigyou_core import config as cfg
from kaigyou_core.analysis import DEFAULT_CATEGORY
from kaigyou_intel import client as llm
from kaigyou_intel.projection import allowed_numbers, for_step4
from kaigyou_intel.schemas import Step4Output, verify_step4

STEP_NUMBER = 4


class StepFailed(RuntimeError):
    """このステップが結果を出せなかった。原因は message に。"""


def build_input(step1_output: Mapping[str, Any], step2_output: Mapping[str, Any],
                step3_output: Mapping[str, Any],
                dataset: Mapping[str, Any]) -> dict[str, Any]:
    return for_step4(step1_output, step2_output, step3_output, dataset)


def known_ids(payload: Mapping[str, Any]) -> set[str]:
    """レポートが根拠にできる id の全体。

    7 系統あります（F/P/C/H/S/M/I）。ここに無い id を書いたら、それは
    どの段でも作られていない id です。§25 の追跡はここで切れます。
    """
    step1 = payload.get("step1") or {}
    step2 = payload.get("step2") or {}
    step3 = payload.get("step3") or {}
    groups = (
        step1.get("facts"), step1.get("patterns"),
        step2.get("external_facts"), step2.get("hypotheses"),
        step3.get("patient_segments"), step3.get("demand_mechanisms"),
        step3.get("insights"),
    )
    return {item.get("id") for group in groups for item in (group or [])
            if isinstance(item, Mapping) and item.get("id")}


def required_categories(frame: Mapping[str, Any] | None = None) -> list[str]:
    """support_needed に必ず含めるべき分類。

    商圏の説明で終わらせないための最低線です。これが空のレポートは、
    「どこで開くか」は語っていても「何を建てるか」を語っていません。
    """
    frame = cfg.hypotheses_config() if frame is None else frame
    return [str(c) for c in (frame.get("required_support_categories") or [])]


def run(payload: Mapping[str, Any], category: str = DEFAULT_CATEGORY,
        ) -> tuple[dict[str, Any], llm.Usage, list[dict[str, Any]]]:
    settings = llm.step_settings(STEP_NUMBER)
    from kaigyou_intel.steps.step1_features import requirement_frame

    frame = cfg.hypotheses_config(category)
    system = (cfg.prompt_text(settings["prompt"], category)
              .replace("{dental_requirements}", requirement_frame(frame))
              .replace("{required_support_categories}",
                       " / ".join(required_categories(frame))))

    user = ("以下がこれまでの分析結果です。これを、開業を検討している歯科医師に"
            "手渡せるレポートに書き直してください。\n\n"
            "```json\n" + json.dumps(payload, ensure_ascii=False, indent=1) + "\n```")

    result = llm.ask(step_number=STEP_NUMBER, system=system, user=user,
                     schema=Step4Output)
    output: Step4Output | None = result.parsed
    if output is None:
        raise StepFailed("構造化出力を受け取れませんでした")

    problems = verify_step4(output, known_ids(payload), allowed_numbers(payload),
                            required_categories(frame))
    if problems:
        raise StepFailed(
            "レポートを保存しませんでした: "
            + "; ".join(f"{p.where}: {p.problem}" for p in problems))

    return output.model_dump(), result.usage, []
