"""レポートを人が読む形にして保存する（要件 §18・§29）。

Markdown はここで作ります。LLM に書かせません。免責・出典・データ時点は
**必ず載る**ものなので、書き忘れの起きうる場所に置かないためです。

出典の一覧も同じ理由でここで組み立てます。STEP2 が analysis_sources に
残した行から作るので、レポート本文に URL を書き写す手間も、写し間違いも
ありません。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

import psycopg
from psycopg.types.json import Json

from kaigyou_intel.schemas import Step4Output, TraceProblem

#: 出典の並び順。要件 §9 の優先順位そのまま。
_SOURCE_ORDER = ("government", "statistics", "prefecture", "municipality",
                 "public_body", "transit", "academic", "company", "news", "other")
_SOURCE_LABEL = {
    "government": "国・省庁", "statistics": "政府統計", "prefecture": "都道府県",
    "municipality": "市区町村", "public_body": "公的機関", "transit": "交通事業者",
    "academic": "大学・研究機関", "company": "企業", "news": "ニュース",
    "other": "その他",
}


def to_markdown(output: Mapping[str, Any], dataset: Mapping[str, Any],
                sources: Sequence[Mapping[str, Any]] = ()) -> str:
    """レポート 1 本ぶんの Markdown。"""
    location = dataset.get("location") or {}
    query = dataset.get("query") or {}
    place = " ".join(x for x in (location.get("prefecture_name"),
                                 location.get("municipality_name")) if x)
    name = location.get("name") or place or \
        f"{location.get('lat')},{location.get('lng')}"
    lines: list[str] = [f"# 商圏分析レポート：{name}", ""]

    where = f"{location.get('lat')}, {location.get('lng')}"
    if query.get("radius_m"):
        lines += [f"地点 {where} / 半径 {query['radius_m']:,}m"
                  f" / プロファイル {query.get('active_profile', '-')}", ""]

    lines += ["## 結論", "", output.get("executive_summary", ""), ""]
    lines += _decision_block(output.get("decision") or {})

    for section in sorted(output.get("sections") or [],
                          key=lambda s: s.get("number", 0)):
        lines += [f"## {section.get('number')}. {section.get('title')}", ""]
        for block in section.get("blocks") or []:
            refs = block.get("evidence") or []
            tail = f"  〔{', '.join(refs)}〕" if refs else ""
            lines.append(f"**[{block.get('tag')}]** {block.get('text')}{tail}")
            lines.append("")

    if output.get("actions"):
        lines += ["## 次に取るべき行動", ""]
        for action in output["actions"]:
            refs = action.get("evidence") or []
            tail = f"  〔{', '.join(refs)}〕" if refs else ""
            lines.append(f"- {action.get('statement')}{tail}")
        lines.append("")

    lines += _sources_block(sources)
    lines += _provenance_block(dataset)
    return "\n".join(lines).rstrip() + "\n"


def _decision_block(decision: Mapping[str, Any]) -> list[str]:
    """要件 §17 の答え。レポートの先頭に、欄のまま置きます。

    散文に溶かすと「誰と競争しないか」が抜けても気づけません。表なら空欄が
    見えます。
    """
    fields = (("主要患者", "primary_patients"),
              ("主要に置かない層", "secondary_patients"),
              ("競争しない領域", "avoid_competing_on"),
              ("患者獲得エリア", "acquisition_area"),
              ("来院理由", "reason_to_visit"),
              ("医院モデル", "clinic_model"))
    lines = ["### 開業方針", "", "| | |", "|---|---|"]
    for label, key in fields:
        item = decision.get(key) or {}
        refs = item.get("evidence") or []
        tail = f"  〔{', '.join(refs)}〕" if refs else ""
        text = str(item.get("statement", "")).replace("|", "\\|")
        lines.append(f"| **{label}** | {text}{tail} |")
    lines.append("")
    for label, key in (("開業上のメリット", "advantages"), ("リスク", "risks")):
        entries = decision.get(key) or []
        if not entries:
            continue
        lines += [f"### {label}", ""]
        for item in entries:
            refs = item.get("evidence") or []
            tail = f"  〔{', '.join(refs)}〕" if refs else ""
            lines.append(f"- {item.get('statement')}{tail}")
        lines.append("")
    if decision.get("confidence"):
        lines += [f"判断の確度: {decision['confidence']}", ""]
    return lines


def _sources_block(sources: Sequence[Mapping[str, Any]]) -> list[str]:
    if not sources:
        return []
    lines = ["## 出典（外部情報）", ""]
    ordered = sorted(sources, key=lambda s: (
        _SOURCE_ORDER.index(s.get("source_type") or "other")
        if (s.get("source_type") or "other") in _SOURCE_ORDER else len(_SOURCE_ORDER),
        s.get("url") or ""))
    for source in ordered:
        label = _SOURCE_LABEL.get(source.get("source_type") or "other", "その他")
        title = source.get("title") or source.get("url")
        retrieved = source.get("retrieved_at")
        when = f"（取得 {retrieved:%Y-%m-%d}）" if hasattr(retrieved, "year") else ""
        lines.append(f"- [{label}] {title}{when}  \n  {source.get('url')}")
    lines.append("")
    return lines


def _provenance_block(dataset: Mapping[str, Any]) -> list[str]:
    """データ時点・注意事項・免責。省略できません（プロジェクトの前提）。

    出典と年次は measures の各指標に付いています。レポートに書き写させると
    写し間違いが起きるので、こちらで集めて並べます。

    caveats も落としません。「標榜診療科目は届出値であって診療内容ではない」
    のような但し書きは、レポートの読み方そのものを変えます。
    """
    lines = ["## データの出典と時点", ""]
    seen: list[tuple[str, str]] = []
    for measure in (dataset.get("measures") or {}).get("items") or []:
        source = measure.get("source")
        if not source:
            continue
        pair = (str(source), str(measure.get("data_year") or "-"))
        if pair not in seen:
            seen.append(pair)
    for source, year in seen:
        lines.append(f"- {source}（{year}）")

    quality = dataset.get("data_quality") or {}
    for gap in quality.get("unavailable_datasets") or []:
        label = gap.get("label") or gap.get("key") if isinstance(gap, Mapping) else gap
        lines.append(f"- 未取得: {label}")
    lines.append("")

    notes = list(quality.get("notes") or []) + list(quality.get("benchmark_notes") or [])
    if notes:
        lines += ["## データについての注記", ""]
        lines += [f"- {note}" for note in notes] + [""]

    if quality.get("caveats"):
        lines += ["## 読むときの注意", ""]
        lines += [f"- {caveat}" for caveat in quality["caveats"]] + [""]

    lines += ["## 免責", ""]
    for key in ("disclaimer", "score_disclaimer"):
        text = dataset.get(key)
        if text:
            lines += [str(text), ""]
    return lines


def save(conn: psycopg.Connection, job_id: str, output: Mapping[str, Any],
         dataset: Mapping[str, Any], problems: Sequence[TraceProblem] = ()) -> str:
    """レポートを保存する。§25 の検算結果も一緒に。

    問題が残っていても保存するのは、「いつから壊れていたか」を後から追うため
    です。ただし ``trace_ok`` は false になり、そのことは読む人にも見えます。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT url, title, source_type, retrieved_at FROM analysis_sources
            WHERE job_id = %s ORDER BY created_at
            """, (job_id,))
        sources = [dict(r) for r in cur.fetchall()]
        # 地点名は Job のほうにあります（「銀座4丁目」のように人が付けた名前）。
        # 座標より、その名前で呼ばれたほうが読む人には分かります。
        cur.execute("SELECT location_name FROM analysis_jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()

    named = dict(dataset)
    if row and row["location_name"]:
        named["location"] = {**(dataset.get("location") or {}),
                             "name": row["location_name"]}

    with conn.cursor() as cur:
        markdown = to_markdown(output, named, sources)
        cur.execute(
            """
            INSERT INTO analysis_reports (job_id, report_json, report_markdown,
                                          trace_ok, trace_problems)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (job_id) DO UPDATE SET
                report_json = EXCLUDED.report_json,
                report_markdown = EXCLUDED.report_markdown,
                trace_ok = EXCLUDED.trace_ok,
                trace_problems = EXCLUDED.trace_problems,
                created_at = now()
            RETURNING id
            """,
            (job_id, Json(dict(output)), markdown, not problems,
             Json([p.model_dump() for p in problems]) if problems else None))
        report_id = str(cur.fetchone()["id"])
    conn.commit()
    return report_id


def markdown_for(conn: psycopg.Connection, job_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT report_markdown FROM analysis_reports WHERE job_id = %s",
                    (job_id,))
        row = cur.fetchone()
    return row["report_markdown"] if row else None
