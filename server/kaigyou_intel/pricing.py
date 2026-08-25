"""使ったトークンから概算の費用を出す。

請求の正解はコンソール側です。ここが答えるのは「このレポートはいくらだったか」
の目安で、それが分からないと `effort` や検索上限を下げる判断ができません
（要件 §34）。

CLI と API の両方から呼びます。片方にだけ表を置くと、画面と端末で違う金額が
出ます。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

#: 1M トークンあたりの単価（入力, 出力）。
PRICES: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5": (10.0, 50.0),
}

#: キャッシュの単価は入力に対する倍率。読み出しは安く、書き込みは少し高い。
CACHE_READ_RATE, CACHE_WRITE_RATE = 0.1, 1.25


def step_cost(step: Mapping[str, Any]) -> float | None:
    """1 ステップぶん。モデルが表に無ければ None（黙って 0 にしない）。"""
    price = PRICES.get(step.get("model") or "")
    if not price:
        return None
    return ((step.get("input_tokens") or 0) / 1e6 * price[0]
            + (step.get("cache_read_tokens") or 0) / 1e6 * price[0] * CACHE_READ_RATE
            + (step.get("cache_write_tokens") or 0) / 1e6 * price[0] * CACHE_WRITE_RATE
            + (step.get("output_tokens") or 0) / 1e6 * price[1])


def total_cost(steps: Iterable[Mapping[str, Any]]) -> float | None:
    """分かるぶんだけ合計する。

    1 ステップでも単価が分からなければ None を返します。「一部だけの合計」を
    総額として見せると、実際より安いと思われます。
    """
    costs = [step_cost(step) for step in steps
             if (step.get("input_tokens") or step.get("output_tokens")
                 or step.get("cache_write_tokens"))]
    if not costs:
        return 0.0
    if any(cost is None for cost in costs):
        return None
    return sum(cost for cost in costs if cost is not None)
