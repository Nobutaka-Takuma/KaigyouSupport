"""The provisional scoring model.

Everything numeric here comes from ``config/<業態>/scoring.yaml``; this module only
implements the arithmetic. No weight, threshold or cap is written into the
code, and no attempt is made to predict revenue, patient counts or the odds of
a practice succeeding -- the scores are relative summaries of public statistics
and are labelled as such throughout the UI.

Missing inputs are handled explicitly rather than silently defaulted to zero:
a component whose inputs are unavailable is dropped, the remaining weights are
renormalised, and the affected component names are reported back to the caller
so the UI can say what could not be computed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

# Raw metrics that are normalised against the observed mesh distribution.
DISTRIBUTION_METRICS = (
    "population",
    "age_0_14",
    "age_15_64",
    "age_65_plus",
    "households",
    "population_per_facility",
    # Daytime side: people at their place of work, and the workplaces
    # themselves. Not 昼間人口 -- that also counts students and everyone else
    # who travels in -- but the part of it published per mesh.
    "workers",
    "establishments",
    "workers_per_facility",
    # Cost. Published land price of the catchment (地価公示 median), which is
    # the only geocoded price this project has. Lower is better here, and the
    # cost component inverts it -- see ScoringModel.cost.
    "land_price_yen_per_sqm",
)


#: 科目で絞った指標の名前の区切り。``population_per_facility@pediatric`` の @。
#: 分布は指標ごとに 1 本なので、科目で絞った比率は別の指標として持たないと、
#: 全科目の分布で小児歯科の比率を評価することになります（競合が少ないぶん
#: 比率は大きく出るので、どこもかしこも高得点になります）。
SPECIALTY_SEP = "@"


def specialty_count_metric(specialty: str) -> str:
    """その科目を標榜する商圏内の医院数の指標名。"""
    return f"facility_count{SPECIALTY_SEP}{specialty}"


def specialty_ratio_metric(population_metric: str, specialty: str) -> str:
    """その科目 1 件あたりの人口の指標名。"""
    return f"{population_metric}_per_facility{SPECIALTY_SEP}{specialty}"


def competition_specialties(config: Mapping[str, Any]) -> list[tuple[str, str]]:
    """設定に現れる (人口指標, 科目) の組。プロファイル横断で重複を除く。

    分布の集計と得点付けの両方がこの一覧を必要とします。片方だけが知っている
    状態だと、プロファイルを足したときに「分布が無いので未算出」になります。
    """
    seen: list[tuple[str, str]] = []
    for profile in (config.get("profiles") or {}).values():
        cfg = (profile or {}).get("competition") or {}
        specialty = cfg.get("specialty")
        if not specialty:
            continue
        pair = (str(cfg.get("population_metric", "population")), str(specialty))
        if pair not in seen:
            seen.append(pair)
    return seen


def derived_metrics(config: Mapping[str, Any]) -> list[str]:
    """DISTRIBUTION_METRICS に足して集計すべき、科目で絞った指標名。"""
    return [specialty_ratio_metric(pop, spec)
            for pop, spec in competition_specialties(config)]


def augment_specialty_metrics(row: dict[str, Any],
                              pairs: Iterable[tuple[str, str]]) -> dict[str, Any]:
    """商圏 1 件の集計に、科目で絞った件数と比率を書き足す。

    ``facility_specialty_counts`` は kg_analyze_point が返す科目キーごとの件数。
    そこに現れない科目は 0 件であって欠測ではありません（商圏内の医院を全部
    見たうえで 1 件も標榜していなかった、という意味）。ただしそれが言えるのは
    科目データを持つ医院についてだけなので、被覆率も一緒に置いておきます。
    """
    counts = row.get("facility_specialty_counts") or {}
    total = row.get("facility_count")
    known = row.get("facilities_with_specialty_data")
    row["specialty_data_coverage"] = (
        None if not total else (known or 0) / total
    )
    for population_metric, specialty in pairs:
        count = int(counts.get(specialty, 0) or 0)
        row[specialty_count_metric(specialty)] = count
        population = row.get(population_metric)
        row[specialty_ratio_metric(population_metric, specialty)] = (
            None if not count or population is None else float(population) / count
        )
    return row


@dataclass(frozen=True)
class Distribution:
    """Observed spread of one metric, used to place a value on a 0-100 scale."""

    metric: str
    min_value: float | None = None
    p05: float | None = None
    p50: float | None = None
    p95: float | None = None
    max_value: float | None = None
    mean_value: float | None = None
    stddev_value: float | None = None
    sample_count: int = 0

    def usable(self, method: str) -> bool:
        if self.sample_count < 2:
            return False
        if method == "zscore":
            return self.mean_value is not None and (self.stddev_value or 0) > 0
        return self.p05 is not None and self.p95 is not None and self.p95 > self.p05


@dataclass
class ComponentScore:
    """One of the four headline scores, plus enough detail to explain it."""

    value: float | None
    parts: dict[str, float | None] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    note: str | None = None
    #: 目盛りの上端・下端に張り付いた入力。ここに名前があるということは、
    #: その指標では**この地点と、もっと大きい/小さい地点の区別が付いていない**
    #: ということです。同点は「同じくらい」ではなく「測れていない」なので、
    #: 黙って並べません。
    saturated: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out = {
            "value": None if self.value is None else round(self.value, 1),
            "parts": {k: (None if v is None else round(v, 1)) for k, v in self.parts.items()},
            "missing": self.missing,
            "note": self.note,
        }
        if self.saturated:
            out["saturated"] = self.saturated
            out["saturated_note"] = (
                "この指標は目盛りの端に達しています。これより大きい（小さい）地点と"
                "同じ点数になるため、この成分では地点間の差を見分けられません。")
        return out


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _linear(value: float, low: float, high: float, clamp: tuple[float, float]) -> float:
    if high == low:
        return (clamp[0] + clamp[1]) / 2
    ratio = (value - low) / (high - low)
    return _clamp(clamp[0] + ratio * (clamp[1] - clamp[0]), *clamp)


def _weighted(parts: Mapping[str, float | None], weights: Mapping[str, float],
              min_coverage: float = 0.0) -> float | None:
    """Weighted mean over the parts that have a value, renormalising weights.

    ``min_coverage`` is the share of the total weight that must actually be
    backed by data. Below it the score is withheld rather than reported: a
    "demand" figure resting on one fifteen-percent input is not a demand
    figure, and rounding it up to one would overstate what we know.
    """
    available_w = 0.0
    total_w = 0.0
    total = 0.0
    for key, weight in weights.items():
        if weight <= 0:
            continue
        total_w += weight
        val = parts.get(key)
        if val is None:
            continue
        total += val * weight
        available_w += weight
    if available_w <= 0:
        return None
    if total_w > 0 and (available_w / total_w) < min_coverage:
        return None
    return total / available_w


class ScoringModel:
    """A single named profile from ``config/<業態>/scoring.yaml``."""

    def __init__(self, config: Mapping[str, Any], profile: str | None = None):
        self.config = config
        profiles = config.get("profiles") or {}
        self.profile_name = profile or config.get("active_profile") or next(iter(profiles), "default")
        if self.profile_name not in profiles:
            raise KeyError(
                f"unknown scoring profile {self.profile_name!r}; "
                f"available: {sorted(profiles)}"
            )
        self.profile: dict[str, Any] = profiles[self.profile_name]
        norm = config.get("normalization") or {}
        self.method: str = norm.get("method", "minmax_p05_p95")
        clamp = norm.get("clamp") or [0, 100]
        self.clamp: tuple[float, float] = (float(clamp[0]), float(clamp[1]))
        # Share of a component's weight that must be backed by real data before
        # the component is reported at all.
        self.min_weight_coverage: float = float(norm.get("min_weight_coverage", 0.5))

    # ------------------------------------------------------------------ meta
    @property
    def label(self) -> str:
        return self.profile.get("label", self.profile_name)

    @property
    def radii(self) -> list[int]:
        return [int(r) for r in self.config.get("trade_area_radii_m", [500, 1000, 2000])]

    @property
    def mesh_scoring_radius_m(self) -> int:
        return int(self.config.get("mesh_scoring_radius_m", 1000))

    def describe(self) -> dict[str, Any]:
        return {
            "profile": self.profile_name,
            "label": self.label,
            "description": self.profile.get("description"),
            "normalization": {"method": self.method, "clamp": list(self.clamp),
                              "min_weight_coverage": self.min_weight_coverage},
            "overall_weights": self.profile.get("overall_weights", {}),
            "demand_weights": self.profile.get("demand_weights", {}),
            "competition": self.profile.get("competition", {}),
            "cost": self.profile.get("cost", {}),
            "growth": self.profile.get("growth", {}),
            "accessibility": self.profile.get("accessibility", {}),
            "trade_area_radii_m": self.radii,
            "is_provisional": True,
        }

    # ------------------------------------------------------------- normalise
    def normalize(self, metric: str, value: float | None,
                  distributions: Mapping[str, Distribution]) -> float | None:
        if value is None:
            return None
        dist = distributions.get(metric)
        if dist is None or not dist.usable(self.method):
            return None
        if self.method == "zscore":
            z = (value - float(dist.mean_value)) / float(dist.stddev_value)
            # mean -> midpoint, +2sd -> top of scale
            mid = (self.clamp[0] + self.clamp[1]) / 2
            span = (self.clamp[1] - self.clamp[0]) / 4
            return _clamp(mid + z * span, *self.clamp)
        return _linear(value, float(dist.p05), float(dist.p95), self.clamp)

    # ------------------------------------------------------------ components
    def demand(self, m: Mapping[str, Any],
               distributions: Mapping[str, Distribution]) -> ComponentScore:
        weights = self.profile.get("demand_weights", {})
        parts: dict[str, float | None] = {}
        # Driven by the weights, not by a list in here: adding an input to a
        # profile is a configuration change, which is the whole point of the
        # weights living in a file. Growth is the exception -- it is a rate, so
        # it is placed on its own configured scale rather than a percentile.
        # 率の指標は、百分位ではなく設定した目盛りに載せます。名前を直書き
        # しないのは、どれが率かをプロファイルの growth.metric が決めるため。
        # 直書きしていたので、成長の指標を将来推計に変えたときに demand 側が
        # 取り残され、**過去の実績を将来の目盛りで採点する**ところでした。
        rate_metric = self.profile.get("growth", {}).get("metric", "population_growth")
        for metric in weights:
            if metric == rate_metric:
                parts[metric] = self._growth_value(m.get(metric))
            elif metric in DISTRIBUTION_METRICS:
                parts[metric] = self.normalize(metric, m.get(metric), distributions)
        missing = [k for k, v in parts.items() if v is None]
        value = _weighted(parts, weights, self.min_weight_coverage)
        note = None
        if value is None and any(v is not None for v in parts.values()):
            note = "算出に必要な指標が不足しているため未算出"
        return ComponentScore(value, parts, missing, note, self._saturated(parts))

    def _saturated(self, parts: Mapping[str, float | None]) -> list[str]:
        """目盛りの端に達した入力の名前。

        端に達した入力は、そこから先の違いを捨てています。農村が大半の県で
        県全域を目盛りにすると市街地が全部上限に張り付く、というのがまさに
        これで、点数は出ているのに地点を選ぶ手掛かりにはなりません。
        """
        low, high = self.clamp
        return sorted(k for k, v in parts.items()
                      if v is not None and (v >= high - 1e-9 or v <= low + 1e-9))

    def competition(self, m: Mapping[str, Any],
                    distributions: Mapping[str, Distribution]) -> ComponentScore:
        """商圏の競合。科目を指定したプロファイルではその科目だけを数える。

        小児歯科をやろうとしている人にとって、小児歯科を標榜していない医院は
        同じ重さの競合ではありません。``competition.specialty`` を置くと、
        件数も分母の人口も（``population_metric``）その科目のものに切り替わり、
        正規化に使う分布もその科目専用のものになります。

        切り替えたときだけ効く安全弁が 1 つあります。商圏内の医院のうち
        診療科目データを持つ割合が ``min_specialty_coverage`` を下回るときは
        算出しません。科目ファイルに載っていない医院は「標榜していない」のでは
        なく「分からない」ので、そのまま数えると競合が少ない土地に見えます。
        """
        cfg = self.profile.get("competition", {})
        specialty = cfg.get("specialty")
        population_metric = cfg.get("population_metric", "population")
        population = m.get(population_metric)

        if specialty:
            metric = specialty_ratio_metric(population_metric, specialty)
            count = m.get(specialty_count_metric(specialty))
            coverage = m.get("specialty_data_coverage")
            minimum = float(cfg.get("min_specialty_coverage", 0.8))
            if count is None:
                return ComponentScore(None, {metric: None}, [metric],
                                      "診療科目データが読み込まれていないため未算出")
            if coverage is not None and coverage < minimum:
                return ComponentScore(
                    None, {metric: None}, [metric],
                    f"商圏内の歯科医院のうち診療科目が分かるのは"
                    f"{coverage * 100:.0f}%（{minimum * 100:.0f}%以上で算出）")
        else:
            metric = cfg.get("metric", "population_per_facility")
            count = m.get("facility_count")

        if count == 0:
            if not population:
                return ComponentScore(
                    float(cfg.get("zero_population_score", 0)),
                    {metric: None}, [],
                    "商圏内に歯科医院・人口ともに存在しないため下限値",
                )
            return ComponentScore(
                float(cfg.get("zero_facility_score", 95)),
                {metric: None}, [],
                ("商圏内に該当する標榜科目の歯科医院が0件のため、"
                 "ゼロ除算を避けて上限付近の固定値を適用" if specialty else
                 "商圏内に歯科医院が0件のため、ゼロ除算を避けて上限付近の固定値を適用"),
            )

        value = m.get(metric)
        if value is None:
            return ComponentScore(None, {metric: None}, [metric])
        cap = cfg.get("cap")
        capped = min(float(value), float(cap)) if cap is not None else float(value)
        score = self.normalize(metric, capped, distributions)
        note = "上限値でクリップ" if cap is not None and float(value) > float(cap) else None
        return ComponentScore(score, {metric: score}, [] if score is not None else [metric], note)

    def _growth_value(self, growth: float | None) -> float | None:
        if growth is None:
            return None
        cfg = self.profile.get("growth", {})
        return _linear(float(growth), float(cfg.get("low", -0.05)),
                       float(cfg.get("high", 0.08)), self.clamp)

    def growth(self, m: Mapping[str, Any], _d: Mapping[str, Distribution]) -> ComponentScore:
        metric = self.profile.get("growth", {}).get("metric", "population_growth")
        value = self._growth_value(m.get(metric))
        return ComponentScore(value, {metric: value}, [] if value is not None else [metric])

    def accessibility(self, m: Mapping[str, Any],
                      _d: Mapping[str, Distribution]) -> ComponentScore:
        cfg = self.profile.get("accessibility", {})
        weights = cfg.get("weights", {"station_distance": 0.6, "daily_passengers": 0.4})
        parts: dict[str, float | None] = {}

        distance = m.get("station_distance_m")
        if distance is None:
            parts["station_distance"] = None
        else:
            # Nearer is better, so the scale runs downwards.
            parts["station_distance"] = _linear(
                float(distance),
                float(cfg.get("distance_worst_m", 2000)),
                float(cfg.get("distance_best_m", 200)),
                self.clamp,
            )

        passengers = m.get("daily_passengers")
        if not passengers or passengers <= 0:
            parts["daily_passengers"] = None
        else:
            lo = float(cfg.get("passengers_min", 1000))
            hi = float(cfg.get("passengers_max", 500000))
            if cfg.get("passengers_scale") == "log10":
                parts["daily_passengers"] = _linear(
                    math.log10(max(float(passengers), 1.0)),
                    math.log10(max(lo, 1.0)), math.log10(max(hi, 10.0)), self.clamp,
                )
            else:
                parts["daily_passengers"] = _linear(float(passengers), lo, hi, self.clamp)

        missing = [k for k, v in parts.items() if v is None]
        value = _weighted(parts, weights, self.min_weight_coverage)
        note = None
        if value is None and any(v is not None for v in parts.values()):
            note = "算出に必要な指標が不足しているため未算出"
        return ComponentScore(value, parts, missing, note, self._saturated(parts))

    def cost(self, m: Mapping[str, Any],
             distributions: Mapping[str, Distribution]) -> ComponentScore:
        """What the location costs, inverted so that cheaper scores higher.

        The axis the model was missing. Without it every ranking puts Ginza
        first, which is true and useless: good locations are expensive, and a
        practice is a business of fixed costs. What moves the ranking is
        location quality *relative to* what it costs.

        Three things this is careful about.

        Log scale, because 地価公示 spans four orders of magnitude within one
        prefecture -- ¥2,240/m² of woodland to ¥67,100,000/m² in Ginza. On a
        linear percentile scale every suburb collapses into one value and the
        component stops distinguishing anything.

        A minimum number of surveyed parcels, because 地価公示 samples a few
        thousand parcels per prefecture and the median of one parcel is that
        parcel. Below the configured minimum the component is withheld, which
        renormalises the remaining weights rather than scoring on a guess.

        And it is a proxy, not a rent. The land under a location is not the
        lease on a unit in it. It is the best published, geocoded, comparable
        stand-in available, and it is labelled as a stand-in everywhere it is
        shown.
        """
        cfg = self.profile.get("cost", {})
        metric = cfg.get("metric", "land_price_yen_per_sqm")
        value = m.get(metric)
        points = m.get("land_price_points")
        min_points = int(cfg.get("min_points", 3))

        if value is None or value <= 0:
            return ComponentScore(None, {metric: None}, [metric],
                                  "商圏内に地価公示の標準地がないため未算出")
        if points is not None and points < min_points:
            return ComponentScore(
                None, {metric: None}, [metric],
                f"商圏内の地価公示の標準地が{points}地点のみのため未算出"
                f"（{min_points}地点以上で算出）")

        dist = distributions.get(metric)
        if dist is None or not dist.usable(self.method):
            return ComponentScore(None, {metric: None}, [metric])

        scale = cfg.get("scale", "log10")
        if scale == "log10":
            low, high = float(dist.p05), float(dist.p95)
            if low <= 0 or high <= low:
                return ComponentScore(None, {metric: None}, [metric])
            placed = _linear(math.log10(max(float(value), 1.0)),
                             math.log10(low), math.log10(high), self.clamp)
        else:
            placed = self.normalize(metric, float(value), distributions)
        if placed is None:
            return ComponentScore(None, {metric: None}, [metric])

        # Invert: the cheapest end of the observed spread scores highest.
        inverted = self.clamp[0] + self.clamp[1] - placed
        return ComponentScore(inverted, {metric: inverted}, [],
                              "地価が安いほど高い（地価公示の中央値・対数スケール）")

    # ---------------------------------------------------------------- public
    def score(self, metrics: Mapping[str, Any],
              distributions: Mapping[str, Distribution] | None = None) -> dict[str, Any]:
        """Score one catchment. ``metrics`` is the raw output of kg_analyze_point."""
        distributions = distributions or {}
        components = {
            "demand": self.demand(metrics, distributions),
            "competition": self.competition(metrics, distributions),
            "growth": self.growth(metrics, distributions),
            "accessibility": self.accessibility(metrics, distributions),
            # Only where a profile asks for it. Adding an unweighted component
            # to every profile would change nothing arithmetically and would
            # still put "コスト —" on screens whose model does not use it.
            **({"cost": self.cost(metrics, distributions)}
               if "cost" in (self.profile.get("overall_weights") or {}) else {}),
        }
        overall = _weighted(
            {k: c.value for k, c in components.items()},
            self.profile.get("overall_weights", {}),
            self.min_weight_coverage,
        )
        unavailable = [k for k, c in components.items() if c.value is None]

        # A component a profile calls required is not optional to it. Dropping
        # one and renormalising the rest -- the right default when an input is
        # merely absent -- turns "we could not price this place" into "this
        # place is free", and ranks the places we know least about highest.
        # 地価公示 covers about 70% of Tokyo's meshes at three parcels or more,
        # so this is the difference between a cost ranking and an artefact.
        required = [c for c in (self.profile.get("required_components") or [])
                    if components.get(c) is None or components[c].value is None]
        if required:
            overall = None
        return {
            "profile": self.profile_name,
            "profile_label": self.label,
            "is_provisional": True,
            "demand": None if components["demand"].value is None else round(components["demand"].value, 1),
            "competition": None if components["competition"].value is None else round(components["competition"].value, 1),
            "growth": None if components["growth"].value is None else round(components["growth"].value, 1),
            "accessibility": None if components["accessibility"].value is None else round(components["accessibility"].value, 1),
            "cost": (None if "cost" not in components or components["cost"].value is None
                     else round(components["cost"].value, 1)),
            "overall": None if overall is None else round(overall, 1),
            "unavailable_components": unavailable,
            # どの成分が目盛りの端に達しているか。ここに名前が並ぶ地点どうしは、
            # 点数が同じでも「同じくらい」ではなく「区別できていない」。
            "saturated_components": sorted(
                k for k, c in components.items() if c.saturated),
            # Named separately from "missing": these are why there is no
            # overall score at all, rather than one computed without them.
            "missing_required_components": required,
            "breakdown": {k: c.as_dict() for k, c in components.items()},
        }


def distributions_from_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Distribution]:
    """Build a metric -> Distribution map from ``metric_distributions`` rows."""
    out: dict[str, Distribution] = {}
    for row in rows:
        out[row["metric"]] = Distribution(
            metric=row["metric"],
            min_value=row.get("min_value"),
            p05=row.get("p05"),
            p50=row.get("p50"),
            p95=row.get("p95"),
            max_value=row.get("max_value"),
            mean_value=row.get("mean_value"),
            stddev_value=row.get("stddev_value"),
            sample_count=row.get("sample_count") or 0,
        )
    return out


#: 正規化の目盛りを、どの集合から作ったか。
#:
#:   all           人口のある商圏すべて
#:   with_clinics  歯科医院が実在する商圏だけ（＝候補になりうる場所）
#:
#: 目盛りを作る集合が違えば、同じ 90 点でも別のことを指します。だから鍵に
#: 入れます。入れないと、片方で作った目盛りをもう片方の得点に黙って使えて
#: しまい、しかも数字はもっともらしいままです。
NORMALIZATION_REFERENCES = ("all", "with_clinics")
DEFAULT_NORMALIZATION_REFERENCE = "with_clinics"


def normalization_reference(config: Mapping[str, Any]) -> str:
    value = str((config.get("normalization") or {}).get(
        "reference", DEFAULT_NORMALIZATION_REFERENCE))
    return value if value in NORMALIZATION_REFERENCES else DEFAULT_NORMALIZATION_REFERENCE


#: 業態の既定値。**ここが唯一の定義で、`analysis.DEFAULT_CATEGORY` は別名です。**
#: 目盛りの鍵を作る側（ここ）と商圏を数える側（analysis）で別々に持つと、
#: 片方だけ直したときに、歯科の目盛りで内科を採点する状態になります。
DEFAULT_FACILITY_CATEGORY = "dental_clinic"


def scope_key(mesh_size_m: int, radius_m: int, prefecture_code: str,
              reference: str = DEFAULT_NORMALIZATION_REFERENCE,
              facility_category: str = DEFAULT_FACILITY_CATEGORY) -> str:
    """目盛りを識別する鍵。**業態が入っていないと、静かに混ざります。**

    ``reference`` の ``with_clinics`` は「歯科医院が実在する商圏」という意味
    です。目盛りの定義そのものが業態に依存していて、内科では別の集合になります。
    同じ文字列に二つの意味を持たせることはできません。

    形は ``mesh:500:r1000:pref13:catdental_clinic:with_clinics``。
    移行（030）で既存の鍵もこの形に書き換えてあるので、歯科の呼び出しは
    再計算なしで今日どおり当たります。
    """
    return (f"mesh:{mesh_size_m}:r{radius_m}:pref{prefecture_code}"
            f":cat{facility_category}:{reference}")


def legacy_scope_key(mesh_size_m: int, radius_m: int, prefecture_code: str,
                     reference: str = DEFAULT_NORMALIZATION_REFERENCE) -> str:
    """業態を入れる前（マイグレーション 030 より前）の鍵。

    **移行のためだけにあります。** コードは push で即デプロイされますが、
    マイグレーションは手で当てます。その間、新しい鍵を探しても見つからず、
    需要と競合が「データ不足」になります（実際に静岡で起きました）。

    書かれた当時はどれも歯科なので、既定の業態のときだけ読みに行きます。
    030 を当てれば古い鍵は書き換わり、ここは二度と当たりません。
    """
    return f"mesh:{mesh_size_m}:r{radius_m}:pref{prefecture_code}:{reference}"
