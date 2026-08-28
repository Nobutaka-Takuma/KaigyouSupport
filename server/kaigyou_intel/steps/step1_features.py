"""STEP1：周辺施設スキャンと商圏特徴抽出。

2 つの呼び出しに分かれます。

    1 回目  **その場所に何があるのか**を Web検索で調べる（周辺施設スキャン）
    2 回目  基礎データから FACT を選び、PATTERN を見つけ、外部調査の質問を作る

なぜ検索を先に置くのか。500m メッシュの統計は「そこに何人いるか」を教えますが、
**「そこが何なのか」は教えません。** 大学のキャンパスも、ショッピングモールも、
総合病院も、統計の上では同じ「人がいる区画」です。同じ「昼間人口5万人」でも、
オフィス街なら平日昼の勤め人、大学のそばなら学生、商業施設なら**商圏そのものが
徒歩圏ではなく施設の集客圏**になります。打ち手がまるで違い、この違いは統計から
永久に出てきません。

要件 §6（STEP1 は Web検索禁止）との関係。禁止の理由は「FACT と EXTERNAL FACT の
区別が最初の段階で壊れる」ことでした。**その区別は保っています。** FACT が引ける
のは ``measures`` と ``citable`` のキーだけで、これは ``verify_step1`` が機械的に
検算します。スキャンの結果は ``surroundings`` という別の欄に、出典 URL 付きで
入ります。効くのは PATTERN の見立てと research_questions、つまり「何を調べに
行くか」のほうです。

スキャンは**落ちても止めません**。付随物のために FACT 十数件と PATTERN の生成を
捨てるのは割に合いません。落ちたことは ``surroundings`` の代わりに not_determinable
に残します。

BENCHMARK は生成しません。パーセンタイル・順位・significance は /api/dataset が
算出済みで、FACT はそれを measure_key で参照します。LLM に作らせると、入力に
無い数字がそれらしく出てきて、しかも間違っていても誰も気づけません。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from kaigyou_core import config as cfg
from kaigyou_intel import client as llm
from kaigyou_intel.projection import citable_keys, for_step1
from kaigyou_intel.schemas import (
    Step1Output,
    drop_unverifiable_facilities,
    verify_step1,
)

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


def _factor_frame(frame: Mapping[str, Any],
                  available: Iterable[str] | None = None) -> str:
    """歯科経営の定性要因を、プロンプトに差し込める形にする。

    **統計には載らないが開業の成否を分けるもの**の一覧です。データから
    出てくるものではないので、枠として与えます。ここを渡さないと、
    research_questions は「この地域はどんな街か」に寄ります。

    設定に置いているのは、これが業界知識だからです（統計と違い、扱う人が
    入れ替えるもの）。config/hypotheses.yaml を参照。

    ``available`` を渡すと、**その地点で実際に引ける代理指標だけ**を並べます。
    これは飾りではありません。設定に書いた代理指標が、その地点では取れて
    いないことがあります（実例：開設年月日は医療機能情報提供制度の配布
    ファイルに列が無く、``clinic_vintage.*`` はどの地点でも作られません）。
    フィルタしないと、**存在しないキーを使えと指示する**ことになり、それを
    引いた FACT は検算で落ちて段ごとやり直しになります。

    取れなかったものは黙って消さず、「取れていない」と名前ごと書きます。
    消すと「そもそも見ていない」と「調べたが無かった」が区別できません。
    """
    known = None if available is None else set(available)
    lines: list[str] = []
    for factor in frame.get("factors") or []:
        lines.append(f"### {factor.get('name')}")
        lines.append("")
        lines.append(str(factor.get("question") or "").strip())
        lines.append("")
        declared = factor.get("proxies") or []
        usable = [p for p in declared
                  if known is None or p.get("key") in known]
        missing = [p for p in declared if p not in usable]
        if usable:
            lines.append("手元にある代理指標（弱いものも含みます。**強い根拠として"
                         "使わないでください**）:")
            for proxy in usable:
                lines.append(f"- `{proxy.get('key')}` … {proxy.get('why')}")
        else:
            lines.append("**手元に代理指標はありません。この要因について、"
                         "統計からは何も言えません。** 外部情報でしか扱えません。")
        if missing:
            names = "、".join(f"`{p.get('key')}`" for p in missing)
            lines.append("")
            lines.append(f"この地点では取れていないもの: {names}"
                         "（**使わないでください。引くと出力ごと破棄されます**）")
        lines.append("")
        research = factor.get("research") or []
        if research:
            lines.append("外部で調べる価値があること:")
            lines += [f"- {item}" for item in research]
            lines.append("")
    return "\n".join(lines).strip() or "（設定されていません）"


@dataclass
class _Scan:
    """周辺施設スキャンの結果。**失敗も結果として持ちます。**"""

    text: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)
    usage: llm.Usage = field(default_factory=llm.Usage)
    error: str | None = None

    @property
    def usable(self) -> bool:
        return bool(self.text.strip() and self.sources)


def surroundings_searches(limits: Mapping[str, Any]) -> int:
    """周辺施設スキャンに使う検索回数。**0 でスキャンを止められます。**

    止める口を残しているのは、これが唯一の「毎回必ず走る検索」だからです。
    レート制限に当たったときや、外部に出られない環境で試すときに、設定
    1 行で切れないと段ごと落ちます。
    """
    return max(0, int(limits.get("surroundings_searches", 2)))


def scan_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    """スキャンに渡すもの。**場所だけです。**

    統計は渡しません。渡すと、検索せずに手元の数字を言い換えたものが
    「周辺にはこういう施設がある」として返ってきます（STEP2 で実際に
    そうなったので、あちらでも base_data を渡していません）。

    駅名は渡します。緯度経度だけでは検索の取っかかりが無く、モデルは
    「35.7,139.7 周辺 施設」のような当たらない問い合わせを作ります。
    """
    location = dict(payload.get("location") or {})
    access = payload.get("access") or {}
    nearest = access.get("nearest_station") or {}
    return {
        "prefecture": location.get("prefecture_name"),
        "municipality": location.get("municipality_name"),
        "address": location.get("address") or location.get("name"),
        "lat": location.get("lat"),
        "lng": location.get("lng"),
        "radius_m": (payload.get("query") or {}).get("radius_m"),
        "nearest_station": {"name": nearest.get("name"),
                            "distance_m": nearest.get("distance_m")},
        "stations_in_radius": [s.get("name")
                               for s in (access.get("stations") or [])],
    }


def scan_surroundings(payload: Mapping[str, Any],
                      limits: Mapping[str, Any]) -> _Scan:
    """その場所に何があるのかを調べる。**例外を上げません。**

    ここが落ちても FACT と PATTERN は作れます。付随物のために段ごと
    やり直すと、時間も費用も倍かかります（実測で 1 回 $1 前後）。
    """
    budget = surroundings_searches(limits)
    if not budget:
        return _Scan(error="設定 limits.surroundings_searches が 0 のため実行していません")
    settings = llm.step_settings(STEP_NUMBER)
    name = settings.get("prompt_surroundings")
    if not name:
        return _Scan(error="config/analysis.yaml の steps.1 に prompt_surroundings がありません")

    system = cfg.prompt_text(name).replace("{max_searches}", str(budget))
    user = ("以下が開業候補地です。ここに何があるのかを調べてください。\n\n"
            "```json\n"
            + json.dumps(scan_input(payload), ensure_ascii=False, indent=1)
            + "\n```")
    try:
        result = llm.ask(step_number=STEP_NUMBER, system=system, user=user,
                         web_search=True, effort=settings["effort_scan"],
                         max_uses=budget)
    except Exception as exc:  # noqa: BLE001 - 付随物の失敗で段を捨てない
        return _Scan(error=f"{type(exc).__name__}: {exc}")

    sources = [s for s in result.sources if s.get("url")]
    # サーバ側ツールのエラーは**例外ではなく content の中身として** HTTP 200 で
    # 返ります。空の結果として扱うと「調べたが何も無かった」になります。
    errors = [str(s["error"]) for s in result.sources if s.get("error")]
    if errors and not sources:
        return _Scan(error="Web検索が実行できませんでした: " + ", ".join(errors))
    if not (result.text or "").strip():
        error = "本文が空でした"
    elif not sources:
        # 本文はあるが URL が 1 つも返っていない＝検索が当たっていません。
        # **モデルの記憶で書かれた文章です。** 施設は開業も閉業もするので、
        # 出典の無い施設名は使えません。
        error = "検索結果が 1 件も返りませんでした（本文は出典なしのため使いません）"
    else:
        error = None
    return _Scan(text=result.text or "", sources=sources, usage=result.usage,
                 error=error)


def _scan_block(scan: _Scan) -> str:
    """スキャンの結果を、2 回目の呼び出しに差し込める形にする。

    取得した URL の一覧を明示して渡します。ここに無い URL を書けば検算で
    落ちるので、「一覧から選ぶ」ほうが易しい問題になります（STEP2 の
    書き写しと同じ考え方です）。
    """
    if not scan.usable:
        return ("## 周辺施設スキャン\n\n"
                f"**実行できませんでした**（{scan.error or '理由不明'}）。"
                "`surroundings` は null にし、周辺に何があるかについては"
                "何も書かないでください。**「周辺に施設は無い」ではありません。**"
                "調べられなかったことを `not_determinable` に 1 行書いてください。")
    catalogue = "\n".join(f"- {s['url']}  {s.get('title') or ''}"
                          for s in scan.sources)
    return ("## 周辺施設スキャン（外部情報）\n\n"
            "分析を始める前に、この場所に何があるのかを Web検索で調べました。\n\n"
            + scan.text
            + "\n\n### このスキャンで取得した URL"
            "（`surroundings.facilities[].source_url` はこの中から選ぶこと）\n\n"
            + catalogue)


def run(payload: Mapping[str, Any]) -> tuple[dict[str, Any], llm.Usage, list[dict[str, Any]]]:
    """射影済みの入力から STEP1 の出力を作る。

    入力は ``build_input`` が作ったものを受け取ります。ここで作り直さないのは、
    worker が記録した入力と実際に渡した入力を同じものにするためです。

    2 回呼びます。1 回目で**その場所に何があるのか**を検索し、2 回目でその
    結果と基礎データを合わせて FACT・PATTERN を作ります。順番が逆では意味が
    ありません。商業施設のテナントかどうかで、同じ半径1km の統計を別の意味に
    読むことになるからです。

    返り値は (出力, 使用量, 出典)。出典はスキャンで取得した URL です。
    """
    limits = cfg.analysis_config().get("limits") or {}
    settings = llm.step_settings(STEP_NUMBER)

    scan = scan_surroundings(payload, limits)

    frame = cfg.hypotheses_config()
    system = (cfg.prompt_text(settings["prompt"])
              .replace("{max_patterns}", str(limits.get("max_patterns", 5)))
              .replace("{min_cross_layer_patterns}", str(min_cross_layer()))
              .replace("{crossing_examples}", _bullets(
                  (frame.get("crossing") or {}).get("examples")))
              .replace("{qualitative_factors}",
                       _factor_frame(frame, citable_keys(payload))))

    user = (
        "以下が基礎商圏データです。**FACT にできるのはこの中の数字だけです。**\n\n"
        "```json\n" + json.dumps(payload, ensure_ascii=False, indent=1) + "\n```"
        "\n\n" + _scan_block(scan)
    )

    result = llm.ask(step_number=STEP_NUMBER, system=system, user=user,
                     schema=Step1Output)
    output: Step1Output | None = result.parsed
    if output is None:
        raise StepFailed("構造化出力を受け取れませんでした")

    # 出典を確かめられなかった施設は落とします。**段は落としません。**
    # スキャンは付随物で、その 1 件のために FACT 十数件を捨てるのは
    # 割に合いません（STEP2 で同じ判断をしています）。
    drop_unverifiable_facilities(output, {s["url"] for s in scan.sources})
    if not scan.usable and output.surroundings is not None:
        # 検索が動いていないのに施設が並んでいる＝モデルの記憶です。
        # 施設は開業も閉業もするので、記憶は出典になりません。
        output.surroundings = None
    if scan.error:
        output.not_determinable = list(output.not_determinable) + [
            f"周辺施設のスキャンは完了していません（{scan.error}）。"
            "周辺に何があるかは確認できていません。"]

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

    usage = llm.Usage(
        input_tokens=scan.usage.input_tokens + result.usage.input_tokens,
        output_tokens=scan.usage.output_tokens + result.usage.output_tokens,
        web_searches=scan.usage.web_searches + result.usage.web_searches,
        cache_read_tokens=scan.usage.cache_read_tokens + result.usage.cache_read_tokens,
        cache_write_tokens=scan.usage.cache_write_tokens + result.usage.cache_write_tokens,
    )
    return output.model_dump(), usage, _cited_sources(output, scan)


def _cited_sources(output: Step1Output, scan: _Scan) -> list[dict[str, Any]]:
    """取得した URL に、それを引用した施設の印を付ける。

    レポートの出典一覧に載るのは ``pattern_id`` の付いたものだけです
    （引用されなかった URL は件数だけ添えられます）。印を付けないと、
    本文が「〇〇大学のキャンパスが商圏内にある」と書いているのに、その
    出典がどこにも出ない状態になります。

    引用されなかった URL も残します。「調べたが使わなかった」も記録で、
    同じ地点を調べ直すときに何を見たかが分かります。
    """
    from kaigyou_intel.schemas import normalize_url

    cited = {normalize_url(f.source_url)
             for f in ((output.surroundings.facilities
                        if output.surroundings else []) or [])}
    return [{**source,
             "pattern_id": ("周辺施設" if normalize_url(source["url"]) in cited
                            else None)}
            for source in scan.sources]
