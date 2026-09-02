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
            # **検索に回したかどうか。** low は最初から現地確認へ回すので、
            # 「答えが出なかった」に数えると、判断が失敗に見えます。
            "researchability": str(question.get("researchability") or "medium"),
            # 疑っていた前提。**これがある問いと無い問いは、別の生き物です。**
            "questioned_an_assumption": bool(question.get("assumption_id")),
            "trigger_type": str((question.get("trigger") or {}).get("type") or ""),
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
    # **検索に回したのに空振りした問い。** 最初から現地確認へ回した問い
    # （researchability: low）は数えません。あれは判断であって、空振りでは
    # ありません。
    searched = [r for r in rows if r["researchability"] != "low"]
    wasted = [r for r in searched if not r["settled"]]
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
        # 疑った前提から出た問いと、そうでない問い。**良い問いの定義そのもの
        # を数えます**（既に持っているデータから見落とされている前提を発見し、
        # それを外部情報で検証できる問い）。
        "questioned_an_assumption": len(
            [r for r in rows if r["questioned_an_assumption"]]),
        "moved_and_questioned": len(
            [r for r in moved if r["questioned_an_assumption"]]),
        "by_trigger": _by_trigger(rows),
        # 検索に回して空振りした問い。**台帳に足す候補です。**
        "searched": len(searched),
        "wasted_searches": len(wasted),
        "dead_end_candidates": _dead_end_candidates(wasted),
        "rows": rows,
    }


def _by_trigger(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """どの生まれ方の問いが、実際に判断を動かしたか。

    **これが「問いのセンス」を測れる唯一の形です。** 手元のデータと外部の
    事実がぶつかって出た問いと、思いついて出た問いのどちらが効いたのかは、
    やってみないと分かりません。
    """
    kinds: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        kinds.setdefault(row["trigger_type"] or "（記録なし）", []).append(row)
    out = []
    for kind, group in sorted(kinds.items(), key=lambda kv: -len(kv[1])):
        settled = [r for r in group if r["settled"]]
        out.append({
            "trigger_type": kind,
            "questions": len(group),
            "settled": len(settled),
            "moved_a_decision": len([r for r in settled if r["levers"]]),
        })
    return out


def _dead_end_candidates(wasted: Sequence[Mapping[str, Any]],
                         at_least: int = 2) -> list[dict[str, Any]]:
    """検索に回したのに、**繰り返し**空振りしている問い。

    `config/dead_ends.yaml` に足す候補です。**足すかどうかは人が決めます。**
    機械が勝手に塞ぐと、あとから公表され始めたものに永久に気づけません。

    まとめ方は素朴です——問いの文から共通する語を拾うのではなく、**同じ語で
    始まる問いを数える**だけ。凝った寄せ方をすると、なぜ同じ扱いになったのかが
    人に説明できなくなり、塞ぐ判断の根拠にできません。
    """
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in wasted:
        groups.setdefault(_gist(row["question"]), []).append(row)
    return [{"gist": gist, "times": len(rows),
             "examples": [r["question"] for r in rows[:3]]}
            for gist, rows in sorted(groups.items(), key=lambda kv: -len(kv[1]))
            if len(rows) >= at_least]


def _gist(question: str) -> str:
    """問いの見出し。**同じことを訊いているかの、粗い当たり**にしか使いません。"""
    return "".join(question.split())[:24] or "（空）"


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
