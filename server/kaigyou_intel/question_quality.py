"""立てた問いが、実際に何かを動かしたか。

指示書 §8-3。**出た問いが凡庸かどうかを、あとから評価できるようにします。**

いま問いは PATTERN ごとに 1〜3 個、LLM が出しています。どれが筋の良い問いで
どれが埋め草だったのかは、出た時点では分かりません。分かるのは**あとから**です。

### LLM に採点させません

「この問いは良い問いですか」と聞けば、それらしい点数が返ってきます。ですが
それは問いを出したのと同じモデルの意見で、外から確かめる手立てがありません。
このプロジェクトが「予測はしない、確かめられることだけ書く」と言ってきたのと
同じ理由で、ここも**実際に何が起きたか**だけを数えます。

追えるのは、調査の連鎖が繋がったからです。

    QUESTION → HYPOTHESIS（question_id）→ EVIDENCE（向き）→ 判定
             → changes / decision_impact（どの判断が動くか）

### 何をもって「効いた問い」とするか

**答えが出たこと**ではありません。答えが出ても判断が動かない問いはあります
（「区画整理で計画的に形成された市街地か」——正しくても診療コンセプトも
設備も診療時間も変わりません）。

**判断が動いたこと**を見ます。答えた仮説が `changes` を持ち、その仮説が
根拠つきで支持または反証されたか。

### 新しい保存はしません

材料はすでに `analysis_steps.output_json` にあります。表を足すと、そこへ
書き込む経路と、古いジョブの埋め戻しと、ずれたときの直し方が要ります。
読むだけなら、いつでも数え直せます。
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

import psycopg

from kaigyou_intel.coverage import PRIMARY_SOURCES

#: 判定が付いたとみなすもの。**UNCERTAIN は入りません**——調べたが
#: どちらとも言えなかった、は答えが出ていないということです。
SETTLED = frozenset({"SUPPORTED", "PARTIALLY_SUPPORTED", "CONTRADICTED"})


def outcomes(step1: Mapping[str, Any] | None, step2: Mapping[str, Any] | None,
             sources: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    """問い 1 件ごとに、その後どうなったかを 1 行にする。

    ジョブ 1 件ぶん。LLM は呼びません。
    """
    questions = list((step1 or {}).get("questions") or [])
    if not questions:
        return []
    hypotheses = list((step2 or {}).get("hypotheses") or [])
    facts = {str(f.get("id")): f for f in ((step2 or {}).get("external_facts") or [])}
    source_type = {str(s.get("url")): str(s.get("source_type") or "")
                   for s in sources}
    open_ids = {str(q.get("question_id"))
                for q in ((step2 or {}).get("open_questions") or [])}

    by_question: dict[str, list[Mapping[str, Any]]] = {}
    for h in hypotheses:
        if h.get("question_id"):
            by_question.setdefault(str(h["question_id"]), []).append(h)

    rows = []
    for question in questions:
        qid = str(question.get("id") or "")
        answers = by_question.get(qid, [])
        settled = [h for h in answers if str(h.get("status")) in SETTLED]
        rows.append({
            "question_id": qid,
            "pattern_id": str(question.get("pattern_id") or ""),
            "question": str(question.get("question") or ""),
            "hypotheses": len(answers),
            # 判定が付いたか。UNCERTAIN しか無い問いは「答えが出ていない」。
            "settled": bool(settled),
            # **開業前に現地で確かめる項目になったか。** これは失敗では
            # ありません。検索では決着しない問いだった、というだけです。
            "left_to_the_field": qid in open_ids,
            # 判断が動いたか。答えが出ても動かない問いはあります。
            "levers": sorted({lever for h in settled
                              for lever in (h.get("changes") or [])}),
            # 一次資料に当たったか。企業ブログ 3 件で「支持された」問いと、
            # 官公庁の告示で支持された問いを、同じ 1 件として数えません。
            "primary_evidence": _has_primary(settled, facts, source_type),
            # 2 周目を要したか。1 周で出た問いと、角度を変えて初めて出た問いは
            # 別物です。**後者は、次にどう問えばよいかの手がかりです。**
            "needed_a_second_round": any(int(h.get("round") or 1) > 1
                                         for h in settled),
            "verdicts": sorted({str(h.get("status")) for h in settled}),
        })
    return rows


def _has_primary(hypotheses: Iterable[Mapping[str, Any]],
                 facts: Mapping[str, Mapping[str, Any]],
                 source_type: Mapping[str, str]) -> bool:
    for h in hypotheses:
        for link in (h.get("evidence_links") or []):
            fact = facts.get(str(link.get("fact_id"))) or {}
            if source_type.get(str(fact.get("source_url"))) in PRIMARY_SOURCES:
                return True
    return False


def across_jobs(conn: psycopg.Connection, limit: int = 200) -> dict[str, Any]:
    """保存済みのジョブを横断して数える。

    プロンプトを直したときに、**問いの当たり方が変わったかどうか**を見る
    ためのものです。1 件だけ見ても分かりません。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT j.id, j.location_name, j.created_at,
                   s1.output_json AS step1, s2.output_json AS step2,
                   s1.prompt_version AS step1_version
            FROM analysis_jobs j
            JOIN analysis_steps s1
              ON s1.job_id = j.id AND s1.step_number = 1 AND s1.status = 'completed'
            LEFT JOIN analysis_steps s2
              ON s2.job_id = j.id AND s2.step_number = 2 AND s2.status = 'completed'
            ORDER BY j.created_at DESC LIMIT %s
            """, (limit,))
        jobs = [dict(r) for r in cur.fetchall()]
        if not jobs:
            return {"jobs": 0, "questions": 0, "rows": []}
        cur.execute(
            "SELECT job_id, url, source_type FROM analysis_sources "
            "WHERE job_id = ANY(%s)", ([j["id"] for j in jobs],))
        by_job: dict[str, list[dict[str, Any]]] = {}
        for row in cur.fetchall():
            by_job.setdefault(str(row["job_id"]), []).append(dict(row))

    rows: list[dict[str, Any]] = []
    counted = 0
    for job in jobs:
        found = outcomes(job["step1"], job["step2"], by_job.get(str(job["id"]), []))
        if not found:
            continue
        counted += 1
        for row in found:
            rows.append({**row, "job_id": str(job["id"]),
                         "location_name": job["location_name"],
                         "prompt_version": job["step1_version"]})

    settled = [r for r in rows if r["settled"]]
    moved = [r for r in settled if r["levers"]]
    return {
        # 問いを記録していない古いジョブは数に入れません。母数に混ぜると、
        # 「答えが出た割合」が版の違いで下がったように見えます。
        "jobs": counted,
        "questions": len(rows),
        "settled": len(settled),
        "left_to_the_field": len([r for r in rows if r["left_to_the_field"]]),
        # **ここがこの表の要です。**答えが出ることと、判断が動くことは違います。
        "moved_a_decision": len(moved),
        "primary_evidence": len([r for r in settled if r["primary_evidence"]]),
        "needed_a_second_round": len([r for r in settled
                                      if r["needed_a_second_round"]]),
        "levers": Counter(l for r in moved for l in r["levers"]).most_common(),
        "by_prompt_version": _by_version(rows),
        "rows": rows,
    }


def _by_version(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """プロンプト版ごとの当たり方。**版を跨いで平均しません。**

    問いを出すプロンプトを直した前後で混ぜると、直した効果が薄まって見えます。
    """
    versions: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        versions.setdefault(str(row.get("prompt_version") or "?"), []).append(row)
    out = []
    for version, group in sorted(versions.items()):
        settled = [r for r in group if r["settled"]]
        out.append({
            "prompt_version": version,
            "questions": len(group),
            "settled": len(settled),
            "moved_a_decision": len([r for r in settled if r["levers"]]),
        })
    return out
