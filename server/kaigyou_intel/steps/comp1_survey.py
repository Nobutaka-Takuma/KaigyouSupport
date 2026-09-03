"""競合分析 STEP1：競合ごとに Web で調べ、STP と 4P に構造化する。

開発指示書 §1・§2・§3。**周辺一般の調査に使っていた検索リソースを、ここに
振り替えます。**

医院ごとに呼び出しを分けます。1 本にまとめると、サーバ側の検索ループが 1 本の
中で直列に回り、増えていく文脈を毎回読み直します（STEP2 で実測済み：4 検索で
入力 794,572 トークン・5分36秒）。**調べる中身は医院ごとに独立しているので、
分けても答えは変わりません。変わるのは待ち時間です。**

構造化のスキーマも医院 1 件ぶんです。全医院を 1 つの出力に入れると文法が
大きくなりすぎて API が 400 を返します（実測済み）。
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from kaigyou_core import config as cfg
from kaigyou_core.analysis import DEFAULT_CATEGORY
from kaigyou_intel import client as llm
from kaigyou_intel.schemas import Competitor, CompetitorSurvey

STEP_NUMBER = 1


class StepFailed(RuntimeError):
    """このステップが結果を出せなかった。原因は message に。"""


def build_input(dataset: Mapping[str, Any],
                category: str = DEFAULT_CATEGORY) -> dict[str, Any]:
    """調べる相手の一覧。**GIS が確定した名前・住所・距離だけ。**

    近い順に切ります。1km 圏に 30 院ある商圏で 30 院ぶん検索するのは、費用も
    時間も見合いません。**切った件数は残します**——黙って落とすと、読み手には
    「その地域には 12 院しかない」に見えます。
    """
    conf = cfg.competitors_config(category)
    survey = conf.get("survey") or {}
    limit = int(survey.get("max_competitors", 12))
    inside = ((dataset.get("competition") or {}).get("clinics_in_radius") or {})
    items = [c for c in (inside.get("items") or []) if c.get("name")]
    items.sort(key=lambda c: c.get("distance_m") if c.get("distance_m") is not None
               else float("inf"))
    return {
        "location": dataset.get("location") or {},
        "label": conf.get("label") or "競合",
        "competitors": [
            {"name": c.get("name"), "address": c.get("address"),
             "distance_m": c.get("distance_m"), "homepage": c.get("homepage"),
             # 届出の標榜科目。**Web で確かめる出発点**であって、答えでは
             # ありません（インプラントは標榜科目にありません）。
             "declared_types": c.get("clinic_types") or []}
            for c in items[:limit]
        ],
        "not_surveyed": max(0, len(items) - limit),
        "total_in_radius": len(items),
        "radius_m": int(survey.get("radius_m", 1000)),
        "vocabulary": {
            "products": conf.get("products") or [],
            "segments": conf.get("segments") or [],
            "place_attributes": conf.get("place_attributes") or [],
            "positioning_map": conf.get("positioning_map") or {},
        },
    }


@dataclass
class _Surveyed:
    """1 医院ぶんの結果。**失敗しても例外を上げません。**"""

    name: str
    competitor: Competitor | None = None
    usage: llm.Usage = field(default_factory=llm.Usage)
    sources: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def run(payload: Mapping[str, Any], category: str = DEFAULT_CATEGORY,
        ) -> tuple[dict[str, Any], llm.Usage, list[dict[str, Any]]]:
    """競合を 1 件ずつ調べる。

    **1 件落ちても全部を捨てません。** 12 件のうち 1 件がしくじっただけで、
    通った 11 件ぶんの検索と時間を捨てるのは割に合いません。落ちたことは
    出力に残ります。
    """
    conf = cfg.competitors_config(category)
    survey = conf.get("survey") or {}
    targets = list(payload.get("competitors") or [])
    if not targets:
        raise StepFailed(
            f"半径{payload.get('radius_m')}m に調べる{payload.get('label') or '競合'}が"
            "ありません。商圏を広げるか、施設データの取り込みを確認してください。")

    settings = llm.step_settings(STEP_NUMBER)
    system = system_prompt(payload, category)
    per_competitor = max(1, int(survey.get("searches_per_competitor", 2)))
    results = _survey_all(targets, payload, system, per_competitor,
                          max(1, int(survey.get("parallel", 4))), category)

    found = [r for r in results if r.competitor is not None]
    if not found:
        raise StepFailed(
            "どの競合についても情報を構造化できませんでした（"
            + "; ".join(f"{r.name}: {r.error}" for r in results if r.error)[:600]
            + "）。")

    return (
        {
            "competitors": [_with_distance(r.competitor, targets)
                            for r in found],
            "surveyed": len(found),
            "requested": len(targets),
            # 調べられなかった医院。**黙って消しません。**
            "failed": [{"name": r.name, "why": r.error}
                       for r in results if r.competitor is None],
            "not_surveyed": payload.get("not_surveyed", 0),
            "total_in_radius": payload.get("total_in_radius"),
            "radius_m": payload.get("radius_m"),
        },
        _total(results),
        [s for r in results for s in r.sources],
    )


def _survey_all(targets: Sequence[Mapping[str, Any]], payload: Mapping[str, Any],
                system: str, per_competitor: int, parallel: int,
                category: str) -> list[_Surveyed]:
    """医院ごとの調査を同時に走らせる。順序は入力どおりに揃えます。"""
    if parallel == 1 or len(targets) == 1:
        return [_survey_one(t, payload, system, per_competitor, category)
                for t in targets]
    with ThreadPoolExecutor(max_workers=min(parallel, len(targets))) as pool:
        futures = [pool.submit(_survey_one, t, payload, system, per_competitor,
                               category) for t in targets]
        return [f.result() for f in futures]


def _survey_one(target: Mapping[str, Any], payload: Mapping[str, Any],
                system: str, per_competitor: int, category: str) -> _Surveyed:
    """1 医院を調べて、構造化する。検索と構造化は別の呼び出しです。

    Web検索（サーバ側ツール）と構造化出力は同じ呼び出しでは併用しません。
    2 回目に検索を残すと、1 回目に無かった事実が増えて、出典の照合が意味を
    失います（STEP2 と同じ考え方）。
    """
    name = str(target.get("name") or "")
    asked = ("以下の 1 院について調べてください。**この 1 院だけです。**\n\n"
             "```json\n"
             + json.dumps({"location": payload.get("location"),
                           "competitor": target}, ensure_ascii=False, indent=1)
             + "\n```")
    try:
        found = llm.ask(step_number=STEP_NUMBER, system=system, user=asked,
                        max_uses=per_competitor)
    except Exception as exc:  # noqa: BLE001 - 1 件の失敗で全部を捨てない
        return _Surveyed(name, error=f"{type(exc).__name__}: {exc}")

    sources = [s for s in found.sources if s.get("url")]
    if not (found.text or "").strip():
        return _Surveyed(name, usage=found.usage, sources=_tagged(sources, name),
                         error="調査の本文が空でした")

    settings = llm.step_settings(STEP_NUMBER)
    catalogue = "\n".join(f"- {s['url']}  {s.get('title') or ''}"
                          for s in sources) or "（なし）"
    try:
        structured = llm.ask(
            step_number=STEP_NUMBER,
            system=cfg.prompt_text(settings["prompt_structure"], category)
            .replace("{vocabulary}", _vocabulary_block(payload)),
            effort=settings["effort_structure"],
            user=("## 調査結果\n\n" + found.text
                  + "\n\n## 今回の検索で取得した URL"
                    "（source_url はこの中から選ぶこと）\n\n" + catalogue
                  + f"\n\n## 医院名\n\n{name}"),
            schema=CompetitorSurvey, web_search=False)
    except Exception as exc:  # noqa: BLE001
        return _Surveyed(name, usage=found.usage, sources=_tagged(sources, name),
                         error=f"構造化に失敗: {type(exc).__name__}: {exc}")

    survey: CompetitorSurvey | None = structured.parsed
    if survey is None:
        return _Surveyed(name, usage=_add(found.usage, structured.usage),
                         sources=_tagged(sources, name),
                         error="構造化出力を受け取れませんでした")

    competitor = survey.competitor
    competitor.name = name          # モデルの書き換えを許さない
    _drop_unverifiable(competitor, {s["url"] for s in sources})
    return _Surveyed(name, competitor=competitor,
                     usage=_add(found.usage, structured.usage),
                     sources=_tagged(sources, name))


def _drop_unverifiable(competitor: Competitor, urls: set[str]) -> None:
    """検索結果に無い URL を挙げた項目を落とす。**黙って落としません。**

    モデルは実在しそうな URL を書けます。実在するかは検索結果で決まります
    （STEP2 と同じ検算）。
    """
    from kaigyou_intel.schemas import normalize_url

    known = {normalize_url(u) for u in urls if u}
    kept = [f for f in competitor.place if normalize_url(f.source_url) in known]
    dropped = len(competitor.place) - len(kept)
    competitor.place = kept
    competitor.sources = [u for u in competitor.sources
                          if normalize_url(u) in known]
    if dropped:
        competitor.not_confirmed = list(competitor.not_confirmed) + [
            f"{dropped} 件は、引用された URL が今回の検索結果に含まれていな"
            "かったため除外しました（内容の当否ではありません）。"]


def system_prompt(payload: Mapping[str, Any],
                  category: str = DEFAULT_CATEGORY) -> str:
    """実際に送られる system プロンプト。

    `--dry-run` と実行で**同じ関数を通します。** 別々に組み立てていた頃は、
    置き換わっていない `{vocabulary}` を含む、実際には送られない文書を
    「送られる内容」として見せていました。課金の前に確かめる道具が、
    送られないものを見せていては確認になりません。
    """
    settings = llm.step_settings(STEP_NUMBER, kind="competitors")
    return (cfg.prompt_text(settings["prompt"], category)
            .replace("{vocabulary}", _vocabulary_block(payload)))


def _vocabulary_block(payload: Mapping[str, Any]) -> str:
    """業態ごとの語彙をプロンプトに差し込む。**コードに埋めません。**"""
    vocabulary = payload.get("vocabulary") or {}
    return json.dumps(vocabulary, ensure_ascii=False, indent=1)


def _with_distance(competitor: Competitor,
                   targets: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """距離は GIS の値を付け直す。**モデルに書かせません。**"""
    by_name = {t.get("name"): t for t in targets}
    out = competitor.model_dump()
    out["distance_m"] = (by_name.get(competitor.name) or {}).get("distance_m")
    return out


def _tagged(sources: Sequence[Mapping[str, Any]], name: str) -> list[dict[str, Any]]:
    return [{**s, "pattern_id": name} for s in sources]


def _add(a: llm.Usage, b: llm.Usage) -> llm.Usage:
    return llm.Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        web_searches=a.web_searches + b.web_searches,
        cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
        cache_write_tokens=a.cache_write_tokens + b.cache_write_tokens)


def _total(results: Sequence[_Surveyed]) -> llm.Usage:
    total = llm.Usage()
    for result in results:
        total = _add(total, result.usage)
    return total
