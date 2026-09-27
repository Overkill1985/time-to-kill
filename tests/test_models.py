import math
from datetime import UTC, datetime, timedelta

import pytest

from ttk.models.elo import EloGame, EloParams, run_elo, win_probability
from ttk.models.margin import MarginModel, fit_margin_model
from ttk.models.metrics import calibration_table, score

T0 = datetime(2020, 9, 10, 17, tzinfo=UTC)


def game(
    gid: int,
    home: int,
    away: int,
    hs: int | None,
    as_: int | None,
    *,
    season: int = 2020,
    days: int = 0,
    neutral: bool = False,
) -> EloGame:
    return EloGame(gid, season, T0 + timedelta(days=days), home, away, neutral, hs, as_)


class TestElo:
    def test_win_probability(self) -> None:
        assert win_probability(0) == pytest.approx(0.5)
        assert win_probability(400) == pytest.approx(10 / 11)

    def test_home_field_and_neutral(self) -> None:
        params = EloParams(home_field=50)
        preds = run_elo(
            [game(1, 1, 2, None, None), game(2, 3, 4, None, None, neutral=True)], params
        )
        assert preds[0].elo_diff == 50 and preds[1].elo_diff == 0

    def test_prediction_uses_only_prior_games(self) -> None:
        # Game 2's prediction must not see game 2's own result.
        games = [game(1, 1, 2, 30, 0, days=0), game(2, 1, 2, 0, 30, days=7)]
        preds = {p.game_id: p for p in run_elo(games, EloParams(home_field=0))}
        assert preds[1].home_win_probability == pytest.approx(0.5)
        assert preds[2].home_win_probability > 0.5  # team 1 won game 1; result of 2 unseen
        # Changing a later result never changes an earlier prediction.
        flipped = [games[0], game(2, 1, 2, 30, 0, days=7)]
        assert {p.game_id: p for p in run_elo(flipped, EloParams(home_field=0))}[2] == preds[2]

    def test_zero_sum_and_tie_no_update(self) -> None:
        games = [
            game(1, 1, 2, 24, 17),
            game(2, 1, 2, 10, 10, days=7),
            game(3, 1, 2, None, None, days=14),
        ]
        preds = run_elo(games, EloParams(home_field=0))
        assert preds[1].home_rating + preds[1].away_rating == pytest.approx(3000)
        assert preds[2].home_rating == pytest.approx(preds[1].home_rating)  # tie: no change

    def test_input_order_does_not_matter(self) -> None:
        games = [game(1, 1, 2, 21, 3), game(2, 2, 3, 14, 10, days=7), game(3, 3, 1, 7, 9, days=14)]
        assert run_elo(games, EloParams()) == run_elo(list(reversed(games)), EloParams())

    def test_season_regression(self) -> None:
        games = [game(1, 1, 2, 40, 0), game(2, 1, 2, None, None, season=2021, days=200)]
        p = EloParams(home_field=0, season_regression=0.5, margin_of_victory=False)
        preds = run_elo(games, p)
        gained = 20 * (1 - 0.5)  # K x (1 - 0.5): even game won
        assert preds[1].home_rating == pytest.approx(1500 + gained * 0.5)


class TestMargin:
    MODEL = MarginModel(intercept=0.0, slope=1 / 25, sigma=13.5)

    def test_half_point_line_has_no_push(self) -> None:
        probs = self.MODEL.spread(-3.5, rating_diff=0.0)
        assert probs.push == 0.0
        assert probs.home_cover + probs.away_cover == pytest.approx(1.0)
        assert probs.home_cover < 0.5  # even teams, home laying 3.5

    def test_whole_number_line_has_push(self) -> None:
        probs = self.MODEL.spread(-3.0, rating_diff=0.0)
        assert 0 < probs.push < 0.05
        assert probs.home_cover + probs.push + probs.away_cover == pytest.approx(1.0)
        assert probs.home_cover_excluding_push == pytest.approx(
            probs.home_cover / (probs.home_cover + probs.away_cover)
        )

    def test_pick_em_is_symmetric(self) -> None:
        probs = self.MODEL.spread(0.0, rating_diff=0.0)
        assert probs.home_cover == pytest.approx(probs.away_cover)

    def test_stronger_home_covers_more(self) -> None:
        assert self.MODEL.spread(-3.5, 200).home_cover > self.MODEL.spread(-3.5, 0).home_cover

    def test_fit_recovers_known_line(self) -> None:
        diffs = [float(d) for d in range(-300, 301, 10)] * 3
        noise = [(-5.0, 0.0, 5.0)[i % 3] for i in range(len(diffs))]
        margins = [2 + d / 25 + e for d, e in zip(diffs, noise, strict=True)]
        m = fit_margin_model(diffs, margins)
        assert m.slope == pytest.approx(1 / 25) and m.intercept == pytest.approx(2)
        assert m.sigma == pytest.approx(math.sqrt(50 / 3), rel=0.02)

    def test_fit_needs_data(self) -> None:
        with pytest.raises(ValueError):
            fit_margin_model([1.0] * 5, [1.0] * 5)


class TestMetrics:
    def test_score(self) -> None:
        s = score([0.5, 0.5], [1, 0])
        assert s.n == 2 and s.brier == pytest.approx(0.25)
        assert s.log_loss == pytest.approx(math.log(2))

    def test_perfect_and_empty(self) -> None:
        assert score([1.0, 0.0], [1, 0]).brier == 0.0
        assert score([], []).n == 0

    def test_calibration_table(self) -> None:
        rows = calibration_table([0.15, 0.18, 0.72, 1.0], [0, 1, 1, 1])
        assert [(r.low, r.n) for r in rows] == [(0.1, 2), (0.7, 1), (0.9, 1)]
        assert rows[0].observed_rate == 0.5


def test_elo_regression_toward_recent_seasons() -> None:
    """'recent' regresses toward the team's own recent season-end average, so a
    weaker tier (FCS) is not pulled up to the global mean every offseason."""
    from datetime import UTC, datetime, timedelta

    from ttk.models.elo import EloGame, EloParams, run_elo

    t0 = datetime(2020, 9, 1, tzinfo=UTC)
    # Team 1 beats team 2 by 30 in each of 3 seasons, then they meet again.
    games = [
        EloGame(i, 2020 + i, t0 + timedelta(days=365 * i), 1, 2, True, 40, 10) for i in range(4)
    ]
    mean = run_elo(games, EloParams(k=20, season_regression=0.5))
    recent = run_elo(games, EloParams(k=20, season_regression=0.5, regression_target="recent"))
    # Season 2: with one season-end on record, 'recent' targets the team's own
    # rating (no pull); 'mean' halves the distance to 1500 from the same end rating.
    end_of_first = recent[1].home_rating
    assert mean[1].home_rating == pytest.approx(1500 + 0.5 * (end_of_first - 1500))
    assert end_of_first > mean[1].home_rating > 1500
    # By season 4 the gap persists: team 2 is not dragged back to 1500 each year.
    assert recent[3].away_rating < mean[3].away_rating < 1500
