"""Monte Carlo game simulation: score, margin and total distributions, market
probabilities, joint (same-game) probabilities, and line sensitivity.

A game is simulated as a (margin, total) pair:

- margin: drawn from a margin distribution (a probability mass function over
  whole-point home margins, e.g. the key-number model around the model's
  expected margin);
- total: the market's total line plus a residual from real history;
- dependence: each draw takes one historical game's pair (u, r), where ``u`` is
  where that game's favorite margin landed within its predicted distribution
  (a randomized probability integral transform, in (0, 1)) and ``r`` is its
  total minus its total line. ``u`` enters by rank (jittered within its rank
  slot, so exactly uniform) and maps through this game's margin CDF; ``r``
  shifts this game's total line. Pairs keep whatever association real
  games had between "the favorite beat expectations" and "the total went over"
  (an empirical copula), while each margin keeps its own model distribution.

Scores follow from home = (total + margin) / 2, away = (total - margin) / 2,
with the total nudged by one point when parity requires whole scores.

Simulation volume reduces sampling error only; every probability reports its
standard error, and model error is untouched by more iterations.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

PRESETS = {"quick": 10_000, "detailed": 50_000, "research": 100_000}


@dataclass(frozen=True)
class Probability:
    value: float
    standard_error: float
    """Monte Carlo sampling error only: sqrt(p(1-p)/n)."""

    @classmethod
    def of(cls, hits: NDArray[np.bool_]) -> Probability:
        n = hits.size
        p = float(hits.mean()) if n else math.nan
        return cls(p, math.sqrt(p * (1 - p) / n) if n else math.nan)


@dataclass(frozen=True)
class SideProbabilities:
    win: Probability
    push: Probability
    loss: Probability

    @property
    def win_excluding_push(self) -> float:
        decided = self.win.value + self.loss.value
        return self.win.value / decided if decided > 0 else 0.5


@dataclass(frozen=True)
class Leg:
    """A same-game event for joint probabilities. line: bookmaker-style spread from
    the side's view (-3.5), a total (44.5), or None for a moneyline."""

    market: str
    selection: str
    line: float | None = None


@dataclass(frozen=True)
class SensitivityRow:
    line: float
    win: float
    push: float
    win_excluding_push: float


class GameSimulation:
    def __init__(self, home: NDArray[np.int64], away: NDArray[np.int64]) -> None:
        self.home = home
        self.away = away
        self.margin = home - away
        self.total = home + away

    @property
    def iterations(self) -> int:
        return int(self.home.size)

    # ------------------------------------------------------------------ events

    def outcome(self, leg: Leg) -> tuple[NDArray[np.bool_], NDArray[np.bool_]]:
        """(win, push) masks for one leg across all simulated games."""
        diff: NDArray[np.float64]
        if leg.market == "TOTAL":
            if leg.line is None:
                raise ValueError("A total needs a line")
            diff = (self.total - leg.line).astype(np.float64)
            diff = diff if leg.selection == "OVER" else -diff
        elif leg.market in ("SPREAD", "MONEYLINE"):
            diff = (self.margin if leg.selection == "HOME" else -self.margin).astype(np.float64)
            if leg.market == "SPREAD":
                if leg.line is None:
                    raise ValueError("A spread needs a line")
                diff = diff + leg.line
        else:
            raise ValueError(f"Unsupported market {leg.market}")
        return diff > 0, diff == 0

    def side(self, leg: Leg) -> SideProbabilities:
        win, push = self.outcome(leg)
        return SideProbabilities(
            Probability.of(win), Probability.of(push), Probability.of(~win & ~push)
        )

    def joint(self, legs: Sequence[Leg]) -> Probability:
        """P(every leg wins) in the same simulated games (pushes count as not winning)."""
        hits = np.ones(self.iterations, dtype=bool)
        for leg in legs:
            hits &= self.outcome(leg)[0]
        return Probability.of(hits)

    # ------------------------------------------------------------------ lines

    def sensitivity(
        self, market: str, selection: str, lines: Sequence[float]
    ) -> list[SensitivityRow]:
        rows = []
        for line in lines:
            s = self.side(Leg(market, selection, line))
            rows.append(SensitivityRow(line, s.win.value, s.push.value, s.win_excluding_push))
        return rows

    def max_acceptable_line(
        self,
        market: str,
        selection: str,
        decimal_odds: float,
        candidates: Sequence[float],
        *,
        min_ev: float = 0.0,
    ) -> float | None:
        """The worst line (for the bettor) at which this price still has EV > min_ev,
        or None if no candidate does. Worse = fewer points for a spread side or an
        over, more points for an under."""
        acceptable = []
        for line in candidates:
            s = self.side(Leg(market, selection, line))
            ev = s.win.value * (decimal_odds - 1) - s.loss.value
            if ev > min_ev:
                acceptable.append(line)
        if not acceptable:
            return None
        return max(acceptable) if selection == "OVER" else min(acceptable)

    # ------------------------------------------------------------------ summaries

    def quantiles(
        self, values: NDArray[np.int64], qs: Sequence[float] = (0.05, 0.25, 0.5, 0.75, 0.95)
    ) -> dict[str, float]:
        return {f"p{int(q * 100)}": float(np.quantile(values, q)) for q in qs}


def _cdf(pmf: Mapping[int, float]) -> tuple[NDArray[np.int64], NDArray[np.float64]]:
    margins = np.array(sorted(pmf), dtype=np.int64)
    probs = np.array([pmf[m] for m in margins], dtype=float)
    cdf = np.cumsum(probs / probs.sum())
    cdf[-1] = 1.0
    return margins, cdf


def randomized_pit(pmf: Mapping[int, float], actual: int, rng: np.random.Generator) -> float:
    """Where ``actual`` falls in ``pmf``: uniform between CDF(actual - 1) and CDF(actual).
    Well calibrated margins give u ~ Uniform(0, 1)."""
    margins, cdf = _cdf(pmf)
    i = int(np.searchsorted(margins, actual))
    upper = (
        float(cdf[i])
        if i < margins.size and margins[i] == actual
        else (float(cdf[i - 1]) if i > 0 else 0.0)
    )
    lower = float(cdf[i - 1]) if i > 0 else 0.0
    if upper <= lower:  # actual outside the support or zero mass: clamp inside (0, 1)
        return min(max(upper, 1e-6), 1 - 1e-6)
    return float(rng.uniform(lower, upper))


def simulate_game(
    margin_pmf: Mapping[int, float],
    total_line: float,
    pairs: NDArray[np.float64],
    *,
    home_favored: bool,
    iterations: int = PRESETS["quick"],
    seed: int | None = None,
) -> GameSimulation:
    """``pairs``: shape (n, 2) of historical (u_favorite, total_residual).
    ``home_favored`` orients u: it measures the favorite's margin surprise."""
    if pairs.ndim != 2 or pairs.shape[1] != 2 or pairs.shape[0] == 0:
        raise ValueError("pairs must be a non-empty (n, 2) array")
    if iterations <= 0:
        raise ValueError("iterations must be positive")
    rng = np.random.default_rng(seed)
    n = pairs.shape[0]
    index = rng.integers(0, n, iterations)
    # Rank copula: a pair contributes its u's RANK, and u is drawn uniformly inside
    # that rank's slot. Simulated u is then exactly uniform, so margins follow
    # ``margin_pmf`` exactly in expectation; reusing the raw historical u values
    # would bake that finite sample's noise (~0.4 pts on key numbers) into every
    # simulation. The ordering - hence the margin/total association - is kept.
    ranks = np.argsort(np.argsort(pairs[:, 0]))
    u_fav = (ranks[index] + rng.random(iterations)) / n
    residual = pairs[index, 1]
    # A favorite doing better than expected = a higher home margin when home is the
    # favorite, a lower one when the away team is.
    u_home = u_fav if home_favored else 1.0 - u_fav
    margins, cdf = _cdf(margin_pmf)
    margin = margins[np.minimum(np.searchsorted(cdf, u_home, side="left"), margins.size - 1)]
    total = np.rint(total_line + residual).astype(np.int64)
    total = np.maximum(total, np.abs(margin))  # neither team can score below zero
    odd = (total + margin) % 2 != 0
    step = np.where(rng.random(iterations) < 0.5, 1, -1)
    total = np.where(odd, total + step, total)
    total = np.where(total < np.abs(margin), np.abs(margin), total)  # after a -1 step
    home = (total + margin) // 2
    away = (total - margin) // 2
    return GameSimulation(home.astype(np.int64), away.astype(np.int64))
