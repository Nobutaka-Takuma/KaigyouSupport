"""Anthropic API を呼ぶ唯一の場所。

モデル・思考の深さ・Web検索の設定を 1 か所に閉じます。ステップごとに散ると、
「なぜこのステップだけ挙動が違うのか」を追うのに 4 ファイル読むことになります。

要件 §33：モデルとプロンプト版と使用トークンは必ず記録します。記録しないと、
出力が変わったときにモデルのせいかプロンプトのせいか永久に分かりません。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence, Type, TypeVar

from pydantic import BaseModel

from kaigyou_core import config as cfg

T = TypeVar("T", bound=BaseModel)

#: Web検索ツール。要件 §8 の外部調査はこれで行います。検索プロバイダを
#: 別途契約せずに済み、出典 URL とタイトルが結果ブロックに入るので
#: analysis_sources にそのまま落とせます。
#:
#: この型（動的フィルタ付き）は Sonnet 5 / Opus 5 系で使えます。それより古い
#: モデルを設定した場合は基本版に落とす必要があるので、モデルを変えるときは
#: ここも確認してください。
WEB_SEARCH_TOOL_TYPE = "web_search_20260209"

#: このプロジェクトが送らないもの。
#:
#: Sonnet 5 / Opus 5 系では ``budget_tokens`` と ``temperature`` などの
#: サンプリング指定は**削除**されていて、送ると 400 で落ちます。思考の深さは
#: ``thinking: {"type": "adaptive"}`` と ``output_config.effort`` で決めます。
#: 「以前はこう書いた」で足さないよう、理由をここに残します。
REMOVED_PARAMETERS = ("budget_tokens", "temperature", "top_p", "top_k")


class Refused(RuntimeError):
    """安全性の判定で応答が拒否された。障害ではないので、そう記録します。"""


class NotConfigured(RuntimeError):
    """API キーが無い。設定漏れであって、障害ではない。"""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    web_searches: int = 0
    #: キャッシュから読めたぶんと、キャッシュへ書いたぶん。単価が違うので
    #: 入力トークンとは別に数えます（読み出しは約0.1倍、書き込みは約1.25倍）。
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class Result:
    """1 回の呼び出しの結果と、記録すべきもの。"""

    parsed: Any
    text: str = ""
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    #: web_search_tool_result ブロックから拾った出典。
    sources: list[dict[str, Any]] = field(default_factory=list)


def _client():
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - 環境の問題
        raise NotConfigured(
            "anthropic パッケージが入っていません: pip install anthropic") from exc
    # SDK は ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / ant auth login の
    # プロファイルを順に見ます。キーが未設定でもプロファイルがあれば動くので、
    # ここで環境変数の有無を判定して弾いたりはしません。
    return anthropic.Anthropic()


def is_configured() -> bool:
    """呼べる状態か。API を叩かずに判定します。"""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    if os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return True
    from pathlib import Path
    return (Path.home() / ".config" / "anthropic").is_dir()


def step_settings(step_number: int) -> dict[str, Any]:
    config = cfg.analysis_config()
    step = (config.get("steps") or {}).get(step_number) or {}
    model = config.get("model") or {}
    return {
        "name": step.get("name", f"step{step_number}"),
        "prompt": step.get("prompt"),
        # 調べた本文を JSON に写すだけの 2 回目の呼び出しで使うプロンプト。
        # Web検索と構造化出力は同じ呼び出しでは併用しないので、STEP2 は
        # 「調べる」「書き写す」の 2 回に分かれます。
        "prompt_structure": step.get("prompt_structure"),
        "prompt_version": step.get("prompt_version", f"step{step_number}-v0"),
        "web_search": bool(step.get("web_search")),
        "model": step.get("model") or model.get("id") or "claude-opus-5",
        "effort": step.get("effort") or model.get("effort") or "high",
        "max_tokens": int(step.get("max_tokens") or model.get("max_tokens") or 16000),
    }


def _web_search_tool(limits: Mapping[str, Any], search: Mapping[str, Any]) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "type": WEB_SEARCH_TOOL_TYPE,
        "name": "web_search",
        "max_uses": int(limits.get("max_searches_total", 15)),
    }
    # 要件 §9 の優先順位は、プロンプトでのお願いではなく API 側で効かせます。
    # 守らせられるものは仕組みで守らせるほうが確実です。
    allowed = [d for d in (search.get("allowed_domains") or []) if d]
    blocked = [d for d in (search.get("blocked_domains") or []) if d]
    if allowed:
        tool["allowed_domains"] = allowed
    elif blocked:
        tool["blocked_domains"] = blocked
    return tool


def build_request(step_number: int, system: str, user: str, *,
                  tools: Sequence[Mapping[str, Any]] | None = None,
                  web_search: bool | None = None) -> dict[str, Any]:
    """送信する本体を組み立てる。呼び出しとは分けてあります。

    分けているのは検算のためです。API キーの無い環境でも、この戻り値が
    そのまま JSON にできるかを確かめられます。最初の実装は Pydantic の
    **クラス**を ``output_config.format`` に入れていて、送信の瞬間に
    ``Object of type ModelMetaclass is not JSON serializable`` で落ちました。
    呼び出しごとスタブしたテストでは、その形を一度も見ていませんでした。
    """
    settings = step_settings(step_number)
    config = cfg.analysis_config()

    request: dict[str, Any] = {
        "model": settings["model"],
        "max_tokens": settings["max_tokens"],
        # 「専門家の思考プロセスの再現」が要件なので、考える量を削りません。
        "thinking": {"type": "adaptive"},
        # effort はここ。スキーマは output_format に渡し、SDK が
        # output_config へマージします（型のまま入れてはいけません）。
        "output_config": {"effort": settings["effort"]},
        "system": system,
        "messages": [{"role": "user", "content": user}],
        # 直前までの安定した部分をキャッシュ対象にします。STEP2 はサーバ側の
        # 検索ループが 1 回の呼び出しの中で回り、増えていく文脈を毎回読み直すので
        # （実測 307,754 トークン）、変わらない前置き（system + 最初の user）を
        # 読み直さずに済めばそのぶん安くなります。効いたかどうかは
        # usage.cache_read_input_tokens に出るので、次の実行で確かめます。
        "cache_control": {"type": "ephemeral"},
    }

    declared = list(tools or [])
    # 既定は設定どおり。呼び出し側が False を渡せる（STEP2 の 2 回目の呼び出しは、
    # 1 回目で調べた本文を JSON に写すだけなので、検索させると新しい事実が
    # 増えて、出典の検算が合わなくなります）。
    searching = settings["web_search"] if web_search is None else bool(web_search)
    if searching:
        declared.append(_web_search_tool(config.get("limits") or {},
                                         config.get("search") or {}))
    if declared:
        request["tools"] = declared
    return request


def ask(*, step_number: int, system: str, user: str,
        schema: Type[T] | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        web_search: bool | None = None) -> Result:
    """1 ステップぶんの呼び出し。

    ``schema`` を渡すと構造化出力で受け取り、Pydantic で検証します。
    渡さないときは本文をそのまま返します（Web検索の段のように、まず調べさせて
    から別呼び出しで構造化するとき）。
    """
    settings = step_settings(step_number)
    client = _client()
    request = build_request(step_number, system, user, tools=tools,
                            web_search=web_search)

    # 常にストリームで受けます。SDK は「10 分を超えうる操作」を非ストリームで
    # 呼ぶと送信前に ValueError を投げ、その境目は max_tokens で決まります。
    # 実測：max_tokens を 16,000 から 24,000 に上げた途端、STEP1 が
    #   ValueError: Streaming is required for operations that may take longer
    #   than 10 minutes
    # で落ちました。ストリームなら上限を気にせず上げられます。
    if schema is not None and "tools" not in request:
        # 構造化出力。schema は **型のまま** output_format に渡します。SDK が
        # JSON Schema へ変換して output_config.format にマージします。自分で
        # output_config へ入れると、型がそのまま送信されて落ちます。
        request = {**request, "output_format": schema}
    with client.messages.stream(**request) as stream:
        message = stream.get_final_message()
    # output_format を渡さなかったときは None。text ブロックに付きます。
    parsed = getattr(message, "parsed_output", None)

    # 拒否は例外ではなく HTTP 200 で返ります。content を読む前に確かめないと、
    # 空の応答を「モデルが何も見つけなかった」と取り違えます。
    if getattr(message, "stop_reason", None) == "refusal":
        details = getattr(message, "stop_details", None)
        raise Refused(
            f"モデルが応答を拒否しました（category={getattr(details, 'category', None)}）: "
            f"{getattr(details, 'explanation', '')}")

    return Result(
        parsed=parsed,
        text="".join(b.text for b in message.content if getattr(b, "type", "") == "text"),
        usage=Usage(
            input_tokens=getattr(message.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(message.usage, "output_tokens", 0) or 0,
            web_searches=_count_searches(message),
            cache_read_tokens=getattr(message.usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(
                message.usage, "cache_creation_input_tokens", 0) or 0,
        ),
        model=getattr(message, "model", settings["model"]),
        sources=extract_sources(message),
    )


def _count_searches(message: Any) -> int:
    return sum(1 for b in message.content
               if getattr(b, "type", "") == "web_search_tool_result")


def extract_sources(message: Any) -> list[dict[str, Any]]:
    """web_search_tool_result から出典を拾う。

    サーバ側ツールはエラーを例外にせず、content にエラーオブジェクトを入れて
    HTTP 200 を返します。成功時の content は**リスト**、失敗時は**オブジェクト**
    なので、添字を取る前に分岐します。
    """
    out: list[dict[str, Any]] = []
    for block in getattr(message, "content", []):
        if getattr(block, "type", "") != "web_search_tool_result":
            continue
        content = getattr(block, "content", None)
        if not isinstance(content, list):
            # {"error_code": "max_uses_exceeded"} のような形。件数だけ残します。
            out.append({"error": getattr(content, "error_code", str(content))})
            continue
        for item in content:
            out.append({
                "url": getattr(item, "url", None),
                "title": getattr(item, "title", None),
                "page_age": getattr(item, "page_age", None),
            })
    return out
