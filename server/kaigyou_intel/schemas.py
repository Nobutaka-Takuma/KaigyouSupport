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


# ------------------------------------------------------------------ STEP3
class PatientSegment(BaseModel):
    """この場所に存在すると推定される患者層 1 つ（要件 §13）。

    ``evidence`` は STEP1 の FACT（F###）と STEP2 の EXTERNAL FACT（C###）の id。
    根拠を挙げられないセグメントは出しません。要件 §13 の「データに根拠がない
    セグメントを無理に生成しない」は、ここを空にできない形で守ります。
    """

    id: str = Field(description="S001 のような通し番号")
    name: str = Field(description="患者層の名前。例：周辺勤務者、子育て世帯")
    evidence: list[str] = Field(
        description="根拠にした FACT / EXTERNAL FACT の id。1 つ以上", min_length=1)
    mechanism_id: str = Field(
        description="この患者層を説明する DEMAND MECHANISM の id")
    importance: Importance
    confidence: Confidence
    note: str | None = Field(
        default=None, description="限定条件があれば。無ければ空でよい")


class DemandMechanism(BaseModel):
    """需要が形成される筋道（要件 §14）。

    ``chain`` は「地域構造 → 生活行動 → 需要形成 → 歯科需要」の各段を 1 要素
    ずつ。3 段以上を必須にしているのは、「駅前だから患者が来る」のような
    一足飛びの説明を書けなくするためです。属性の言い換えは筋道ではありません。
    """

    id: str = Field(description="M001 のような通し番号")
    title: str = Field(description="この筋道を 1 文で")
    chain: list[str] = Field(
        description="地域構造 → 生活行動 → 需要形成 → 歯科需要。各段を1要素ずつ",
        min_length=3)
    evidence: list[str] = Field(
        description="根拠にした FACT / EXTERNAL FACT の id。2 つ以上", min_length=2)
    confidence: Confidence


class DemandInsight(BaseModel):
    """複数の筋道・患者層を横断して見えること。"""

    id: str = Field(description="I001 のような通し番号")
    statement: str
    evidence: list[str] = Field(
        description="根拠にした FACT / EXTERNAL FACT / SEGMENT / MECHANISM の id。2 つ以上",
        min_length=2)


class Step3Output(BaseModel):
    """STEP3（需要形成・患者分析）の出力（要件 §15）。"""

    patient_segments: list[PatientSegment] = Field(default_factory=list)
    demand_mechanisms: list[DemandMechanism] = Field(default_factory=list)
    insights: list[DemandInsight] = Field(default_factory=list)
    #: 根拠が足りず出さなかった患者層。要件 §13 を「出さなかった」側から残します。
    not_supported: list[str] = Field(
        default_factory=list,
        description="想定はできるが、データに根拠が無いので出さなかった患者層")


def verify_step3(output: Step3Output, fact_ids: set[str],
                 external_ids: set[str]) -> list[TraceProblem]:
    """患者層と筋道が、実在の事実を指しているか（要件 §25）。

    STEP3 は初めて手元のデータと外部事実の両方を持つ段です。参照先が 2 系統に
    増えるので、どちらでもない id（作った id、前段に無い id）が混ざりやすい。
    """
    problems: list[TraceProblem] = []
    known = fact_ids | external_ids

    def check(where: str, refs: list[str], extra: set[str] = frozenset()) -> None:
        for ref in refs:
            if ref not in known and ref not in extra:
                problems.append(TraceProblem(
                    where=where,
                    problem=f"存在しない根拠を参照しています: {ref!r}"))

    mechanism_ids: set[str] = set()
    for mechanism in output.demand_mechanisms:
        if mechanism.id in mechanism_ids:
            problems.append(TraceProblem(
                where=f"demand_mechanisms[{mechanism.id}]",
                problem="MECHANISM の id が重複しています"))
        mechanism_ids.add(mechanism.id)
        check(f"demand_mechanisms[{mechanism.id}].evidence", mechanism.evidence)
        if any(not step.strip() for step in mechanism.chain):
            problems.append(TraceProblem(
                where=f"demand_mechanisms[{mechanism.id}].chain",
                problem="空の段があります。筋道は各段を言葉で埋めてください"))

    segment_ids: set[str] = set()
    for segment in output.patient_segments:
        if segment.id in segment_ids:
            problems.append(TraceProblem(
                where=f"patient_segments[{segment.id}]",
                problem="SEGMENT の id が重複しています"))
        segment_ids.add(segment.id)
        check(f"patient_segments[{segment.id}].evidence", segment.evidence)
        if segment.mechanism_id not in mechanism_ids:
            problems.append(TraceProblem(
                where=f"patient_segments[{segment.id}]",
                problem=("需要形成メカニズムが解決しません: "
                         f"{segment.mechanism_id!r}")))

    for insight in output.insights:
        check(f"insights[{insight.id}].evidence", insight.evidence,
              segment_ids | mechanism_ids)
    return problems


# ------------------------------------------------------------------ STEP4
#: 要件 §18 のレポート構成。章立てはモデルに決めさせません。毎回違う章立てで
#: 出てくると、2 地点を並べて読めなくなります。
REPORT_SECTIONS = (
    "エグゼクティブサマリー", "商圏人口", "昼間人口", "交通アクセス",
    "競合歯科医院", "地価", "商圏の特徴", "開業上のメリット", "リスク", "総合評価",
)

#: 要件 §19 の分析順序。章の中でこの順を保ちます（全部入れる必要はありません）。
BLOCK_TAGS = ("FACT", "BENCHMARK", "PATTERN", "WHY", "INSIGHT", "IMPLICATION", "ACTION")

BlockTag = Literal["FACT", "BENCHMARK", "PATTERN", "WHY", "INSIGHT",
                   "IMPLICATION", "ACTION"]

#: 出してはいけない予測。開業成功確率・売上・患者数・家賃の予測は、この
#: システムの目的外です。プロンプトで禁じたうえで、出力でも落とします。
FORBIDDEN_PREDICTIONS = (
    "成功確率", "年商", "月商", "想定売上", "売上予測", "売上高", "患者数予測",
    "来院数予測", "収支予測", "損益予測", "投資回収",
)


class Evidenced(BaseModel):
    """根拠つきの 1 文。§25 の追跡はこの id を辿ります。"""

    statement: str
    evidence: list[str] = Field(
        description="根拠にした F### / P### / C### / H### / S### / M### / I### の id",
        min_length=1)


class BusinessDecision(BaseModel):
    """要件 §17 の最重要アウトプット。

    「誰を主要患者とし、誰とは競争せず、どの診療圏から何を理由に患者を引っ張り、
    どの医院モデルにするべきか」に答えます。「良い商圏」で終わらせないために、
    答えるべき項目を欄として置いてあります。埋められない欄は書けません。
    """

    primary_patients: Evidenced = Field(description="主要患者として設定する層")
    secondary_patients: Evidenced = Field(description="主要には置かない層と、その理由")
    avoid_competing_on: Evidenced = Field(description="競争しない領域")
    acquisition_area: Evidenced = Field(description="患者獲得エリア")
    reason_to_visit: Evidenced = Field(description="患者がこの医院を選ぶ理由")
    clinic_model: Evidenced = Field(description="医院モデル")
    advantages: list[Evidenced] = Field(description="経営上のメリット", min_length=1)
    risks: list[Evidenced] = Field(description="リスク", min_length=1)
    confidence: Confidence


class ReportBlock(BaseModel):
    tag: BlockTag
    text: str
    #: FACT / BENCHMARK / PATTERN には根拠を付けます。WHY 以降は前段の
    #: 結論を受けるので、空でも構いません。
    evidence: list[str] = Field(default_factory=list)


class ReportSection(BaseModel):
    number: int = Field(description="1〜10。要件 §18 の並び")
    title: str = Field(description="要件 §18 の章名をそのまま")
    blocks: list[ReportBlock] = Field(min_length=1)


class Step4Output(BaseModel):
    """STEP4（経営判断・レポート生成）の出力（要件 §16〜§21）。"""

    executive_summary: str = Field(description="3〜5 文。判断の結論から書く")
    decision: BusinessDecision
    sections: list[ReportSection] = Field(min_length=1)
    actions: list[Evidenced] = Field(
        description="次に取るべき具体的な行動", min_length=1)


def verify_step4(output: Step4Output, known_ids: set[str]) -> list[TraceProblem]:
    """章立て・分析順序・根拠・禁止事項（要件 §18 / §19 / §25）。"""
    problems: list[TraceProblem] = []

    def check(where: str, refs: list[str]) -> None:
        for ref in refs:
            if ref not in known_ids:
                problems.append(TraceProblem(
                    where=where, problem=f"存在しない根拠を参照しています: {ref!r}"))

    titles = [s.title for s in sorted(output.sections, key=lambda s: s.number)]
    if titles != list(REPORT_SECTIONS):
        problems.append(TraceProblem(
            where="sections",
            problem=("章立てが要件 §18 と違います。"
                     f"期待: {list(REPORT_SECTIONS)} / 実際: {titles}")))

    for section in output.sections:
        backwards = _conclusion_before_evidence(section)
        if backwards:
            problems.append(TraceProblem(
                where=f"sections[{section.number}]",
                problem=(f"{backwards} が根拠より先に来ています（要件 §19）: "
                         + " → ".join(b.tag for b in section.blocks))))
        for index, block in enumerate(section.blocks):
            check(f"sections[{section.number}].blocks[{index}]", block.evidence)

    decision = output.decision
    for name in ("primary_patients", "secondary_patients", "avoid_competing_on",
                 "acquisition_area", "reason_to_visit", "clinic_model"):
        check(f"decision.{name}", getattr(decision, name).evidence)
    for name in ("advantages", "risks"):
        for index, item in enumerate(getattr(decision, name)):
            check(f"decision.{name}[{index}]", item.evidence)
    for index, action in enumerate(output.actions):
        check(f"actions[{index}]", action.evidence)

    problems.extend(_forbidden_predictions(output))
    return problems


#: 根拠の側。FACT と BENCHMARK は「値と比較」で 1 組なので、行き来してかまい
#: ません（要件 §22 の「値 + 比較 + 意味」はむしろ交互に書くことを求めます）。
#: PATTERN も、章の中で複数の筋を立てるなら繰り返せます。
_EVIDENCE_TAGS = ("FACT", "BENCHMARK", "PATTERN")
#: 結論の側。
_CONCLUSION_TAGS = ("IMPLICATION", "ACTION")


def _conclusion_before_evidence(section: ReportSection) -> str | None:
    """結論を書いてから根拠を足していないか（要件 §19）。

    最初の実装は、章の中のタグが §19 の並びどおり**単調に**進むことを
    求めていました。厳しすぎました。

      FACT → FACT → BENCHMARK → FACT → BENCHMARK → PATTERN → WHY → …

    これは良い章です。事実ひとつに比較ひとつを添えて書けば当然こうなりますし、
    §22 の「値 + 比較 + 意味」はむしろそう書くことを求めています。これを
    落としていたので、レポート 1 本ぶんの費用が書式の理由で捨てられていました。

    §19 は「可能な限り順番を維持する」と書いてあって、「すべての章にすべての
    タグを無理に入れる必要はない」とも書いてあります。守らせる価値があるのは
    **結論を先に書かないこと**だけです。
    """
    seen_conclusion: str | None = None
    for block in section.blocks:
        if block.tag in _CONCLUSION_TAGS:
            seen_conclusion = seen_conclusion or block.tag
        elif seen_conclusion and block.tag in _EVIDENCE_TAGS:
            return seen_conclusion
    return None


def _forbidden_predictions(output: Step4Output) -> list[TraceProblem]:
    """売上・患者数・成功確率の予測が混じっていないか。

    このシステムは需要の**構造**を説明するもので、予測をするものではありません
    （プロジェクトの前提）。プロンプトで禁じたうえで、出力でも落とします。
    お願いだけで守られることに賭けない。
    """
    problems: list[TraceProblem] = []
    haystack = output.model_dump_json()
    for word in FORBIDDEN_PREDICTIONS:
        if word in haystack:
            problems.append(TraceProblem(
                where="report",
                problem=f"予測にあたる語が含まれています: {word!r}"))
    return problems
