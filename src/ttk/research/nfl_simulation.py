"""Fit and validate the NFL Monte Carlo engine's dependence structure.

- Pairs (u_favorite, total_residual) come from TRAIN seasons only: u is the
  randomized PIT of the favorite's actual margin under the feature margin
  model's key-number distribution; the residual is actual total minus the
  reported total line, centered on its MEDIAN. A market total is a 50/50 point,
  so the simulated over at the line should be ~50% (minus pushes); totals are
  right-skewed, so mean-centering left it at ~47.7%. Uncentered, train-era
  totals landed +0.8 over their lines on average and validation log loss was
  worse (1.4057 vs 1.4045 centered).
- Validation (VALIDATE seasons): for each game with a reported spread and total,
  predict the four same-game outcomes of (home covers, over) two ways - the
  simulation's joint distribution, and the product of the same simulation's
  marginals (independence). Scored with categorical log loss on games where
  neither leg pushed. The paired difference says whether modeled dependence
  beats assuming independence.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from ttk.db.models import ReportedLine
from ttk.models.elo import EloGame, EloPrediction
from ttk.models.simulation import Leg, randomized_pit, simulate_game
from ttk.research.nfl_elo import FittedModels, PairedComparison, _in


@dataclass(frozen=True)
class SimRow:
    """One finished game for fitting or validating the simulation: the model's
    expected home margin and the game's result and closing lines."""

    game_id: int
    mu: float
    home_score: int
    away_score: int
    home_spread: float | None
    total: float | None


Pmf = Callable[[float], Mapping[int, float]]


def pairs_from_rows(rows: Iterable[SimRow], pmf: Pmf, *, seed: int = 7) -> NDArray[np.float64]:
    """(u_favorite, total residual) for every row with a total, the residual
    centered on its median (see the module docstring)."""
    rng = np.random.default_rng(seed)
    out = []
    for r in rows:
        if r.total is None:
            continue
        u_home = randomized_pit(pmf(r.mu), r.home_score - r.away_score, rng)
        u_fav = u_home if r.mu >= 0 else 1.0 - u_home
        out.append((u_fav, r.home_score + r.away_score - r.total))
    pairs = np.asarray(out, dtype=float)
    if pairs.size:
        pairs[:, 1] -= np.median(pairs[:, 1])
    return pairs


def _nfl_rows(
    predictions: Sequence[EloPrediction],
    games: dict[int, EloGame],
    lines: dict[int, ReportedLine],
    models: FittedModels,
    window: tuple[int, int],
) -> list[SimRow]:
    rows = []
    for p in predictions:
        g = games[p.game_id]
        line = lines.get(g.game_id)
        if not (_in(p.season, window) and g.played and line):
            continue
        mu = models.feature_margin(p)
        if mu is None:
            continue
        rows.append(
            SimRow(
                g.game_id, mu, g.home_score or 0, g.away_score or 0, line.home_spread, line.total
            )
        )
    return rows


def fit_pairs(
    predictions: Sequence[EloPrediction],
    games: dict[int, EloGame],
    lines: dict[int, ReportedLine],
    models: FittedModels,
    window: tuple[int, int],
    *,
    seed: int = 7,
) -> NDArray[np.float64]:
    if models.features is None:
        raise ValueError("Simulation pairs need the feature margin model")
    rows = _nfl_rows(predictions, games, lines, models, window)
    return pairs_from_rows(rows, models.features.key.pmf_at_mean, seed=seed)


@dataclass(frozen=True)
class JointValidation:
    games: int
    simulated_log_loss: float
    independent_log_loss: float
    vs_independent: PairedComparison
    """Per-game simulated minus independent log loss (negative = simulation better)."""
    observed_cover_and_over: float
    simulated_cover_and_over: float
    independent_cover_and_over: float
    pairs_correlation: float
    """Spearman-like association in TRAIN pairs: corr(u_favorite, total residual)."""


def validate_joint(
    predictions: Sequence[EloPrediction],
    games: dict[int, EloGame],
    lines: dict[int, ReportedLine],
    models: FittedModels,
    pairs: NDArray[np.float64],
    window: tuple[int, int],
    *,
    iterations: int = 10_000,
    seed: int = 11,
) -> JointValidation:
    assert models.features is not None
    rows = _nfl_rows(predictions, games, lines, models, window)
    return validate_rows(
        rows, models.features.key.pmf_at_mean, pairs, iterations=iterations, seed=seed
    )


def validate_rows(
    rows: Iterable[SimRow],
    pmf: Pmf,
    pairs: NDArray[np.float64],
    *,
    iterations: int = 10_000,
    seed: int = 11,
) -> JointValidation:
    sim_losses, ind_losses = [], []
    observed = sim_both = ind_both = 0.0
    for r in rows:
        if r.home_spread is None or r.total is None:
            continue
        home, away = r.home_score, r.away_score
        cover_margin = home - away + r.home_spread
        over_margin = home + away - r.total
        if cover_margin == 0 or over_margin == 0:
            continue
        sim = simulate_game(
            pmf(r.mu),
            r.total,
            pairs,
            home_favored=r.mu >= 0,
            iterations=iterations,
            seed=seed + r.game_id,
        )
        cover_win, cover_push = sim.outcome(Leg("SPREAD", "HOME", r.home_spread))
        over_win, over_push = sim.outcome(Leg("TOTAL", "OVER", r.total))
        decided = ~cover_push & ~over_push
        c, o = cover_win[decided], over_win[decided]
        n = max(int(decided.sum()), 1)
        eps = 1.0 / (n + 2)
        joint = {
            (True, True): (c & o).sum() / n,
            (True, False): (c & ~o).sum() / n,
            (False, True): (~c & o).sum() / n,
            (False, False): (~c & ~o).sum() / n,
        }
        pc, po = c.mean(), o.mean()
        independent = {
            (True, True): pc * po,
            (True, False): pc * (1 - po),
            (False, True): (1 - pc) * po,
            (False, False): (1 - pc) * (1 - po),
        }
        actual = (cover_margin > 0, over_margin > 0)
        sim_losses.append(-math.log(max(float(joint[actual]), eps)))
        ind_losses.append(-math.log(max(float(independent[actual]), eps)))
        observed += actual == (True, True)
        sim_both += float(joint[(True, True)])
        ind_both += float(independent[(True, True)])

    diffs = [s - i for s, i in zip(sim_losses, ind_losses, strict=True)]
    k = len(diffs)
    mean = sum(diffs) / k if k else 0.0
    se = (sum((d - mean) ** 2 for d in diffs) / (k - 1) / k) ** 0.5 if k > 1 else 0.0
    corr = float(np.corrcoef(pairs[:, 0], pairs[:, 1])[0, 1]) if pairs.shape[0] > 2 else 0.0
    return JointValidation(
        games=k,
        simulated_log_loss=sum(sim_losses) / k if k else math.nan,
        independent_log_loss=sum(ind_losses) / k if k else math.nan,
        vs_independent=PairedComparison(mean, se),
        observed_cover_and_over=observed / k if k else math.nan,
        simulated_cover_and_over=sim_both / k if k else math.nan,
        independent_cover_and_over=ind_both / k if k else math.nan,
        pairs_correlation=corr,
    )
