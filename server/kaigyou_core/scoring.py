"""The provisional scoring model.

Everything numeric here comes from ``config/scoring.yaml``; this module only
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": None if self.value is None else round(self.value, 1),
            "parts": {k: (None if v is None else round(v, 1)) for k, v in self.parts.items()},
            "missing": self.missing,
            "note": self.note,
        }


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
    """A single named profile from ``config/scoring.yaml``."""

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
        for metric in weights:
            if metric == "population_growth":
                parts[metric] = self._growth_value(m.get(metric))
            elif metric in DISTRIBUTION_METRICS:
                parts[metric] = self.normalize(metric, m.get(metric), distributions)
        missing = [k for k, v in parts.items() if v is None]
        value = _weighted(parts, weights, self.min_weight_coverage)
        note = None
        if value is None and any(v is not None for v in parts.values()):
            note = "算出に必要な指標が不足しているため未算出"
        return ComponentScore(value, parts, missing, note)

    def competition(self, m: Mapping[str, Any],
                    distributions: Mapping[str, Distribution]) -> ComponentScore:
        cfg = self.profile.get("competition", {})
        metric = cfg.get("metric", "population_per_facility")
        population = m.get("population")
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
                "商圏内に歯科医院が0件のため、ゼロ除算を避けて上限付近の固定値を適用",
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
        return ComponentScore(value, parts, missing, note)

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


def scope_key(mesh_size_m: int, radius_m: int, prefecture_code: str) -> str:
    return f"mesh:{mesh_size_m}:r{radius_m}:pref{prefecture_code}"
