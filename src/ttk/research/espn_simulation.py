"""Monte Carlo inputs for the frozen ESPN-history models (college football, NBA,
college basketball), fitted and validated as for the NFL (research/nfl_simulation).

- The margin distribution is the frozen model's key-number distribution around
  its expected margin, except that these sports cannot end tied (overtime; FBS
  since 1996): the mass at a 0 margin is removed and the rest renormalized. The
  frozen models themselves keep their fitted weights (they are frozen); only the
  simulation applies the rule.
- Pairs (u_favorite, total residual) come from TRAIN seasons of the model's own
  splits, with ESPN's closing total.
- Validation (VALIDATE seasons): the simulated joint of (home covers, over)
  against the independence product of its own marginals.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
from numpy.typing import NDArray

from ttk.domain import Sport
from ttk.research.espn_models import feature_values
from ttk.research.frozen import FrozenSpreadModel
from ttk.research.nfl_simulation import (
    JointValidation,
    SimRow,
    pairs_from_rows,
    validate_rows,
)

NO_TIES = frozenset({Sport.CFB, Sport.NBA, Sport.NCAAB})
JOINT_DEPENDENCE = {Sport.CFB: True, Sport.NBA: True, Sport.NCAAB: False}
"""Whether the TRAIN pairs keep their spread/total association (docs/MODELS.md,
validation 2026-10-05, 20,000 runs a game): CFB's beat independence (z -3.6),
NBA's made no difference (z +0.4), NCAAB's was not supported (z +1.5 against;
shuffled +0.3), so NCAAB's totals are shuffled against margins: spread and total
legs are independent there, while margin legs still share one margin."""


def independent_pairs(pairs: NDArray[np.float64], *, seed: int = 13) -> NDArray[np.float64]:
    """The same margins and totals with their pairing broken (residuals permuted)."""
    out = pairs.copy()
    if out.size:
        out[:, 1] = np.random.default_rng(seed).permutation(out[:, 1])
    return out


def without_ties(pmf: Mapping[int, float]) -> dict[int, float]:
    rest = 1.0 - pmf.get(0, 0.0)
    return {k: p / rest for k, p in pmf.items() if k != 0}


class FrozenMarginPmf:
    """mu -> the frozen model's margin distribution, with the sport's tie rule."""

    def __init__(self, model: FrozenSpreadModel) -> None:
        self.key = model.margin.key
        self.no_ties = Sport(model.data.config.sport) in NO_TIES
        self._cache: dict[float, dict[int, float]] = {}

    def __call__(self, mu: float) -> Mapping[int, float]:
        cached = self._cache.get(mu)
        if cached is not None:
            return cached
        pmf = self.key.pmf_at_mean(mu)
        out = without_ties(pmf) if self.no_ties else dict(pmf)
        if len(self._cache) < 50_000:
            self._cache[mu] = out
        return out


def frozen_rows(model: FrozenSpreadModel, window: tuple[int, int]) -> list[SimRow]:
    data = model.data
    games = {g.game_id: g for g in data.games}
    rows = []
    for game_id, pred in model.elo.items():
        g = games[game_id]
        if not (window[0] <= pred.season <= window[1]) or not g.played:
            continue
        line = data.closes.get(game_id)
        if line is None:
            continue
        assert g.home_score is not None and g.away_score is not None
        mu = model.margin.expected_margin(feature_values(pred, data))
        rows.append(SimRow(game_id, mu, g.home_score, g.away_score, line.home_spread, line.total))
    return sorted(rows, key=lambda r: r.game_id)


def fit_frozen_pairs(model: FrozenSpreadModel, *, seed: int = 7) -> NDArray[np.float64]:
    train = tuple(model.artifact["splits"]["train"])
    pairs = pairs_from_rows(
        frozen_rows(model, (train[0], train[1])), FrozenMarginPmf(model), seed=seed
    )
    if not JOINT_DEPENDENCE.get(Sport(model.data.config.sport), True):
        pairs = independent_pairs(pairs)
    return pairs


def validate_frozen_joint(
    model: FrozenSpreadModel, pairs: NDArray[np.float64], *, iterations: int = 10_000
) -> JointValidation:
    validate = tuple(model.artifact["splits"]["validate"])
    rows = frozen_rows(model, (validate[0], validate[1]))
    return validate_rows(rows, FrozenMarginPmf(model), pairs, iterations=iterations)
