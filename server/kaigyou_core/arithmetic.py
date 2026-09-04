"""レポート本文で使う「計算した数字」を、こちらで計算し直して確かめる。

**LLM に計算式まで書かせます。** 割り算を禁じても守られず、黙って許すと
根拠のない数字が残り、弾くと文書ごと消えました（実測 3 回）。第 4 の道は、
式を出させてこちらで計算することです。

    LLM   「年少人口比 13.1%」  式: 1572 / 12000 * 100
    こちら 1572 と 12000 が束にあるか確かめ、式を計算して 13.1 と一致するか見る

これで派生値が**検証可能な事実**になります。読み手にも式が見えるので、
「その数字はどこから来たのか」を文書の中だけで追えます。

式は `ast` で構文木にしてから、**数と四則演算だけ**を通します。名前も呼び出しも
通しません（`eval` に文字列を渡すのとは別物です）。
"""
from __future__ import annotations

import ast
import operator
from typing import Any, Mapping, Sequence

#: 通す演算。**これだけです。**
_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub,
           ast.Mult: operator.mul, ast.Div: operator.truediv}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}

#: 式に書いてよい、データ由来でない数。
#:
#: 「% にするための 100」「1 万人あたりの 10000」のような換算だけです。
#: これを広く取ると、**式の形をした作り話**が通ります。
CONSTANTS = frozenset({1, 2, 3, 4, 5, 10, 12, 100, 1000, 10000, 100000})

#: 式の長さの上限。長い式は、たいてい説明ではなく辻褄合わせです。
MAX_LENGTH = 120


class BadExpression(ValueError):
    """式として受け取れない。"""


def evaluate(expression: str) -> float:
    """四則演算だけの式を計算する。**名前も呼び出しも通しません。**"""
    text = str(expression or "").strip()
    if not text:
        raise BadExpression("式が空です")
    if len(text) > MAX_LENGTH:
        raise BadExpression(f"式が長すぎます（{len(text)} 文字）")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise BadExpression(f"式として読めません: {exc.msg}") from exc
    return _walk(tree.body)


def _walk(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise BadExpression("数以外が書かれています")
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        right = _walk(node.right)
        if isinstance(node.op, ast.Div) and right == 0:
            raise BadExpression("0 で割っています")
        return _BINARY[type(node.op)](_walk(node.left), right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_walk(node.operand))
    raise BadExpression("四則演算だけが書けます")


def literals(expression: str) -> list[float]:
    """式に出てくる数を、そのまま並べる。"""
    try:
        tree = ast.parse(str(expression or ""), mode="eval")
    except SyntaxError:
        return []
    return [float(n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant)
            and not isinstance(n.value, bool)
            and isinstance(n.value, (int, float))]


def tolerance(stated: str | float) -> float:
    """書かれた値の**桁数から**、許す差を決める。

    「13.1」と書いてあれば、それは小数第 1 位に丸めた値です。だから 0.05 まで
    許します。「13」なら 0.5。**丸めたことを間違いにしません。**
    """
    text = str(stated)
    places = len(text.split(".")[1]) if "." in text else 0
    return 0.5 * (10 ** -places)


def check(items: Sequence[Mapping[str, Any]],
          pack_numbers: set[str]) -> list[dict[str, Any]]:
    """派生値を 1 つずつ計算し直す。

    返すのは、元の項目に判定を足したものです。**落としません**——読み手に
    見せるのは「確かめられた式」と「確かめられなかった式」の両方で、後者を
    黙って消すと、その数字が本文に残ったまま根拠だけが消えます。
    """
    out: list[dict[str, Any]] = []
    for item in items:
        row = {"label": item.get("label") or "",
               "expression": str(item.get("expression") or ""),
               "value": item.get("value"),
               "unit": item.get("unit") or "",
               "source": item.get("source") or ""}
        out.append({**row, **_verdict(row, pack_numbers)})
    return out


def _verdict(row: Mapping[str, Any], pack_numbers: set[str]) -> dict[str, Any]:
    from kaigyou_core.dd import _renderings

    try:
        computed = evaluate(row["expression"])
    except BadExpression as exc:
        return {"ok": False, "computed": None, "problem": str(exc)}

    # 式に出てくる数が、束にあるか。**ここが「作り話でない」の担保です。**
    unknown = [n for n in literals(row["expression"])
               if not (int(n) in CONSTANTS and float(n).is_integer())
               and not (_renderings(n) & pack_numbers)]
    if unknown:
        return {"ok": False, "computed": computed,
                "problem": "式の "
                           + "・".join(_format(n) for n in unknown)
                           + " が、確定した事実の中にありません"}

    stated = row.get("value")
    if stated is None:
        return {"ok": True, "computed": computed, "problem": ""}
    try:
        difference = abs(float(stated) - computed)
    except (TypeError, ValueError):
        return {"ok": False, "computed": computed,
                "problem": f"値「{stated}」が数として読めません"}
    if difference > max(tolerance(stated), abs(computed) * 0.01):
        return {"ok": False, "computed": computed,
                "problem": f"式の答えは {_format(computed)} で、"
                           f"書かれた {stated} と違います"}
    return {"ok": True, "computed": computed, "problem": ""}


def _format(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.4g}"


def verified_values(checked: Sequence[Mapping[str, Any]]) -> set[str]:
    """確かめられた派生値。**本文で使ってよい数**として足します。"""
    from kaigyou_core.dd import _renderings

    out: set[str] = set()
    for row in checked:
        if not row.get("ok"):
            continue
        for value in (row.get("value"), row.get("computed")):
            if value is None:
                continue
            try:
                out.update(_renderings(float(value)))
            except (TypeError, ValueError):
                continue
    return out
