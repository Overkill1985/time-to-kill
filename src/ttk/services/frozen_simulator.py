"""Monte Carlo for the frozen card models (college football, NBA, college
basketball): the same engine as the NFL (models/simulation), with inputs from
research/espn_simulation.

With a spread market (``anchor``), the margin is centered where the card model's
market-anchored cover probability holds at the main line (for the NBA that view
includes the live injury report), so the simulation adds distribution shape and
joint structure but no unvalidated opinion. Without one, the center is the
frozen standalone margin model's expected margin.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta

import numpy as np
from numpy.typing import NDArray

from ttk.db.models import utcnow
from ttk.models.simulation import PRESETS, GameSimulation, simulate_game
from ttk.research.espn_simulation import FrozenMarginPmf, fit_frozen_pairs
from ttk.research.frozen import FrozenSpreadModel
from ttk.services.forward_test import ForwardModel


def home_cover_excluding_push(pmf: Mapping[int, float], home_line: float) -> float:
    win = sum(p for k, p in pmf.items() if k + home_line > 0)
    push = sum(p for k, p in pmf.items() if k + home_line == 0)
    return win / (1 - push) if push < 1 else 0.5


class FrozenSimulator:
    def __init__(
        self,
        card_model: ForwardModel,
        frozen: FrozenSpreadModel,
        pairs: NDArray[np.float64] | None = None,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if frozen.artifact.get("variant", "anchored") != "anchored":
            raise ValueError("The simulator centers on a market-anchored card model")
        self.card_model = card_model
        self.pmf = FrozenMarginPmf(frozen)
        self.pairs = fit_frozen_pairs(frozen) if pairs is None else pairs
        """(u_favorite, total residual) from the model's TRAIN seasons."""
        self._kickoffs = {g.game_id: g.commence_time for g in frozen.data.games}
        self._clock = clock

    def center_for(self, home_line: float, target: float) -> float:
        """The expected margin at which P(home covers | no push) at ``home_line``
        equals ``target`` (bisection; the cover probability rises with the mean)."""
        low, high = -60.0, 60.0
        for _ in range(40):
            mid = (low + high) / 2
            p = home_cover_excluding_push(self.pmf(mid), home_line)
            low, high = (mid, high) if p < target else (low, mid)
        return (low + high) / 2

    def simulate(
        self,
        game_id: int,
        total_line: float,
        *,
        iterations: int = PRESETS["quick"],
        seed: int | None = None,
        anchor: tuple[float, float] | None = None,
    ) -> GameSimulation | None:
        kickoff = self._kickoffs.get(game_id)
        if kickoff is None or self.pairs.size == 0:
            return None
        now = self._clock()
        horizon = 1 if kickoff - now <= timedelta(hours=1) else 24
        home_line, market = anchor if anchor is not None else (0.0, 0.5)
        view = self.card_model.view(game_id, home_line, market, now, horizon)
        if view is None or view.expected_margin is None:
            return None
        mu = (
            self.center_for(home_line, view.home_cover)
            if anchor is not None
            else view.expected_margin
        )
        return simulate_game(
            self.pmf(mu),
            total_line,
            self.pairs,
            home_favored=mu >= 0,
            iterations=iterations,
            seed=seed,
        )
