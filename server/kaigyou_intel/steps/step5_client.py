"""STEP5：顧客提出用レポート。

STEP4 までは根拠を辿れる形（タグと id）で材料を作ります。それは検算のための
形であって、人が読むための形ではありません。`[FACT]` が20個並んだ文書は、
読み手に「自分で要約してください」と言っているのと同じです。

ここで散文に起こし直します。**書き直しであって、書き足しではありません。**

散文にすると数字はいくらでも滑らかに増やせるので（「約5万人」は、元が
13,268 でも 494,517 でも文としては通ります）、本文の数値が前の段に実在した
ものかを機械的に照合します。読みやすさのための丸めは通し、別の数になった
ものだけを落とします。
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from kaigyou_core import config as cfg
from kaigyou_intel import client as llm
from kaigyou_intel.projection import allowed_numbers, for_step5
from kaigyou_intel.schemas import Step5Output, verify_step5
from kaigyou_intel.steps.step4_strategy import known_ids

STEP_NUMBER = 5


class StepFailed(RuntimeError):
    """このステップが結果を出せなかった。原因は message に。"""


def build_input(step3_output: Mapping[str, Any], step4_output: Mapping[str, Any],
                dataset: Mapping[str, Any]) -> dict[str, Any]:
    return for_step5(step4_output, step3_output, dataset)


def run(payload: Mapping[str, Any]) -> tuple[dict[str, Any], llm.Usage, list[dict[str, Any]]]:
    settings = llm.step_settings(STEP_NUMBER)
    system = cfg.prompt_text(settings["prompt"])

    user = ("以下がこれまでの分析結果です。これを、開業を検討している歯科医師に"
            "手渡せるレポートに書き直してください。\n\n"
            "```json\n" + json.dumps(payload, ensure_ascii=False, indent=1) + "\n```")

    result = llm.ask(step_number=STEP_NUMBER, system=system, user=user,
                     schema=Step5Output)
    output: Step5Output | None = result.parsed
    if output is None:
        raise StepFailed("構造化出力を受け取れませんでした")

    problems = verify_step5(output, _known_ids(payload), allowed_numbers(payload))
    if problems:
        raise StepFailed(
            "レポートを保存しませんでした: "
            + "; ".join(f"{p.where}: {p.problem}" for p in problems))

    return output.model_dump(), result.usage, []


def _known_ids(payload: Mapping[str, Any]) -> set[str]:
    """引ける id。STEP4 の入力とは形が違うので、ここで集め直します。"""
    ids = known_ids(payload)
    for key in ("demand_mechanisms", "patient_segments"):
        ids |= {item.get("id") for item in (payload.get(key) or [])
                if isinstance(item, Mapping) and item.get("id")}
    # STEP4 の中の id（F###/C### など）も引けます。文書としては同じ根拠を
    # 指しているので、参照できないと書き直しのときに根拠が落ちます。
    ids |= _ids_in(payload.get("step4"))
    return ids


def _ids_in(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key == "evidence" and isinstance(value, list):
                found |= {v for v in value if isinstance(v, str)}
            else:
                found |= _ids_in(value)
    elif isinstance(node, list):
        for item in node:
            found |= _ids_in(item)
    return found
