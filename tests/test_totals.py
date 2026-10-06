from datetime import UTC, datetime, timedelta

import pytest

from ttk.models.elo import EloGame
from ttk.research.totals import (
    Calibration,
    TeamGame,
    TotalRow,
    TotalsParams,
    fit_calibration,
    p_over,
    walk_forward,
)

T0 = datetime(2024, 11, 5, tzinfo=UTC)


def game(gid: int, home: int, away: int, day: int, hs: int = 70, aws: int = 60) -> EloGame:
    return EloGame(gid, 2025, T0 + timedelta(days=day), home, away, False, hs, aws)


def test_predictions_use_only_earlier_games() -> None:
    games = [game(1, 1, 2, 0), game(2, 1, 2, 1), game(3, 1, 2, 2)]
    stats = [
        TeamGame(1, 1, 80, 100),  # a fast, high-scoring first game
        TeamGame(1, 2, 80, 90),
        TeamGame(2, 1, 60, 60),
        TeamGame(2, 2, 60, 60),
    ]
    params = TotalsParams(prior_possessions=1e-9, prior_games=1e-9, opponent_adjust=False)
    preds = walk_forward(games, stats, params)
    # Game 2 sees only game 1. Pace is additive around the league (still half its
    # 68-possession prior after one game): 80 + 80 - 74 = 86.
    league_pace = (80 + 68) / 2
    assert preds[2].possessions == pytest.approx(80 + 80 - league_pace)
    lp = (100 + 90 + 1) / (160 + 1)  # league points per possession, prior 1 at 1.0
    home_ppp = 100 / 80 + 100 / 80 - lp  # team 1's offense + team 2's defense
    away_ppp = 90 / 80 + 90 / 80 - lp
    assert preds[2].total == pytest.approx((80 + 80 - league_pace) * (home_ppp + away_ppp))
    assert preds[1].home_games == 0 and preds[2].home_games == 1 and preds[3].home_games == 2
    # Changing a later game never changes an earlier prediction.
    later = walk_forward(
        games, [*stats[:2], TeamGame(2, 1, 60, 10), TeamGame(2, 2, 60, 10)], params
    )
    assert later[2] == preds[2] and later[3] != preds[3]


def test_opponent_adjustment_credits_a_soft_defense() -> None:
    # Team 3 allows a lot to team 1; team 2 then scores the same against team 3.
    games = [game(1, 1, 3, 0), game(2, 2, 3, 1), game(3, 1, 2, 2)]
    stats = [
        TeamGame(1, 1, 70, 98),
        TeamGame(1, 3, 70, 70),
        TeamGame(2, 2, 70, 98),
        TeamGame(2, 3, 70, 70),
    ]
    plain = walk_forward(games, stats, TotalsParams(opponent_adjust=False))
    adjusted = walk_forward(games, stats, TotalsParams(opponent_adjust=True))
    assert adjusted[3].total < plain[3].total  # their scoring came partly from team 3's defense


def test_p_over_and_calibration() -> None:
    cal = Calibration(0.0, 1.0, 10.0)
    assert p_over(cal, 140.0, 140.5) < 0.5 < p_over(cal, 141.0, 140.5)
    assert p_over(cal, 140.0, 140.0) == pytest.approx(0.5)  # whole line: push mass removed evenly
    rows = [
        TotalRow(i, 2025, float(p), 2.0 * p + 5, 140.5, 0.5, -110, -110, 10)
        for i, p in enumerate(range(60, 90))
    ]
    fitted = fit_calibration(rows)
    assert fitted.slope == pytest.approx(2.0) and fitted.intercept == pytest.approx(5.0)
    assert fitted.sigma == pytest.approx(0.0, abs=1e-6)
