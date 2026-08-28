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

from kaigyou_core.measures import LAYERS

#: measures から Step1 へ渡す欄。平坦な代表値（= primary の写し）と、
#: 入れ子の benchmarks の両方を残します。
_MEASURE_FIELDS = (
    # ``layer`` は落とさないこと。層を跨いだ PATTERN かどうかは、引かれた
    # 指標の層から機械的に数えます。落とすと全部が同じ層に見えます。
    "key", "label", "layer", "value", "unit", "data_year", "source", "higher_means",
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


#: measures に無いが、FACT の根拠として引ける数字。
#:
#: **なぜ要るか。** ``Fact.measure_key`` は ``measures`` のキーしか受け付けま
#: せんでした。ところが診療時間・標榜科目・産業別従業者・将来推計・開設年は
#: measures ではなく、別のブロックに入っています（比較用の分布が mesh_scores
#: に無いので指標にできません）。
#:
#: つまり「日曜に開けている医院は2割」を根拠として**引けませんでした**。
#: 実測のレポートはその事実を本文に書きながら、根拠には別の指標の id を
#: 添えていました。追跡が切れています。
#:
#: そしてこれは、仮説の質にそのまま効きます。「人口動態 × 競合の提供体制」を
#: 掛けろと言っても、片方が引けなければ掛けようがありません。
_CITABLE_BLOCKS = "citable"


def _citable(dataset: Mapping[str, Any]) -> list[dict[str, Any]]:
    """measures 以外のブロックから、引ける数字を平らに並べる。

    値はデータセットからそのまま写します。ここで計算はしません（割合は
    元のブロックが既に持っています）。層（layer）を付けるのは、PATTERN が
    層を跨いだかどうかを機械的に数えるためです。
    """
    out: list[dict[str, Any]] = []

    def add(key: str, label: str, value: Any, unit: str, layer: str,
            source: str, note: str | None = None) -> None:
        if value is None:
            return
        item = {"key": key, "label": label, "value": value, "unit": unit,
                "layer": layer, "source": source}
        if note:
            item["note"] = note
        out.append(item)

    competition = dataset.get("competition") or {}

    # --- 競合の提供体制：診療時間 -----------------------------------------
    hours = competition.get("hours") or {}
    declared = hours.get("declared") or 0
    for entry in hours.get("counts") or []:
        add(f"clinic_hours.{entry.get('key')}", f"{entry.get('label')}の医院数",
            entry.get("count"), "院", "competition_offer", MHLW,
            note=(f"診療時間の届出がある{declared}院のうち。届出値であって"
                  "運用実態ではありません。"))
    add("clinic_hours.weekly_hours_median", "週間診療時間の中央値",
        hours.get("weekly_hours_median"), "時間", "competition_offer", MHLW)

    # --- 競合の提供体制：標榜科目 -----------------------------------------
    specialty = competition.get("by_specialty") or {}
    for entry in specialty.get("detail") or []:
        key = entry.get("key")
        if not key:
            continue
        add(f"clinic_specialty.{key}", f"{entry.get('label')}を標榜する医院数",
            entry.get("count"), "院", "competition_offer", MHLW,
            note=("自由記載欄の科目。書かなかった医院を「やっていない」と"
                  "数えるので、実施医院数の下限にすぎません。"
                  if entry.get("declared_only") else None))

    # --- 競合の提供体制：開設年 -------------------------------------------
    vintage = competition.get("vintage") or {}
    if vintage.get("available"):
        add("clinic_vintage.median_year", "商圏内医院の開設年（中央値）",
            vintage.get("median_opening_year"), "年", "competition_offer", MHLW,
            note=f"開設年が分かる{vintage.get('with_opening_date')}院のうち。"
                 "開設年は院長の年齢ではありません。")
        add("clinic_vintage.opened_long_ago",
            f"開設から{vintage.get('opened_over_years_ago')}年以上経つ医院数",
            vintage.get("opened_long_ago"), "院", "competition_offer", MHLW)
        add("clinic_vintage.opened_recently",
            f"直近{vintage.get('opened_within_years')}年に開設した医院数",
            vintage.get("opened_recently"), "院", "competition_offer", MHLW)

    # --- 産業・雇用 --------------------------------------------------------
    mix = ((dataset.get("demand") or {}).get("daytime") or {}).get("industry_mix") or {}
    for division, values in mix.items():
        add(f"industry.{division}.workers", f"{division}の従業者数",
            (values or {}).get("workers"), "人", "economy", CENSUS_BIZ)

    # --- 将来推計 ----------------------------------------------------------
    outlook = (dataset.get("demand") or {}).get("outlook") or {}
    if outlook.get("available"):
        label = outlook.get("estimate_label") or "将来推計人口"
        for year in outlook.get("years") or []:
            y = year.get("year")
            add(f"outlook.{y}.population", f"{y}年の推計人口",
                year.get("population"), "人", "future", label)
            add(f"outlook.{y}.index_vs_base",
                f"{y}年の推計人口（{outlook.get('base_year')}年=1）",
                year.get("index_vs_base"), "", "future", label)
            add(f"outlook.{y}.elderly_share", f"{y}年の65歳以上の割合",
                year.get("elderly_share"), "", "future", label)
            add(f"outlook.{y}.late_elderly_share", f"{y}年の75歳以上の割合",
                year.get("late_elderly_share"), "", "future", label)
            add(f"outlook.{y}.age_0_14", f"{y}年の0〜14歳人口",
                year.get("age_0_14"), "人", "future", label)
    return out


MHLW = "厚生労働省 医療機能情報提供制度"
CENSUS_BIZ = "総務省・経済産業省 経済センサス"


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
        # measures に無いが引ける数字。層を跨いだ PATTERN は、これが無いと
        # そもそも作れません（診療時間も標榜科目も measures ではありません）。
        "citable": _citable(dataset),
        "layers": LAYERS,
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


def citable_keys(payload: Mapping[str, Any]) -> dict[str, str]:
    """FACT が引けるキーと、その層。**検算とプロンプトで同じものを使います。**

    片方だけを見て作ると、「引いてよいと書いてあるのに落ちる」キーが出ます。
    """
    keys = {m["key"]: m.get("layer", "residents")
            for m in (payload.get("measures") or []) if m.get("key")}
    keys.update({c["key"]: c.get("layer", "residents")
                 for c in (payload.get("citable") or []) if c.get("key")})
    return keys


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


def for_step2(step1: Mapping[str, Any], dataset: Mapping[str, Any],
              limits: Mapping[str, Any]) -> dict[str, Any]:
    """STEP2（外部コンテクスト調査）の入力。

    base_data は渡しません。渡すと、外部情報を調べずに手元の数字を言い換えた
    ものが「外部事実」として返ってきます。ここで欲しいのは、**手元のデータでは
    説明できないこと**の説明だけです。

    例外が 1 つあります。**近隣の歯科医院の名前**です。届出データには標榜
    診療科目しかなく、インプラント・審美・訪問診療は自由記載欄にしかありません
    （東京都でインプラントの記載は1%台）。つまり手元のデータでは
    「この商圏でインプラントを扱う医院が何院あるか」は原理的に分かりません。
    固有名詞を渡さないかぎり、外部でも調べようがない。渡すのは名前と距離と
    標榜科目だけで、数字は渡しません。
    """
    patterns = (step1.get("patterns") or [])[: int(limits.get("max_patterns", 5))]
    inside = ((dataset.get("competition") or {}).get("clinics_in_radius") or {})
    clinics = (inside.get("items") or [])[: int(limits.get("clinics_to_research", 6))]
    return {
        "location": dataset.get("location") or {},
        "patterns": [
            {"id": p.get("id"), "title": p.get("title"),
             "evidence_summary": p.get("evidence_summary") or p.get("title"),
             "importance": p.get("importance"),
             "research_questions": p.get("research_questions") or []}
            for p in patterns
        ],
        "nearby_clinics": [
            {"name": c.get("name"), "distance_m": c.get("distance_m"),
             "specialties": [s.get("label") for s in (c.get("specialties") or [])],
             # 公式サイトがあるなら、そこが自費診療の一次情報です。まとめ
             # サイトより先に見てほしいので、URL を添えて渡します。
             "homepage": c.get("homepage")}
            for c in clinics if c.get("name")
        ],
        "searches_per_pattern": int(limits.get("searches_per_pattern", 3)),
        # この地点で実際に引けた指標のキー。STEP2 は数字を使いませんが、
        # 定性要因の枠を STEP1 と同じ形で見せるために要ります。片方だけ
        # 「代理指標あり」と書くと、STEP1 が立てた問いを STEP2 が別の前提で
        # 読むことになります。
        "available_keys": sorted(citable_keys(for_step1(dataset))),
    }


def for_step3(step1: Mapping[str, Any], step2: Mapping[str, Any],
              dataset: Mapping[str, Any]) -> dict[str, Any]:
    """STEP3（需要形成・患者分析と経営判断）の入力。

    ここで初めて、手元のデータと外部事実の両方が揃います。人口構成と競合の
    内訳は必要なので base_data から戻しますが、医院の一覧は戻しません。

    経営判断もこの段で下すので、地価・規制・スコアも渡します。「どの医院
    モデルにするか」は床面積とコストの話でもあるので、それが無いと決められ
    ません。以前は判断が別の段にあり、この 3 つはそちらに渡していました。
    """
    measures = dataset.get("measures") or {}
    return {
        "location": dataset.get("location"),
        "measures": [_measure(m) for m in (measures.get("items") or [])],
        "primary_benchmark": measures.get("primary_benchmark"),
        "benchmark_scopes": measures.get("benchmark_scopes"),
        "demand": dataset.get("demand"),
        "competition": _competition_summary(dataset),
        "access": dataset.get("access"),
        "cost": _cost_summary(dataset),
        "regulation": dataset.get("regulation"),
        "scores": dataset.get("scores"),
        "step1": {"facts": step1.get("facts"), "patterns": step1.get("patterns")},
        "step2": {"external_facts": step2.get("external_facts"),
                  "hypotheses": step2.get("hypotheses"),
                  "unanswered": step2.get("unanswered")},
        "data_quality": dataset.get("data_quality"),
    }


def for_step4(step1: Mapping[str, Any], step2: Mapping[str, Any],
              step3: Mapping[str, Any], dataset: Mapping[str, Any]) -> dict[str, Any]:
    """STEP4（顧客提出用レポート）の入力。最終段です。

    書き直しであって、書き足しではありません。だから渡すのは前 3 段の結論と、
    文中で引ける数値の出どころだけです。基礎データ全部を戻すと、分析が採ら
    なかった数字が本文に湧きます。

    ``benchmarks``（母集団6つの比較）は渡しません。位置づけは STEP1 が
    FACT の ``position_label`` として既に選んでいて、ここでもう一度選ばせると
    「県内では上位だが市内では下位」のどちらを書くかが段ごとにぶれます。
    """
    measures = dataset.get("measures") or {}
    return {
        "location": dataset.get("location"),
        "query": dataset.get("query"),
        "step1": {"facts": step1.get("facts"), "patterns": step1.get("patterns"),
                  "not_determinable": step1.get("not_determinable")},
        "step2": {"external_facts": step2.get("external_facts"),
                  "hypotheses": step2.get("hypotheses"),
                  "unanswered": step2.get("unanswered")},
        "step3": step3,
        # 文中で引ける数値。丸めて書いてよいので、元の値だけ渡します。
        "measures": [_measure(m, keep_benchmarks=False)
                     for m in (measures.get("items") or [])],
        "primary_benchmark": measures.get("primary_benchmark"),
        "benchmark_scopes": measures.get("benchmark_scopes"),
        "demand": dataset.get("demand"),
        "competition": _competition_summary(dataset),
        "access": _access_summary(dataset),
        "cost": _cost_summary(dataset),
        "data_quality": dataset.get("data_quality"),
        "disclaimer": dataset.get("disclaimer"),
        "score_disclaimer": dataset.get("score_disclaimer"),
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
                    value = float(match.group(1).replace(",", ""))
                except ValueError:
                    continue
                # 書かれたままの値と、単位を掛けた値の**両方**を入れます。
                # 「1.5万人」と書かれていれば、1.5 も 15000 も入力に現れた
                # 数です。検査側（schemas._NUMBER）は単位を読んで 15000 を
                # 探すので、こちらが 1.5 しか入れないと、前の段が書いた文章を
                # 次の段がそのまま引用しただけで捏造として落ちます。
                # 実測：STEP4 の「1.5万人」を STEP5 が引用して2回落ちました。
                found.add(_canonical(value))
                scale = _SCALE_IN_TEXT.get(match.group(2) or "")
                if scale:
                    found.add(_canonical(value * scale))

    walk(payload)
    return found


#: 文章の中の数値。桁区切り・小数・「万」「億」に対応します。
#: **schemas._NUMBER と同じものを読むこと。** 集める側と検査する側で読み方が
#: ずれると、正しい引用が捏造として落ちます。
_NUMBER_IN_TEXT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(万|億)?")

_SCALE_IN_TEXT = {"万": 10_000.0, "億": 100_000_000.0}


def _canonical(value: float) -> str:
    """1234.0 と 1234 を同じ数として扱う。"""
    if isinstance(value, int) or float(value).is_integer():
        return str(int(value))
    return f"{float(value):.4f}".rstrip("0").rstrip(".")
