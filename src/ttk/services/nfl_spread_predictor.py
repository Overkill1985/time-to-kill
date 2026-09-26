"""Live NFL spread probabilities from the validated research model.

Uses exactly the artifact that was validated: the market-anchored EPA + QB model
fitted on TRAIN seasons (research.nfl_elo.backtest). Nothing is refitted on
validation or test seasons, so the sealed test split stays untouched. Ratings
and features are state, not fitted parameters: they run walk-forward through
the latest completed game, so upcoming games use everything played so far.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from sqlalchemy import select
from sqlalchemy.orm import Session

from ttk.db.models import ModelVersion
from ttk.domain import Sport
from ttk.models.elo import EloPrediction
from ttk.models.nfl_features import GameFeatures
from ttk.models.simulation import PRESETS, GameSimulation, simulate_game
from ttk.research.nfl_elo import (
    DEFAULT_SPLITS,
    BacktestReport,
    backtest,
    load_feature_inputs,
    load_nfl_games,
)
from ttk.research.nfl_simulation import fit_pairs

MODEL_NAME = "nfl-spread-market-anchored-features"
MODEL_VERSION = "0.1.0"
CANDIDATE = "market_anchored_features"


@dataclass(frozen=True)
class SpreadView:
    home_cover: float
    """P(home covers | no push)."""
    push: float
    expected_margin: float
    features: GameFeatures
    elo_diff: float


class NflSpreadPredictor:
    def __init__(
        self,
        report: BacktestReport,
        predictions: dict[int, EloPrediction],
        starters: dict[tuple[int, int], str],
        pairs: NDArray[np.float64] | None = None,
    ) -> None:
        self.report = report
        self._predictions = predictions
        self.starters = starters
        self.pairs = pairs
        """Simulation copula pairs fitted on TRAIN (research.nfl_simulation)."""
        features, anchored = report.models.features, report.models.anchored_features
        if features is None or anchored is None:
            raise ValueError("The NFL spread model needs play-by-play features")
        self._features = features
        self._anchored = anchored

    @classmethod
    def build(cls, session: Session) -> NflSpreadPredictor | None:
        """None when NFL history or play-by-play aggregates are missing."""
        games, lines = load_nfl_games(session)
        inputs = load_feature_inputs(session)
        if not games or not inputs.available:
            return None
        report = backtest(games, lines, inputs=inputs)  # test seasons stay sealed
        ordered = report.tuned.run(games)
        by_id = {g.game_id: g for g in games}
        pairs = fit_pairs(ordered, by_id, lines, report.models, DEFAULT_SPLITS.train)
        return cls(report, {p.game_id: p for p in ordered}, inputs.starters, pairs)

    def simulate(
        self,
        game_id: int,
        total_line: float,
        *,
        iterations: int = PRESETS["quick"],
        seed: int | None = None,
        anchor: tuple[float, float] | None = None,
    ) -> GameSimulation | None:
        """Monte Carlo for one game: key-number margin distribution, totals around
        ``total_line`` (the market), joined by the TRAIN copula pairs.

        ``anchor`` = (home line, market no-vig P(home covers)) at the main spread.
        With it, the margin is centered where the validated market-anchored model's
        cover probability holds, so the simulation adds shape, key numbers and joint
        structure but no unvalidated opinion. Without it, the center is the
        standalone feature model's expected margin (validated worse than the
        market; used only when no spread market exists). None without inputs."""
        p = self._predictions.get(game_id)
        f = self.report.models.game_features.get(game_id)
        if p is None or f is None or self.pairs is None or self.pairs.size == 0:
            return None
        mu = self._features.expected_margin(p, f)
        if anchor is not None:
            home_line, market_home_cover = anchor
            target = self._anchored.home_cover_probability(market_home_cover, mu + home_line)
            mu = self._center_for(home_line, target)
        return simulate_game(
            self._features.key.pmf_at_mean(mu),
            total_line,
            self.pairs,
            home_favored=mu >= 0,
            iterations=iterations,
            seed=seed,
        )

    def spread(self, game_id: int, home_line: float, market_home_cover: float) -> SpreadView | None:
        p = self._predictions.get(game_id)
        f = self.report.models.game_features.get(game_id)
        if p is None or f is None:
            return None
        mu = self._features.expected_margin(p, f)
        push = self._features.key.spread_at_mean(home_line, mu).push
        home_cover = self._anchored.home_cover_probability(market_home_cover, mu + home_line)
        return SpreadView(home_cover, push, mu, f, p.elo_diff)

    def _center_for(self, home_line: float, target: float) -> float:
        """The expected margin at which P(home covers | no push) at ``home_line``
        equals ``target`` (bisection; the cover probability rises with the mean)."""
        low, high = -60.0, 60.0
        for _ in range(40):
            mid = (low + high) / 2
            p = self._features.key.spread_at_mean(home_line, mid).home_cover_excluding_push
            low, high = (mid, high) if p < target else (low, mid)
        return (low + high) / 2

    def ensure_registered(self, session: Session) -> ModelVersion:
        """The registry row predictions point at. Created as DEVELOPMENT with its
        validation metrics; its status is whatever the registry says after that."""
        row = session.scalar(
            select(ModelVersion).where(
                ModelVersion.name == MODEL_NAME, ModelVersion.version == MODEL_VERSION
            )
        )
        if row is not None:
            return row
        candidate = next(c for c in self.report.validate.spread_candidates if c.name == CANDIDATE)
        s = DEFAULT_SPLITS
        row = ModelVersion(
            name=MODEL_NAME,
            version=MODEL_VERSION,
            sport=Sport.NFL,
            market="SPREAD",
            algorithm="logistic(market no-vig, feature-model disagreement)",
            features=[
                "elo_diff",
                "epa_net_diff_pts",
                "qb_change_diff_pts",
                "market_no_vig_same_snapshot",
            ],
            training_window=f"{s.train[0]}-{s.train[1]}",
            validation_window=f"{s.validate[0]}-{s.validate[1]}",
            calibration_method="fitted on TRAIN",
            brier_score=candidate.spread.brier,
            log_loss=candidate.spread.log_loss,
            status="DEVELOPMENT",
        )
        session.add(row)
        session.flush()
        return row
