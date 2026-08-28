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
from kaigyou_intel.projection import citable_keys, for_step1
from kaigyou_intel.schemas import Step1Output, verify_step1

STEP_NUMBER = 1


class StepFailed(RuntimeError):
    """このステップが結果を出せなかった。原因は message に。"""


def build_input(dataset: Mapping[str, Any]) -> dict[str, Any]:
    """何を渡すかは config/analysis.yaml の projection: で決まります。"""
    return for_step1(dataset, cfg.analysis_config().get("projection") or {})


def min_cross_layer() -> int:
    """層を跨いだ PATTERN を最低いくつ求めるか。

    **プロンプトと検算で同じ値を使うこと。** 別々に読むと、「3件以上」と
    書いておきながら 2 件で通る（またはその逆で、書いていない条件で落ちる）
    状態になります。落ちるとその段はやり直しで、費用も倍かかります。
    """
    crossing = cfg.hypotheses_config().get("crossing") or {}
    return int(crossing.get("min_cross_layer_patterns", 0))


def _bullets(items: Any) -> str:
    return "\n".join(f"- {item}" for item in (items or [])) or "（設定されていません）"


def requirement_frame(frame: Mapping[str, Any]) -> str:
    """歯科医院として必ず答えることを、プロンプトに差し込める形にする。

    ``_factor_frame``（外部で調べる論点）とは役割が違います。こちらは
    **調べなくても答えるべきこと**で、歯科という業態に固有です。

    実測：沼津駅前のレポートは、通勤者と前期高齢者という需要の読み分けまでは
    到達していましたが、ユニットを何台置くのか・駐車場は要るのか・衛生士は
    何人要るのかには触れていませんでした。それは商圏の話ではなく医院の話
    なので、商圏データだけを見ていると永久に出てきません。
    """
    lines: list[str] = []
    for item in frame.get("requirements") or []:
        lines.append(f"### [{item.get('category')}] {item.get('question', '').strip()}")
        lines.append("")
        decided = item.get("decided_by") or []
        if decided:
            lines.append("これを左右するもの:")
            lines += [f"- {x}" for x in decided]
        if item.get("note"):
            lines.append("")
            lines.append(str(item["note"]).strip())
        lines.append("")
    return "\n".join(lines).strip() or "（設定されていません）"


def _factor_frame(frame: Mapping[str, Any]) -> str:
    """歯科経営の定性要因を、プロンプトに差し込める形にする。

    **統計には載らないが開業の成否を分けるもの**の一覧です。データから
    出てくるものではないので、枠として与えます。ここを渡さないと、
    research_questions は「この地域はどんな街か」に寄ります。

    設定に置いているのは、これが業界知識だからです（統計と違い、扱う人が
    入れ替えるもの）。config/hypotheses.yaml を参照。
    """
    lines: list[str] = []
    for factor in frame.get("factors") or []:
        lines.append(f"### {factor.get('name')}")
        lines.append("")
        lines.append(str(factor.get("question") or "").strip())
        lines.append("")
        proxies = factor.get("proxies") or []
        if proxies:
            lines.append("手元にある代理指標（弱いものも含みます。**強い根拠として"
                         "使わないでください**）:")
            for proxy in proxies:
                lines.append(f"- `{proxy.get('key')}` … {proxy.get('why')}")
        else:
            lines.append("手元に代理指標はありません。**この要因について、"
                         "統計からは何も言えません。**")
        lines.append("")
        research = factor.get("research") or []
        if research:
            lines.append("外部で調べる価値があること:")
            lines += [f"- {item}" for item in research]
            lines.append("")
    return "\n".join(lines).strip() or "（設定されていません）"


def run(payload: Mapping[str, Any]) -> tuple[dict[str, Any], llm.Usage, list[dict[str, Any]]]:
    """射影済みの入力から STEP1 の出力を作る。

    入力は ``build_input`` が作ったものを受け取ります。ここで作り直さないのは、
    worker が記録した入力と実際に渡した入力を同じものにするためです。

    返り値は (出力, 使用量, 出典)。STEP1 に出典はありませんが、他のステップと
    形を揃えておくと worker が分岐を持たずに済みます。
    """
    limits = cfg.analysis_config().get("limits") or {}
    settings = llm.step_settings(STEP_NUMBER)

    frame = cfg.hypotheses_config()
    system = (cfg.prompt_text(settings["prompt"])
              .replace("{max_patterns}", str(limits.get("max_patterns", 5)))
              .replace("{min_cross_layer_patterns}", str(min_cross_layer()))
              .replace("{crossing_examples}", _bullets(
                  (frame.get("crossing") or {}).get("examples")))
              .replace("{qualitative_factors}", _factor_frame(frame)))

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
    # 層は指標から引きます。モデルの自己申告にすると、形だけ整った出力が
    # 通ります（「人口 × 競合」と書きながら人口の指標を2つ引く、など）。
    layer_of = citable_keys(payload)
    problems = verify_step1(output, set(layer_of), layer_of,
                            min_cross_layer=min_cross_layer())
    if problems:
        raise StepFailed(
            "参照が解決しませんでした: "
            + "; ".join(f"{p.where}: {p.problem}" for p in problems))

    return output.model_dump(), result.usage, []
