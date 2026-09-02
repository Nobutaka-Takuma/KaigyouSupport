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


class Truncated(RuntimeError):
    """出力が max_tokens に達して途中で切れた。

    構造化出力では、これは「壊れた JSON」として現れます。pydantic の
    ``Invalid JSON: EOF while parsing a string at line 1 column 18741`` は、
    モデルが間違った JSON を書いたのではなく、**書き終わる前に止められた**
    という意味です。この 2 つは直し方がまったく違うので、区別して報告します。
    """


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
        # STEP1 の 3 回目。**読む**（prompt）と**疑う**（prompt_inquiry）を
        # 分けています。1 回の構造化出力に全部入れると、文法が大きくなりすぎて
        # API が 400 を返しました。無ければ前提と問いは出ません（FACT と
        # PATTERN は出ます）。
        "prompt_inquiry": step.get("prompt_inquiry"),
        # STEP2 の 2 周目。1 周目の結果を見て、答えの出なかった問いだけを
        # 角度を変えて調べ直す呼び出しで使うプロンプト。無ければ 2 周目は
        # 走りません（limits.research_rounds でも止められます）。
        "prompt_followup": step.get("prompt_followup"),
        # STEP1 の 1 回目。**その場所に何があるのか**を検索する呼び出しで
        # 使うプロンプト。統計を読む前に立地類型が決まらないと、同じ
        # 「昼間人口5万人」をオフィス街としても大学のそばとしても読めます。
        "prompt_surroundings": step.get("prompt_surroundings"),
        "prompt_version": step.get("prompt_version", f"step{step_number}-v0"),
        "web_search": bool(step.get("web_search")),
        "model": step.get("model") or model.get("id") or "claude-opus-5",
        "effort": step.get("effort") or model.get("effort") or "high",
        # 2 回目の呼び出し（STEP2 の書き写し）用。指定が無ければ同じ深さ。
        # 書き写すだけの呼び出しに考えさせても、出るのは調べていないことを
        # 補った文だけです（そして出典の検算で落ちます）。
        "effort_structure": (step.get("effort_structure") or step.get("effort")
                             or model.get("effort") or "high"),
        # 周辺施設スキャン用。検索が律速する呼び出しなので、待っている間の
        # 思考を増やしても待ち時間が伸びるだけです（STEP2 と同じ理由）。
        "effort_scan": (step.get("effort_scan") or step.get("effort")
                        or model.get("effort") or "high"),
        "max_tokens": int(step.get("max_tokens") or model.get("max_tokens") or 16000),
    }


#: 検索回数を環境変数で上書きできるようにします。ホスティング先によって
#: 関数の実行時間の上限が違うためです（Vercel は Hobby で 300 秒、Pro で 800 秒）。
#: STEP2 は検索のたびに文脈を読み直すので、回数がそのまま実行時間になります。
#: コードにも設定ファイルにも「本番はこう」と書かずに、環境で決めます。
MAX_SEARCHES_ENV = "KAIGYOU_MAX_SEARCHES"


def max_searches(limits: Mapping[str, Any]) -> int:
    override = os.getenv(MAX_SEARCHES_ENV)
    if override and override.strip().isdigit():
        return max(1, int(override))
    return int(limits.get("max_searches_total", 15))


def _web_search_tool(limits: Mapping[str, Any], search: Mapping[str, Any],
                     max_uses: int | None = None) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "type": WEB_SEARCH_TOOL_TYPE,
        "name": "web_search",
        # PATTERN ごとに呼び出しを分けるときは、その 1 本ぶんの上限を渡します。
        # 全体の上限をそのまま渡すと、1 本が全部使い切れてしまいます。
        "max_uses": int(max_uses) if max_uses else max_searches(limits),
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


#: キャッシュの保持時間。既定の 5 分か、`1h`。
#:
#: 5 分は**応答の開始から**数えます。レポート 1 本が 12 分かかるなら、次の
#: 地点を分析するころには 5 分のキャッシュは消えています。何地点か続けて
#: 見るなら 1h のほうが当たりますが、書き込みの単価が 1.25 倍から 2 倍に
#: 上がるので、当たらなければ損です。既定は 5 分のままにしてあります。
CACHE_TTL_ENV = "KAIGYOU_CACHE_TTL"


def _cache_control() -> dict[str, Any]:
    ttl = (os.getenv(CACHE_TTL_ENV) or "").strip()
    if ttl == "1h":
        return {"type": "ephemeral", "ttl": "1h"}
    return {"type": "ephemeral"}


def build_request(step_number: int, system: str, user: str, *,
                  tools: Sequence[Mapping[str, Any]] | None = None,
                  web_search: bool | None = None,
                  effort: str | None = None,
                  max_uses: int | None = None) -> dict[str, Any]:
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
        "output_config": {"effort": effort or settings["effort"]},
        # **区切りはシステムプロンプトの末尾に置きます。**
        #
        # 以前はトップレベルに `cache_control` を置いていました（自動キャッシュ）。
        # 自動キャッシュは区切りを「最後のキャッシュ可能なブロック」に置きますが、
        # このアプリではそれが **地点ごとに中身の違う user メッセージ**です。
        # 区切りより前のハッシュが毎回変わるので、**毎回書き込んで一度も読まない**
        # という、いちばん損な形になっていました（公式ドキュメントの
        # "Common mistake: Breakpoint on content that changes every request"）。
        #
        # システムプロンプトは同じ段・同じプロンプト版なら 1 バイトも変わりません。
        # ここに置けば、やり直し（max_attempts で最大3回）と、続けて何地点か
        # 分析したときに読み出しが効きます。
        #
        # **これは費用の話であって、速度の話ではありません。** キャッシュは
        # 出力の生成時間を 1 秒も縮めません（公式：「Prompt caching has no effect
        # on output token generation」）。このアプリの所要時間は思考と JSON の
        # 生成で決まるので、レポート 1 本の時間はこれでは変わりません。
        "system": [{"type": "text", "text": system,
                    "cache_control": _cache_control()}],
        "messages": [{"role": "user", "content": user}],
    }

    declared = list(tools or [])
    # 既定は設定どおり。呼び出し側が False を渡せる（STEP2 の 2 回目の呼び出しは、
    # 1 回目で調べた本文を JSON に写すだけなので、検索させると新しい事実が
    # 増えて、出典の検算が合わなくなります）。
    searching = settings["web_search"] if web_search is None else bool(web_search)
    if searching:
        declared.append(_web_search_tool(config.get("limits") or {},
                                         config.get("search") or {}, max_uses))
    if declared:
        request["tools"] = declared
    return request


def ask(*, step_number: int, system: str, user: str,
        schema: Type[T] | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        web_search: bool | None = None,
        effort: str | None = None,
        max_uses: int | None = None) -> Result:
    """1 ステップぶんの呼び出し。

    ``schema`` を渡すと構造化出力で受け取り、Pydantic で検証します。
    渡さないときは本文をそのまま返します（Web検索の段のように、まず調べさせて
    から別呼び出しで構造化するとき）。
    """
    settings = step_settings(step_number)
    client = _client()
    request = build_request(step_number, system, user, tools=tools,
                            web_search=web_search, effort=effort,
                            max_uses=max_uses)

    # 常にストリームで受けます。SDK は「10 分を超えうる操作」を非ストリームで
    # 呼ぶと送信前に ValueError を投げ、その境目は max_tokens で決まります。
    # 実測：max_tokens を 16,000 から 24,000 に上げた途端、STEP1 が
    #   ValueError: Streaming is required for operations that may take longer
    #   than 10 minutes
    # で落ちました。ストリームなら上限を気にせず上げられます。
    constrained = schema is not None and "tools" not in request
    if constrained:
        # 構造化出力。schema は **型のまま** output_format に渡します。SDK が
        # JSON Schema へ変換して output_config.format にマージします。自分で
        # output_config へ入れると、型がそのまま送信されて落ちます。
        request = {**request, "output_format": schema}

    try:
        message = _send(client, request, settings, step_number)
    except Exception as exc:  # noqa: BLE001 - 文法が大きすぎるときだけ拾う
        if not (constrained and _grammar_too_large(exc)):
            raise
        # **制約を外して、もう一度だけ頼みます。**
        #
        # スキーマが大きすぎると API は 400 を返します。**そのまま落とすと、
        # 段が丸ごと失敗します。** スキーマを小さく保つのが本筋ですが、欄を
        # 1 つ足しただけで越えることがあり、越えたことは動かすまで分かりません。
        #
        # 制約を外しても、欲しい JSON の形は伝えられます。検算（verify_step1
        # など）はどちらの経路でも同じように掛かるので、**通ってはいけない
        # 出力が通るようにはなりません。** 落ちるか、緩い経路で通って検算に
        # かかるかの違いです。
        message = _send(client, _as_prose_json(request, schema, settings),
                        settings, step_number)
    # output_format を渡さなかったときは None。text ブロックに付きます。
    parsed = getattr(message, "parsed_output", None)
    if parsed is None and constrained and schema is not None:
        # 制約を外して頼んだぶん。本文から JSON を取り出して検証します。
        parsed = _parse_prose_json(message, schema)

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


def _send(client: Any, request: Mapping[str, Any], settings: Mapping[str, Any],
          step_number: int) -> Any:
    """常にストリームで受けます。

    SDK は「10 分を超えうる操作」を非ストリームで呼ぶと送信前に ValueError を
    投げ、その境目は max_tokens で決まります。実測：max_tokens を 16,000 から
    24,000 に上げた途端、STEP1 が
      ValueError: Streaming is required for operations that may take longer
      than 10 minutes
    で落ちました。ストリームなら上限を気にせず上げられます。
    """
    with client.messages.stream(**request) as stream:
        try:
            return stream.get_final_message()
        except Exception as exc:  # noqa: BLE001 - 種類を見分けてから投げ直す
            if _looks_truncated(exc):
                raise Truncated(
                    f"出力が max_tokens（{settings['max_tokens']:,}）に達して"
                    "途中で切れました。JSON が壊れているのではなく、書き終わる前に"
                    "止められています。config/analysis.yaml の model.max_tokens か、"
                    f"steps.{step_number}.max_tokens を上げてから、"
                    "そのステップだけやり直してください"
                    "（払うのは実際に出た分だけなので、上げても高くなりません）。"
                ) from exc
            raise


def _grammar_too_large(exc: Exception) -> bool:
    """スキーマが大きすぎて構造化出力を組めなかったか。

    実測のメッセージ：

        The compiled grammar is too large, which would cause performance
        issues. Simplify your tool schemas or reduce the number of strict tools.

    **状態コードでは見分けられません。** 400 は「入力が長すぎる」でも
    「モデル名が違う」でも返ります。緩い経路へ落として構わないのは、
    スキーマが原因のときだけです。
    """
    text = str(exc).lower()
    return "grammar is too large" in text or (
        "grammar" in text and "too large" in text)


def _as_prose_json(request: Mapping[str, Any], schema: Any,
                   settings: Mapping[str, Any]) -> dict[str, Any]:
    """構造化出力の制約を外し、代わりに JSON の形を文章で頼む。

    **スキーマは system の末尾ではなく user の末尾に置きます。** system は
    キャッシュの区切りより前で、そこを変えると以降のキャッシュが全部外れます。
    """
    import json as _json

    body = {k: v for k, v in request.items() if k != "output_format"}
    messages = list(body.get("messages") or [])
    if messages:
        last = dict(messages[-1])
        last["content"] = (
            str(last.get("content", ""))
            + "\n\n---\n\n## 出力形式\n\n"
            "**JSON だけを出力してください。** 前後に説明を書かないでください。"
            "コードブロックの記号（```）も付けないでください。\n\n"
            "次の JSON Schema に従ってください。\n\n```json\n"
            + _json.dumps(schema.model_json_schema(), ensure_ascii=False, indent=1)
            + "\n```")
        messages[-1] = last
    body["messages"] = messages
    return body


def _parse_prose_json(message: Any, schema: Any) -> Any:
    """本文から JSON を取り出して検証する。

    **取れなければ None を返します。** 呼び出し側は「構造化出力を受け取れ
    ませんでした」として扱い、やり直しの対象になります。ここで例外を投げると、
    緩い経路に落ちたことが truncated と区別できなくなります。
    """
    import json as _json

    text = "".join(b.text for b in message.content
                   if getattr(b, "type", "") == "text").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return schema.model_validate(_json.loads(text[start:end + 1]))
    except Exception:  # noqa: BLE001 - 壊れていれば「受け取れなかった」
        return None


def _looks_truncated(exc: Exception) -> bool:
    """途中で切れた JSON かどうか。

    SDK は content_block_stop の時点で本文を解析するので、``stop_reason`` が
    ``max_tokens`` だと分かる前に例外が出ます。だから停止理由では判定できず、
    壊れ方のほうを見ます。「EOF while parsing」は、閉じられていない文字列や
    括弧で終わったということで、モデルの書き間違いではありません。
    """
    text = str(exc)
    return "json_invalid" in text and (
        "EOF while parsing" in text or "control character" in text)


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
