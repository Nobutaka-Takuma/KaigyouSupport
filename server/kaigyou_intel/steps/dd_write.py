"""STEP2：確定した事実を読んで、散文を書く。**LLM の出番はここだけ。**

渡すのは事実の束だけです。統計の生データも、問いも、仮説の枠も渡しません。
以前は「問いを立てて調べて検証する」段を重ねていて、その筋書きがそのまま本文に
なっていました。**読み手が知りたい「この商圏はどうなのか」がどこにも無い**
文書になっていた原因がそれです。

書かせるのは 4 つだけ。

    Executive Summary       結論から
    章ごとの読みどころ       その章の事実が何を意味するか
    成長余地の仮説           但し書きつき
    総合評価                 開業する人と、買う人に分けて

**数字は作らせません。** 束にある数値との照合を通します。
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from kaigyou_core import arithmetic
from kaigyou_core import config as cfg
from kaigyou_core import dd
from kaigyou_core.analysis import DEFAULT_CATEGORY
from kaigyou_intel import client as llm
from kaigyou_intel.schemas import DDReport, verify_dd_report

STEP_NUMBER = 2


class StepFailed(RuntimeError):
    pass


def build_input(pack: Mapping[str, Any],
                category: str = DEFAULT_CATEGORY) -> dict[str, Any]:
    """LLM に渡すもの。**事実の束そのもの**です。"""
    return dict(pack)


def run(payload: Mapping[str, Any], category: str = DEFAULT_CATEGORY,
        ) -> tuple[dict[str, Any], llm.Usage, list[dict[str, Any]]]:
    settings = llm.step_settings(STEP_NUMBER)
    if not payload.get("chapters"):
        raise StepFailed("章立てが空です。config/<業態>/dd.yaml を確認してください。")

    result = llm.ask(
        step_number=STEP_NUMBER,
        system=cfg.prompt_text(settings["prompt"], category),
        user=("## 確定した事実（**この中の数字だけを使ってください**）\n\n"
              "```json\n"
              + json.dumps(_for_prompt(payload), ensure_ascii=False, indent=1)
              + "\n```"),
        schema=DDReport, web_search=False)

    report: DDReport | None = result.parsed
    if report is None:
        raise StepFailed("構造化出力を受け取れませんでした")

    written = report.model_dump()

    # **計算式を計算し直します。** LLM が書いた式を、こちらで評価して答えと
    # 突き合わせます。式に出てくる数が束にあり、答えが合っていれば、その
    # 派生値は**確かめられた事実**になり、本文で使ってよい数に加わります。
    pack_numbers = dd.numbers_in(payload)
    checked = arithmetic.check(written.get("derived") or [], pack_numbers)
    written["derived"] = checked
    allowed = pack_numbers | arithmetic.verified_values(checked)

    # **照合に落ちても段は止めません。** 3 回止めました——15.3 も 13.1 も、
    # 束の人数から割り算して出した割合で、コンサルタントが書いて当然の数字
    # です。照合はあくまで「渡していない事実を作っていないか」を見る網で、
    # 完全ではありません。**不完全な網で、料金を払って書かせた文書を破棄
    # するほうが間違っています。**
    #
    # 代わりに、辿れなかった数値を文書に明記します。読み手には「この数字は
    # 確定した事実の中に見つからなかった」と伝わり、黙って信用させることは
    # ありません。止めずに、隠しもしません。
    unverified = verify_dd_report(written, allowed)
    written["unverified_numbers"] = _numbers_only(unverified)
    return written, result.usage, []


def _numbers_only(problems: Sequence[str]) -> list[str]:
    """指摘の文から、数値そのものだけを取り出す。

    文書に出すのはメッセージではなく数値です。「本文の数値 15.3 は…」を
    そのまま載せても読み手には長いだけで、要るのは「15.3」です。
    """
    import re

    seen: list[str] = []
    for problem in problems:
        found = re.search(r"本文の数値 ([\d,.]+)", problem)
        value = found.group(1) if found else problem
        if value not in seen:
            seen.append(value)
    return seen


def _for_prompt(pack: Mapping[str, Any]) -> dict[str, Any]:
    """束から、**プロンプトに載せる必要のないもの**を落とす。

    定義文や注記は本文の言い回しに使ってほしいものではありません。載せると
    そのまま写された文が返ってきます。
    """
    trimmed = dict(pack)
    trade = dict(trimmed.get("trade_area") or {})
    trade.pop("shape_note", None)
    trimmed["trade_area"] = trade
    return trimmed
