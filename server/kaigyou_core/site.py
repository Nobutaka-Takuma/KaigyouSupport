"""候補地**そのもの**の性質を、測って確定させる。

このモジュールが答えるのは 2 つだけです。

1. **駅前と呼んでよい場所か。** 距離で決まります。
2. **候補地は密集の中心にいるのか、その外れにいるのか。**

どちらも引き算と割り算です。**LLM に判定させません。**

実測：沼津駅の南南西 810m の候補地が「駅前」として分析されていました。
徒歩でおよそ 10 分です。駅前ではありません。距離は `station_distance_m` として
測ってあるのに、呼び方だけをモデルの読み取りに任せていたのが原因でした。
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

#: 徒歩の分速（不動産表示の慣行）。距離を「徒歩何分」に直すためだけに使います。
WALK_M_PER_MINUTE = 80.0


def station_band(distance_m: float | None,
                 bands: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """駅からの距離を、呼び方に変える。

    ``bands`` は近い順。``within_m`` が null のものが最後の受け皿です。
    距離が測れていなければ None を返します——**「駅から遠い」ではありません。**
    """
    if distance_m is None or not bands:
        return None
    for band in bands:
        limit = band.get("within_m")
        if limit is None or float(distance_m) <= float(limit):
            return {
                "label": band.get("label"),
                "note": band.get("note"),
                "distance_m": round(float(distance_m)),
                "walk_minutes": max(1, round(float(distance_m) / WALK_M_PER_MINUTE)),
                "within_m": limit,
            }
    return None


def concentration(inner: float | None, outer: float | None,
                  inner_radius_m: float, outer_radius_m: float) -> dict[str, Any] | None:
    """候補地の足元と、その周りを比べる。

    面積比は ``(inner/outer)²``。人が一様に住んでいれば、内側の円には外側の
    円の値がその割合だけ入るはずです。実際の割合をそれで割ると、

        1.0 付近 … 候補地の周りは一様
        1.0 より大 … 候補地そのものが密集の中心にある
        1.0 より小 … 密集は外側の円のどこかにあるが、**候補地の足元ではない**

    **これは半径を変えるだけでは出ません。** 2 つの半径を突き合わせて初めて
    「その一帯の話」と「その地点の話」が分かれます。

    外側が 0 なら None。0 で割らないためですが、意味の上でも
    「周りに誰もいない」ときに中心度を語っても仕方がありません。
    """
    if inner is None or not outer or outer <= 0 or outer_radius_m <= 0:
        return None
    expected = (float(inner_radius_m) / float(outer_radius_m)) ** 2
    share = float(inner) / float(outer)
    index = share / expected if expected else None
    return {
        "inner_radius_m": int(inner_radius_m),
        "outer_radius_m": int(outer_radius_m),
        "inner": round(float(inner), 1),
        "outer": round(float(outer), 1),
        "share": round(share, 4),
        # 一様ならこの割合になる、という基準。**読み手が自分で確かめられる形で。**
        "share_if_even": round(expected, 4),
        "index": None if index is None else round(index, 2),
        "reading": _reading(index),
    }


#: 「中心にいる」「外れにいる」と言い切る境目。
#:
#: **狭くしすぎないこと。** メッシュ統計は面積按分なので、境目のあたりの
#: 揺れはメッシュの切れ目で簡単に出ます。0.85〜1.15 は「差とは言わない」幅です。
_EVEN_LOW, _EVEN_HIGH = 0.85, 1.15


def _reading(index: float | None) -> str | None:
    if index is None:
        return None
    if index > _EVEN_HIGH:
        return "候補地そのものが密集の中心にあります。"
    if index < _EVEN_LOW:
        return ("密集は商圏のどこかにありますが、**候補地の足元ではありません。**"
                "円の中の人が、候補地から見て遠い側に寄っています。")
    return "候補地の周りは、ほぼ一様な密度です。"


def resolution_warning(radius_m: float, mesh_size_m: int | None) -> str | None:
    """商圏がメッシュより小さくないか。

    **500m メッシュで半径 500m を測ると、面積換算で 1 メッシュ分もありません。**
    値は面積按分で出ますが、それは「メッシュの中では人が一様に住んでいる」と
    仮定した数字です。半分が公園で半分が団地のメッシュでは、その仮定は外れます。

    黙って出すと、細かい半径を指定したぶんだけ精密になったように見えます。
    """
    if not mesh_size_m or radius_m <= 0:
        return None
    # 円の面積 ÷ メッシュ 1 個の面積
    equivalent = math.pi * radius_m ** 2 / float(mesh_size_m) ** 2
    if equivalent >= 3.0:
        return None
    return (f"商圏（半径{radius_m:.0f}m）は{mesh_size_m}mメッシュ"
            f"{equivalent:.1f}個ぶんの広さしかありません。人口はメッシュとの"
            "面積按分で出していて、**メッシュの中では人が一様に住んでいると"
            "仮定しています。** 半分が公園のメッシュでは、その仮定は外れます。"
            + (f"　{mesh_size_m}mより細かいメッシュを取り込むと精度が上がります。"
               if mesh_size_m > 250 else ""))


# ------------------------------------------------- 階層：候補地から外へ広げる
#
# **始まりは候補地です。最寄り駅ではありません。**
#
# 「最寄り駅がどこか」から始めると、駅の性格が地域の性格として語られます。
# 500m で足元を見て、1km で商圏を見て、2km で「その地域」を見る。駅はそのあと、
# 必要なときだけ説明変数として出てきます（開発指示書 §2・§6）。

def rings(values: Sequence[Mapping[str, Any]],
          radii: Sequence[int]) -> list[dict[str, Any]]:
    """半径ごとの集計を、外へ広げる形に並べる。

    ``values`` は半径の小さい順。各段には**そのひとつ外側との差**も入れます。
    合計だけを並べると、外側が大きいのは当たり前で、**どこで増えたのか**が
    読めません。「1km→2km で人口が 3 倍」と「1.1 倍」は別のことです。
    """
    out: list[dict[str, Any]] = []
    for i, (radius, value) in enumerate(zip(radii, values)):
        row: dict[str, Any] = {"radius_m": int(radius), **dict(value)}
        if i:
            inner = values[i - 1]
            row["growth_from_inner"] = {
                key: _ratio(value.get(key), inner.get(key))
                for key in ("population", "facility_count", "workers")
                if value.get(key) is not None
            }
        out.append(row)
    return out


def _ratio(outer: Any, inner: Any) -> float | None:
    try:
        outer, inner = float(outer), float(inner)
    except (TypeError, ValueError):
        return None
    return None if inner <= 0 else round(outer / inner, 2)


#: 駅の方角から何度までを「駅の側」と数えるか。
#:
#: 90 度なら半円ちょうど。**狭くしすぎないこと**——メッシュは 500m 角なので、
#: 細い扇形にすると入るメッシュが 1〜2 個になり、たまたまで決まります。
STATION_SIDE_DEGREES = 90.0


def station_side(toward: float | None, away: float | None) -> dict[str, Any] | None:
    """商圏の人口は、駅の側に寄っているか。

    **距離だけでは「駅との関係」は決まりません**（開発指示書 §6）。駅まで
    800m でも、人が全員反対側に住んでいるなら、駅は動線ではありません。
    逆に 1km でも、駅との間に住宅が詰まっているなら関係はあります。

    候補地から見て、駅の方角±90 度に入るメッシュを「駅の側」として合計し、
    反対側と比べます。**半々（0.5）なら、駅の方向に偏りはありません。**
    """
    if toward is None or away is None:
        return None
    total = float(toward) + float(away)
    if total <= 0:
        return None
    share = float(toward) / total
    return {
        "toward_station": round(float(toward), 1),
        "away_from_station": round(float(away), 1),
        "share_toward_station": round(share, 3),
        "reading": _side_reading(share),
    }


#: 「駅の側に寄っている」と言い切る境目。0.5 が偏りなし。
_SIDE_LOW, _SIDE_HIGH = 0.42, 0.58


def _side_reading(share: float) -> str:
    if share > _SIDE_HIGH:
        return ("商圏の人口は駅の側に寄っています。駅は来院動線として"
                "働きうる位置にあります。")
    if share < _SIDE_LOW:
        return ("商圏の人口は駅と**反対側**に寄っています。駅までの距離が"
                "近くても、住民の動線は駅を向いていない可能性があります。")
    return "商圏の人口は、駅の側と反対側にほぼ半々です。"
