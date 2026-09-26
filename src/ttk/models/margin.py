"""Point-margin distributions: turn a rating difference into spread probabilities,
including the probability of a push on whole-number lines.

- ``MarginModel``: margin ~ Normal(intercept + slope x rating_diff, sigma),
  discretized to whole points. Ignores football's key numbers, so it understates
  how often games land on 3 and 7 (and pushes there).
- ``KeyNumberMarginModel``: the same normal shape reweighted by how often each
  final margin actually occurs relative to the normal model's expectation
  (fitted on training games only), then renormalized. Captures 3, 7, 10, 14 and
  the rarity of ties.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


@dataclass(frozen=True)
class SpreadProbabilities:
    home_cover: float
    push: float
    away_cover: float

    @property
    def home_cover_excluding_push(self) -> float:
        decided = self.home_cover + self.away_cover
        return self.home_cover / decided if decided > 0 else 0.5


@dataclass(frozen=True)
class MarginModel:
    intercept: float
    slope: float
    sigma: float

    def expected_margin(self, rating_diff: float) -> float:
        return self.intercept + self.slope * rating_diff

    def p_margin_at_least(self, points: int, rating_diff: float) -> float:
        """P(home margin >= points) for whole-number margins."""
        mu = self.expected_margin(rating_diff)
        return 1.0 - _phi((points - 0.5 - mu) / self.sigma)

    def spread(self, home_line: float, rating_diff: float) -> SpreadProbabilities:
        """Probabilities for the home side at a bookmaker-style line (home -3.5 -> -3.5).
        Home covers when margin + home_line > 0."""
        threshold = -home_line  # home must win by more than this
        if threshold == math.floor(threshold):
            k = int(threshold)
            home = self.p_margin_at_least(k + 1, rating_diff)
            push = self.p_margin_at_least(k, rating_diff) - home
        else:
            home = self.p_margin_at_least(math.ceil(threshold), rating_diff)
            push = 0.0
        return SpreadProbabilities(home, push, 1.0 - home - push)


class MarginDistribution(Protocol):
    def expected_margin(self, rating_diff: float) -> float: ...

    def spread(self, home_line: float, rating_diff: float) -> SpreadProbabilities: ...


def _spread_from_pmf(pmf: Mapping[int, float], home_line: float) -> SpreadProbabilities:
    threshold = -home_line
    home = sum(p for k, p in pmf.items() if k > threshold)
    push = pmf.get(int(threshold), 0.0) if threshold == math.floor(threshold) else 0.0
    return SpreadProbabilities(home, push, max(1.0 - home - push, 0.0))


@dataclass(frozen=True)
class KeyNumberMarginModel:
    base: MarginModel
    weights: Mapping[int, float]
    """|margin| -> observed / expected frequency under ``base`` (1.0 when absent)."""
    max_margin: int = 70
    _cache: dict[float, dict[int, float]] = field(default_factory=dict, compare=False, repr=False)

    def expected_margin(self, rating_diff: float) -> float:
        return self.base.expected_margin(rating_diff)

    def pmf(self, rating_diff: float) -> dict[int, float]:
        cached = self._cache.get(rating_diff)
        if cached is not None:
            return cached
        mu, sigma = self.base.expected_margin(rating_diff), self.base.sigma
        raw = {
            k: (_phi((k + 0.5 - mu) / sigma) - _phi((k - 0.5 - mu) / sigma))
            * self.weights.get(abs(k), 1.0)
            for k in range(-self.max_margin, self.max_margin + 1)
        }
        total = sum(raw.values())
        pmf = {k: p / total for k, p in raw.items()}
        if len(self._cache) < 50_000:
            self._cache[rating_diff] = pmf
        return pmf

    def spread(self, home_line: float, rating_diff: float) -> SpreadProbabilities:
        return _spread_from_pmf(self.pmf(rating_diff), home_line)


def fit_key_number_weights(
    base: MarginModel,
    rating_diffs: Sequence[float],
    margins: Sequence[int],
    *,
    max_abs_margin: int = 40,
    prior_games: float = 20.0,
) -> KeyNumberMarginModel:
    """weight(|m|) = (observed + prior) / (expected + prior), where expected is the
    base model's probability mass at +-m summed over the training games. The prior
    shrinks thinly observed margins toward 1 (no adjustment)."""
    expected = dict.fromkeys(range(max_abs_margin + 1), 0.0)
    observed = dict.fromkeys(range(max_abs_margin + 1), 0.0)
    for diff, margin in zip(rating_diffs, margins, strict=True):
        mu = base.expected_margin(diff)
        for a in range(max_abs_margin + 1):
            for k in {a, -a}:
                expected[a] += _phi((k + 0.5 - mu) / base.sigma) - _phi((k - 0.5 - mu) / base.sigma)
        if abs(margin) <= max_abs_margin:
            observed[abs(margin)] += 1
    weights = {
        a: (observed[a] + prior_games) / (expected[a] + prior_games)
        for a in range(max_abs_margin + 1)
    }
    return KeyNumberMarginModel(base, weights)


def fit_margin_model(rating_diffs: Sequence[float], margins: Sequence[float]) -> MarginModel:
    """Least-squares line of margin on rating difference; sigma is the residual SD."""
    if len(rating_diffs) < 30:
        raise ValueError("Too few games to fit a margin model")
    x = np.asarray(rating_diffs, dtype=float)
    y = np.asarray(margins, dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    residuals = y - (intercept + slope * x)
    return MarginModel(float(intercept), float(slope), float(residuals.std(ddof=2)))
