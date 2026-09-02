"""地域の位置づけを、**計算して確定させる**（指示書 §7・§9・§14）。

このモジュールが存在する理由はひとつです。

    人口：       市街地 上位 8%
    歯科医院数： 市街地 上位 35%

この 2 行から「人口規模に対して歯科医院が相対的に少ない」を読み取るのは、
これまで LLM の仕事でした。**それは Fact ではなく、LLM の解釈です。**
引き算で出るものを LLM にやらせると、検算できない文が 1 つ増えます。

ここで計算して Fact にすれば、LLM はそれを**引用**できます。引用された数字は
``verify_step1`` が実在を確かめます。**GIS が Fact を確定し、LLM はそれを
解釈する**——分業はそこです（指示書 §17）。

LLM は呼びません。しきい値も軸も文言も ``config/<業態>/positioning.yaml`` に
あります。
"""
from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Mapping, Sequence

#: percentile の差がこれ未満なら、アンバランスとは言いません。設定で軸ごとに
#: 上書きできます。**小さくすると、誤差を特徴として読ませることになります。**
DEFAULT_GAP_THRESHOLD = 0.20


def build(measures: Sequence[Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """この地点の位置づけ。**母集団を 1 つに揃えて**語ります。

    母集団が違えば同じ数字の意味が変わります。人口を市街地の中で、医院数を
    県内全メッシュの中で見て引き算すると、**意味のない数字がそれらしい顔で
    出ます。** だからまず 1 つ選び、選べなければ何も言いません。
    """
    if not config:
        return {}
    scope = _choose_scope(measures, config.get("prefer_scopes") or [])
    if scope is None:
        return {
            "available": False,
            "why_not": ("位置づけを語れる比較母集団がありません。"
                        "メッシュスコアが未計算か、この地点で比較できる商圏が"
                        "見つかりませんでした（kaigyou-etl compute-scores）。"),
        }

    percentiles = _percentiles_in(measures, scope["type"])
    axes = _axes(percentiles, measures, config)
    return {
        "available": True,
        # **どれと比べたのかを必ず出します**（指示書 §16）。書かずに「上位8%」
        # とだけ言えば、それはもう統計ではありません。
        "compared_with": scope,
        "benchmark_version": str(config.get("benchmark_version") or "v0"),
        "calculated_on": date.today().isoformat(),
        "axes": axes,
        "gaps": _gaps(percentiles, measures, config),
        "region_type": _region_type(axes, config),
        "note": ("percentile は同一都道府県内のメッシュ分布に対する相対値です。"
                 "**予測ではありません。**「相対的に少ない」は、比較した母集団の"
                 "中での位置の差であって、需要が余っていることではありません。"),
    }


def _choose_scope(measures: Sequence[Any],
                  prefer: Sequence[str]) -> dict[str, Any] | None:
    """使う母集団を 1 つ選ぶ。**上から順に、使える最初のもの。**

    `discriminating` でない母集団は使いません。県内全メッシュのように大半が
    無人のところでは、町の中心はどこでも上位数%に入り、percentile は
    「市街地かどうか」しか測っていません。
    """
    usable: dict[str, Any] = {}
    for measure in measures:
        for bench in getattr(measure, "benchmarks", []) or []:
            if bench.percentile is None or not bench.discriminating:
                continue
            usable.setdefault(bench.type, {
                "type": bench.type, "label": bench.label,
                "sample_count": bench.sample_count})
    for name in prefer:
        if name in usable:
            return usable[name]
    return None


def _percentiles_in(measures: Sequence[Any], scope: str) -> dict[str, float]:
    """その母集団での percentile を、指標キーごとに **0〜1 に揃えて**。

    ``Benchmark.percentile`` は **0〜100（%）**です。この単位の取り違えは、
    落ちずに**それらしい数字を出しつづけます**——軸の点が 9560 になり、
    しきい値 0.20 は 100 点満点の 0.2 ポイントとして働き、どんな差でも
    「アンバランス」と判定されます。実測でそうなりました。
    """
    out: dict[str, float] = {}
    for measure in measures:
        for bench in getattr(measure, "benchmarks", []) or []:
            if bench.type == scope and bench.percentile is not None:
                out[measure.key] = float(bench.percentile) / 100.0
    return out


def _label_for(percentile: float, bands: Iterable[Mapping[str, Any]]) -> str:
    for band in bands:
        if percentile >= float(band.get("at_least", 0)):
            return str(band.get("label", ""))
    return ""


def _axes(percentiles: Mapping[str, float], measures: Sequence[Any],
          config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """ランキング → 評価（指示書 §14）。

    **取れなかった指標は平均に入れません。** 0 として扱うと、データが無いこと
    が「低い」になります。1 つも取れなければ、その軸は未確認として返します
    ——黙って落とすと、読み手には「調べたうえで低い」に見えます。
    """
    labels = {m.key: m.label for m in measures}
    bands = config.get("bands") or []
    out: list[dict[str, Any]] = []
    for key, spec in (config.get("axes") or {}).items():
        keys = list(spec.get("measures") or [])
        found = [(k, percentiles[k]) for k in keys if k in percentiles]
        missing = [labels.get(k, k) for k in keys if k not in percentiles]
        if not found:
            out.append({
                "key": key, "label": spec.get("label", key),
                "score": None, "percentile": None, "assessment": None,
                "means": spec.get("means"),
                "unavailable_reason": (
                    "、".join(missing) + " の分布が取れないため、この軸は未確認です。"),
            })
            continue
        percentile = sum(p for _, p in found) / len(found)
        out.append({
            "key": key,
            "label": spec.get("label", key),
            # 0〜100。**順位ではなく、母集団の中の位置です。**
            "score": round(percentile * 100),
            "percentile": round(percentile, 4),
            "assessment": _label_for(percentile, bands),
            "means": spec.get("means"),
            "from_measures": [k for k, _ in found],
            # 何が欠けたまま出した平均なのか。**平均は欠落を隠します。**
            "missing_measures": missing or None,
        })
    return out


def _gaps(percentiles: Mapping[str, float], measures: Sequence[Any],
          config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """指標間のアンバランス（指示書 §7・§11）。

    **これがこのモジュールの本体です。** 引き算そのものは単純ですが、
    引き算をどこでやるかが問題でした。LLM にやらせると解釈になり、ここで
    やれば Fact になります。

    しきい値に届かなかった組み合わせも返します。**「調べたが差は無かった」と
    「見ていない」は別のこと**で、返さないと読み手には区別がつきません。
    """
    labels = {m.key: m.label for m in measures}
    out: list[dict[str, Any]] = []
    for key, spec in (config.get("gaps") or {}).items():
        a_key, b_key = str(spec.get("a")), str(spec.get("b"))
        a, b = percentiles.get(a_key), percentiles.get(b_key)
        if a is None or b is None:
            missing = [labels.get(k, k) for k, v in ((a_key, a), (b_key, b))
                       if v is None]
            out.append({
                "key": key, "label": spec.get("label", key), "present": False,
                "unavailable_reason": "、".join(missing) + " の分布が取れませんでした。",
            })
            continue
        gap = a - b
        threshold = float(spec.get("threshold", DEFAULT_GAP_THRESHOLD))
        statement = (str(spec.get("a_over_b") or "") if gap > 0
                     else str(spec.get("b_over_a") or ""))
        out.append({
            "key": key,
            "label": spec.get("label", key),
            "present": abs(gap) >= threshold,
            # 差そのもの。**符号を残します**——どちらが上かが結論を変えます。
            "gap": round(gap, 4),
            "threshold": threshold,
            "statement": statement if abs(gap) >= threshold else None,
            "a": {"key": a_key, "label": labels.get(a_key, a_key),
                  "percentile": round(a, 4)},
            "b": {"key": b_key, "label": labels.get(b_key, b_key),
                  "percentile": round(b, 4)},
            "note": spec.get("note"),
        })
    return out


def _region_type(axes: Sequence[Mapping[str, Any]],
                 config: Mapping[str, Any]) -> dict[str, Any]:
    """地域タイプ（指示書 §9）。**名称を LLM に決めさせません。**

    上から順に、条件を全部満たした最初のもの。どれにも当たらなければ
    「型に当てはまらない」と言います。**無理に名前を付けません**——付けた
    名前は、そのあと読み手の頭の中で事実として働きます。
    """
    scores = {a["key"]: a["percentile"] for a in axes
              if a.get("percentile") is not None}
    for rule in (config.get("region_types") or []):
        conditions = rule.get("when") or {}
        because: list[str] = []
        for axis, bound in conditions.items():
            value = scores.get(axis)
            if value is None:
                break
            if "at_least" in bound and value < float(bound["at_least"]):
                break
            if "below" in bound and value >= float(bound["below"]):
                break
            because.append(f"{axis}={value:.2f}")
        else:
            return {"label": rule.get("label"), "because": because,
                    "rule": conditions}
    return {
        "label": None,
        "because": [],
        "why_not": ("設定した型のどれにも当てはまりませんでした。"
                    "**型が無いことは、特徴が無いことではありません。**"
                    "軸ごとの評価を読んでください。"),
    }


def citable(positioning: Mapping[str, Any],
            layer_of: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """FACT が引ける形に平らにする。

    **ここを通さないと、LLM はこの計算結果を引用できません。** 引用できない
    数字は LLM が言い直すしかなく、言い直した瞬間に検算できなくなります。

    層（``layer``）は**元の指標のもの**を使います。``positioning`` という層を
    新設しません。新設すると、ギャップを 2 つ引いた PATTERN が「層を跨いだ」
    と判定されます。**跨いでいるのは同じ構造を 2 回言い換えただけ**で、
    そこを見張るために層の検算があります。

    ギャップは 2 つの層をまたぐ事実です。片方（``a``）の層を付け、note に
    もう片方を書きます。**PATTERN が本当に跨ぐには、``b`` 側も引く必要が
    あります。**
    """
    if not positioning.get("available"):
        return []
    layer_of = layer_of or {}
    out: list[dict[str, Any]] = []
    scope = positioning.get("compared_with") or {}
    version = positioning.get("benchmark_version", "")
    source = (f"GIS 事前計算 {version}（比較母集団：{scope.get('label', '')}／"
              f"{scope.get('sample_count', 0):,} 商圏）")

    for axis in positioning.get("axes") or []:
        if axis.get("score") is None:
            continue
        keys = axis.get("from_measures") or []
        out.append({
            "key": f"positioning.axis.{axis['key']}",
            "label": f"{axis['label']}の位置づけ（0〜100）",
            "value": axis["score"],
            "unit": "",
            "layer": layer_of.get(keys[0] if keys else "", "residents"),
            "source": source,
            "note": (f"{axis.get('assessment', '')}。{axis.get('means') or ''}"
                     "　**順位ではなく、比較母集団の中での位置です。**"),
        })

    for gap in positioning.get("gaps") or []:
        if not gap.get("present"):
            continue
        a, b = gap["a"], gap["b"]
        a_layer = layer_of.get(a["key"], "residents")
        b_layer = layer_of.get(b["key"], "residents")
        out.append({
            "key": f"positioning.gap.{gap['key']}",
            "label": f"{gap['label']}の差（percentile ポイント）",
            "value": round(float(gap["gap"]) * 100, 1),
            "unit": "ポイント",
            "layer": a_layer,
            "source": source,
            "note": (f"{gap.get('statement') or ''}"
                     f"（{a['label']} {a['percentile']:.0%} 対 "
                     f"{b['label']} {b['percentile']:.0%}）。"
                     f"この差は {a_layer} と {b_layer} をまたぐ計算です。"
                     + (f" {gap['note']}" if gap.get("note") else "")),
        })
    return out
