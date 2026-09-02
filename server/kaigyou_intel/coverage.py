"""何を問い、何に答えが出て、何が未確認か——を**1か所で数える**。

指示書 §25 の段階表示（「5つのパターンを発見しました」「7つの問いを立て、
5つに答えが出ました」）と、レポート冒頭の「この分析で確かめたこと」は、
同じことを数えています。**別々に数えさせません。**

別々に数えると、待っている間に画面で見た「答えが出た 5 件」と、出来上がった
レポートの「4 件」が食い違います。どちらが正しいのかを読み手が確かめる術は
なく、確かめられない食い違いは、数字そのものへの信用を落とします。

ここは LLM を呼びません。保存済みのステップ出力を読んで数えるだけです。
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

#: 一次資料とみなす出典区分。**「23件調べました」だけでは質が分かりません。**
#: 官公庁の告示と個人ブログを同じ 1 件として数えると、件数が多いほど
#: 信頼できるように見えてしまいます。
PRIMARY_SOURCES = frozenset(
    {"government", "statistics", "prefecture", "municipality", "public_body"})

#: 判定の並びと表示名。**「違うと分かった」を最後に置きません。**
#: 末尾は読み飛ばされます。ここは読ませたいところです。
VERDICT_COUNTS = (
    ("SUPPORTED", "支持された"),
    ("PARTIALLY_SUPPORTED", "一部が支持された"),
    ("CONTRADICTED", "調べたら違うと分かった"),
    ("UNCERTAIN", "調べたが分からなかった"),
    ("UNSUPPORTED", "支持されなかった（旧判定）"),
)


def inquiry_from_steps(step1: Mapping[str, Any] | None,
                       step2: Mapping[str, Any] | None) -> dict[str, Any]:
    """問い（STEP1）と答え（STEP2）を 1 つに束ねる。

    問いを立てる段と答える段が別なので、読む側はここで合わせます。
    レポートも画面も同じ束を見ます。
    """
    one = step1 or {}
    two = step2 or {}
    return {
        "questions": list(one.get("questions") or []),
        "patterns": list(one.get("patterns") or []),
        "facts": list(one.get("facts") or []),
        "hypotheses": list(two.get("hypotheses") or []),
        "external_facts": list(two.get("external_facts") or []),
        "open_questions": list(two.get("open_questions") or []),
    }


def tally(inquiry: Mapping[str, Any] | None,
          sources: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """数える。**判定が 0 件の欄は返しません。**

    「調べたら違うと分かった 0」を並べると、読み手はそれを結果として読みます。
    0 件は「その判定が出なかった」であって「0 という結果が出た」ではありません
    （このプロジェクトが「0 と NULL は違う」と言ってきたのと同じ話です）。
    """
    inquiry = inquiry or {}
    questions = list(inquiry.get("questions") or [])
    hypotheses = list(inquiry.get("hypotheses") or [])
    open_questions = list(inquiry.get("open_questions") or [])

    # 「答えが出た」は、仮説が紐づいていて、かつ未決着に挙がっていないもの。
    # 仮説が付いただけでは足りません——調べた結果 UNCERTAIN で終わった問いは、
    # STEP2 が open_questions にも入れます。
    open_ids = {str(q.get("question_id")) for q in open_questions}
    answered_ids = {str(h.get("question_id")) for h in hypotheses
                    if h.get("question_id")}
    answered = len([q for q in questions
                    if str(q.get("id")) in answered_ids
                    and str(q.get("id")) not in open_ids])

    counts = Counter(str(h.get("status")) for h in hypotheses)
    # 本文が引用したものだけを数えます。検索して開いただけの頁は、
    # 「調べた件数」ではあっても「根拠になった件数」ではありません。
    cited = [s for s in sources if s.get("pattern_id")]
    primary = [s for s in cited if (s.get("source_type") or "") in PRIMARY_SOURCES]

    return {
        "facts": len(inquiry.get("facts") or []),
        "patterns": len(inquiry.get("patterns") or []),
        "questions": len(questions),
        "answered": answered,
        "hypotheses": len(hypotheses),
        "verdicts": [{"key": key, "label": label, "count": counts[key]}
                     for key, label in VERDICT_COUNTS if counts.get(key)],
        "external_facts": len(inquiry.get("external_facts") or []),
        "cited_sources": len(cited),
        "primary_sources": len(primary),
        "open_questions": _open_list(open_questions, questions),
    }


def _open_list(open_questions: Sequence[Mapping[str, Any]],
               questions: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """未確認のまま残った問いを、**次にやることとセットで**返す。

    「分かりませんでした」だけを返すと、受け取った側は何もできません。
    決着させる手立てまで一緒に出します（指示書 §15）。
    """
    text = {str(q.get("id")): str(q.get("question") or "") for q in questions}
    out: list[dict[str, str]] = []
    for item in open_questions:
        qid = str(item.get("question_id") or "")
        out.append({
            "question_id": qid,
            "question": text.get(qid, qid),
            "what_would_settle_it": str(item.get("what_would_settle_it") or ""),
        })
    return out


def is_empty(counts: Mapping[str, Any]) -> bool:
    """数えるものが何も無いか。

    古い形で保存されたジョブでは問いも仮説もありません。そこに「問い 0 件」と
    出すと、**「調べていない」に見えます。**実際は「この版では記録していない」
    です。区別が付けられないなら、出さないほうが正確です。
    """
    return not (counts.get("questions") or counts.get("hypotheses")
                or counts.get("patterns"))
