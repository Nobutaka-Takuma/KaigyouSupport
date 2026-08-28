"""STEP3：需要形成・患者分析と経営判断（要件 §12〜§17）。

「この場所にどのような患者が、なぜ存在するのか」を推定し、そこから
「誰を主要患者に置き、誰とは競争しないか」まで決めます。ここで初めて、
手元のデータ（STEP1）と外部で確認できた事実（STEP2）の両方が揃います。

**判断を別の段にしていました。** その段はタグ付きの10章レポートも書いていて、
最終段がそれを散文に書き直していました。読み手に届くのは散文だけなので、
タグ付きのほうは捨てるために書いていたことになります。分析と判断は同じ材料を
見る仕事なので、間に生成を1本挟む理由がありません。

Web検索はしません（要件 §38）。STEP2 が調べたものだけを使います。ここで足せると、
出典の検算を通っていない外部情報がレポートに入ります。

このステップの本体は患者層の一覧ではなく、**需要形成メカニズム**です。
「駅前だから患者が来る」は筋道ではなく結論の言い換えなので、chain を 3 段以上
必須にして、書けない形にしてあります。
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from kaigyou_core import config as cfg
from kaigyou_intel import client as llm
from kaigyou_intel.projection import for_step3
from kaigyou_intel.schemas import Step3Output, verify_step3

STEP_NUMBER = 3


class StepFailed(RuntimeError):
    """このステップが結果を出せなかった。原因は message に。"""


def build_input(step1_output: Mapping[str, Any], step2_output: Mapping[str, Any],
                dataset: Mapping[str, Any]) -> dict[str, Any]:
    return for_step3(step1_output, step2_output, dataset)


def run(payload: Mapping[str, Any]) -> tuple[dict[str, Any], llm.Usage, list[dict[str, Any]]]:
    settings = llm.step_settings(STEP_NUMBER)
    # 歯科医院として必ず答えることの一覧。商圏データだけを見ていると、
    # 「ユニット何台・床面積・衛生士何人」は永久に出てきません。
    from kaigyou_intel.steps.step1_features import requirement_frame

    system = cfg.prompt_text(settings["prompt"]).replace(
        "{dental_requirements}", requirement_frame(cfg.hypotheses_config()))

    user = ("以下が基礎データと、これまでのステップの結論です。"
            "この中にある事実だけを使ってください。\n\n"
            "```json\n" + json.dumps(payload, ensure_ascii=False, indent=1) + "\n```")

    result = llm.ask(step_number=STEP_NUMBER, system=system, user=user,
                     schema=Step3Output)
    output: Step3Output | None = result.parsed
    if output is None:
        raise StepFailed("構造化出力を受け取れませんでした")

    step1 = payload.get("step1") or {}
    step2 = payload.get("step2") or {}
    fact_ids = {f.get("id") for f in (step1.get("facts") or []) if f.get("id")}
    # PATTERN も根拠にできます。STEP1 の発見をそのまま使えないと、同じことを
    # 言い直させることになります。
    fact_ids |= {p.get("id") for p in (step1.get("patterns") or []) if p.get("id")}
    external_ids = {c.get("id") for c in (step2.get("external_facts") or [])
                    if c.get("id")}
    external_ids |= {h.get("id") for h in (step2.get("hypotheses") or []) if h.get("id")}

    problems = verify_step3(output, fact_ids, external_ids)
    if problems:
        raise StepFailed(
            "参照が解決しませんでした: "
            + "; ".join(f"{p.where}: {p.problem}" for p in problems))

    return output.model_dump(), result.usage, []
