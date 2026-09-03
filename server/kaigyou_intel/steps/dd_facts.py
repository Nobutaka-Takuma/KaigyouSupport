"""STEP1：事実を確定する。**LLM を呼びません。**

自社 DB に入っている静的データ——人口メッシュ、医院の位置と開設年、駅、地価、
都市計画——を数え直すだけです。API 費用はゼロ、所要は 1 秒未満。

段として独立させているのは、**この境目を見えるようにする**ためです。ここまでが
「誰が何度実行しても同じ数字」で、次の段から先が文章です。読み手にも、直す人
にも、どちらの話をしているのかが分かります。

競合の中身（各院サイトの訴求）は動的なので、ここには入りません。別の分析
（周辺の競合を分析する）の結果があれば、それを取り込みます。
"""
from __future__ import annotations

from typing import Any, Mapping

from kaigyou_core import dd
from kaigyou_core.analysis import DEFAULT_CATEGORY
from kaigyou_intel import client as llm

STEP_NUMBER = 1


def build_input(dataset: Mapping[str, Any],
                survey: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"dataset": dataset, "survey": survey}


def run(payload: Mapping[str, Any], category: str = DEFAULT_CATEGORY,
        ) -> tuple[dict[str, Any], llm.Usage, list[dict[str, Any]]]:
    """事実の束を作る。**使用トークンは 0 です。**"""
    pack = dd.fact_pack(payload.get("dataset") or {},
                        payload.get("survey"), category)
    return pack, llm.Usage(), []
