"""各ステップの出力の形。

構造化出力（``output_config.format``）でモデルに強制し、さらに受け取ったあとに
検算します。スキーマが保証するのは「形」であって「中身」ではないためです。
FACT が実在の measure を指しているか、PATTERN の evidence が実在の FACT を
指しているかは、こちらで確かめます（要件 §25）。
"""
from __future__ import annotations

import math
import re
from typing import Literal, Mapping, Sequence

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


#: 周辺施設の種類。**候補地の性格を決めるもの**だけを並べます。コンビニも
#: 飲食店もどこにでもあるので、あっても判断は動きません。
FacilityCategory = Literal[
    "大学・専門学校", "大型商業施設", "病院・介護施設", "事業所・工場",
    "学校（小中高）", "官公庁・公共施設", "住宅・再開発", "その他",
]

#: 候補地の立地類型。**これが決まらないと商圏の読み方が決まりません。**
#: 商業施設内のテナントなら、商圏は徒歩圏ではなく施設の集客圏です。同じ
#: 半径1km のデータを、まるで違う意味に読むことになります。
#: 候補地が**どんな性格の場所か**。
#:
#: **「駅前」は入っていません。** 駅前かどうかは駅からの距離で決まり、距離は
#: 測ってあります（`access.station_band`）。両者は別のことで、駅から 200m の
#: 住宅地の路面もあれば、駅から 2km のロードサイドもあります。
#:
#: 実測：沼津駅の南南西 810m（徒歩10分）の候補地が「駅前」として分析されて
#: いました。**測った距離があるのに、呼び方をモデルの読み取りに任せていた**
#: のが原因です。
LocationSetting = Literal[
    "住宅地の路面", "商業地の路面", "商業施設内・隣接", "オフィス街",
    "郊外ロードサイド", "不明",
]


class NearbyFacility(BaseModel):
    """候補地の周りにある、判断を動かす施設 1 件。"""

    name: str = Field(
        description="固有名詞。「大型商業施設」のような一般名詞は書かない")
    category: FacilityCategory
    where: str = Field(description="候補地から見た位置。「商圏内」「約1.2km北」など")
    scale: str | None = Field(
        default=None,
        description="学生数・店舗数・病床数・従業員数など。分からなければ null")
    why_it_matters: str = Field(
        description="この施設があることで歯科医院の何が変わるか。"
                    "何も変わらないなら挙げないこと")
    source_url: str = Field(description="今回の検索で取得した URL から選ぶ")


class Surroundings(BaseModel):
    """候補地の周辺施設と立地類型。統計の前に、まず何がある場所なのか。"""

    setting: LocationSetting
    setting_reason: str = Field(description="なぜその類型と判断したか。1〜2文")
    facilities: list[NearbyFacility] = Field(default_factory=list)
    note: str = Field(
        default="",
        description="調べたが確認できなかったこと。無ければ空文字")


#: 仮説が正しかったときに動きうるもの。**config/<業態>/hypotheses.yaml の
#: screening.levers と同じ並びにしてください。** 片方だけ足すと、設定に
#: 書いてあるのにスキーマが受け付けない選択肢ができます。
#:
#: なぜ欄にするか。「思いつきの仮説」と「戦略が変わる仮説」を、文章の中で
#: 区別するのは読み手の仕事になります。欄なら空にできません。
DecisionLever = Literal["診療コンセプト", "設備投資", "診療時間",
                        "人員体制", "立地判断", "患者層"]


#: どこから問いが生まれたか（指示書 §52・§57）。**問いだけを保存しては
#: いけません。**なぜその問いが生まれたのかが残らないと、あとから「この AI は
#: なぜこれを訊いたのか」が誰にも分かりません。
#:
#: 型は、実際に効いた発生源を並べたものです。**思いつきではなく、2 つ以上の
#: 事実の突き合わせから出た問いだけが良い問いになります。**
TriggerType = Literal[
    # 手元のデータと外部の状況が食い違う。**いちばん当たりが多い型です。**
    # 例：将来人口は減少予測、しかし大規模な土地区画整理事業が進行中。
    "data_external_conflict",
    # 現在のデータと将来のデータが食い違う。例：人口は減少、しかし駅前で再開発。
    "present_future_conflict",
    # 周辺との大きな乖離。ただし「なぜ若者が多い？」で止めないこと。
    "deviation_from_peers",
    # 手元の指標同士が噛み合わない。例：人口は多い・昼間人口は少ない・駅は多い。
    "inconsistent_measures",
    # 将来の重要な前提を疑う。再開発・区画整理・新駅・大型施設・医療機関の開廃。
    "future_assumption",
    "other",
]

#: 公表情報で確かめられる見込み（指示書 §55）。**この 1 つだけが検索するか
#: どうかを決めます。**
#:
#: 実測でいちばん多い無駄がこれでした：「市区町村単位の歯科医師・歯科衛生士の
#: 年齢構成」「在宅療養支援歯科診療所の届出数」。**答えが公表されていないと
#: 既に分かっているのに、毎回検索していました。** 検索の上限は決まっている
#: ので、そこに使ったぶんは、答えの出る問いに回りません。
#:
#: `low` は失敗ではありません。**「重要だが調べられない」は、そのまま
#: 「開業前に現地で確かめること」になります**（指示書 §56）。
Researchability = Literal[
    "high",    # 官公庁・自治体・事業者が公表している見込みが高い
    "medium",  # あるかもしれないが、粒度や公開範囲が読めない
    "low",     # 公表されていないと分かっている。**検索しません**
]


class Assumption(BaseModel):
    """この分析が置いている前提（指示書 §53・§58）。FACT → PATTERN → 前提 → QUESTION。"""

    id: str = Field(description="A001 のような通し番号")
    statement: str = Field(
        description="この分析が正しいとして扱っている前提を 1 文で。"
                    "「〜は〜である（と扱ってよい）」の形")
    rests_on: list[str] = Field(
        default_factory=list,
        description="その前提を置いている根拠の FACT / PATTERN の id")
    if_wrong: str = Field(
        description="この前提が間違っていたら、どの判断がどう変わるのか")


class QuestionTrigger(BaseModel):
    """その問いを生んだ突き合わせ（指示書 §57）。"""

    type: TriggerType
    facts: list[str] = Field(
        default_factory=list,
        description="突き合わせた事実。**2 つ以上**書いてください。"
                    "1 つしか書けないなら、それは突き合わせではありません")
    reason: str = Field(
        default="",
        description="なぜこの 2 つを突き合わせると問いになるのか、1 文で")


class Question(BaseModel):
#: **長い説明をここに書かないでください。** class docstring はそのまま
#: JSON schema の description になり、構造化出力の文法に毎回乗ります。
#: 実測：STEP1 のスキーマが 7,977 文字まで膨らみ、API が
#: 「The compiled grammar is too large」で 400 を返しました。
#: 問いの立て方の説明は config/<業態>/prompts/step1_features.md にあります。
    """PATTERN から出た問い（指示書 §8）。"""

    id: str = Field(description="Q001 のような通し番号")
    pattern_id: str = Field(description="どの PATTERN から出た問いか")
    question: str = Field(description="その前提は本当にそうなのか")
    why_it_matters: str = Field(description="答えが出ると何の判断が変わるか")
    what_would_answer_it: str = Field(description="何を調べれば答えが出るか")
    #: 以下は**空でも受け付けます**——古い形の保存済み出力を読み直せなく
    #: なるほうが困るためです。新しい出力では埋まります（プロンプトが求め、
    #: verify_step1 が検算します）。
    #: `| None` を使いません。anyOf が 1 つ増えるたびに文法の分岐が増えます。
    #: 「無い」は空文字で表せます。
    assumption_id: str = Field(
        default="", description="疑っている ASSUMPTION の id（A001 など）")
    trigger: QuestionTrigger = Field(
        default_factory=lambda: QuestionTrigger(type="other"),
        description="この問いを生んだ突き合わせ")
    researchability: Researchability = Field(
        default="medium", description="low なら検索しません")
    researchability_reason: str = Field(
        default="", description="low なら、どこに無いのかまで")
    #: 答えが出たとき何が動くか。**Hypothesis と同じ 6 つ**を使います。
    #: 問いの段で書かせるのは、動かないと分かっている問いに検索を使わせない
    #: ためです（指示書 §59）。
    decision_levers: list[DecisionLever] = Field(
        default_factory=list, description="答えによって動きうるもの。1 つ以上")
    #: `importance` は置きません。**動作を変えない欄は、文法を太らせるだけ**
    #: です。問いの重さは `decision_levers`（どの判断が動くか）で表せていて、
    #: 順序づけにしか使っていませんでした。
    #: **手元のデータで既に答えが出ている問いを、外部に訊きに行かせない。**
    #: 「昼間人口は何人か」はデータセットに書いてあります（指示書 §62）。
    already_in_data: bool = Field(
        default=False, description="手元のデータだけで答えが出るなら true")


class Step1Output(BaseModel):
    """STEP1 の出力（保存される形）。benchmarks は含みません。"""

    facts: list[Fact] = Field(min_length=1)
    patterns: list[Pattern] = Field(min_length=1)
    #: この分析が置いている前提（指示書 §53）。**問いはここから生まれます。**
    #: 空でも受け付けます（古い形の読み出し互換）。
    assumptions: list[Assumption] = Field(default_factory=list)
    #: PATTERN から出た問い。**空でも受け付けます**——古い形で保存された
    #: ジョブを読み直せなくなるほうが困るためです（expand → migrate →
    #: contract）。新しい出力では必ず埋まります（プロンプトが求めます）。
    questions: list[Question] = Field(default_factory=list)
    #: 分析できなかったこと。空でも構いませんが、書けるなら書かせます。
    not_determinable: list[str] = Field(
        default_factory=list,
        description="基礎データからは判断できなかった論点")
    #: 周辺施設スキャンの結果。**これは外部情報なので FACT ではありません。**
    #: FACT は measures と citable のキーしか引けないという規則は変えません。
    #: ここが効くのは PATTERN の見立てと research_questions で、つまり
    #: 「何を調べに行くか」のほうです。
    surroundings: Surroundings | None = None


# --------------------------------------------------------- STEP1 の 2 つの呼び出し
#
# **`Step1Output` は保存される形です。API に渡す形ではありません。**
#
# 1 回の構造化出力に FACT・PATTERN・周辺施設・前提・問いを全部入れていたら、
# 文法が大きくなりすぎて API が 400 を返しました。
#
#     The compiled grammar is too large, which would cause performance issues.
#
# 実測：Step1Output のスキーマ 7,977 文字。通っている Step2Output は 4,512 文字。
# この差はこのセッションで足したもの（Assumption・QuestionTrigger・Question の
# 7 欄）です。
#
# **分けたのは、しのぎのためではありません。** STEP1 は 2 つの違う仕事を 1 回で
# やっていました。
#
#     読む   … GIS が確定した数字から FACT と PATTERN を起こし、
#              周辺スキャンの本文を構造化する（機械的な写し取り）
#     疑う   … 起こした FACT / PATTERN が置いている前提を見つけ、
#              それを確かめる問いを立てる（解釈）
#
# 分けると、**「疑う」側は確定した FACT を入力として受け取ります。** これは
# ちょうど「GIS が Fact を確定し、LLM がそれを解釈する」という分業そのもの
# です。1 回でやらせていたときは、読みながら疑うことになっていました。


class Step1Reading(BaseModel):
    """STEP1 前半：読む。GIS が確定した数字と、周辺スキャンの本文から。"""

    facts: list[Fact] = Field(min_length=1)
    patterns: list[Pattern] = Field(min_length=1)
    not_determinable: list[str] = Field(default_factory=list)
    surroundings: Surroundings | None = None


class Step1Inquiry(BaseModel):
    """STEP1 後半：疑う。読み取った FACT と PATTERN が置いている前提を。"""

    assumptions: list[Assumption] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)


class TraceProblem(BaseModel):
    """参照が解決しなかった箇所。"""

    where: str
    problem: str


def verify_step1(output: Step1Output, allowed_measure_keys: set[str],
                 layer_of: Mapping[str, str] | None = None,
                 min_cross_layer: int = 0) -> list[TraceProblem]:
    """FACT が実在の指標を、PATTERN が実在の FACT を指しているか。

    スキーマは形しか保証しません。存在しない measure_key を書いた FACT も、
    存在しない FACT を指す PATTERN も、スキーマ上は正しい JSON です。
    ここで落とさないと、レポートの末尾まで残って §25 の追跡が切れます。

    ``layer_of`` を渡すと、**層を跨いでいるか**も見ます。単一の層の中で
    数字を並べ替えただけのものは、PATTERN ではなく要約です。実測のレポートは
    「人口が市内2位」「医院数も市内2位」を PATTERN として出していました。
    どちらも同じ商圏の大きさを別の言葉で言っているだけで、掛けても何も
    出てきません。

    **層はモデルに申告させません。** 引かれた指標から数えます。自己申告に
    すると「layers: [人口, 競合]」と書きながら人口の指標を2つ引く、という
    形だけ整った出力が通ります。
    """
    problems: list[TraceProblem] = []
    fact_ids = {f.id for f in output.facts}
    layer_of = layer_of or {}
    layer_by_fact = {f.id: layer_of.get(f.measure_key) for f in output.facts}

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

    # **問いの検算は層の情報に依存しません。** 早期 return より前に置きます。
    # 後ろに置いていたため、layer_of を渡さない呼び出しでは問いが一切
    # 検算されませんでした。
    problems += _verify_questions(output, fact_ids, seen_pattern_ids)

    if not layer_of:
        return problems

    # 層を跨いだ PATTERN。importance が high のものは必ず跨いでいること。
    # high は「競合戦略・患者層・医院モデルに影響する」という宣言なので、
    # 1 つの層の中で完結する観察がそれに当たることはありません。
    crossing = 0
    for pattern in output.patterns:
        spanned = {layer_by_fact.get(ref) for ref in pattern.evidence}
        spanned.discard(None)
        if len(spanned) >= 2:
            crossing += 1
        elif pattern.importance == "high":
            names = ", ".join(sorted(spanned)) or "（不明）"
            problems.append(TraceProblem(
                where=f"patterns[{pattern.id}]",
                problem=(f"importance が high ですが、根拠が {names} の層だけで"
                         "閉じています。同じ層の数字を並べ替えたものは要約で"
                         "あって、構造ではありません。別の層の FACT を足すか、"
                         "importance を下げてください")))

    if crossing < min_cross_layer:
        problems.append(TraceProblem(
            where="patterns",
            problem=(f"層を跨いだ PATTERN が {crossing} 件しかありません"
                     f"（{min_cross_layer} 件以上）。人口動態・産業雇用・競合の"
                     "提供体制・将来推計などを掛け合わせ、データ同士の矛盾や"
                     "構造的なギャップを指摘してください")))
    return problems


def _verify_questions(output: Step1Output, fact_ids: set[str],
                      pattern_ids: set[str]) -> list[TraceProblem]:
    """問いが、疑うに足る形をしているか（指示書 §51・§55・§62）。

    **問いを立てた数は成績ではありません。** ここで見るのは、その問いが
    次の段の検索を使うに値するかどうかです。検索の上限は決まっているので、
    値しない問いに使ったぶんは、答えの出る問いに回りません。

    古い形（`questions` が空）には掛けません。掛けると、問いを第一級にする
    前に保存されたジョブが再実行のたびに落ちます。
    """
    if not output.questions:
        return []
    problems: list[TraceProblem] = []
    assumption_ids = {a.id for a in output.assumptions}

    for assumption in output.assumptions:
        for ref in assumption.rests_on:
            if ref not in fact_ids and ref not in pattern_ids:
                problems.append(TraceProblem(
                    where=f"assumptions[{assumption.id}].rests_on",
                    problem=f"存在しない FACT / PATTERN を参照しています: {ref!r}"))

    for question in output.questions:
        where = f"questions[{question.id}]"
        # **手元で答えが出る問いを、外部に訊きに行かせない。**「昼間人口は
        # 何人か」はデータセットに書いてあります（指示書 §62）。
        if question.already_in_data:
            problems.append(TraceProblem(
                where=where,
                problem=("手元のデータで答えが出る問いです（already_in_data）。"
                         "外部調査の問いにはしないでください。データに無い"
                         "『なぜそうなっているのか』を問いにしてください")))
        # 答えが出ても何も動かない問いは、知識が増えるだけです（§59）。
        if not question.decision_levers:
            problems.append(TraceProblem(
                where=where,
                problem=("答えによって動くものが 1 つも挙がっていません"
                         "（decision_levers）。診療コンセプト・設備投資・"
                         "診療時間・人員体制・立地判断・患者層のどれも動かない"
                         "なら、その問いは出さないでください")))
        # **low は失敗ではありませんが、行き先が要ります**（§56）。
        if question.researchability == "low" and not question.researchability_reason:
            problems.append(TraceProblem(
                where=where,
                problem=("researchability が low なのに理由がありません。"
                         "どこに公表されていないのかを書いてください。"
                         "これは検索せずに現地確認へ回す判断の根拠になります")))
        if question.assumption_id and question.assumption_id not in assumption_ids:
            problems.append(TraceProblem(
                where=where,
                problem=f"存在しない ASSUMPTION を参照しています: "
                        f"{question.assumption_id!r}"))
        # **1 つの事実から出た問いは、突き合わせではありません**（§58・§63）。
        if question.trigger and len(question.trigger.facts) < 2:
            problems.append(TraceProblem(
                where=f"{where}.trigger",
                problem=("突き合わせた事実が 1 つ以下です。良い問いは 2 つ以上の"
                         "事実がぶつかるところから出ます（将来人口は減少予測、"
                         "しかし区画整理事業が進行中、のように）")))
    return problems


# ============================================================ 競合分析（3C の C2）
#
# 開発指示書「地域競合分析AI MVP」。**周辺一般の調査に使っていた検索リソースを、
# 競合医院の情報収集に振り替えます。**
#
# 枠（STP と 4P）は業態を問わず同じで、中に入る語だけが違います。だから枠は
# ここに、語は config/<業態>/competitors.yaml に置きます。歯科で始めますが、
# 飲食・小売・学習塾でも同じ枠が使えます。
#
# **スキーマは小さく保ちます。** 1 医院につき 1 回の構造化出力です。全医院を
# 1 つの出力に入れると、文法が大きくなりすぎて API が 400 を返します
# （実測済み：`The compiled grammar is too large`）。

# ---------------------------------------------------------------------------
# **Competitor は平らに保ちます。** 入れ子の配列と null 許容を並べたところ、
# API が
#
#     400 invalid_request_error: Schema is too complex.
#
# を返し、2 院とも構造化できずに段ごと落ちました。文法を組むのは配列の「数」と
# 「入れ子の深さ」に効くので、こう畳んであります。
#
#     place: list[Finding]      → place_confirmed: list[str]（キーだけ）
#     promotion: list[str]      → promotion_note: str
#     not_confirmed: list[str]  → not_confirmed: str
#     map_x: int | None         → map_x: int + map_placed: bool
#
# **数えるもの（§4 の「駐車場あり何院」）は残しています**——キーの配列があれば
# 数えられるので、1 項目ごとの入れ子は要りません。
#
# この説明を docstring ではなくコメントに置いているのは、**docstring が
# JSON Schema の description になって毎回の呼び出しに乗るから**です。設計の
# 経緯は送る必要がありません。
# ---------------------------------------------------------------------------
class Competitor(BaseModel):
    """競合 1 件の STP と 4P。確認できなかった項目は not_confirmed へ。"""

    name: str = Field(description="医院名。入力で渡した名前をそのまま")
    homepage: str = Field(default="", description="公式サイトの URL。無ければ空")
    # --- STP ---
    segments: list[str] = Field(
        default_factory=list, description="主な顧客層。設定の語彙から")
    target: list[str] = Field(
        default_factory=list, description="特に訴求している層。設定の語彙から")
    positioning: list[str] = Field(
        default_factory=list, description="何を強みとして訴求しているか")
    # --- 4P ---
    products: list[str] = Field(
        default_factory=list, description="扱っている診療領域。設定の語彙から")
    price_note: str = Field(
        default="", description="価格の訴求。確認できた自費価格があれば具体的に")
    #: **確認できたものだけ。** 「駐車場：不明」は入れません——不明を並べると
    #: 調べた量が多く見えます。確認できなかったことは not_confirmed へ。
    place_confirmed: list[str] = Field(
        default_factory=list,
        description="確認できた設備・条件のキー（parking / weekend など）だけを列挙")
    place_note: str = Field(
        default="", description="立地・診療時間で分かったことを 1〜2 文で")
    promotion_note: str = Field(
        default="", description="Web での主な訴求・SNS・キャンペーンを 1〜2 文で")
    # --- ポジショニングマップ（指示書 §5）---
    #: **位置は書かせません。観測できた事実だけを挙げさせます。**
    #:
    #: 「保険中心か自費中心か」は売上の構成比で、Web からは見えません。見えない
    #: 量を judgement で当てさせると、名前から推測するか全件「判定不能」に
    #: なります（両方とも実測しました）。位置は competition.py が、この
    #: signals と設定の重みから計算します。
    signals: list[str] = Field(
        default_factory=list,
        description="サイトで**実際に確認できた**観測のキーだけを列挙。"
                    "確認できなかったものは入れない")
    map_basis: str = Field(
        default="",
        description="観測の裏づけ。どの頁の何を見てそのキーを挙げたか")
    not_confirmed: str = Field(
        default="", description="調べたが確認できなかったこと。空にしないでください")
    sources: list[str] = Field(
        default_factory=list, description="参照したページの URL")


class CompetitorSurvey(BaseModel):
    """1 医院ぶんの調査結果（構造化の呼び出しが返す形）。"""

    competitor: Competitor


class OpportunityHypothesis(BaseModel):
    """競合分布から見える機会の**仮説**（指示書 §6）。

    **「競合が少ない＝市場機会がある」と断定しません。** 少ないのは、
    やってみて成立しなかったからかもしれません。
    """

    position: str = Field(description="どのポジションか")
    why: str = Field(description="競合データのどこからそう言えるのか")
    caveat: str = Field(
        description="この仮説が外れるとしたら何が理由か。"
                    "「競合が少ないのは需要が無いからかもしれない」など")


class CompetitionSummary(BaseModel):
    """地域の競争環境の要約（指示書 §6）。**集計は済んでいます。**

    数え上げは Python がやりました。ここでやるのは、**その数字が何を
    意味するかを言うこと**だけです。新しい数字を作らないでください。
    """

    landscape: str = Field(description="競争環境を 2〜3 文で。集計値に基づいて")
    character: str = Field(
        description="「この地域の歯科医院は○○型が多い」の形で 1 文")
    crowded: list[str] = Field(
        default_factory=list, description="競争が集中している領域")
    sparse: list[str] = Field(
        default_factory=list, description="比較的競合が少ない領域")
    opportunities: list[OpportunityHypothesis] = Field(default_factory=list)
    not_determinable: list[str] = Field(
        default_factory=list,
        description="調べたが確認できなかったこと。空にしないでください")


# ------------------------------------------------------------------ STEP2
#: 仮説の判定（指示書 §12）。
#:
#: **`UNSUPPORTED` は 2 つの別のことを兼ねていました。**「調べたが支持する
#: 情報が見つからなかった」と「調べたら違うと分かった」です。経営判断に
#: とってこの 2 つは正反対で、前者は「現地で確かめる」、後者は「その筋は
#: 消えたので別を追う」になります。同じ札に入れると、レポートを読んだ人は
#: どちらなのか判断できません。
#:
#: `UNSUPPORTED` は**読み出しのためだけ**に残します。古い保存済みレポートが
#: そのまま読めなくなるほうが困るので消しません。新しい出力で使わないことは
#: プロンプトと検算が見張ります。
HypothesisStatus = Literal[
    "SUPPORTED",            # 支持する外部事実がある
    "PARTIALLY_SUPPORTED",  # 一部だけ確かめられた
    "UNCERTAIN",            # 調べたが、どちらとも言えない
    "CONTRADICTED",         # 調べたら、違うと分かった
    "UNSUPPORTED",          # 旧。読み出し互換のためだけ
]

#: 新しい出力で使ってよい判定。`UNSUPPORTED` を除いたもの。
CURRENT_HYPOTHESIS_STATUSES = frozenset(
    {"SUPPORTED", "PARTIALLY_SUPPORTED", "UNCERTAIN", "CONTRADICTED"})

#: 根拠が 1 つも無くてよい判定。**調べて何も出なかったことを、そのまま
#: 書けるようにするため**です。それ以外は根拠が要ります——「支持されて
#: いる」と言うなら、何がそう言わせているのかが要ります。
STATUSES_WITHOUT_EVIDENCE = frozenset({"UNCERTAIN"})


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
    #: 何周目の調査で出てきたか。**モデルの申告ではなく、サーバが上書きします。**
    #: 1 周目で答えが出ず、2 周目で角度を変えて出てきた事実は、最初から
    #: 出ていた事実とは意味が違います。「調べ直した」という事実そのものが
    #: 記録で、それが消えると読み手には 1 周で出たように見えます。
    round: int = Field(default=1, description="システムが設定します（記入不要）")


class EvidenceLink(BaseModel):
    """外部事実 1 件が、その仮説をどちら向きに動かすか（指示書 §11）。

    **向きが無いと、反証が残りません。** これまで ``evidence`` は id の
    並びで、支持したのか否定したのかは ``reasoning`` の文章の中にしか
    ありませんでした。文章の中にあるものは、機械で数えられず、後の段が
    読み違えます。
    """

    fact_id: str = Field(description="EXTERNAL FACT の id（C001 など）")
    stance: Literal["supports", "contradicts", "context"] = Field(
        description="supports=仮説を支持 / contradicts=否定 / "
                    "context=関係はあるが判定には効かない")
    note: str = Field(
        description="その事実がなぜ支持／反証になるのかを1文で")


class Hypothesis(BaseModel):
    """PATTERN の背景についての仮説と、その判定（要件 §11、指示書 §9・§12）。"""

    id: str = Field(description="H001 のような通し番号")
    pattern_id: str
    #: どの問いへの答えか。**空でも受け付けます**——古い形の保存済み出力を
    #: 読み直せなくなるほうが困るためです。新しい出力では必ず埋まります。
    question_id: str | None = Field(
        default=None, description="この仮説が答えている QUESTION の id（Q001 など）")
    statement: str = Field(description="この PATTERN がなぜ存在するのかの説明")
    status: HypothesisStatus
    #: 旧・id の並び。読み出しのためだけに残します。新しい出力は
    #: ``evidence_links`` を埋めます。
    evidence: list[str] = Field(
        default_factory=list,
        description="判定の根拠にした EXTERNAL FACT の id")
    #: 向きつきの根拠。**UNCERTAIN 以外では 1 つ以上**（検算が見張ります）。
    evidence_links: list[EvidenceLink] = Field(
        default_factory=list,
        description="根拠にした外部事実と、それが支持か反証か")
    reasoning: str = Field(description="その外部事実からなぜその判定になるのか")
    confidence: Confidence
    #: So What?。ここが埋まらない仮説は、正しくても何も変えません。
    #: 実測：「区画整理により計画的に形成された市街地である」という仮説が
    #: 出ましたが、正しくても診療コンセプトも設備も診療時間も変わりません。
    changes: list[DecisionLever] = Field(
        description="この仮説が正しかったとき、根本から変わりうるもの。1つ以上",
        min_length=1)
    decision_impact: str = Field(
        description="何がどう変わるのかを1〜2文で。「〜を検討する余地がある」"
                    "ではなく、「AではなくBにする」の形で書く")
    #: 何周目で立てた仮説か。サーバが上書きします（``ExternalFact.round`` と同じ）。
    round: int = Field(default=1, description="システムが設定します（記入不要）")


class UnansweredQuestion(BaseModel):
    """答えの出なかった問いと、決着させる道筋（指示書 §12・§15）。"""

    question_id: str = Field(description="QUESTION の id（Q001 など）")
    why: str = Field(description="なぜ外部情報からは答えが出なかったのか")
    what_would_settle_it: str = Field(
        description="何があれば決着するのか。現地確認・自治体照会・"
                    "統計の取得など、実際に取れる手段で書く")


class Step2Output(BaseModel):
    """STEP2（外部コンテクスト調査）の出力。

    否定された仮説も残します（要件 §11）。「調べたが違った」は、調べていない
    のとは別のことで、STEP3 以降が同じ筋を追い直さずに済みます。
    """

    external_facts: list[ExternalFact] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    #: 調べたが答えが見つからなかった質問。空にしないでほしい欄です。
    #: 旧・自由文。読み出しのためだけに残します。
    unanswered: list[str] = Field(
        default_factory=list,
        description="調べたが外部情報からは確認できなかった論点")
    #: どの問いが答えられずに残ったか。**次に何をすればよいかまで書かせます。**
    #: 自由文だけだと「分かりませんでした」で終わり、開業前に現地で確かめる
    #: べきこと（§15）に繋がりません。
    open_questions: list[UnansweredQuestion] = Field(default_factory=list)


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


def drop_unverifiable_facilities(output: Step1Output,
                                retrieved_urls: set[str]) -> list[str]:
    """周辺施設のうち、出典を確かめられなかったものを落とす。

    **段ごと落としません。** 施設スキャンは STEP1 の本題（FACT と PATTERN）の
    付随物で、その 1 件のために FACT 十数件と PATTERN の生成を捨てるのは
    割に合いません（STEP2 で同じ判断をしています）。

    落としたことは ``surroundings.note`` に書き残します。黙って消すと、
    次に読む人には「その施設は無かった」と読めてしまいます。
    """
    if output.surroundings is None:
        return []
    known = {normalize_url(u) for u in retrieved_urls if u}
    kept, dropped = [], []
    for facility in output.surroundings.facilities:
        if normalize_url(facility.source_url) in known:
            kept.append(facility)
        else:
            dropped.append(facility.name)
    if not dropped:
        return []
    output.surroundings.facilities = kept
    note = output.surroundings.note.strip()
    output.surroundings.note = (note + " " if note else "") + (
        f"周辺施設 {len(dropped)}件（{'、'.join(dropped)}）は、引用された URL が"
        "今回の検索結果に含まれていなかったため除外しました。"
        "実在しないという意味ではなく、出典を確かめられなかったという意味です。")
    return dropped


def verify_step2(output: Step2Output, allowed_pattern_ids: set[str],
                 retrieved_urls: set[str],
                 allowed_question_ids: set[str] | None = None,
                 ) -> list[TraceProblem]:
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
        problems.extend(_verify_verification(hypothesis, fact_ids,
                                             allowed_question_ids))
        problems.extend(_screen_for_impact(hypothesis))

    # 答えの出なかった問いも、実在する問いを指していること。
    if allowed_question_ids is not None:
        for open_q in output.open_questions:
            if open_q.question_id not in allowed_question_ids:
                problems.append(TraceProblem(
                    where=f"open_questions[{open_q.question_id}]",
                    problem=f"存在しない QUESTION を参照しています: "
                            f"{open_q.question_id!r}"))
    return problems


def _verify_verification(hypothesis: Hypothesis, fact_ids: set[str],
                         allowed_question_ids: set[str] | None,
                         ) -> list[TraceProblem]:
    """判定と根拠が噛み合っているか（指示書 §11・§12）。

    **「支持されている」と言うなら、支持する根拠が要ります。** これは文章の
    説得力の話ではなく、機械で確かめられる形の話です。判定が SUPPORTED なのに
    supports の根拠が 1 つも無い出力は、読んだ人には「調べて確かめた」ように
    見えて、実際には何も確かめていません。

    ``UNCERTAIN`` だけは根拠が無くてよい判定です。**調べて何も出なかったことを、
    そのまま書けるようにするため**で、そこを塞ぐと関係のない事実を無理に
    supports に入れるか、仮説ごと消すかになります。消すと「調べていない」と
    区別がつかなくなります。
    """
    problems: list[TraceProblem] = []
    where = f"hypotheses[{hypothesis.id}]"

    if hypothesis.status not in CURRENT_HYPOTHESIS_STATUSES:
        problems.append(TraceProblem(
            where=where,
            problem=f"使わなくなった判定です: {hypothesis.status!r}。"
                    "調べて支持が見つからなかったなら UNCERTAIN、"
                    "調べて違うと分かったなら CONTRADICTED を使ってください"))

    if (allowed_question_ids is not None and hypothesis.question_id
            and hypothesis.question_id not in allowed_question_ids):
        problems.append(TraceProblem(
            where=where,
            problem=f"存在しない QUESTION を参照しています: "
                    f"{hypothesis.question_id!r}"))

    links = hypothesis.evidence_links
    for link in links:
        if link.fact_id not in fact_ids:
            problems.append(TraceProblem(
                where=f"{where}.evidence_links",
                problem=f"存在しない EXTERNAL FACT を参照しています: "
                        f"{link.fact_id!r}"))

    # 古い形（evidence_links が無い）には掛けません。掛けると、保存済みの
    # レポートを読み直すたびに問題として並びます。
    if not links:
        if hypothesis.evidence:
            return problems
        if hypothesis.status not in STATUSES_WITHOUT_EVIDENCE:
            problems.append(TraceProblem(
                where=where,
                problem=f"根拠が 1 つもありません。{hypothesis.status} と"
                        "言うなら、そう言わせている外部事実が要ります"
                        "（分からなかったなら UNCERTAIN）"))
        return problems

    supports = [link for link in links if link.stance == "supports"]
    contradicts = [link for link in links if link.stance == "contradicts"]

    if hypothesis.status in ("SUPPORTED", "PARTIALLY_SUPPORTED") and not supports:
        problems.append(TraceProblem(
            where=where,
            problem=f"{hypothesis.status} なのに、支持する根拠が 1 つも"
                    "ありません（context だけでは支持になりません）"))
    if hypothesis.status == "CONTRADICTED" and not contradicts:
        problems.append(TraceProblem(
            where=where,
            problem="CONTRADICTED なのに、否定する根拠が 1 つもありません"))
    if hypothesis.status == "SUPPORTED" and contradicts:
        problems.append(TraceProblem(
            where=where,
            problem="否定する根拠があるのに SUPPORTED です。"
                    "PARTIALLY_SUPPORTED か UNCERTAIN のどちらかです"))
    return problems


#: 「正しくても何も変わらない」仮説を落とすための最低線。
#:
#: 中身のよしあしは機械には判定できません。判定できるのは、**そもそも
#: 書こうとしていない**ことだけです。それでも実測ではこの2つがいちばん
#: 多い抜け方でした。仮説文をそのまま写す、1行に満たない言い切りで済ませる。
_MIN_IMPACT_CHARS = 20


def _screen_for_impact(hypothesis: Hypothesis) -> list[TraceProblem]:
    """So What? が書かれているか（要件の「意思決定へのインパクト」）。

    ``changes`` が空でないことはスキーマが保証します。ここで見るのは、
    ``decision_impact`` が仮説の言い換えになっていないかどうかです。
    「区画整理で計画的に形成された市街地である」に対して
    「計画的に形成された市街地であることが分かる」と書かれても、
    次の一手は 1 ミリも動きません。
    """
    impact = (hypothesis.decision_impact or "").strip()
    where = f"hypotheses[{hypothesis.id}].decision_impact"
    if len(impact) < _MIN_IMPACT_CHARS:
        return [TraceProblem(
            where=where,
            problem=("何がどう変わるのかが書かれていません。"
                     "「AではなくBにする」の形で、変わる先まで書いてください"))]
    if impact == hypothesis.statement.strip():
        return [TraceProblem(
            where=where,
            problem=("仮説文と同じです。仮説が正しかったときに**別のもの**が"
                     "選ばれる、という形で書いてください"))]
    return []


# ------------------------------------------------------- STEP3 と最終段の共通部
#
# 経営判断（BusinessDecision）は STEP3 の出力に入ります。以前は独立した段で
# したが、その段はタグ付きの10章レポートも書いていて、次の段がそれを散文に
# 書き直していました。**同じ内容を2回書いていた**わけで、レポート1本ぶんの
# 時間の1/3がそこに消えていました。判断は分析と同じ段で下し、書くのは1回です。

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
    """STEP3（需要形成・患者分析と経営判断）の出力（要件 §15〜§17）。

    分析と判断を同じ段で行います。分けていた頃は、判断の段が 10 章のレポートを
    タグ付きで書き、次の段がそれを散文に書き直していました。読み手に届くのは
    後者だけなので、前者は**捨てるために書いていた**ことになります。

    判断を欄のまま持つのは変えていません。散文に溶かすと「誰と競争しないか」が
    抜けても気づけないからです。欄なら空欄が見えます。
    """

    patient_segments: list[PatientSegment] = Field(default_factory=list)
    demand_mechanisms: list[DemandMechanism] = Field(default_factory=list)
    insights: list[DemandInsight] = Field(default_factory=list)
    #: 要件 §17 の答え。ここが埋まらないなら、判断が出せていないということです。
    decision: BusinessDecision
    actions: list[Evidenced] = Field(
        description="次に取るべき具体的な行動", min_length=1)
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

    # 判断は、この段で作った患者層・筋道・横断所見も根拠にできます。同じ段で
    # 作ったものを引けないと、判断のためにもう一度同じことを書く羽目になります。
    own = segment_ids | mechanism_ids | {i.id for i in output.insights}
    decision = output.decision
    for name in ("primary_patients", "secondary_patients", "avoid_competing_on",
                 "acquisition_area", "reason_to_visit", "clinic_model"):
        check(f"decision.{name}", getattr(decision, name).evidence, own)
    for name in ("advantages", "risks"):
        for index, item in enumerate(getattr(decision, name)):
            check(f"decision.{name}[{index}]", item.evidence, own)
    for index, action in enumerate(output.actions):
        check(f"actions[{index}]", action.evidence, own)

    # 売上・患者数・成功確率の予測は、どの段で書かれても落とします。判断を
    # この段に移したので、検査もここに移ります。
    problems.extend(_forbidden_predictions_in(output.model_dump_json()))
    return problems


# ------------------------------------------------------------------ STEP4
#
# STEP3 までは根拠を辿れる形（タグと id）で材料を作ります。それは検算のための
# 形であって、人が読むための形ではありません。[FACT] が20個並んだ文書は、
# 読み手に「自分で要約してください」と言っているのと同じです。
#
# ここで顧客に渡す文書に起こし直します。書き手は開業支援の担当者、読み手は
# 開業を考えている歯科医師。知りたいのは「なぜここか」と「何が要るか」です。
#
# **この段だけが文章を書きます。** タグ付きのレポートを一度書いてから散文に
# 起こし直していた頃は、同じ内容を 2 回生成していました。

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


class Step4Output(BaseModel):
    """STEP4（顧客提出用レポート）の出力。最終段です。"""

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


def verify_step4(output: Step4Output, known_ids: set[str],
                 known_numbers: set[str],
                 required_categories: Sequence[str] = ()) -> list[TraceProblem]:
    """書き直しであって、書き足しではないこと。

    散文にすると、数字はいくらでも滑らかに増やせます。「約5万人」「およそ3倍」
    は、元の数字と少し違っていても文としては通ります。だから、本文に出てくる
    数値が**前の段に実在したもの**かを機械的に確かめます。

    ``known_numbers`` は projection.allowed_numbers が集めた集合です。完全な
    検出ではありませんが、いちばん起きやすい捏造（丸めながらの書き換え）は
    捕まえられます。
    """
    problems: list[TraceProblem] = []

    for where, refs in _report_references(output):
        for ref in refs:
            if ref not in known_ids:
                problems.append(TraceProblem(
                    where=where, problem=f"存在しない根拠を参照しています: {ref!r}"))

    for where, text in _report_prose(output):
        for number in invented_numbers(text, known_numbers):
            problems.append(TraceProblem(
                where=where,
                problem=f"前の段に無い数値です: {number}"))

    # 歯科医院として必ず答えること。**商圏の説明で終わらせないための最低線**
    # です。実測：沼津駅前のレポートは需要の読み分けまでは到達していましたが、
    # ユニット何台・床面積・衛生士何人には触れていませんでした。それは商圏の
    # 話ではなく医院の話なので、商圏データだけを見ていると出てきません。
    covered = {item.category for item in output.support_needed}
    for category in required_categories:
        if category not in covered:
            problems.append(TraceProblem(
                where="support_needed",
                problem=(f"「{category}」について何も書かれていません。"
                         "この商圏で開業するなら何が要るのかを、歯科医院として"
                         "答えてください（ユニット台数・床面積と階数・駐車場・"
                         "歯科衛生士の確保など）")))

    # judgement_note は「これは予測ではない」と書くための欄です。ここまで
    # 検査に含めると、書いてほしい一文で落ちます。
    body = output.model_copy(update={"judgement_note": ""})
    problems.extend(_forbidden_predictions_in(body.model_dump_json()))
    return problems


def _report_references(output: Step4Output) -> list[tuple[str, list[str]]]:
    out = [("verdict", output.verdict.basis)]
    out += [(f"sections[{i}]", s.evidence) for i, s in enumerate(output.sections)]
    out += [(f"support_needed[{i}]", s.evidence)
            for i, s in enumerate(output.support_needed)]
    return out


def _report_prose(output: Step4Output) -> list[tuple[str, str]]:
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


# ===========================================================================
# プレDD レポート（10 章）。
#
# **LLM が書くのは散文だけです。** 数字も表もリスク判定も、DB のデータから
# Python が確定させて渡します。ここで受け取るのは「その事実が何を意味するか」
# の文だけ。仮説を立てて検証させる段はもうありません——**それが本文を
# 見失わせていました。**
#
# スキーマは平らに保ちます（`Schema is too complex.` を実測で踏んでいます）。
# ===========================================================================
class ChapterTakeaway(BaseModel):
    """1 章ぶんの読みどころ。"""

    chapter: str = Field(description="章のキー（trade_area / competition …）")
    takeaway: str = Field(
        description="この章の事実が何を意味するか。1〜2 文。"
                    "**渡した事実にある数字だけ**を使う")


class GrowthHypothesis(BaseModel):
    """成長余地の**仮説**。断定ではありません。"""

    position: str = Field(description="どの方向か")
    why: str = Field(description="渡した事実のどこからそう言えるか")
    caveat: str = Field(
        description="外れるとしたら何が理由か。**空にしないこと**")


class Verdict(BaseModel):
    """総合評価。**開業と承継では読み方が違うので、分けて書きます。**"""

    statement: str = Field(description="この地点をひとことで。2〜3 文")
    for_opening: str = Field(description="これから開業する人にとっての意味。1〜2 文")
    for_acquisition: str = Field(
        description="既存医院を買う人にとっての意味。のれん代の前提に触れる。1〜2 文")
    counterpoint: str = Field(
        description="この評価が外れるとしたら何が理由か。1 文")


class DDReport(BaseModel):
    """プレDD レポートの散文部分。

    **数字を作らないでください。** 渡された事実の束にある数字だけを使います。
    束に無い数字が本文にあれば、それは作られた数字として検算に引っかかります。
    """

    title: str = Field(description="この地点のレポートの表題")
    summary: str = Field(
        description="Executive Summary。3〜5 文。**結論から。**"
                    "開業を考える人にも、買収を考える人にも読める書き方で")
    takeaways: list[ChapterTakeaway] = Field(
        default_factory=list,
        description="第2章〜第8章それぞれの読みどころ")
    growth_hypotheses: list[GrowthHypothesis] = Field(default_factory=list)
    verdict: Verdict


def verify_dd_report(report: Mapping[str, Any],
                     allowed: set[str]) -> list[str]:
    """本文の数字が、渡した事実の束に実在するか。

    **LLM に数字を作らせないための検算です。** 束に無い数字が本文にあれば、
    それはどこから来たのか誰にも辿れません。

    年号（4 桁）と 1 桁の数は見逃します。「2020年の国勢調査」「3 つの観点」の
    ような文中の数まで拾うと、ほぼ全文が引っかかって使い物になりません。
    """
    import re

    problems: list[str] = []
    texts = [str(report.get("summary") or "")]
    texts += [str(t.get("takeaway") or "") for t in report.get("takeaways") or []]
    texts += [str(h.get(k) or "") for h in report.get("growth_hypotheses") or []
              for k in ("why", "caveat")]
    verdict = report.get("verdict") or {}
    texts += [str(verdict.get(k) or "")
              for k in ("statement", "for_opening", "for_acquisition")]

    for text in texts:
        for raw in re.findall(r"\d[\d,]*\.?\d*", text):
            token = raw.replace(",", "")
            if len(token) <= 1 or re.fullmatch(r"(19|20)\d\d", token):
                continue
            if token in allowed or _trimmed(token) in allowed:
                continue
            problems.append(f"本文の数値 {raw} は、渡した事実の中にありません")
    return problems


def _trimmed(token: str) -> str:
    """末尾の 0 を落とす。**小数点があるときだけ。**

    「33.70」と「33.7」は同じ数ですが、「45000」と「45」は違う数です。
    整数から 0 を削ると、**丸い数ほど何かに当たるようになります** ——
    実測：作られた 45,000 も 1,200,000 も 3,400 も、削られて 45 / 12 / 34 に
    なり、束のどれかに当たって素通りしました。作られた数字は丸いことが多い
    ので、これはいちばん通してはいけない抜け方でした。
    """
    if "." not in token:
        return token
    return token.rstrip("0").rstrip(".")


# ===========================================================================
# 提言レポート（第II部）。**ここでは推論します。**
#
# 第I部（プレDD）は事実だけで、推論を禁じています。この文書は逆で、
# 「ここで開業するならどうするか」を組み立てます。**混ぜないのが要点**で、
# 混ぜると第I部が「どこまでが確定か分からない文書」になります。
#
# スキーマは平らに保ちます。1〜3 章（主要／準主要／競争しない患者層）を
# 3 本の配列に分けず、`role` を持つ 1 本にしているのはそのためです
# （`Schema is too complex.` を実測で踏んでいます）。
# ===========================================================================
class AdviceSegment(BaseModel):
    """患者層の見立て。**想像で作らず、データから推論します。**"""

    role: str = Field(
        description="primary（主要）/ secondary（準主要）/ avoid（競争しない）")
    label: str = Field(description="どんな層か。「0〜14歳の子を持つ世帯」のように")
    basis: str = Field(
        description="**どのデータから**そう言えるか。2 つ以上を組み合わせること。"
                    "「年少人口比が高い」だけでなく「＋世帯あたり人員が多い」まで")
    why: str = Field(description="その層がこの地域に存在する背景（[WHY] または [HYPOTHESIS]）")
    caution: str = Field(description="この見立てが外れるとしたら何が理由か")


class AdviceCatchment(BaseModel):
    """第1商圏・第2商圏。**距離だけでなく、動線で定義します。**"""

    rank: str = Field(description="primary（第1商圏）/ secondary（第2商圏）")
    extent: str = Field(description="範囲。半径だけでなく方向・動線で")
    basis: str = Field(description="そう引いた根拠となるデータ")
    expectation: str = Field(description="この範囲から何を期待するか")


class AdviceEvidence(BaseModel):
    """推論の 1 段。**印で、確定と推測を字面で分けます。**"""

    tag: str = Field(
        description="FACT / BENCHMARK / PATTERN / WHY / HYPOTHESIS / "
                    "INSIGHT / IMPLICATION / ACTION のいずれか")
    statement: str = Field(description="その段で言えること。1〜2 文")
    source: str = Field(
        default="",
        description="FACT/BENCHMARK ならデータの出どころ、WHY なら参照した URL。"
                    "推測なら空でよい")


class AdviceReport(BaseModel):
    """この商圏で歯科医院を開業すると仮定した提言。

    **競合の情報が足りないときに「競合が少ない」「競合が弱い」と書かないこと。**
    情報不足そのものを分析結果として書きます。
    """

    title: str = Field(description="提言レポートの表題")
    reasoning: list[AdviceEvidence] = Field(
        default_factory=list,
        description="FACT → BENCHMARK → PATTERN → WHY → INSIGHT → IMPLICATION "
                    "→ ACTION の連鎖。**この順に並べること**")
    segments: list[AdviceSegment] = Field(
        default_factory=list, description="第1〜3章。role で区別する")
    catchments: list[AdviceCatchment] = Field(
        default_factory=list, description="第4〜5章")
    reason_to_visit: str = Field(description="第6章。なぜ他院ではなくここに来るのか")
    clinic_model: str = Field(description="第7章。規模・ユニット数・診療時間・体制")
    differentiation: str = Field(description="第8章。周辺と何で違うと言えるか")
    opening_risks: list[str] = Field(
        default_factory=list, description="第9章。開業そのものの主要リスク")
    before_opening: list[str] = Field(
        default_factory=list, description="第10章。開業前に追加取得すべき情報")
    information_gaps: str = Field(
        default="",
        description="**競合について情報が足りない場合、その事実をここに書く。**"
                    "足りないことを「競合が少ない」と読み替えないこと")
