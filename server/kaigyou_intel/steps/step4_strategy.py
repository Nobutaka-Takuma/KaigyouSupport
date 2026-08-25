"""STEP4：経営判断・レポート生成（要件 §16〜§25）。

STEP1〜3 の結論を経営判断に変換します。**新しい外部事実を足しません**（§16）。
Web検索を与えないだけでなく、足せるだけの材料も渡しません。

このレポートが答えるべきことは 1 つです（§17）。

    この物件で開業するなら、誰を主要患者として設定し、誰とは競争せず、
    どの診療圏から何を理由に患者を引っ張り、どの医院モデルにするべきか

「良い商圏です」で終わらせないために、答えるべき項目をスキーマの欄にして
あります。埋められない欄は書けません。
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from kaigyou_core import config as cfg
from kaigyou_intel import client as llm
from kaigyou_intel.projection import for_step4
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


def run(payload: Mapping[str, Any]) -> tuple[dict[str, Any], llm.Usage, list[dict[str, Any]]]:
    settings = llm.step_settings(STEP_NUMBER)
    system = cfg.prompt_text(settings["prompt"])

    user = ("以下がこれまでのステップの結論と、レポートに数字として載せる集計です。"
            "この中にある事実だけを使ってください。\n\n"
            "```json\n" + json.dumps(payload, ensure_ascii=False, indent=1) + "\n```")

    result = llm.ask(step_number=STEP_NUMBER, system=system, user=user,
                     schema=Step4Output)
    output: Step4Output | None = result.parsed
    if output is None:
        raise StepFailed("構造化出力を受け取れませんでした")

    problems = verify_step4(output, known_ids(payload))
    if problems:
        raise StepFailed(
            "レポートを保存しませんでした: "
            + "; ".join(f"{p.where}: {p.problem}" for p in problems))

    return output.model_dump(), result.usage, []
