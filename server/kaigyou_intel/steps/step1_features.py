"""STEP1：商圏特徴抽出。

基礎データだけを見て、FACT を選び、PATTERN を見つけ、外部調査の質問を作ります。
Web検索は使いません（要件 §6）。ここで外部情報が混ざると、FACT と EXTERNAL FACT
の区別が最初の段階で壊れ、あとの全ステップが汚染されます。

BENCHMARK は生成しません。パーセンタイル・順位・significance は /api/dataset が
算出済みで、FACT はそれを measure_key で参照します。LLM に作らせると、入力に
無い数字がそれらしく出てきて、しかも間違っていても誰も気づけません。
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from kaigyou_core import config as cfg
from kaigyou_intel import client as llm
from kaigyou_intel.projection import for_step1
from kaigyou_intel.schemas import Step1Output, verify_step1

STEP_NUMBER = 1


class StepFailed(RuntimeError):
    """このステップが結果を出せなかった。原因は message に。"""


def build_input(dataset: Mapping[str, Any]) -> dict[str, Any]:
    """何を渡すかは config/analysis.yaml の projection: で決まります。"""
    return for_step1(dataset, cfg.analysis_config().get("projection") or {})


def run(dataset: Mapping[str, Any]) -> tuple[dict[str, Any], llm.Usage, list[dict[str, Any]]]:
    """基礎データから STEP1 の出力を作る。

    返り値は (出力, 使用量, 出典)。STEP1 に出典はありませんが、他のステップと
    形を揃えておくと worker が分岐を持たずに済みます。
    """
    payload = build_input(dataset)
    limits = cfg.analysis_config().get("limits") or {}
    settings = llm.step_settings(STEP_NUMBER)

    system = cfg.prompt_text(settings["prompt"]).replace(
        "{max_patterns}", str(limits.get("max_patterns", 5)))

    user = (
        "以下が基礎商圏データです。この中にある事実だけを使ってください。\n\n"
        "```json\n" + json.dumps(payload, ensure_ascii=False, indent=1) + "\n```"
    )

    result = llm.ask(step_number=STEP_NUMBER, system=system, user=user,
                     schema=Step1Output)
    output: Step1Output | None = result.parsed
    if output is None:
        raise StepFailed("構造化出力を受け取れませんでした")

    # スキーマは形しか保証しません。参照が解決するかはこちらで確かめます。
    allowed = {m["key"] for m in payload.get("measures") or []}
    problems = verify_step1(output, allowed)
    if problems:
        raise StepFailed(
            "参照が解決しませんでした: "
            + "; ".join(f"{p.where}: {p.problem}" for p in problems))

    return output.model_dump(), result.usage, []
