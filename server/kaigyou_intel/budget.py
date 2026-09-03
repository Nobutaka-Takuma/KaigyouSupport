"""1 回の分析が、最悪どれだけのトークンを使いうるか。

**実測ではなく上限の見積もりです。** 実際にはこれよりずっと少なく済みます
（思考が短く終わる、検索が少なくて済む）。見たいのは「最悪でいくらか」で、
料金が尽きるのは最悪が続いたときだからです。

数え方は単純です。段ごとに

    入力  = system プロンプト + 渡すデータ（+ 検索結果）
    出力  = max_tokens（天井まで使ったとみなす）

として足します。検索結果の大きさは実測の目安（1 回 4,000 トークン前後）を
置いています。ここだけは推測なので、その旨を出力に書きます。
"""
from __future__ import annotations

from typing import Any, Mapping

from kaigyou_core import config as cfg
from kaigyou_core.analysis import DEFAULT_CATEGORY

#: 日本語 1 トークンあたりのバイト数（概算）。厳密な値が要るときは
#: `messages.count_tokens` を使ってください——こちらは課金前の目安です。
BYTES_PER_TOKEN = 2

#: Web 検索 1 回で文脈に入る量の目安（トークン）。実測の概算です。
SEARCH_RESULT_TOKENS = 4_000

#: 100 万トークンあたりの単価（USD）。**設定に置きます**——モデルを変えたら
#: ここも変わります。書いていないモデルは見積もりを出しません（0 と書くと
#: 「ただ」に見えます）。
PRICES = {
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def estimate(category: str = DEFAULT_CATEGORY) -> dict[str, Any]:
    """周辺一般と競合、それぞれ 1 回ぶんの上限見積もり。"""
    config = cfg.analysis_config()
    model = (config.get("model") or {}).get("id", "")
    limits = config.get("limits") or {}
    survey = (cfg.competitors_config(category).get("survey") or {})

    return {
        "model": model,
        "budget_mode": cfg.budget_mode(),
        "prices_known": model in PRICES,
        "runs": [_area(config, limits, category), _competitors(config, survey, category)],
        "note": ("上限の見積もりです。実際にはこれより少なく済みます"
                 "（思考が短く終わる、検索が少なくて済む）。"
                 f"検索 1 回 = 約 {SEARCH_RESULT_TOKENS:,} トークンとして数えています。"),
    }


def _area(config: Mapping[str, Any], limits: Mapping[str, Any],
          category: str) -> dict[str, Any]:
    """プレDD レポート。**LLM の呼び出しは 1 回だけです。**

    1 段目（事実の確定）は DB のデータを数えるだけで API を叩きません。
    数えるのは 2 段目の 1 回きりです。
    """
    steps = config.get("steps") or {}
    calls = {number: (0 if (step or {}).get("prompt") is None else 1)
             for number, step in steps.items()}
    return _rollup("周辺一般の分析（プレDD）", steps, calls, 0, config, category,
                   detail="1 段目は DB を数えるだけ（API を叩きません）")


def _competitors(config: Mapping[str, Any], survey: Mapping[str, Any],
                 category: str) -> dict[str, Any]:
    steps = config.get("competitor_steps") or {}
    n = int(survey.get("max_competitors", 0))
    per = int(survey.get("searches_per_competitor", 0))
    # 1 院 = 調べる + 構造化 の 2 回。
    return _rollup(f"周辺の競合（{n} 院）", steps, {1: 2 * n, 2: 1},
                   n * per, config, category, detail=f"1 院 2 呼び出し × {n} 院")


def _rollup(label: str, steps: Mapping[Any, Any], calls: Mapping[int, int],
            searches: int, config: Mapping[str, Any], category: str,
            detail: str = "") -> dict[str, Any]:
    model = config.get("model") or {}
    total_calls = sum(calls.values())
    out_per_call = int(model.get("max_tokens", 16_000))
    if not total_calls:
        return {"label": label, "calls": 0, "searches": 0, "input_tokens": 0,
                "output_tokens": 0, "detail": detail, "usd": 0.0}

    # 入力：system プロンプトは呼び出しごとに送ります（キャッシュに乗れば
    # 1/10 の単価ですが、**乗らない前提で数えます**——上限の見積もりなので）。
    prompt_tokens = 0
    for number, count in calls.items():
        step = steps.get(number) or {}
        prompt_tokens += count * _prompt_size(step, category)

    inputs = prompt_tokens + searches * SEARCH_RESULT_TOKENS
    outputs = total_calls * out_per_call
    row = {"label": label, "calls": total_calls, "searches": searches,
           "input_tokens": inputs, "output_tokens": outputs, "detail": detail}
    price = PRICES.get(str(model.get("id")))
    if price:
        row["usd"] = inputs / 1e6 * price[0] + outputs / 1e6 * price[1]
    return row


def _prompt_size(step: Mapping[str, Any], category: str) -> int:
    """その段の system プロンプトの大きさ（トークン概算）。"""
    total = 0
    for key in ("prompt", "prompt_structure", "prompt_surroundings",
                "prompt_inquiry", "prompt_followup"):
        name = step.get(key)
        if not name:
            continue
        try:
            total = max(total, len(cfg.prompt_text(name, category).encode()))
        except cfg.ConfigNotFound:
            continue
    return total // BYTES_PER_TOKEN
