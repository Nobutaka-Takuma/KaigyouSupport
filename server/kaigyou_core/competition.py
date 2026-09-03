"""競合データを集計する（開発指示書 §4・§5）。

**LLM を呼びません。** 数え上げと並べ替えです。指示書 §8 の
Fact → Analysis → Hypothesis のうち、ここは Analysis にあたります。

- Fact       … Web / GIS から確認できた事実（各 Competitor）
- Analysis   … それを集計・比較した結果（このモジュール）
- Hypothesis … そこから考えられる市場機会（LLM）

数え上げを LLM にやらせない理由は、これまでと同じです。**数えた結果は
検算できますが、モデルが数えたと言った数は検算できません。**
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence


def tally(competitors: Sequence[Mapping[str, Any]],
          config: Mapping[str, Any],
          near_radius_m: int = 500) -> dict[str, Any]:
    """この地域では何が多く、何が少ないか（指示書 §4）。

    **語彙は設定から来ます。** 出てこなかった項目も 0 件として並べます——
    「この地域にインプラントを掲げる医院は無い」は、**数えた結果であって
    調べ落としではない**、と読めるようにするためです。
    """
    label = str(config.get("label") or "競合")
    surveyed = [c for c in competitors if c.get("name")]
    products = Counter(p for c in surveyed for p in (c.get("products") or []))
    targets = Counter(t for c in surveyed for t in (c.get("target") or []))
    positions = Counter(p for c in surveyed for p in (c.get("positioning") or []))
    place = Counter(f.get("key") for c in surveyed
                    for f in (c.get("place") or []) if f.get("key"))

    return {
        "surveyed": len(surveyed),
        "within_near": len([c for c in surveyed
                            if _distance(c) is not None
                            and _distance(c) <= near_radius_m]),
        "near_radius_m": near_radius_m,
        # 設定の語彙を全部並べます。**0 件も行として残します。**
        "products": _counted(config.get("products") or [], products),
        "targets": _counted(config.get("segments") or [], targets),
        # ポジションは自由記述なので、出てきたものだけ。
        "positioning": [{"label": k, "count": v}
                        for k, v in positions.most_common()],
        "place": [
            {"key": a.get("key"), "label": a.get("label"),
             "count": place.get(a.get("key"), 0)}
            for a in (config.get("place_attributes") or [])
        ],
        # 横軸の高いほうに寄っている件数（指示書 §4 の最後：自費診療を強く
        # 訴求する医院数）。**呼び方は設定の軸から**取ります——飲食なら
        # 「高価格帯」、学習塾なら「受験対応」です。判定は位置から。
        "leaning_x_high": len([c for c in surveyed
                               if (c.get("map_x") or 0) > 0]),
        "leaning_x_high_label": str(
            ((config.get("positioning_map") or {}).get("x") or {}).get("high") or ""),
        "note": (f"件数は**公開情報から確認できたもの**です。"
                 f"各{label}のサイトに書かれていない項目は数に入りません。"
                 f"**扱っていないという意味ではありません。**"),
    }


def _counted(vocabulary: Sequence[str],
             counts: Mapping[str, int]) -> list[dict[str, Any]]:
    known = [{"label": label, "count": counts.get(label, 0)} for label in vocabulary]
    # 設定に無い語が出てきたら、それも残します。捨てると、語彙の抜けに
    # 気づけません。
    extra = [{"label": k, "count": v, "outside_vocabulary": True}
             for k, v in counts.items() if k not in set(vocabulary)]
    return sorted(known + extra, key=lambda r: -r["count"])


def positioning_map(competitors: Sequence[Mapping[str, Any]],
                    config: Mapping[str, Any]) -> dict[str, Any]:
    """2 軸に並べる（指示書 §5）。

    **判定困難を無理に置きません。** 置いてしまうと、地図の上では他の点と
    同じ確かさに見えます。置けなかった医院は件数と理由で残します。
    """
    axes = config.get("positioning_map") or {}
    label = str(config.get("label") or "競合")
    placed, undecided = [], []
    for c in competitors:
        if not c.get("name"):
            continue
        x, y = c.get("map_x"), c.get("map_y")
        if x is None or y is None:
            undecided.append({"name": c["name"],
                              "why": c.get("map_basis") or "公開情報から判定できず"})
            continue
        placed.append({"name": c["name"], "x": int(x), "y": int(y),
                       "distance_m": _distance(c),
                       "basis": c.get("map_basis") or ""})
    return {
        "x": axes.get("x") or {},
        "y": axes.get("y") or {},
        "scale": axes.get("scale") or [-2, -1, 0, 1, 2],
        "placed": placed,
        "undecided": undecided,
        # どの区画に何院いるか。**空いている区画が「機会」とは限りません。**
        "quadrants": _quadrants(placed, axes),
        "note": (f"位置は各{label}の Web 上の訴求から判定したものです。"
                 f"**判定できなかった{label}は置いていません**——"
                 f"無理に置くと、他の点と同じ確かさに見えます。"),
    }


def _quadrant_labels(axes: Mapping[str, Any]) -> dict[tuple[int, int], str]:
    """4 区画の呼び方を、**設定の軸から作ります。**

    「自費 × 専門」をコードに書くと、この関数は歯科でしか読めません。飲食なら
    「高単価 × 専門店」、学習塾なら「高価格 × 受験対応」です。枠（2 軸 4 区画）は
    業態を問わず同じで、**呼び方だけが業態のもの**なので、設定から取ります。
    """
    x, y = axes.get("x") or {}, axes.get("y") or {}
    def side(axis: Mapping[str, Any], sign: int, fallback: str) -> str:
        return str(axis.get("high" if sign > 0 else "low") or fallback)
    return {
        (sx, sy): f"{side(x, sx, '−' if sx < 0 else '＋')}"
                  f" × {side(y, sy, '−' if sy < 0 else '＋')}"
        for sx in (1, -1) for sy in (1, -1)
    }


def _quadrants(placed: Sequence[Mapping[str, Any]],
               axes: Mapping[str, Any]) -> list[dict[str, Any]]:
    counts: Counter = Counter()
    on_axis = 0
    for point in placed:
        x, y = point["x"], point["y"]
        if x == 0 or y == 0:
            on_axis += 1
            continue
        counts[(1 if x > 0 else -1, 1 if y > 0 else -1)] += 1
    out = [{"label": label, "count": counts.get(key, 0)}
           for key, label in _quadrant_labels(axes).items()]
    if on_axis:
        out.append({"label": "どちらとも言えない（軸上）", "count": on_axis})
    return out


def _distance(competitor: Mapping[str, Any]) -> float | None:
    value = competitor.get("distance_m")
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
