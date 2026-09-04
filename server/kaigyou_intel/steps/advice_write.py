"""STEP3：提言レポート（第II部）。**ここでは推論します。**

第I部（プレDD）は事実だけで、推論を禁じています。この段は逆で、「ここで
開業するならどうするか」を組み立てます。**分けているのが要点**で、混ぜると
第I部が「どこまでが確定か分からない文書」になります。

呼び出しは 2 回です。

    1 回目  STEP1〜7 を考えながら Web で外部コンテクストを探す（散文）
    2 回目  それを 10 章のスキーマに写す（検索なし）

Web検索と構造化出力を同じ呼び出しで併用しないのは、2 回目に検索を残すと
1 回目に無かった事実が増えて、出典の照合が意味を失うためです。
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from kaigyou_core import arithmetic
from kaigyou_core import config as cfg
from kaigyou_core import dd
from kaigyou_core.analysis import DEFAULT_CATEGORY
from kaigyou_intel import client as llm
from kaigyou_intel.schemas import AdviceReport

STEP_NUMBER = 3


class StepFailed(RuntimeError):
    pass


def build_input(pack: Mapping[str, Any], dd_report: Mapping[str, Any] | None = None,
                category: str = DEFAULT_CATEGORY) -> dict[str, Any]:
    """渡すのは**確定した事実**と、第I部の結論だけ。

    統計の生データは渡しません。事実の束に無い数字を本文に出させないためです。
    """
    return {
        "facts": dict(pack),
        # 第I部が何と言ったか。**同じ地点で 2 つの文書が矛盾しないように。**
        "pre_dd_summary": (dd_report or {}).get("summary"),
        "pre_dd_verdict": (dd_report or {}).get("verdict"),
    }


def run(payload: Mapping[str, Any], category: str = DEFAULT_CATEGORY,
        ) -> tuple[dict[str, Any], llm.Usage, list[dict[str, Any]]]:
    settings = llm.step_settings(STEP_NUMBER)
    limits = cfg.analysis_config().get("limits") or {}
    searches = max(0, int(limits.get("advice_searches", 3)))

    thought = llm.ask(
        step_number=STEP_NUMBER,
        system=cfg.prompt_text(settings["prompt"], category),
        user=("## 確定した事実（**この中の数字だけを使ってください**）\n\n"
              "```json\n"
              + json.dumps(payload, ensure_ascii=False, indent=1, default=str)
              + "\n```"),
        max_uses=searches or None,
        fetch_uses=max(0, int(limits.get("advice_fetches", 2))),
        web_search=bool(searches))
    if not (thought.text or "").strip():
        raise StepFailed("分析の本文が空でした")

    sources = [s for s in thought.sources if s.get("url")]
    catalogue = "\n".join(f"- {s['url']}  {s.get('title') or ''}"
                          for s in sources) or "（外部情報は使っていません）"
    structured = llm.ask(
        step_number=STEP_NUMBER,
        system=cfg.prompt_text(settings["prompt_structure"], category),
        effort=settings["effort_structure"],
        user=("## 直前の分析\n\n" + thought.text
              + "\n\n## 今回の検索で取得した URL\n\n" + catalogue),
        schema=AdviceReport, web_search=False)

    report: AdviceReport | None = structured.parsed
    if report is None:
        raise StepFailed("構造化出力を受け取れませんでした")

    written = report.model_dump()
    # 提言でも、計算した数字は式ごと出させて計算し直します。
    written["derived"] = arithmetic.check(
        written.get("derived") or [],
        dd.numbers_in((payload.get("facts") or {})))
    problems = _verify(written, payload)
    if problems:
        raise StepFailed("；".join(problems[:5]))
    return (written, _add(thought.usage, structured.usage),
            [{**s, "step": STEP_NUMBER} for s in sources])


def _verify(report: Mapping[str, Any], payload: Mapping[str, Any]) -> list[str]:
    """提言として成立しているかを見る。**数字の検算は第I部の役目です。**

    ここで見るのは推論の形です。1 つのデータだけから引いた結論と、情報不足を
    「競合が少ない」と読み替えた記述を弾きます。**どちらも、この文書でいちばん
    起きやすい壊れ方です。**
    """
    problems: list[str] = []
    tags = [str(s.get("tag") or "") for s in report.get("reasoning") or []]
    if not tags:
        problems.append("推論の連鎖（reasoning）が空です")
    elif "ACTION" not in tags:
        problems.append("推論が ACTION まで届いていません（示唆で終わっています）")

    roles = {str(s.get("role") or "") for s in report.get("segments") or []}
    for role, label in (("primary", "主要患者"), ("avoid", "競争すべきでない層")):
        if role not in roles:
            problems.append(f"{label}（role={role}）が書かれていません")

    if not any(str(c.get("rank")) == "primary"
               for c in report.get("catchments") or []):
        problems.append("第1商圏が書かれていません")

    # **情報不足を「競合が少ない」と読み替えていないか。**
    surveyed = ((payload.get("facts") or {}).get("competition") or {}) \
        .get("surveyed_count") or 0
    if not surveyed:
        text = " ".join(str(report.get(k) or "") for k in
                        ("reason_to_visit", "clinic_model", "differentiation"))
        text += " ".join(str(s.get("basis") or "") + str(s.get("why") or "")
                         for s in report.get("segments") or [])
        for phrase in ("競合が少な", "競合は少な", "競合が弱", "競合は弱"):
            if phrase in text:
                problems.append(
                    f"競合の中身を 1 件も調べていないのに「{phrase}い」と書いています。"
                    "情報不足は情報不足として書いてください")
                break
        if not str(report.get("information_gaps") or "").strip():
            problems.append(
                "競合の中身を調べていないのに、information_gaps が空です")
    return problems


def _add(a: llm.Usage, b: llm.Usage) -> llm.Usage:
    return llm.Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        web_searches=a.web_searches + b.web_searches,
        cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
        cache_write_tokens=a.cache_write_tokens + b.cache_write_tokens)
