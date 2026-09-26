"""Sport-agnostic Elo ratings, run strictly in time order.

Every prediction for a game is made from ratings before that game is played, and
ratings are updated only after it - so a single chronological pass is a
leakage-free walk-forward backtest by construction.

Update rule follows FiveThirtyEight's NFL Elo: a margin-of-victory multiplier
ln(|margin| + 1) scaled down for favorites that win (autocorrelation correction),
and regression toward the mean between seasons. All constants are parameters,
tuned on training seasons only.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class EloParams:
    k: float = 20.0
    home_field: float = 48.0
    """Elo points added to the home side (not at neutral sites)."""
    season_regression: float = 1 / 3
    """Fraction of the distance to the mean removed between seasons."""
    margin_of_victory: bool = True
    initial: float = 1500.0


@dataclass(frozen=True)
class EloGame:
    game_id: int
    season: int
    commence_time: datetime
    home_id: int
    away_id: int
    neutral_site: bool
    home_score: int | None
    away_score: int | None

    @property
    def played(self) -> bool:
        return self.home_score is not None and self.away_score is not None


@dataclass(frozen=True)
class EloPrediction:
    game_id: int
    season: int
    home_rating: float
    away_rating: float
    elo_diff: float
    """Home minus away, including home field."""
    home_win_probability: float


def win_probability(elo_diff: float) -> float:
    return 1.0 / (1.0 + math.pow(10.0, -elo_diff / 400.0))


def _mov_multiplier(margin: int, winner_elo_diff: float) -> float:
    return math.log(abs(margin) + 1.0) * (2.2 / (winner_elo_diff * 0.001 + 2.2))


def run_elo(
    games: Sequence[EloGame],
    params: EloParams,
    *,
    home_field_by_season: Mapping[int, float] | None = None,
) -> list[EloPrediction]:
    """Pregame predictions for every game (played or not), in time order.
    Unplayed games are predicted from the latest ratings and do not update them.

    ``home_field_by_season`` overrides ``params.home_field`` per season. The caller
    must build it from earlier seasons only (see research.nfl_elo.prior_home_field)."""
    ratings: dict[int, float] = {}
    last_season: dict[int, int] = {}
    predictions: list[EloPrediction] = []

    def rating(team: int, season: int) -> float:
        r = ratings.get(team, params.initial)
        if last_season.get(team, season) != season:
            r = params.initial + (1.0 - params.season_regression) * (r - params.initial)
        ratings[team] = r
        last_season[team] = season
        return r

    for g in sorted(games, key=lambda g: (g.commence_time, g.game_id)):
        home, away = rating(g.home_id, g.season), rating(g.away_id, g.season)
        hfa = (home_field_by_season or {}).get(g.season, params.home_field)
        diff = home - away + (0.0 if g.neutral_site else hfa)
        p_home = win_probability(diff)
        predictions.append(EloPrediction(g.game_id, g.season, home, away, diff, p_home))
        if not g.played:
            continue
        assert g.home_score is not None and g.away_score is not None
        margin = g.home_score - g.away_score
        actual = 1.0 if margin > 0 else 0.0 if margin < 0 else 0.5
        if params.margin_of_victory:
            winner_diff = diff if margin > 0 else -diff
            multiplier = _mov_multiplier(margin, winner_diff) if margin else 0.0
        else:
            multiplier = 1.0
        shift = params.k * multiplier * (actual - p_home)
        ratings[g.home_id] = home + shift
        ratings[g.away_id] = away - shift
    return predictions
