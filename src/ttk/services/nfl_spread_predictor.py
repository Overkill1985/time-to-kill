"""Live NFL spread probabilities from the validated research model.

Uses exactly the artifact that was validated: the market-anchored EPA + QB model
fitted on TRAIN seasons (research.nfl_elo.backtest). Nothing is refitted on
validation or test seasons, so the sealed test split stays untouched. Ratings
and features are state, not fitted parameters: they run walk-forward through
the latest completed game, so upcoming games use everything played so far.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ttk.db.models import ModelVersion
from ttk.domain import Sport
from ttk.models.elo import EloPrediction
from ttk.models.nfl_features import GameFeatures
from ttk.research.nfl_elo import (
    DEFAULT_SPLITS,
    BacktestReport,
    backtest,
    load_feature_inputs,
    load_nfl_games,
)

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
    ) -> None:
        self.report = report
        self._predictions = predictions
        self.starters = starters
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
        predictions = {p.game_id: p for p in report.tuned.run(games)}
        return cls(report, predictions, inputs.starters)

    def spread(self, game_id: int, home_line: float, market_home_cover: float) -> SpreadView | None:
        p = self._predictions.get(game_id)
        f = self.report.models.game_features.get(game_id)
        if p is None or f is None:
            return None
        mu = self._features.expected_margin(p, f)
        push = self._features.key.spread_at_mean(home_line, mu).push
        home_cover = self._anchored.home_cover_probability(market_home_cover, mu + home_line)
        return SpreadView(home_cover, push, mu, f, p.elo_diff)

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
