"""統計を「値」から「自己記述する測定値」に変える層。

商圏の集計そのものは kg_analyze_point が返します。ここがやるのは、その 1 つ
1 つに「で、それは高いのか低いのか」を付けることです。

    7,331 人

だけを渡された読み手にできるのは、7,331 という数を言い換えることだけです。

    0〜14歳人口 7,331 人 / 東京都の 1km 商圏 5,448 件の中央値は 3,137 人 /
    上位 6%（327 位）/ 2015→2020 で +4.3%（都の中央値は +1.2%）/
    2020年 国勢調査

を渡された読み手は、どこを調べる価値があるかを言えます。同じ数字です。
違うのは、比較相手が付いているかどうかだけです。

設計の要点が 3 つあります。

**比較相手は 1 つではありません。** 都道府県のメッシュ分布・同じ市区町村・
同規模の商圏の 3 つを、計算できたものだけ返します。銀座の人口は東京都の中では
下位でも、同じ中央区の中では、また違う位置にあります。どれと比べたのかを
書かずに「上位 6%」とだけ言うと、それはもう統計ではありません。

**計算できないものは書きません。** 全国のメッシュ統計は読み込んでいないので、
全国比較は返しません。「不明」を「平均並み」に丸めるのがいちばん危険で、
いちばん気づかれにくい誤りです。欠けている比較は理由付きで data_quality に出ます。

**significance は閾値です。** 「極めて高い」は percentile の閾値で決まり、
その閾値は下に書いてあって定義にも入ります。語感で決めていません。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil
from typing import Any, Mapping, Sequence

import psycopg

from bisect import bisect_right

from kaigyou_core.db import (
    column_exists, columns_that_exist, table_exists)
from kaigyou_core.scoring import DEFAULT_FACILITY_CATEGORY

#: percentile（その値以下のメッシュの割合）から言葉を決める閾値。
#: 定義にも出すので、読み手は「極めて高い」が何を意味するか確かめられます。
SIGNIFICANCE_BANDS: tuple[tuple[float, str, str], ...] = (
    (95.0, "very_high", "極めて高い（上位5%以内）"),
    (80.0, "high", "高い（上位20%以内）"),
    (20.0, "typical", "周辺と同程度（上位20%〜下位20%）"),
    (5.0, "low", "低い（下位20%以内）"),
    (0.0, "very_low", "極めて低い（下位5%以内）"),
)

#: 中央値からこの割合以内なら direction は typical。percentile ではなく比で
#: 見るのは、分布が平坦な指標で 1 位と 100 位の実数がほとんど変わらないことが
#: あるためです。
DIRECTION_TOLERANCE = 0.10

#: 「同規模の商圏」の幅。この地点の人口の ±この割合。
SIMILAR_POPULATION_BAND = 0.20

#: 比較相手が少なすぎるときは返しません。10 件の中の「上位 10%」は 1 位という
#: だけのことで、分布ではありません。
MIN_BENCHMARK_SAMPLE = 30


def significance_for(percentile: float | None) -> tuple[str | None, str | None]:
    if percentile is None:
        return None, None
    for threshold, key, label in SIGNIFICANCE_BANDS:
        if percentile >= threshold:
            return key, label
    return "very_low", "極めて低い（下位5%以内）"


@dataclass
class Benchmark:
    """1 つの比較相手に対する位置。"""

    type: str
    label: str
    value: float | None                 # 比較相手の代表値（中央値）
    comparison: str                     # 何を代表値としたか
    sample_count: int
    percentile: float | None = None     # その値以下のメッシュの割合（%）
    top_share_pct: float | None = None  # 上位何%か（= 100 - percentile）
    #: そのまま日本語にできる位置。上半分なら「上位x%」、下半分なら「下位x%」。
    #: top_share_pct だけを渡すと、低い値が「上位94%」と書かれます。数としては
    #: 正しく、文としては逆の意味に読めます。
    position_label: str | None = None
    rank: int | None = None
    of: int | None = None
    p25: float | None = None
    p75: float | None = None
    direction: str | None = None
    direction_label: str | None = None
    significance: str | None = None
    significance_label: str | None = None
    #: この母集団で上位・下位を語れるか。語れないときは significance を出しません。
    discriminating: bool = True
    significance_withheld_reason: str | None = None

    def as_dict(self, *, include_label: bool = False) -> dict[str, Any]:
        """比較 1 件ぶん。

        ``label`` と ``sample_count`` は既定で入りません。比較対象の説明文は
        指標ごとに変わらないので、17 指標 × 3 対象ぶん同じ日本語を並べると
        文書の 5 分の 1 がその繰り返しになります。説明は measures の
        ``benchmark_scopes`` に 1 回だけ置き、ここは type で参照します。
        """
        out = {
            "benchmark_type": self.type,
            "benchmark_value": self.value,
            "benchmark_comparison": self.comparison,
            "percentile": self.percentile,
            "top_share_pct": self.top_share_pct,
            "position_label": self.position_label,
            "rank": self.rank,
            "of": self.of,
            "p25": self.p25,
            "p75": self.p75,
            "direction": self.direction,
            "direction_label": self.direction_label,
            "significance": self.significance,
            "significance_label": self.significance_label,
            "discriminating": self.discriminating,
        }
        if self.significance_withheld_reason:
            out["significance_withheld_reason"] = self.significance_withheld_reason
        if include_label:
            out["benchmark_label"] = self.label
            out["benchmark_sample_count"] = self.sample_count
        return out


@dataclass
class Measure:
    """1 つの統計と、それを読むために要るもの全部。

    平坦な ``benchmark_value`` / ``percentile`` / ``rank`` / ``direction`` /
    ``significance`` は ``benchmarks[0]``（既定は都道府県）の写しです。比較相手が
    複数あるので入れ子が正しい形ですが、1 つだけ読みたい読み手に毎回
    ``benchmarks`` を辿らせるのは、そこを読み飛ばさせるのと同じことなので、
    代表値は上に出しておきます。
    """

    key: str
    label: str
    value: float | int | None
    unit: str
    source: str
    #: どの層の数字か（LAYERS のキー）。層を跨いだ組み合わせだけが構造を
    #: 見せるので、PATTERN の検算がここを読みます。
    layer: str = "residents"
    data_year: int | None = None
    definition: str | None = None
    #: 値が大きいことが何を意味するか。competition 系は「多い＝競合が多い」
    #: とは限らないので、良し悪しではなく事実として書きます。
    higher_means: str | None = None
    benchmarks: list[Benchmark] = field(default_factory=list)
    growth: dict[str, Any] | None = None
    unavailable_reason: str | None = None

    def primary(self) -> Benchmark | None:
        return self.benchmarks[0] if self.benchmarks else None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "layer": self.layer,
            "value": self.value,
            "unit": self.unit,
            "data_year": self.data_year,
            "source": self.source,
            "definition": self.definition,
            "higher_means": self.higher_means,
        }
        primary = self.primary()
        if primary is not None:
            out.update(primary.as_dict(include_label=True))
        else:
            out.update({
                "benchmark_type": None, "benchmark_value": None,
                "percentile": None, "top_share_pct": None, "rank": None,
                "of": None, "direction": None, "significance": None,
            })
            out["benchmark_unavailable_reason"] = (
                self.unavailable_reason or "比較できるメッシュ分布がありません")
        out["benchmarks"] = [b.as_dict() for b in self.benchmarks]
        out["growth"] = self.growth
        return out


# ---------------------------------------------------------------------------
# 指標の定義。
#
# `column` は mesh_scores の列か、その列から作る式。商圏の集計（kg_analyze_point）
# と、比較相手のメッシュ分布は、同じ量でなければ比べられません。だから両方を
# ここに 1 か所で書きます。片方だけ直すと、単位の違うもの同士を比べたまま
# それらしい percentile が出ます。
# ---------------------------------------------------------------------------

CENSUS = "総務省統計局 国勢調査（e-Stat 統計GIS）"
ECONOMIC_CENSUS = "総務省統計局・経済産業省 経済センサス（e-Stat 統計GIS）"
MHLW = "厚生労働省 医療機能情報提供制度（医療情報ネット）"
MLIT_STATIONS = "国土交通省 国土数値情報 S12（駅別乗降客数）"
MLIT_LAND = "国土交通省 国土数値情報 L01（地価公示）"

#: (指標キー) -> 定義。`metric` は kg_analyze_point の返す名前。
#: 数字の「層」。**別々の層を掛け合わせたときにだけ、構造が見えます。**
#:
#: 実測：レポートが「人口が市内2位」「医院数も市内2位」と並べて終わっていま
#: した。どちらも同じ商圏の大きさを別の言葉で言っただけで、掛けても何も出て
#: きません。「高齢化は進むのに日曜に開けている医院は2割」なら、そこには
#: 埋まっていない需要があるかもしれない、という話になります。
#:
#: 層はモデルに申告させません。指標ごとにここで決めて、PATTERN が跨いだか
#: どうかは引かれた指標から機械的に数えます（schemas.verify_step1）。
#:
#: ``competition`` と ``competition_offer`` を分けているのは、「何院あるか」と
#: 「その医院が何を、いつ提供しているか」が別の話だからです。医院数と1院
#: あたり人口を掛けても、それは同じ数の割り算でしかありません。
LAYERS: dict[str, str] = {
    "residents": "人口動態（誰が住んでいるか）",
    "economy": "産業・雇用（どんな経済か）",
    "competition": "競合の数（何院あるか）",
    "competition_offer": "競合の提供体制（何を・いつ・いつから）",
    "access": "交通・立地",
    "cost": "地価・コスト",
    "future": "将来推計",
    "regulation": "都市計画（何を建ててよいか）",
}


MEASURE_SPECS: dict[str, dict[str, Any]] = {
    "population": {
        "layer": "residents",
        "label": "商圏人口（常住）", "unit": "人", "metric": "population",
        "column": "ms.population", "source": CENSUS, "year": 2020,
        "definition": "常住人口（夜間人口）。500mメッシュを商圏との面積按分で合算。",
        "higher_means": "住んでいる人が多い",
        "growth_metric": "population_growth",
    },
    "child_population": {
        "layer": "residents",
        "label": "0〜14歳人口", "unit": "人", "metric": "age_0_14",
        "column": "ms.age_0_14", "source": CENSUS, "year": 2020,
        "definition": "0〜14歳の常住人口。小児歯科の需要側の代理指標。",
        "higher_means": "子どもが多い",
    },
    "working_age_population": {
        "layer": "residents",
        "label": "15〜64歳人口", "unit": "人", "metric": "age_15_64",
        "column": "ms.age_15_64", "source": CENSUS, "year": 2020,
        "definition": "15〜64歳の常住人口。",
        "higher_means": "生産年齢人口が多い",
    },
    "elderly_population": {
        "layer": "residents",
        "label": "65歳以上人口", "unit": "人", "metric": "age_65_plus",
        "column": "ms.age_65_plus", "source": CENSUS, "year": 2020,
        "definition": "65歳以上の常住人口。",
        "higher_means": "高齢者が多い",
    },
    "child_share": {
        "layer": "residents",
        "label": "0〜14歳の割合", "unit": "%", "metric": "age_0_14",
        "column": "100.0 * ms.age_0_14 / NULLIF(ms.population, 0)",
        "ratio_of": "population", "scale": 100.0,
        "source": CENSUS, "year": 2020,
        "definition": "常住人口に占める0〜14歳の割合。人口の多さと子どもの多さは別。",
        "higher_means": "子どもの構成比が高い",
    },
    "elderly_share": {
        "layer": "residents",
        "label": "65歳以上の割合", "unit": "%", "metric": "age_65_plus",
        "column": "100.0 * ms.age_65_plus / NULLIF(ms.population, 0)",
        "ratio_of": "population", "scale": 100.0,
        "source": CENSUS, "year": 2020,
        "definition": "常住人口に占める65歳以上の割合。",
        "higher_means": "高齢化が進んでいる",
    },
    "households": {
        "layer": "residents",
        "label": "世帯数", "unit": "世帯", "metric": "households",
        "column": "ms.households", "source": CENSUS, "year": 2020,
        "definition": "商圏内の世帯数。",
        "higher_means": "世帯が多い",
    },
    "population_growth": {
        "layer": "residents",
        "label": "人口増減率", "unit": "%", "metric": "population_growth",
        "column": "100.0 * ms.population_growth", "scale": 100.0,
        "source": "総務省統計局 国勢調査（2015年・2020年の差）", "year": 2020,
        "definition": "2015年→2020年の人口増減率。人口で加重平均。直近の動向とは異なる場合がある。",
        "higher_means": "人口が増えている",
    },
    "workers": {
        "layer": "economy",
        "label": "従業者数（昼）", "unit": "人", "metric": "workers",
        "column": "ms.workers", "source": ECONOMIC_CENSUS, "year": 2021,
        "definition": ("従業地ベースの従業者数。そこで働く人の数であり、昼間人口ではない"
                       "（通学者・来街者を含まない）。常住人口と足すと通勤者を二重に数える。"),
        "higher_means": "働きに来ている人が多い",
    },
    "establishments": {
        "layer": "economy",
        "label": "事業所数", "unit": "事業所", "metric": "establishments",
        "column": "ms.establishments", "source": ECONOMIC_CENSUS, "year": 2021,
        "definition": "商圏内の事業所数。",
        "higher_means": "事業所が多い",
    },
    "dental_clinics": {
        "layer": "competition",
        "label": "歯科医院数", "unit": "院", "metric": "facility_count",
        "column": "ms.facility_count", "source": MHLW, "year": 2026,
        "definition": "商圏内の歯科診療所の数（標榜科目を問わない）。",
        "higher_means": "競合が多い",
    },
    "population_per_clinic": {
        "layer": "competition",
        "label": "歯科医院1院あたり人口", "unit": "人/院",
        "metric": "population_per_facility",
        "column": "ms.population_per_facility",
        "source": f"{CENSUS} × {MHLW}", "year": 2026,
        "definition": "商圏人口 ÷ 歯科医院数。需給の粗い代理指標。",
        "higher_means": "1院あたりの人口が多い（＝相対的に競合が少ない）",
    },
    "clinics_per_10k": {
        "layer": "competition",
        "label": "人口1万人あたり歯科医院数", "unit": "院/万人",
        "metric": "facility_count",
        "column": "10000.0 * ms.facility_count / NULLIF(ms.population, 0)",
        "ratio_of": "population", "scale": 10000.0,
        "source": f"{CENSUS} × {MHLW}", "year": 2026,
        "definition": "歯科医院数 ÷ 商圏人口 × 10,000。人口規模の違う商圏を並べるため。",
        "higher_means": "人口あたりの歯科医院が多い（＝相対的に競合が多い）",
    },
    "station_distance_m": {
        "layer": "access",
        "label": "最寄り駅までの距離", "unit": "m", "metric": "station_distance_m",
        "column": "ms.station_distance_m", "source": MLIT_STATIONS, "year": 2025,
        "definition": "最寄り駅までの直線距離。徒歩経路の距離ではない。",
        "higher_means": "駅から遠い",
    },
    "daily_passengers": {
        "layer": "access",
        "label": "最寄り駅の乗降客数", "unit": "人/日", "metric": "daily_passengers",
        "column": "ms.daily_passengers", "source": MLIT_STATIONS, "year": 2024,
        "definition": "最寄り駅の1日あたり乗降客数。同一駅を複数事業者が使う場合は代表値。",
        "higher_means": "最寄り駅の利用者が多い",
    },
    "land_price_yen_per_sqm": {
        "layer": "cost",
        "label": "地価（公示・中央値）", "unit": "円/m²",
        "metric": "land_price_yen_per_sqm",
        "column": "ms.land_price_yen_per_sqm", "source": MLIT_LAND, "year": 2026,
        "definition": ("商圏内の地価公示標準地の中央値。土地1m²の価格であり、"
                       "賃料でも初期投資額でもない。"),
        "higher_means": "土地が高い（コストが高い可能性）",
    },
}

#: 標榜科目で絞ったときだけ値が入る指標。`{label}` に科目名が入ります。
#:
#: 科目を絞っていない呼び出しでも、この指標は値 None のまま返します。黙って
#: 落とすと、読み手には「調べたうえで該当なし」に見えるためです。gaps に
#: requires の文言が出て、「未確認」であることと、どう呼べば取れるかが残ります。
SPECIALTY_SPECS: dict[str, dict[str, Any]] = {
    "specialty_clinics": {
        "layer": "competition_offer",
        "label": "{label}を標榜する歯科医院数", "unit": "院",
        "metric": "facility_specialty_count",
        "column": "ms.facility_specialty_count", "source": MHLW, "year": 2026,
        "definition": "商圏内で{label}を標榜している歯科医院の数。届出値。",
        "higher_means": "その科目の競合が多い",
        "requires": ("標榜科目を絞ったプロファイル（profile=pediatric / orthodontics）"
                     "で呼ぶと取得できます。この呼び出しでは供給側は未確認です。"),
    },
}

#: 科目を絞らずに呼ばれたときの、指標そのものの名前。
UNSCOPED_SPECIALTY_LABEL = "標榜科目別の歯科医院数"


def measure_value(spec: Mapping[str, Any], metrics: Mapping[str, Any]) -> float | None:
    """商圏の集計から、その指標の値を作る。

    比較相手の式（``column``）と同じ量になっていることが肝心です。片方が割合で
    片方が実数なら、percentile はそれらしい数字を返しつづけます。
    """
    raw = metrics.get(spec["metric"])
    if raw is None:
        return None
    denominator_key = spec.get("ratio_of")
    if denominator_key:
        denominator = metrics.get(MEASURE_SPECS[denominator_key]["metric"])
        if not denominator:
            return None
        return float(spec.get("scale", 1.0)) * float(raw) / float(denominator)
    factor = spec.get("scale")
    return float(raw) * float(factor) if factor else float(raw)


@dataclass
class BenchmarkScope:
    """比較する母集団ひとつぶん。

    ``discriminating`` は「この集合で上位・下位を語れるか」。県全域のように
    大半が無人のメッシュで埋まっている集合では、町の中心はどこでも上位数%に
    入るので、パーセンタイルは「市街地かどうか」しか測っていません。
    """

    type: str
    label: str
    where: str
    params: tuple[Any, ...]
    sample_count: int = 0
    share_below_viable_floor: float | None = None
    discriminating: bool = True
    not_discriminating_reason: str | None = None


#: `mesh_scores.facility_category` があるか。**デプロイとマイグレーションの
#: 間の窓のためです。** コードは push で即デプロイされますが、マイグレーションは
#: 手で当てます。存在しない列を SELECT すると分析全体が 500 になります
#: （実際に静岡で起きました）。列が無いときは業態で絞らずに読みます——当時は
#: どれも歯科なので、絞らないことが正しい答えになります。
def _scores_scoped_by_category(conn: psycopg.Connection) -> bool:
    return column_exists(conn, "mesh_scores", "facility_category")


def _category_filter(conn: psycopg.Connection, facility_category: str,
                     ) -> tuple[str, tuple[Any, ...]]:
    """業態で絞る WHERE 断片。列が無い環境では空を返します。"""
    if _scores_scoped_by_category(conn):
        return "ms.facility_category = %s AND ", (facility_category,)
    return "", ()


def viable_floor(conn: psycopg.Connection, *, profile: str, radius_m: int,
                 facility_category: str = DEFAULT_FACILITY_CATEGORY,
                 prefecture_code: str, percentile: float) -> float | None:
    """その県で開業が成立している商圏人口の下限。実測値であって、決め打ちではない。

    県内で**実際に歯科医院がある**商圏の人口の下位パーセンタイル点を使います。
    誰も山の中では開業していないので、この値は「その県でこの規模の商圏に
    診療所が成立している」の実測下限になります。恣意的な閾値を置かずに、
    無人メッシュと生活圏を分けられます。（東京では9,636人）
    """
    category, params = _category_filter(conn, facility_category)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT percentile_cont(%s) WITHIN GROUP (ORDER BY ms.population) AS floor,
                   count(*) AS n
            FROM mesh_scores ms
            JOIN population_mesh pm ON pm.id = ms.mesh_id
            WHERE ms.profile = %s AND ms.radius_m = %s AND {category}
                  pm.prefecture_code = %s AND ms.facility_count > 0
              AND ms.population IS NOT NULL
            """,
            (percentile, profile, radius_m) + params + (prefecture_code,))
        row = cur.fetchone()
    if not row or not row["n"] or row["floor"] is None:
        return None
    return float(row["floor"])


def benchmark_scopes(*, prefecture_code: str, prefecture_label: str,
                     municipality: str | None, population: float | None,
                     radius_m: int, lat: float, lng: float,
                     config: Mapping[str, Any],
                     viable_floor_population: float | None = None,
                     neighbours: Sequence[str] | None = None) -> list[BenchmarkScope]:
    """比較できる母集団を、計算できるものだけ組み立てる。

    全国は入りません。全国のメッシュ統計を読み込んでいないからで、都のメッシュを
    全国と言い換えることはできません。欠けていることは data_quality に出ます。
    """
    nearby_m = float(config.get("nearby_radius_m", 10000))
    station_m = float(config.get("station_front_m", 800))

    scopes = [
        BenchmarkScope(
            "prefecture", f"{prefecture_label}の半径{radius_m}m商圏（全メッシュ）",
            "pm.prefecture_code = %s", (prefecture_code,)),
        # 実績による絞り込み。閾値をひとつも置かずに山林を外せる。
        #
        # ただしこれだけでは足りません。山あいの集落に1院あるだけの商圏も
        # 「歯科医院が実在する商圏」です。静岡では、この母集団の53%が
        # 生活圏の人口下限を下回っていて、医院数の中央値は2院でした。
        # 「9院は中央値の4.5倍」は、市街地と農村を混ぜた比較です。
        BenchmarkScope(
            "with_clinics", f"{prefecture_label}内で歯科医院が実在する商圏",
            "pm.prefecture_code = %s AND ms.facility_count > 0", (prefecture_code,)),
        # 実際に選びうる代替地。「県内で上位」ではなく「この辺で上位」を答える。
        BenchmarkScope(
            "nearby", f"この地点から半径{nearby_m / 1000:g}km以内の商圏",
            "pm.prefecture_code = %s AND ST_DWithin(pm.centroid::geography, "
            "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)",
            (prefecture_code, lng, lat, nearby_m)),
        # 駅前どうし。駅前は駅前と比べるのが筋で、田畑と比べても何も分からない。
        BenchmarkScope(
            "station_front", f"{prefecture_label}内の駅から{station_m:g}m以内の商圏",
            "pm.prefecture_code = %s AND EXISTS (SELECT 1 FROM stations s "
            "WHERE ST_DWithin(pm.centroid::geography, s.geom::geography, %s))",
            (prefecture_code, station_m)),
    ]
    if viable_floor_population:
        # 市街地どうしの比較。閾値は決め打ちではなく、その県で歯科医院が
        # 成立している商圏人口の実測下限です。ここを入れると、農村部の
        # 「1院しかない商圏」が母集団から外れ、医院数の中央値が市街地の
        # 実態に寄ります。
        scopes.append(BenchmarkScope(
            "urban",
            f"{prefecture_label}内で人口が生活圏規模に達する商圏"
            f"（{viable_floor_population:,.0f}人以上）",
            "pm.prefecture_code = %s AND ms.population >= %s",
            (prefecture_code, viable_floor_population)))
    if municipality:
        scopes.append(BenchmarkScope(
            "municipality", f"{municipality}内の同条件の商圏",
            "pm.prefecture_code = %s AND ms.area_label = %s",
            (prefecture_code, municipality)))
        # 市区町村を1つだけ見た順位は、その市区町村の大きさで意味が変わります。
        # 小さい市なら市街地はどこでも上位に入り、「市内2位」は開業地を選ぶ
        # 判断に効きません。**隣接市町を足した母集団**なら、実際に検討して
        # いる候補地どうしの比較になります。裾野市なら三島市・長泉町・
        # 御殿場市・沼津市が入り、これは読み手が自分で並べている範囲です。
        #
        # 名前を label に並べます。「近隣」とだけ書くと、どこまでを近隣と
        # 言っているのかが読み手に分かりません。
        area = [municipality] + [n for n in (neighbours or []) if n != municipality]
        if len(area) > 1:
            scopes.append(BenchmarkScope(
                "neighbourhood", "・".join(area) + " の同条件の商圏",
                "pm.prefecture_code = %s AND ms.area_label = ANY(%s)",
                (prefecture_code, area)))
    if population and population > 0:
        low = population * (1 - SIMILAR_POPULATION_BAND)
        high = population * (1 + SIMILAR_POPULATION_BAND)
        scopes.append(BenchmarkScope(
            "similar_population",
            f"商圏人口が同規模（{low:,.0f}〜{high:,.0f}人）の商圏",
            "pm.prefecture_code = %s AND ms.population BETWEEN %s AND %s",
            (prefecture_code, low, high)))
    return scopes


def measure_scope_shape(conn: psycopg.Connection, scope: BenchmarkScope, *,
                        profile: str, radius_m: int,
                        facility_category: str = DEFAULT_FACILITY_CATEGORY, floor: float | None,
                        max_share_below: float, min_sample: int) -> None:
    """母集団の大きさと、そのうち生活圏の下限を下回る割合を測る。

    これが「県内上位4.5%」を止める仕掛けです。母集団の大半が無人なら、
    町の中心はどこでも上位に来ます。その集合では順位は出しても
    「極めて高い」という評価は付けません。評価が付かなければ、
    「本商圏の最大のエンジン」という文はそもそも書けません。
    """
    category, params = _category_filter(conn, facility_category)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT count(*)::int AS n,
                   count(*) FILTER (WHERE ms.population < %s)::int AS below
            FROM mesh_scores ms
            JOIN population_mesh pm ON pm.id = ms.mesh_id
            WHERE ms.profile = %s AND ms.radius_m = %s AND {category}{scope.where}
            """,
            (floor if floor is not None else -1.0, profile, radius_m)
            + params + scope.params)
        row = cur.fetchone()

    scope.sample_count = int(row["n"] or 0)
    if scope.sample_count < min_sample:
        scope.discriminating = False
        scope.not_discriminating_reason = (
            f"比較できる商圏が{scope.sample_count}件しかないため（{min_sample}件以上で算出）")
        return
    if floor is None:
        return
    share = row["below"] / scope.sample_count
    scope.share_below_viable_floor = round(share, 3)
    if share > max_share_below:
        scope.discriminating = False
        scope.not_discriminating_reason = (
            f"この母集団の{share * 100:.0f}%が、県内で歯科医院が成立している"
            f"人口規模の下限（{floor:,.0f}人）を下回ります。"
            f"市街地であればどこでも上位に入るため、順位は出しますが"
            f"「高い・低い」の評価は付けません。")


def _direction(value: float, median: float | None) -> tuple[str, str]:
    if median is None or median == 0:
        return "unknown", "比較できません"
    ratio = value / median
    if ratio > 1 + DIRECTION_TOLERANCE:
        return "high", "比較対象の中央値より高い"
    if ratio < 1 - DIRECTION_TOLERANCE:
        return "low", "比較対象の中央値より低い"
    return "typical", "比較対象の中央値と同程度"


def build_measures(conn: psycopg.Connection, metrics: Mapping[str, Any], *,
                   profile: str, radius_m: int, prefecture_code: str,
                   prefecture_label: str, municipality: str | None,
                   lat: float, lng: float,
                   facility_category: str = DEFAULT_FACILITY_CATEGORY,
                   neighbours: Sequence[str] | None = None,
                   specialty: str | None = None,
                   specialty_label: str | None = None,
                   config: Mapping[str, Any] | None = None,
                   ) -> tuple[list[Measure], list[str], dict[str, Any]]:
    """全指標を、比較相手つきで組み立てる。

    比較は mesh_scores に対して行います。同じ半径・同じ商圏の形・同じ情報源で
    採点済みの全メッシュなので、同じ量どうしの比較になります。

    母集団はひとつではありません。県全域は、人口の少ない県では「市街地かどうか」
    しか測らなくなるので、そのときは弁別力のある別の母集団を代表に選び、
    選んだ理由を返します。返り値は (指標, 算出できなかった比較の理由, 代表母集団の説明)。
    """
    specs: dict[str, dict[str, Any]] = dict(MEASURE_SPECS)
    for key, spec in SPECIALTY_SPECS.items():
        filled = dict(spec)
        if specialty:
            name = specialty_label or specialty
            filled["label"] = spec["label"].format(label=name)
            filled["definition"] = spec["definition"].format(label=name)
            filled.pop("requires", None)
        else:
            filled["label"] = UNSCOPED_SPECIALTY_LABEL
            filled["definition"] = spec["definition"].format(label="指定した標榜科目")
        specs[key] = filled

    config = config or {}
    preference = [str(x) for x in (config.get("preference") or ["prefecture"])]
    min_sample = int(config.get("min_sample", MIN_BENCHMARK_SAMPLE))
    max_share = float(config.get("max_share_below_viable_floor", 0.5))

    notes: list[str] = []
    primary_info: dict[str, Any] = {}
    if not table_exists(conn, "mesh_scores"):
        notes.append("メッシュスコアが未計算のため、すべての比較（percentile・順位）を"
                     "算出できません。kaigyou-etl compute-scores を実行してください。")
        return ([_bare(key, spec, metrics) for key, spec in specs.items()],
                notes, primary_info)

    # 列が無い指標は比較を諦めます。デプロイとマイグレーションの間の窓で、
    # 存在しない列を SELECT すると分析全体が落ちるため。
    #
    # **列ごとに訊くと、列の数だけ往復します。** 問いが 14 個あることと、
    # 往復が 14 回必要なことは別です（実測：14 往復 → 1 往復）。
    needed_by_key = {
        key: [c.split(".")[1].rstrip(")") for c in _columns_in(spec["column"])]
        for key, spec in specs.items()}
    present = columns_that_exist(
        conn, "mesh_scores", [c for cs in needed_by_key.values() for c in cs])
    usable = {}
    for key, spec in specs.items():
        if all(c in present for c in needed_by_key[key]):
            usable[key] = spec
        else:
            notes.append(f"{spec['label']}: 比較用の列が未作成のため percentile と"
                         f"順位を算出できません（kaigyou-etl migrate）。")

    floor = viable_floor(conn, profile=profile, radius_m=radius_m,
                         facility_category=facility_category,
                         prefecture_code=prefecture_code,
                         percentile=float(config.get("viable_floor_percentile", 0.10)))
    scopes = benchmark_scopes(
        prefecture_code=prefecture_code, prefecture_label=prefecture_label,
        municipality=municipality, population=metrics.get("population"),
        radius_m=radius_m, lat=lat, lng=lng, config=config,
        viable_floor_population=floor, neighbours=neighbours)
    # **事前計算した分布があれば、母集団を測り直しません。**
    #
    # 実測（銀座1km・手元）：母集団の形を測るのに 14 往復・49ms、商圏の集計
    # そのものは 15ms。「この地点が何人か」より「周りがどうなっているか」の
    # ほうが 3 倍高く、しかも周りはクリックした地点によって変わりません。
    #
    # 無ければその場で測ります。**新しく県を読み込んだ直後に位置づけが出ない
    # より、遅いほうがましです。**
    stored = stored_distributions(
        conn, prefecture_code=prefecture_code, municipality=municipality,
        profile=profile, radius_m=radius_m, facility_category=facility_category)
    live_scopes: list[str] = []
    for scope in scopes:
        if shape_from_stored(scope, stored.get(scope.type) or {}):
            continue
        live_scopes.append(scope.type)
        measure_scope_shape(conn, scope, profile=profile, radius_m=radius_m,
                            facility_category=facility_category,
                            floor=floor, max_share_below=max_share,
                            min_sample=min_sample)

    by_type = {s.type: s for s in scopes}
    if municipality is None:
        notes.append("市区町村の境界データが無いため、同一市区町村内での比較は"
                     "算出していません。")
    notes.append("全国のメッシュ統計を読み込んでいないため、全国比較は算出していません。"
                 "比較はすべて同一都道府県内のメッシュ分布に対するものです。")

    # 代表に使う母集団。設定の順に見て、最初に弁別力のあるもの。
    primary_type = next(
        (name for name in preference
         if name in by_type and by_type[name].discriminating
         and by_type[name].sample_count >= min_sample),
        None)
    skipped = [by_type[name] for name in preference
               if name in by_type and not by_type[name].discriminating]
    fell_back = False
    if primary_type is None:
        # 弁別力のある母集団がひとつも無い。いちばん大きいものを代表にしますが、
        # 弁別力が無いことは変わらないので significance は出ません。
        primary_type = next((s.type for s in scopes if s.sample_count >= min_sample), None)
        fell_back = primary_type is not None
    primary_info = {
        "benchmark_type": primary_type,
        # **どの母集団を事前計算から読み、どれをその場で測ったか。**
        # 書いておかないと、「なぜこの地点だけ遅いのか」に後から答えられません。
        # nearby と similar_population は地点で母集団が変わるので、常にここ。
        "measured_live": live_scopes or None,
        "viable_floor_population": None if floor is None else round(floor),
        "viable_floor_definition": (
            "県内で歯科医院が実在する商圏の人口の下位10%点。"
            "その県で開業が成立している商圏規模の実測下限であり、決め打ちの閾値ではありません。"),
        "reason": None,
        "skipped": [
            {"benchmark_type": s.type, "reason": s.not_discriminating_reason}
            for s in skipped if s.not_discriminating_reason
        ],
    }
    if fell_back and primary_type:
        primary_info["reason"] = (
            f"弁別力のある比較対象がありません。{by_type[primary_type].label}を"
            f"代表にしていますが、順位のみで「高い・低い」の評価は付けていません。")
        notes.append(primary_info["reason"])
    elif skipped and primary_type and primary_type != preference[0]:
        primary_info["reason"] = (
            f"{by_type[skipped[0].type].label}は弁別力がないため、"
            f"{by_type[primary_type].label}を代表の比較対象にしています。")
        notes.append(primary_info["reason"] + " " + (skipped[0].not_discriminating_reason or ""))

    # 代表が先頭に来るように並べ替える。平坦な benchmark_* はこの先頭の写し。
    ordered = ([by_type[primary_type]] if primary_type else []) + \
              [s for s in scopes if s.type != primary_type]

    values = {key: measure_value(spec, metrics) for key, spec in specs.items()}
    results: dict[str, list[Benchmark]] = {key: [] for key in specs}

    for scope in ordered:
        if scope.sample_count < min_sample:
            continue
        saved = stored.get(scope.type) or {}
        if saved:
            # 二分探索。SQL の往復はゼロ。
            rows = statistics_from_stored(usable, values, scope, saved)
        else:
            rows = _scope_statistics(conn, usable, values, profile, radius_m,
                                     scope, facility_category)
        if not rows:
            continue
        for key, bench in rows.items():
            results[key].append(bench)

    measures = []
    for key, spec in specs.items():
        measure = _bare(key, spec, metrics)
        measure.benchmarks = results.get(key, [])
        measures.append(measure)
    _attach_growth(measures)
    return measures, notes, primary_info


def _columns_in(expression: str) -> list[str]:
    return [token for token in expression.replace("(", " ").replace(")", " ")
            .replace(",", " ").split() if token.startswith("ms.")]


def _bare(key: str, spec: Mapping[str, Any], metrics: Mapping[str, Any]) -> Measure:
    value = measure_value(spec, metrics)
    return Measure(
        key=key, label=spec["label"], layer=spec.get("layer", "residents"),
        value=None if value is None else round(value, 4),
        unit=spec["unit"], source=spec["source"], data_year=spec.get("year"),
        definition=spec.get("definition"), higher_means=spec.get("higher_means"),
        # 値が無いときは、無い理由を持たせます。「未確認」と「該当なし」は
        # 読み手にとって別のことなので、区別できないまま渡しません。
        unavailable_reason=(spec.get("requires") if value is None else None),
    )


def _scope_statistics(conn: psycopg.Connection, specs: Mapping[str, Mapping[str, Any]],
                      values: Mapping[str, float | None], profile: str, radius_m: int,
                      scope: BenchmarkScope,
                      facility_category: str = DEFAULT_FACILITY_CATEGORY,
                      ) -> dict[str, Benchmark] | None:
    """1 つの比較対象について、全指標の中央値・四分位・順位を 1 クエリで。

    指標ごとにクエリを投げると、比較対象 3 つ × 指標 16 個で 48 往復になります。
    ホストされたデータベース相手だと、それだけで応答が数秒のびます。
    """
    parts: list[str] = []
    params: list[Any] = []
    active = [(key, spec) for key, spec in specs.items() if values.get(key) is not None]
    if not active:
        return None

    for key, spec in active:
        column = spec["column"]
        # 四分位は配列版で 1 回のソートにまとめます。指標ごとに 3 回呼ぶと、
        # 17 指標 × 6 母集団で 300 回以上ソートすることになり、応答が 10 秒に
        # なりました。同じ ORDER BY を 3 度並べても、答えは 1 度ぶんです。
        parts.append(
            f"percentile_cont(ARRAY[0.25, 0.5, 0.75]) WITHIN GROUP (ORDER BY {column})"
            f" AS {key}_q")
        parts.append(f"count({column}) AS {key}_total")
        parts.append(f"count(*) FILTER (WHERE {column} <= %s) AS {key}_below")
        params.append(values[key])

    category, cat_params = _category_filter(conn, facility_category)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT count(*)::int AS meshes, {', '.join(parts)}
            FROM mesh_scores ms
            JOIN population_mesh pm ON pm.id = ms.mesh_id
            WHERE ms.profile = %s AND ms.radius_m = %s AND {category}{scope.where}
            """,
            params + [profile, radius_m] + list(cat_params) + list(scope.params))
        row = cur.fetchone()

    if not row or (row["meshes"] or 0) < MIN_BENCHMARK_SAMPLE:
        return None

    out: dict[str, Benchmark] = {}
    for key, spec in active:
        total = row.get(f"{key}_total") or 0
        if not total:
            continue
        value = float(values[key])
        quartiles = row.get(f"{key}_q") or [None, None, None]
        median = quartiles[1]
        below = row.get(f"{key}_below") or 0
        percentile = round(100.0 * below / total, 1)
        # 「上位何位か」。同値は同順位（自分より大きいものの数 + 1）。
        rank = total - below + 1
        direction, direction_label = _direction(value, median)
        # 弁別力のない母集団では「極めて高い」を出しません。順位は事実なので
        # 出しますが、評価まで付けると「県内上位4.5%だから最大の強み」という、
        # 母集団の形しか反映していない文が書けてしまいます。
        if scope.discriminating:
            significance, significance_label = significance_for(percentile)
            withheld = None
        else:
            significance, significance_label = None, None
            withheld = scope.not_discriminating_reason
        out[key] = Benchmark(
            type=scope.type, label=scope.label,
            value=None if median is None else round(float(median), 4),
            comparison="median", sample_count=int(row["meshes"]),
            percentile=percentile, top_share_pct=round(100.0 - percentile, 1),
            position_label=_position_label(int(rank), int(total)),
            rank=int(rank), of=int(total),
            p25=_maybe_round(quartiles[0]), p75=_maybe_round(quartiles[2]),
            direction=direction, direction_label=direction_label,
            significance=significance, significance_label=significance_label,
            discriminating=scope.discriminating,
            significance_withheld_reason=withheld,
        )
    return out


# ------------------------------------------------- 事前計算した分布から位置を出す
#
# **地点をクリックしたときに、母集団を測り直しません。**
#
# 実測（銀座1km・手元）：母集団の形を測るのに 14 往復・49ms、商圏の集計そのものは
# 15ms でした。「この地点が何人か」より「周りがどうなっているか」のほうが 3 倍
# 高い。そして周りは、クリックした地点によって変わりません。
#
# 保存してあるのは**昇順に並んだ値**（benchmark_distributions.boundaries）です。
# 任意の値の位置は二分探索で出ます。SQL の往復はゼロ。

def stored_distributions(conn: psycopg.Connection, *, prefecture_code: str,
                         municipality: str | None, profile: str, radius_m: int,
                         facility_category: str = DEFAULT_FACILITY_CATEGORY,
                         ) -> dict[str, dict[str, Any]]:
    """この地点に関係する母集団の分布を、**1 往復で**まとめて読む。

    無ければ空を返します。呼び出し側はその場で測り直します——**新しく県を
    読み込んだ直後に、位置づけが出ないより遅いほうがましです。**
    """
    if not table_exists(conn, "benchmark_distributions"):
        return {}
    keys = [prefecture_code]
    if municipality:
        keys.append(f"{prefecture_code}:{municipality}")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT scope_kind, scope_label, metric, sample_count, value_count,
                   boundaries, is_exact, median, p25, p75, discriminating,
                   not_discriminating_reason, share_below_viable_floor
            FROM benchmark_distributions
            WHERE profile = %s AND radius_m = %s AND facility_category = %s
              AND scope_key = ANY(%s)
            """, (profile, radius_m, facility_category, keys))
        rows = cur.fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        out.setdefault(row["scope_kind"], {})[row["metric"]] = dict(row)
    return out


def shape_from_stored(scope: BenchmarkScope,
                      stored: Mapping[str, Mapping[str, Any]]) -> bool:
    """母集団の大きさと弁別力を、保存済みの値から埋める。

    どの指標の行にも同じ値が入っています（母集団の性質なので）。1 行取れれば
    足ります。埋められなければ False を返し、呼び出し側が測りに行きます。
    """
    any_row = next(iter(stored.values()), None)
    if any_row is None:
        return False
    scope.sample_count = int(any_row["sample_count"])
    scope.discriminating = bool(any_row["discriminating"])
    scope.not_discriminating_reason = any_row["not_discriminating_reason"]
    scope.share_below_viable_floor = any_row["share_below_viable_floor"]
    if any_row["scope_label"]:
        scope.label = any_row["scope_label"]
    return True


def statistics_from_stored(specs: Mapping[str, Mapping[str, Any]],
                           values: Mapping[str, float | None],
                           scope: BenchmarkScope,
                           stored: Mapping[str, Mapping[str, Any]],
                           ) -> dict[str, Benchmark]:
    """保存済みの分布に、この地点の値を当てる。**SQL は投げません。**

    `_scope_statistics` と同じ答えを返します（分布が exact なとき）。違うのは
    往復の回数だけです。
    """
    out: dict[str, Benchmark] = {}
    for key in specs:
        value = values.get(key)
        row = stored.get(key)
        if value is None or row is None:
            continue
        boundaries = row["boundaries"] or []
        total = int(row["value_count"] or 0)
        if not boundaries or not total:
            continue
        value = float(value)
        if row["is_exact"]:
            # 全部の値が入っているので、順位も percentile も厳密。
            below = bisect_right(boundaries, value)
            percentile = round(100.0 * below / total, 1)
            rank = total - below + 1
        else:
            # 分位点の格子。**順位は格子の刻みまでしか分かりません。**
            # 刻みは 0.1 パーセンタイルなので、母集団が 10,000 を超えると
            # 「上位0.05%」のような言い方はできなくなります。
            below_share = bisect_right(boundaries, value) / len(boundaries)
            percentile = round(100.0 * below_share, 1)
            rank = max(1, int(round(total * (1.0 - below_share))) + 1)
        median = row["median"]
        direction, direction_label = _direction(
            value, float(median) if median is not None else None)
        if scope.discriminating:
            significance, significance_label = significance_for(percentile)
            withheld = None
        else:
            significance, significance_label = None, None
            withheld = scope.not_discriminating_reason
        out[key] = Benchmark(
            type=scope.type, label=scope.label,
            value=None if median is None else round(float(median), 4),
            comparison="median", sample_count=scope.sample_count,
            percentile=percentile, top_share_pct=round(100.0 - percentile, 1),
            position_label=_position_label(int(rank), total),
            rank=int(rank), of=total,
            p25=_maybe_round(row["p25"]), p75=_maybe_round(row["p75"]),
            direction=direction, direction_label=direction_label,
            significance=significance, significance_label=significance_label,
            discriminating=scope.discriminating,
            significance_withheld_reason=withheld,
        )
    return out


def _position_label(rank: int, total: int) -> str:
    """「上位6%」か「下位6%」か。近いほうの端から数えます。

    percentile ではなく順位から作ります。percentile は最大値のとき 100 で
    頭打ちになるので、そこから引くと 5,448 件中 1 位が「上位0%」になります。
    順位からなら「上位0.1%」で、これは正しい包含関係の言い方です。

    切り上げているのも同じ理由です。「上位x%」は「上位x%の中に入っている」と
    読まれる文なので、切り捨てると入っていない範囲を名乗ることになります。
    """
    if total <= 0:
        return ""
    top = ceil(1000.0 * rank / total) / 10
    bottom = ceil(1000.0 * (total - rank + 1) / total) / 10
    return f"上位{top:g}%" if top <= bottom else f"下位{bottom:g}%"


def _maybe_round(value: Any) -> float | None:
    return None if value is None else round(float(value), 4)


def _attach_growth(measures: Sequence[Measure]) -> None:
    """人口増減率を、人口系の指標に「その指標の伸び」として添える。

    国勢調査から取れる増減率は人口総数のものだけです。0〜14歳人口の増減率は
    公表メッシュからは作れないので、人口総数の増減率を「参考」として添え、
    どの量の増減かを applies_to に書きます。子どもの増減とは限らないという
    ことが、読み手に伝わっていなければ意味がありません。
    """
    by_key = {m.key: m for m in measures}
    growth = by_key.get("population_growth")
    if growth is None or growth.value is None:
        return
    primary = growth.primary()
    payload = {
        "value": growth.value,
        "unit": "%",
        "period": "2015年→2020年",
        "applies_to": "population",
        "benchmark_value": primary.value if primary else None,
        "benchmark_type": primary.type if primary else None,
        "direction": primary.direction if primary else None,
        "note": ("国勢調査から算出できるのは人口総数の増減率のみです。"
                 "年齢別の増減率ではありません。"),
        "source": growth.source,
    }
    for key in ("population", "child_population", "working_age_population",
                "elderly_population", "households"):
        measure = by_key.get(key)
        if measure is not None and measure.value is not None:
            measure.growth = payload


# ---------------------------------------------------------------------------
# 複合指標。
#
# 単独の統計から出るのは、その統計の言い換えまでです。判断の材料になるのは、
# 需要側・供給側・時間変化を同時に見たときで、そこで初めて「需要側は厚い、
# 供給側は未確認、だから需給ギャップを調べる価値がある」という文が書けます。
#
# ここは結論を出しません。出すのは「同時に見るべき数字がどれで、そのうち何が
# 取れて何が取れなかったか」までです。取れなかったものを黙って落とすと、
# 読み手には「調べたうえで該当なし」に見えます。gaps はそのために返します。
# ---------------------------------------------------------------------------

def scope_summary(measures: Sequence[Measure]) -> list[dict[str, Any]]:
    """使った比較対象を 1 回だけ説明する。

    どれと比べたかを書かずに「上位6%」とだけ言えば、それはもう統計ではないので、
    説明そのものは落とせません。落とせるのは繰り返しのほうです。
    """
    seen: dict[str, dict[str, Any]] = {}
    for measure in measures:
        for bench in measure.benchmarks:
            seen.setdefault(bench.type, {
                "benchmark_type": bench.type,
                "label": bench.label,
                "sample_count": bench.sample_count,
                "comparison": bench.comparison,
                "discriminating": bench.discriminating,
                "not_discriminating_reason": bench.significance_withheld_reason,
            })
    return list(seen.values())


def build_insights(measures: Sequence[Measure],
                   config: Mapping[str, Any]) -> list[dict[str, Any]]:
    by_key = {m.key: m for m in measures}
    out: list[dict[str, Any]] = []

    for key, spec in (config.get("insights") or {}).items():
        components: list[dict[str, Any]] = []
        gaps: list[str] = []

        for name in spec.get("components") or []:
            measure = by_key.get(name)
            if measure is None:
                gaps.append(f"{name}: この商圏では算出していません")
                continue
            if measure.value is None:
                gaps.append(f"{measure.label}: "
                            + (measure.unavailable_reason or "データが無いため未確認"))
                continue
            primary = measure.primary()
            components.append({
                "key": measure.key,
                "label": measure.label,
                "value": measure.value,
                "unit": measure.unit,
                "benchmark_value": primary.value if primary else None,
                "benchmark_type": primary.type if primary else None,
                "percentile": primary.percentile if primary else None,
                "top_share_pct": primary.top_share_pct if primary else None,
                "position_label": primary.position_label if primary else None,
                "rank": primary.rank if primary else None,
                "of": primary.of if primary else None,
                "direction": primary.direction if primary else None,
                "significance": primary.significance if primary else None,
                "higher_means": measure.higher_means,
            })
            if primary is None:
                gaps.append(f"{measure.label}: 値はあるが、比較できる分布が無いため"
                            "周辺との位置づけは未確認")

        out.append({
            "insight_metric": key,
            "label": spec.get("label", key),
            "question": spec.get("question"),
            "components": components,
            "component_count": len(components),
            "components_requested": len(spec.get("components") or []),
            # 揃わなかったもの。「調べたが該当なし」と「そもそも見ていない」を
            # 読み手が区別できるようにするための欄で、この構造のいちばんの働き。
            "gaps": gaps,
            "complete": not gaps,
            "note": spec.get("note"),
        })
    return out


#: データセットの読み方。読み手が最初に当たる場所なので、この文書で何が言えて
#: 何が言えないかをここに置きます。
READING_GUIDE: dict[str, Any] = {
    "start_here": "measures",
    "sections": {
        "measures": ("各統計を、単位・出典・基準年・比較対象・percentile・順位・"
                     "増減とともに 1 件ずつ。まずここを読んでください。"),
        "insight_metrics": ("同時に見るべき指標の組み合わせ。gaps に「何が確認できて"
                            "いないか」が入ります。結論は含みません。"),
        "competition": "商圏内の歯科医院の一覧・標榜科目別の内訳・診療時間。",
        "demand": "人口と従業者数の半径別内訳、商圏内の分布、産業構成。",
        "scores": "設定済みプロファイルごとの暫定スコア。相対値であり予測ではありません。",
        "data_quality": "取得できなかったデータと、算出できなかった比較の一覧。",
        "provenance": "情報源ごとの名称・提供元・データ時点・取得日時。",
    },
    "how_to_read_a_measure": {
        "value": "実数。null は「不明」で、0 は「数えた結果0」。",
        "unit": "単位。",
        "benchmark_value": "比較対象の中央値。",
        "benchmark_type": "何と比べたか。prefecture / municipality / similar_population。",
        "percentile": "その値以下の商圏の割合（%）。94 なら「94%より上」。",
        "top_share_pct": "上位何%か（= 100 - percentile）。6 なら「上位6%」。",
        "position_label": ("そのまま文に使える位置。「上位6%」または「下位6%」。"
                           "低い値を top_share_pct だけで書くと『上位94%』となり、"
                           "数は正しいまま逆の意味に読めるため、この欄を使ってください。"),
        "rank": "比較対象の中での順位。of が母数。",
        "direction": "中央値と比べて high / low / typical。",
        "significance": "percentile の閾値による区分。very_high / high / typical / low / very_low。",
        "higher_means": "値が大きいことが何を意味するか。良し悪しではなく事実。",
        "growth": "その量の増減率。applies_to がどの量の増減かを示します。",
        "data_year": "その統計の基準年。指標ごとに違います。",
    },
    "significance_bands": [
        {"key": key, "min_percentile": threshold, "label": label}
        for threshold, key, label in SIGNIFICANCE_BANDS
    ],
    "cannot_answer": [
        "開業の成否・売上・患者数・家賃の予測は含みません（要件で禁止）。",
        "全国比較は含みません。全国のメッシュ統計を読み込んでいないためです。",
        "スコアは同一都道府県内での相対値です。都道府県をまたいだ比較はできません。",
    ],
}
