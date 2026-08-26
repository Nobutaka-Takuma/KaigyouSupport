"""各ステップの出力の形。

構造化出力（``output_config.format``）でモデルに強制し、さらに受け取ったあとに
検算します。スキーマが保証するのは「形」であって「中身」ではないためです。
FACT が実在の measure を指しているか、PATTERN の evidence が実在の FACT を
指しているかは、こちらで確かめます（要件 §25）。
"""
from __future__ import annotations

import math
import re
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
#: 「想定家賃」はここに入っていません。地価からの換算を出すことにしたためです
#: （cost.rent_estimate）。ただしそれは予測ではなく、想定利回りという 1 つの
#: 仮定による次元の置き換えで、式も仮定もレポートに載ります。
#: 売上・患者数・成功確率は引き続き出しません。
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
    return _forbidden_predictions_in(output.model_dump_json())


# ------------------------------------------------------------------ STEP5
#
# STEP4 までは根拠を辿れる形（タグと id）で作ります。それは検算のための形で
# あって、人が読むための形ではありません。[FACT] が20個並んだ文書は、読み手に
# 「自分で要約してください」と言っているのと同じです。
#
# ここで顧客に渡す文書に起こし直します。書き手は開業支援の担当者、読み手は
# 開業を考えている歯科医師。知りたいのは「なぜここか」と「何が要るか」です。

#: 評価の強さ。**予測ではありません。** 「有望」は「儲かる」ではなく、
#: 「データ上、条件が揃っている」という意味です。
VerdictLabel = Literal["有望", "条件付きで有望", "慎重に検討", "推奨しない"]


class Judgement(BaseModel):
    """価値判断。データそのものではないので、そう分かる形で持ちます。"""

    label: VerdictLabel
    statement: str = Field(description="そう判断する理由を 2〜3 文で")
    basis: list[str] = Field(
        description="根拠にした F### / C### / S### / M### / I### の id", min_length=1)
    counterpoint: str = Field(
        description="この判断が外れるとしたら何が原因か。書けないなら判断が弱い")


class NarrativeSection(BaseModel):
    """散文の1章。タグは付けません。読み物として通して読める形にします。"""

    heading: str
    body: str = Field(description="段落。箇条書きの羅列にしない")
    #: 章の要点。読み飛ばす人のための行で、本文の代わりではありません。
    takeaway: str | None = None
    evidence: list[str] = Field(default_factory=list)


class SupportItem(BaseModel):
    """この立地で開業するために要る支援 1 件。

    このレポートを配るのは開業支援の事業者です。「良い立地です」で終わる文書は、
    その人たちの仕事の役に立ちません。何を用意する必要があるのかまで書きます。
    """

    item: str = Field(description="必要なこと。例：平日夜間まで回せる人員体制")
    why: str = Field(description="この商圏の何がそれを要求するのか")
    evidence: list[str] = Field(default_factory=list)
    #: 物件 / 設備 / 人員 / 資金 / 手続き / 集患 のどれか。
    category: Literal["物件", "設備", "人員", "資金", "手続き", "集患"]


class ResearchDirection(BaseModel):
    """次に調べるとよいこと 1 件。

    このレポートを配るのは開業支援の事業者で、読み手はその先の調査を自分で
    手配できる立場にあります。「ご存知ですか」と本人に尋ねる欄ではなく、
    **どこを掘ればこの分析が確度を上げるか**を示す欄です。
    """

    topic: str = Field(description="調べる対象。例：既存小児歯科の受入れ余力")
    why: str = Field(description="この分析のどこが、それで確かめられるのか")
    how: str = Field(
        description="調べ方の当て。現地確認 / 自治体資料 / 事業者への照会 など")


class Step5Output(BaseModel):
    """STEP5（顧客提出用レポート）の出力。"""

    title: str
    #: 開業を考えている歯科医師が最初に読む数文。結論から書きます。
    summary: str = Field(description="3〜5 文。判断と、その理由の骨格")
    verdict: Judgement
    #: なぜこの物件か。ここがレポートの背骨です。
    why_here: str = Field(description="この立地を選ぶ理由を、筋道として散文で")
    sections: list[NarrativeSection] = Field(min_length=3)
    support_needed: list[SupportItem] = Field(min_length=1)
    #: 次に掘るべきところ。公的統計で見える範囲の外側を指します。
    further_research: list[ResearchDirection] = Field(default_factory=list)
    #: 価値判断がどこに入っているかの明示。省略できません。
    judgement_note: str = Field(
        description="どこまでがデータで、どこからが評価かを 1〜2 文で")


def verify_step5(output: Step5Output, known_ids: set[str],
                 known_numbers: set[str]) -> list[TraceProblem]:
    """書き直しであって、書き足しではないこと。

    散文にすると、数字はいくらでも滑らかに増やせます。「約5万人」「およそ3倍」
    は、元の数字と少し違っていても文としては通ります。だから、本文に出てくる
    数値が**前の段に実在したもの**かを機械的に確かめます。

    ``known_numbers`` は projection.allowed_numbers が集めた集合です。完全な
    検出ではありませんが、いちばん起きやすい捏造（丸めながらの書き換え）は
    捕まえられます。
    """
    problems: list[TraceProblem] = []

    for where, refs in _step5_references(output):
        for ref in refs:
            if ref not in known_ids:
                problems.append(TraceProblem(
                    where=where, problem=f"存在しない根拠を参照しています: {ref!r}"))

    for where, text in _step5_prose(output):
        for number in invented_numbers(text, known_numbers):
            problems.append(TraceProblem(
                where=where,
                problem=f"前の段に無い数値です: {number}"))

    # judgement_note は「これは予測ではない」と書くための欄です。ここまで
    # 検査に含めると、書いてほしい一文で落ちます。
    body = output.model_copy(update={"judgement_note": ""})
    problems.extend(_forbidden_predictions_in(body.model_dump_json()))
    return problems


def _step5_references(output: Step5Output) -> list[tuple[str, list[str]]]:
    out = [("verdict", output.verdict.basis)]
    out += [(f"sections[{i}]", s.evidence) for i, s in enumerate(output.sections)]
    out += [(f"support_needed[{i}]", s.evidence)
            for i, s in enumerate(output.support_needed)]
    return out


def _step5_prose(output: Step5Output) -> list[tuple[str, str]]:
    out = [("summary", output.summary), ("why_here", output.why_here),
           ("verdict", output.verdict.statement + " " + output.verdict.counterpoint)]
    for i, section in enumerate(output.sections):
        out.append((f"sections[{i}]", section.body + " " + (section.takeaway or "")))
    for i, item in enumerate(output.support_needed):
        out.append((f"support_needed[{i}]", item.item + " " + item.why))
    out += [(f"further_research[{i}]", f"{r.topic} {r.why} {r.how}")
            for i, r in enumerate(output.further_research)]
    return out


#: 本文から数値を拾う正規表現。桁区切り・小数・「万」「億」に対応します。
#: 「約5万人」を 5 として見逃すと、いちばん起きやすい書き換えが素通りします。
_NUMBER = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(万|億)?")

#: 数値として拾っても意味のないもの。「2020年」「1つ目」「3つの理由」のような、
#: 文章の部品として出てくる小さい整数と年です。ここを弾かないと、正しい文が
#: 捏造として落ちます。単位（万・億）が付くものは除きます。
_HARMLESS = {float(n) for n in range(0, 101)} | {float(y) for y in range(1900, 2101)}

_SCALE = {"万": 10_000.0, "億": 100_000_000.0}


def invented_numbers(text: str, known: set[str]) -> list[str]:
    """本文にあって、入力に無かった数値。

    完全な検出ではありません。狙いは「丸めながらの書き換え」で、これがいちばん
    起きやすく、いちばん気づかれません（「13,268人」が「約1.3万人」になるのは
    構いませんが、「約2万人」になったら別の数字です）。

    割合は %表記 と 小数 のどちらでも書けるので、100 倍・100 分の1 も同じ数と
    して扱います。ここを厳しくすると、正しい「27.9%」が落ちます。
    """
    numbers = {float(k) for k in known if _is_number(k)}
    found: list[str] = []
    for match in _NUMBER.finditer(text or ""):
        raw, suffix = match.group(1).rstrip("."), match.group(2)
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        if suffix:
            value *= _SCALE[suffix]
        elif value in _HARMLESS:
            continue
        if _matches(value, numbers, _significant_digits(raw)):
            continue
        found.append(raw + (suffix or ""))
    return found


def _significant_digits(raw: str) -> int:
    """書かれた桁数。「49万」は2桁、「13,268」は5桁、「7,400」は2桁。

    小数点が無いときの末尾のゼロは有効桁に数えません。「約7,400人」は
    7,431 を百の位で丸めた書き方で、4桁で照合すると落ちます。実測：
    それでレポート1本を落としました。
    """
    plain = raw.replace(",", "")
    if "." in plain:
        return max(1, len(plain.replace(".", "").lstrip("0")))
    return max(1, len(plain.lstrip("0").rstrip("0")) or 1)


def _matches(value: float, numbers: set[float], digits: int) -> bool:
    """同じ数と見なせるか。

    **書かれた桁数まで丸めて**比べます。読み物として渡す文書で
    「494,517人」と書けとは言えません。「約49万人」は正しい書き方で、
    落としてはいけない。一方「約5万人」は別の数で、これは落とします。

    割合は %表記 と 小数 のどちらでも書けるので、100 倍・100 分の1 も
    同じ数として扱います。ここを厳しくすると、正しい「27.9%」が落ちます。
    """
    for candidate in (value, value / 100.0, value * 100.0):
        for known in numbers:
            rounded = _round_to(known, digits)
            if abs(candidate - rounded) <= max(abs(candidate), 1.0) * 1e-9:
                return True
    return False


def _round_to(value: float, digits: int) -> float:
    if value == 0:
        return 0.0
    exponent = math.floor(math.log10(abs(value)))
    return round(value, -(exponent - digits + 1))


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


#: 予測語のすぐ後ろに来る打ち消し。「開業の成功確率を示すものではありません」は
#: 免責であって予測ではありません。実測：この一文でレポート1本を落としました。
_NEGATIONS = ("ではありません", "ではない", "しません", "しない", "できません",
              "ありません", "保証するものではありません", "予測するものではありません",
              "示すものではありません", "を行いません", "を出しません", "は扱いません")


def _forbidden_predictions_in(haystack: str) -> list[TraceProblem]:
    """売上・患者数・成功確率の予測が混じっていないか。

    語の有無だけを見ると、**それを否定する文**まで落ちます。レポートには
    「開業の成否を予測するものではありません」と書いてほしいので、後ろに
    打ち消しが続くものは通します。
    """
    problems: list[TraceProblem] = []
    for word in FORBIDDEN_PREDICTIONS:
        for match in re.finditer(re.escape(word), haystack):
            tail = haystack[match.end():match.end() + 40]
            if any(negation in tail for negation in _NEGATIONS):
                continue
            problems.append(TraceProblem(
                where="report",
                problem=f"予測にあたる語が含まれています: {word!r}"))
            break
    return problems
