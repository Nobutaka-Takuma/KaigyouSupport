"""失敗を「やり直す価値があるか」で分ける。

同じ「失敗」でも、直し方はまったく違います。

    モデルが出典を1つ書き間違えた   → もう一度やれば通ることが多い
    残高が足りない                 → 何度やっても同じ
    安全性の判定で拒否された        → 何度やっても同じ
    Anthropic 側が混雑している      → 少し待てば通る

区別せずに「失敗」とだけ扱うと、直るものまで人がボタンを押しに行くことになり、
直らないものは費用だけが増えます。
"""
from __future__ import annotations

#: もう一度やれば通ることがあるもの。モデルの言い間違いと、相手側の一時的な事情。
RETRYABLE_MARKERS = (
    # 検算で落ちたもの。プロンプトは守られる確率の問題なので、引き直しが効きます。
    "参照が解決しませんでした",
    "検索結果に無い URL",
    "前の段に無い数値",
    "レポートを保存しませんでした",
    "構造化出力を受け取れませんでした",
    "調査の本文が空でした",
    # 長さの上限で切れた。思考の量は毎回違うので、引き直しで収まることがあります。
    "max_tokens",
    "EOF while parsing",
    # 相手側の一時的な事情。
    "overloaded",
    "rate_limit",
    "429",
    "529",
    "Connection",
    "timeout",
    "Timeout",
)

#: 何度やっても同じもの。設定・契約・安全性の判断。
PERMANENT_MARKERS = (
    "credit balance is too low",
    "invalid x-api-key",
    "authentication_error",
    "permission_error",
    "ANTHROPIC_API_KEY",
    "モデルが応答を拒否しました",
    "は未実装です",
    "prompt_structure がありません",
)


def is_retryable(error: str | BaseException) -> bool:
    """もう一度やる価値があるか。

    **判定できないものは、やり直しません。** 分からないものを繰り返すのは、
    費用だけが増えていちばん気づかれにくい失敗の仕方です。
    """
    text = str(error)
    if any(marker in text for marker in PERMANENT_MARKERS):
        return False
    return any(marker in text for marker in RETRYABLE_MARKERS)


def describe(error: str | BaseException, attempts: int, limit: int) -> str:
    """人に見せる 1 行。何が起きて、次に何が起きるのか。"""
    if is_retryable(error) and attempts < limit:
        return f"自動でやり直します（{attempts}/{limit} 回目）"
    if is_retryable(error):
        return f"{limit} 回やり直しましたが通りませんでした"
    return "やり直しても同じ結果になる種類の失敗です"
