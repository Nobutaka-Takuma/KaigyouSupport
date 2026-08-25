"""各ステップの出力の形。

構造化出力（``output_config.format``）でモデルに強制し、さらに受け取ったあとに
検算します。スキーマが保証するのは「形」であって「中身」ではないためです。
FACT が実在の measure を指しているか、PATTERN の evidence が実在の FACT を
指しているかは、こちらで確かめます（要件 §25）。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Importance = Literal["high", "medium", "low"]
Confidence = Literal["high", "medium", "low"]


class Fact(BaseModel):
    """基礎データに実在する事実 1 件。"""

    id: str = Field(description="F001 のような通し番号")
    statement: str = Field(description="事実を 1 文で。値と単位を含める")
    measure_key: str = Field(
        description="根拠にした measures[].key。入力に無いキーは書かない")
    value: float | None = Field(
        default=None, description="引用した値。丸めずそのまま")
    unit: str | None = None
    #: 比較を引用したときだけ。position_label をそのまま入れる。
    position_label: str | None = Field(
        default=None, description="「上位6%」など。自分で作らず入力から写す")
    benchmark_type: str | None = Field(
        default=None, description="どの母集団と比べた値か")


class Pattern(BaseModel):
    """複数の FACT を組み合わせて見えた地域の特徴。"""

    id: str = Field(description="P001 のような通し番号")
    title: str = Field(description="特徴を 1 文で")
    evidence: list[str] = Field(
        description="根拠にした FACT の id。2 つ以上", min_length=2)
    evidence_summary: str = Field(
        description="STEP2 に渡す短い説明。基礎データを見なくても意味が通ること")
    importance: Importance
    research_questions: list[str] = Field(
        description="この特徴の背景を外部情報で調べるための質問", min_length=1)


class Step1Output(BaseModel):
    """STEP1（商圏特徴抽出）の出力。

    benchmarks を含みません。パーセンタイル・順位・significance は
    /api/dataset が算出済みで、FACT はそれを ``measure_key`` で参照します。
    LLM に作らせると、入力に無い数字が「それらしく」出てきます（要件 §3 原則2）。
    """

    facts: list[Fact] = Field(min_length=1)
    patterns: list[Pattern] = Field(min_length=1)
    #: 分析できなかったこと。空でも構いませんが、書けるなら書かせます。
    not_determinable: list[str] = Field(
        default_factory=list,
        description="基礎データからは判断できなかった論点")


class TraceProblem(BaseModel):
    """参照が解決しなかった箇所。"""

    where: str
    problem: str


def verify_step1(output: Step1Output, allowed_measure_keys: set[str]) -> list[TraceProblem]:
    """FACT が実在の指標を、PATTERN が実在の FACT を指しているか。

    スキーマは形しか保証しません。存在しない measure_key を書いた FACT も、
    存在しない FACT を指す PATTERN も、スキーマ上は正しい JSON です。
    ここで落とさないと、レポートの末尾まで残って §25 の追跡が切れます。
    """
    problems: list[TraceProblem] = []
    fact_ids = {f.id for f in output.facts}

    if len(fact_ids) != len(output.facts):
        problems.append(TraceProblem(where="facts", problem="FACT の id が重複しています"))

    for fact in output.facts:
        if fact.measure_key not in allowed_measure_keys:
            problems.append(TraceProblem(
                where=f"facts[{fact.id}]",
                problem=f"入力に存在しない measure_key: {fact.measure_key!r}"))

    seen_pattern_ids: set[str] = set()
    for pattern in output.patterns:
        if pattern.id in seen_pattern_ids:
            problems.append(TraceProblem(
                where=f"patterns[{pattern.id}]", problem="PATTERN の id が重複しています"))
        seen_pattern_ids.add(pattern.id)
        for ref in pattern.evidence:
            if ref not in fact_ids:
                problems.append(TraceProblem(
                    where=f"patterns[{pattern.id}].evidence",
                    problem=f"存在しない FACT を参照しています: {ref!r}"))
    return problems


# ------------------------------------------------------------------ STEP2
HypothesisStatus = Literal["SUPPORTED", "PARTIALLY_SUPPORTED", "UNSUPPORTED"]


class ExternalFact(BaseModel):
    """外部情報から確認できた事実 1 件（要件 §10）。

    ``source_type`` と ``retrieved_at`` はここにありません。どちらも URL と
    取得時刻から機械的に決まるので、サーバ側で付けます。LLM に書かせると、
    出典の区分がモデルの気分で変わります。
    """

    id: str = Field(description="C001 のような通し番号")
    pattern_id: str = Field(description="どの PATTERN を調べていて出てきたか")
    statement: str = Field(description="外部情報で確認できた事実を 1 文で")
    source_url: str = Field(
        description="検索結果に実在した URL。記憶から書かない")
    source_title: str = Field(description="そのページの表題")
    confidence: Confidence = Field(
        description="high=一次資料に明記 / medium=公的資料からの読み取り / low=それ以外")


class Hypothesis(BaseModel):
    """PATTERN の背景についての仮説と、その判定（要件 §11）。"""

    id: str = Field(description="H001 のような通し番号")
    pattern_id: str
    statement: str = Field(description="この PATTERN がなぜ存在するのかの説明")
    status: HypothesisStatus
    evidence: list[str] = Field(
        description="判定の根拠にした EXTERNAL FACT の id。1 つ以上", min_length=1)
    reasoning: str = Field(description="その外部事実からなぜその判定になるのか")
    confidence: Confidence


class Step2Output(BaseModel):
    """STEP2（外部コンテクスト調査）の出力。

    否定された仮説も残します（要件 §11）。「調べたが違った」は、調べていない
    のとは別のことで、STEP3 以降が同じ筋を追い直さずに済みます。
    """

    external_facts: list[ExternalFact] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    #: 調べたが答えが見つからなかった質問。空にしないでほしい欄です。
    unanswered: list[str] = Field(
        default_factory=list,
        description="調べたが外部情報からは確認できなかった論点")


def normalize_url(url: str) -> str:
    """同じページを指す URL を同じ文字列にする。

    検索結果の URL と、モデルが書き写した URL を突き合わせるためだけの正規化です。
    末尾のスラッシュと大文字小文字と scheme の違いで「捏造」と判定されると、
    本物の出典まで落ちます。
    """
    trimmed = url.strip().split("#", 1)[0].rstrip("/")
    for prefix in ("https://", "http://"):
        if trimmed.lower().startswith(prefix):
            trimmed = trimmed[len(prefix):]
            break
    if trimmed.lower().startswith("www."):
        trimmed = trimmed[4:]
    host, _, rest = trimmed.partition("/")
    return host.lower() + ("/" + rest if rest else "")


def verify_step2(output: Step2Output, allowed_pattern_ids: set[str],
                 retrieved_urls: set[str]) -> list[TraceProblem]:
    """参照と出典が実在するか（要件 §25・§29）。

    いちばん重要なのは URL の検算です。モデルは「それらしい」URL を書けます
    （``https://www.city.chuo.lg.jp/kurashi/toukei/jinkou.html`` は、実在しなくても
    実在しそうに見えます）。検索で**実際に返ってきた** URL の集合に無いものは、
    出典ではありません。
    """
    problems: list[TraceProblem] = []
    known = {normalize_url(u) for u in retrieved_urls if u}

    fact_ids: set[str] = set()
    for fact in output.external_facts:
        if fact.id in fact_ids:
            problems.append(TraceProblem(
                where=f"external_facts[{fact.id}]",
                problem="EXTERNAL FACT の id が重複しています"))
        fact_ids.add(fact.id)
        if fact.pattern_id not in allowed_pattern_ids:
            problems.append(TraceProblem(
                where=f"external_facts[{fact.id}]",
                problem=f"存在しない PATTERN を参照しています: {fact.pattern_id!r}"))
        if normalize_url(fact.source_url) not in known:
            problems.append(TraceProblem(
                where=f"external_facts[{fact.id}]",
                problem=f"検索結果に無い URL です: {fact.source_url!r}"))

    seen: set[str] = set()
    for hypothesis in output.hypotheses:
        if hypothesis.id in seen:
            problems.append(TraceProblem(
                where=f"hypotheses[{hypothesis.id}]",
                problem="HYPOTHESIS の id が重複しています"))
        seen.add(hypothesis.id)
        if hypothesis.pattern_id not in allowed_pattern_ids:
            problems.append(TraceProblem(
                where=f"hypotheses[{hypothesis.id}]",
                problem=f"存在しない PATTERN を参照しています: {hypothesis.pattern_id!r}"))
        for ref in hypothesis.evidence:
            if ref not in fact_ids:
                problems.append(TraceProblem(
                    where=f"hypotheses[{hypothesis.id}].evidence",
                    problem=f"存在しない EXTERNAL FACT を参照しています: {ref!r}"))
    return problems
