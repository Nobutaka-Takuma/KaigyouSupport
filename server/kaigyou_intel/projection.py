"""ステップごとに、LLM へ渡す断面を切り出す。

100KB の商圏 JSON を 4 回渡すと、1 レポートで 16 万トークンの入力になります
（要件 §34 が「元JSONを毎回丸ごと渡さない」と言っているのはこのためです）。
ただし、削る理由はコストだけではありません。

**渡さなかったものについては、LLM は何も言えません。** これは制約ではなく
道具です。Step2 に base_data を渡さなければ、Step2 は「外部情報で分かったこと」
しか書けません。Step4 に外部事実の一覧しか渡さなければ、Step4 は新しい事実を
足せません（要件 §16）。分離は、プロンプトでのお願いより確実です。

もうひとつ。Step1 に渡す measures からは、比較対象を代表の 1 つに間引きます。
6 つ全部渡すと 59KB あり、しかも読み手（LLM）は「どれを使うべきか」を毎回
自分で決めることになります。どれが代表かは既にサーバ側が決めていて、その
理由も primary_benchmark に書いてあります。決まっている判断を、もう一度
させません。
"""
from __future__ import annotations

import re

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping

#: measures から Step1 へ渡す欄。平坦な代表値（= primary の写し）と、
#: 入れ子の benchmarks の両方を残します。
_MEASURE_FIELDS = (
    "key", "label", "value", "unit", "data_year", "source", "higher_means",
    "benchmark_type", "benchmark_value", "position_label",
    "rank", "of", "direction", "significance", "significance_withheld_reason",
    "growth",
)

#: 位置を表す欄のうち、LLM に渡さないもの。
#:
#: どちらも数としては正しく、文にすると逆の意味に読めます。``top_share_pct``
#: は低い値を「上位94%」にしますし、``percentile`` は最小値のとき 0.0 なので
#: 「下位0%」と書かれます（実測：銀座の人口増減率が中央区内で最下位のとき、
#: モデルは position_label ではなく percentile から文を作りました）。
#:
#: 「使うな」とプロンプトで書くより、渡さないほうが確実です。同じ情報は
#: ``position_label`` と ``rank`` / ``of`` にあり、そちらは端でも壊れません。
_BENCHMARK_DROP = ("percentile", "top_share_pct")


def to_jsonable(value: Any) -> Any:
    """DB とハッシュに渡せる形にする。

    build_dataset は API の応答として作られているので、日付や Decimal が
    Python のオブジェクトのまま入っています。FastAPI は出口で変換しますが、
    こちらは自分で保存するので自分で変換します。
    """
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def base_data_hash(dataset: Mapping[str, Any]) -> str:
    """同じ地点・同じデータの再実行を見分ける鍵。

    generated_at を除いて数えます。含めると毎回別物になり、キャッシュが
    永久に当たりません。
    """
    payload = to_jsonable({k: v for k, v in dataset.items() if k != "generated_at"})
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _measure(item: Mapping[str, Any], keep_benchmarks: bool = True) -> dict[str, Any]:
    out = {k: item[k] for k in _MEASURE_FIELDS if k in item and item[k] is not None}
    if keep_benchmarks and item.get("benchmarks"):
        # 6つの比較。代表（平坦な benchmark_*）は prefecture などの写しですが、
        # 母集団によって読みが変わることこそがパターンの材料です。
        out["benchmarks"] = [
            {k: v for k, v in b.items() if k not in _BENCHMARK_DROP}
            for b in item["benchmarks"]
        ]
    # 比較できなかったものは、なぜできなかったかごと残します。欄ごと消すと
    # 「平凡だった」と読まれます。
    if item.get("value") is not None and item.get("percentile") is None:
        out["benchmark_unavailable_reason"] = item.get(
            "benchmark_unavailable_reason", "比較できる分布がありません")
    return out


def for_step1(dataset: Mapping[str, Any],
              settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """STEP1（商圏特徴抽出）の入力。

    FACT と BENCHMARK は既に算出済みなので、ここでは**発見**だけをさせます。
    パーセンタイルや順位を LLM に作らせません（要件 §3 原則2）。作らせると、
    それらしい数字が出てきて、しかも間違っていても誰も気づけません。

    削る基準は「大きいもの」ではなく「発見に寄与しないもの」です。最初の実装は
    そこを取り違えて、母集団6つの比較（42.9KB）を代表1つに間引いていました。
    ですがその比較こそがパターンの材料で、「県内では上位45.7%だが市区町村内では
    下位2.4%」という食い違いは、6つ揃っていないと見つかりません。
    """
    settings = settings or {}
    if settings.get("full_dataset"):
        # 何も削らない。比較実験用。
        return to_jsonable(dataset)

    measures = dataset.get("measures") or {}
    keep_benchmarks = settings.get("all_benchmarks", True)
    return {
        "location": dataset.get("location"),
        "query": dataset.get("query"),
        "catchment": dataset.get("catchment"),
        # 比較の土台。どの母集団と比べた数字なのかが分からないと、
        # 「上位6%」を書き写すことしかできません。
        "measurement_basis": measures.get("measurement_basis"),
        "primary_benchmark": measures.get("primary_benchmark"),
        "benchmark_scopes": measures.get("benchmark_scopes"),
        "measures": [_measure(m, keep_benchmarks)
                     for m in (measures.get("items") or [])],
        # 組み合わせと「何が確認できていないか」だけ。成分の値は measures に
        # 既にあるので再掲しません（再掲すると 11.7KB が 1.4KB で済むところを
        # 使い、しかも同じ数字が 2 か所にある状態で読ませることになります）。
        "insight_metrics": [_insight(i) for i in (dataset.get("insight_metrics") or [])],
        "demand": dataset.get("demand"),
        "competition": _competition_summary(dataset, settings.get("clinic_list", False)),
        "access": _access_summary(dataset, settings.get("station_list", True)),
        "cost": _cost_summary(dataset),
        "regulation": dataset.get("regulation"),
        "data_quality": dataset.get("data_quality"),
        "reading_guide": (dataset.get("reading_guide") or {}).get("how_to_read_a_measure"),
        "significance_bands": (dataset.get("reading_guide") or {}).get("significance_bands"),
    }


def _insight(insight: Mapping[str, Any]) -> dict[str, Any]:
    """複合指標を、成分の**名前**と gaps に畳む。

    gaps はそのまま残します。ここが「小児歯科の供給状況は未確認」を言える
    唯一の場所で、落とすと LLM には「調べたうえで該当なし」に見えます。
    """
    return {
        "insight_metric": insight.get("insight_metric"),
        "label": insight.get("label"),
        "question": insight.get("question"),
        "component_keys": [c.get("key") for c in (insight.get("components") or [])],
        "complete": insight.get("complete"),
        "gaps": insight.get("gaps"),
        "note": insight.get("note"),
    }


def _access_summary(dataset: Mapping[str, Any],
                    include_list: bool = True) -> dict[str, Any]:
    """最寄り駅と、商圏内の駅の要点だけ。

    路線名と事業者名まで並べると 3KB になりますが、Step1 が見つけるべきなのは
    「駅がいくつあって、どれだけ人が乗り降りするか」です。
    """
    access = dataset.get("access") or {}
    stations = (access.get("stations_in_radius") or {}).get("items") or []
    return {
        "nearest_station": access.get("nearest_station"),
        "stations_in_radius": len(stations),
        "stations": ([
            {"name": s.get("name"), "distance_m": s.get("distance_m"),
             "daily_passengers": s.get("daily_passengers")}
            for s in stations[:5]
        ] if include_list else None),
    }


def _competition_summary(dataset: Mapping[str, Any],
                         include_list: bool = False) -> dict[str, Any]:
    """医院の一覧は落とし、集計だけ残す。

    50 件の医院名と住所は 34KB あって、Step1 が発見すべきパターンには
    寄与しません。件数と標榜科目の内訳と診療時間の分布が、パターンの材料です。
    """
    comp = dataset.get("competition") or {}
    inside = comp.get("clinics_in_radius") or {}
    out = {
        "by_radius": comp.get("by_radius"),
        "nearest": comp.get("nearest"),
        "clinic_count": inside.get("count"),
        "by_specialty": comp.get("by_specialty"),
        "hours": comp.get("hours"),
        "proximity": comp.get("proximity"),
    }
    if include_list:
        out["clinics"] = inside.get("items")
    return out


def _cost_summary(dataset: Mapping[str, Any]) -> dict[str, Any]:
    cost = dataset.get("cost") or {}
    return {k: cost.get(k) for k in
            ("land_price_yen_per_sqm", "surveyed_points", "basis",
             "by_use_division", "note", "rent_estimate")}


def for_step2(step1: Mapping[str, Any], location: Mapping[str, Any],
              limits: Mapping[str, Any]) -> dict[str, Any]:
    """STEP2（外部コンテクスト調査）の入力。

    base_data を渡しません。渡すと、外部情報を調べずに手元の数字を言い換えた
    ものが「外部事実」として返ってきます。ここで欲しいのは、**手元のデータでは
    説明できないこと**の説明だけです。
    """
    patterns = (step1.get("patterns") or [])[: int(limits.get("max_patterns", 5))]
    return {
        "location": location,
        "patterns": [
            {"id": p.get("id"), "title": p.get("title"),
             "evidence_summary": p.get("evidence_summary") or p.get("title"),
             "importance": p.get("importance"),
             "research_questions": p.get("research_questions") or []}
            for p in patterns
        ],
        "searches_per_pattern": int(limits.get("searches_per_pattern", 3)),
    }


def for_step3(step1: Mapping[str, Any], step2: Mapping[str, Any],
              dataset: Mapping[str, Any]) -> dict[str, Any]:
    """STEP3（需要形成・患者分析）の入力。

    ここで初めて、手元のデータと外部事実の両方が揃います。人口構成と競合の
    内訳は必要なので base_data から戻しますが、医院の一覧は戻しません。
    """
    measures = dataset.get("measures") or {}
    return {
        "location": dataset.get("location"),
        "measures": [_measure(m) for m in (measures.get("items") or [])],
        "primary_benchmark": measures.get("primary_benchmark"),
        "demand": dataset.get("demand"),
        "competition": _competition_summary(dataset),
        "access": dataset.get("access"),
        "step1": {"facts": step1.get("facts"), "patterns": step1.get("patterns")},
        "step2": {"external_facts": step2.get("external_facts"),
                  "hypotheses": step2.get("hypotheses")},
        "data_quality": dataset.get("data_quality"),
    }


def for_step4(step1: Mapping[str, Any], step2: Mapping[str, Any],
              step3: Mapping[str, Any], dataset: Mapping[str, Any]) -> dict[str, Any]:
    """STEP4（経営判断・レポート生成）の入力。

    要件 §16：ここで新しい外部事実を足さない。だから外部検索を与えないだけで
    なく、**足せるだけの材料も渡しません**。渡すのは前3ステップの結論と、
    レポートに数字として載せる必要のある集計だけです。
    """
    measures = dataset.get("measures") or {}
    return {
        "location": dataset.get("location"),
        "measures": [_measure(m) for m in (measures.get("items") or [])],
        "primary_benchmark": measures.get("primary_benchmark"),
        "competition": _competition_summary(dataset),
        "access": dataset.get("access"),
        "cost": _cost_summary(dataset),
        "regulation": dataset.get("regulation"),
        "step1": step1,
        "step2": step2,
        "step3": step3,
        "scores": dataset.get("scores"),
        "data_quality": dataset.get("data_quality"),
        "disclaimer": dataset.get("disclaimer"),
        "score_disclaimer": dataset.get("score_disclaimer"),
    }


def for_step5(step4: Mapping[str, Any], step3: Mapping[str, Any],
              dataset: Mapping[str, Any]) -> dict[str, Any]:
    """STEP5（顧客提出用レポート）の入力。

    書き直しであって、書き足しではありません。だから渡すのは STEP4 の結論と、
    文中で引ける数値の出どころだけです。基礎データ全部を戻すと、STEP4 が
    採らなかった数字が本文に湧きます。

    STEP3 の需要形成メカニズムは残します。「なぜここか」を散文で書くとき、
    筋道がそこにしか無いためです。
    """
    measures = dataset.get("measures") or {}
    return {
        "location": dataset.get("location"),
        "query": dataset.get("query"),
        "step4": step4,
        "demand_mechanisms": step3.get("demand_mechanisms"),
        "patient_segments": step3.get("patient_segments"),
        # 文中で引ける数値。丸めて書いてよいので、元の値だけ渡します。
        "measures": [_measure(m, keep_benchmarks=False)
                     for m in (measures.get("items") or [])],
        "competition": _competition_summary(dataset),
        "access": _access_summary(dataset),
        "cost": _cost_summary(dataset),
        "data_quality": dataset.get("data_quality"),
    }


def allowed_numbers(payload: Mapping[str, Any]) -> set[str]:
    """入力に実際に現れる数値の集合。

    出力の検算に使います。LLM が書いた数字がこの集合に無ければ、それは
    入力に無かった数字です（要件 §3 原則2）。完全な検出ではありませんが、
    「7,331人」を「約7,300人」と書き換えるような、いちばん起きやすい
    捏造は捕まえられます。
    """
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            found.add(_canonical(node))
        elif isinstance(node, str):
            # 文字列の中の数値も拾います。前の段が文章で書いた数字
            #（「下位30.1%」「409件中1位」）は、次の段が引用してよいものです。
            # 数値欄しか見ていなかったので、正しい引用が捏造として落ちました。
            for match in _NUMBER_IN_TEXT.finditer(node):
                try:
                    found.add(_canonical(float(match.group().replace(",", ""))))
                except ValueError:
                    continue

    walk(payload)
    return found


#: 文章の中の数値。桁区切りと小数に対応します。
_NUMBER_IN_TEXT = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _canonical(value: float) -> str:
    """1234.0 と 1234 を同じ数として扱う。"""
    if isinstance(value, int) or float(value).is_integer():
        return str(int(value))
    return f"{float(value):.4f}".rstrip("0").rstrip(".")
