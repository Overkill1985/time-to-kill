"""Market-anchored cover model: start from the market's no-vig probability and let
a rating model move it only as far as history says its disagreement is worth.

    logit P(home covers | no push) = a + b * logit(market) + c * disagreement

where disagreement = model expected margin + home line (points by which the
rating model expects home to beat the spread). With c = 0 and b = 1, a = 0 this
is exactly the market; the fit decides how much to trust the rating model.

Leakage rule: the market input must be the same price snapshot the bet is
placed at (or earlier). Never a later line - never the close for an earlier bet.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from sklearn.linear_model import LogisticRegression


def logit(p: float) -> float:
    p = min(max(p, 1e-9), 1 - 1e-9)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


@dataclass(frozen=True)
class MarketAnchoredModel:
    intercept: float
    market_coef: float
    disagreement_coef: float
    n_train: int

    def home_cover_probability(self, market_probability: float, disagreement: float) -> float:
        return sigmoid(
            self.intercept
            + self.market_coef * logit(market_probability)
            + self.disagreement_coef * disagreement
        )


def fit_market_anchored(
    market_probabilities: Sequence[float],
    disagreements: Sequence[float],
    outcomes: Sequence[int],
) -> MarketAnchoredModel:
    """Unpenalized logistic regression (large C). Needs both outcomes present."""
    if len(outcomes) < 100 or len(set(outcomes)) < 2:
        raise ValueError("Too few or one-sided outcomes to fit a market-anchored model")
    x = np.column_stack(
        [[logit(p) for p in market_probabilities], np.asarray(disagreements, dtype=float)]
    )
    lr = LogisticRegression(C=1e6, max_iter=1000)
    lr.fit(x, np.asarray(outcomes))
    return MarketAnchoredModel(
        intercept=float(lr.intercept_[0]),
        market_coef=float(lr.coef_[0][0]),
        disagreement_coef=float(lr.coef_[0][1]),
        n_train=len(outcomes),
    )
