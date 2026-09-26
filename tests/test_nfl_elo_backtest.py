import math
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ttk.db.models import ReportedLine
from ttk.models.anchored import fit_market_anchored
from ttk.models.elo import EloGame, EloParams
from ttk.models.margin import MarginModel, fit_key_number_weights
from ttk.research.nfl_elo import Splits, backtest, prior_home_field

SPLITS = Splits(burn_in=(2000, 2000), train=(2001, 2003), validate=(2004, 2004), test=(2005, 2005))


def synthetic_league() -> tuple[list[EloGame], dict[int, ReportedLine]]:
    """8 teams with fixed true strengths; the market line is the true margin."""
    rng = random.Random(7)
    strength = {t: (t - 4.5) * 2.0 for t in range(1, 9)}
    games, lines, gid = [], {}, 0
    start = datetime(2000, 9, 1, 17, tzinfo=UTC)
    for season in range(2000, 2006):
        for week in range(16):
            teams = list(strength)
            rng.shuffle(teams)
            for home, away in zip(teams[::2], teams[1::2], strict=True):
                gid += 1
                true_margin = strength[home] - strength[away] + 2.0
                margin = round(rng.gauss(true_margin, 13.0))
                kickoff = start + timedelta(days=365 * (season - 2000) + 7 * week)
                games.append(EloGame(gid, season, kickoff, home, away, False, 20 + margin, 20))
                lines[gid] = ReportedLine(
                    game_id=gid,
                    provider="nflverse",
                    home_spread=-round(true_margin * 2) / 2,
                    home_spread_odds=-110,
                    away_spread_odds=-110,
                )
    return games, lines


def test_test_split_is_sealed_by_default() -> None:
    games, lines = synthetic_league()
    assert backtest(games, lines, splits=SPLITS, params=EloParams()).test is None
    sealed_open = backtest(games, lines, splits=SPLITS, params=EloParams(), include_test=True)
    assert sealed_open.test is not None


def test_candidates_compared_on_same_games_with_sound_accounting() -> None:
    games, lines = synthetic_league()
    r = backtest(games, lines, splits=SPLITS, params=EloParams()).validate
    names = [c.name for c in r.spread_candidates]
    assert names == ["elo_normal", "elo_key_numbers", "market_anchored"]
    assert len({c.spread.n for c in r.spread_candidates} | {r.spread_market.n}) == 1
    for c in r.spread_candidates:
        for b in c.betting:
            assert b.bets == b.wins + b.losses + b.pushes
            # every bet is at -110: a win pays 100/110, a loss costs 1, a push 0
            assert b.units == pytest.approx(b.wins * (100 / 110) - b.losses)
        counts = [b.bets for b in c.betting]
        assert counts == sorted(counts, reverse=True)  # higher threshold, fewer bets
    anchored = r.spread_candidates[2]
    assert anchored.push_rate_predicted is None  # it does not model pushes


def test_market_equal_to_truth_is_hard_to_beat() -> None:
    games, lines = synthetic_league()
    r = backtest(games, lines, splits=SPLITS, params=EloParams()).validate
    for c in r.spread_candidates:
        assert r.spread_market.log_loss <= c.spread.log_loss + 0.01


def test_prior_home_field_never_uses_its_own_season() -> None:
    games, _ = synthetic_league()
    before = prior_home_field(games)
    # Blow up every 2003 result: 2003's own value must not move; 2004's must.
    changed = [replace(g, home_score=90) if g.season == 2003 else g for g in games]
    after = prior_home_field(changed)
    assert after[2003] == before[2003]
    assert after[2004] > before[2004]
    assert 2000 not in before  # no earlier seasons, no estimate


def test_key_number_weights_learn_excess_threes() -> None:
    base = MarginModel(intercept=0.0, slope=0.04, sigma=13.5)
    rng = random.Random(3)
    diffs, margins = [], []
    for _ in range(6000):
        d = rng.uniform(-200, 200)
        m = round(rng.gauss(base.expected_margin(d), 13.5))
        if rng.random() < 0.08:
            m = 3 if m > 0 else -3  # inject a key number
        diffs.append(d)
        margins.append(m)
    model = fit_key_number_weights(base, diffs, margins)
    assert model.weights[3] > 1.5
    assert 0.8 < model.weights[5] < 1.2
    pmf = model.pmf(0.0)
    assert sum(pmf.values()) == pytest.approx(1.0)
    assert model.spread(-3.0, 0.0).push > base.spread(-3.0, 0.0).push


def test_market_anchored_trusts_market_when_rating_adds_nothing() -> None:
    rng = random.Random(11)
    market, disagreement, outcomes = [], [], []
    for _ in range(20000):
        p = rng.uniform(0.35, 0.65)
        market.append(p)
        disagreement.append(rng.gauss(0, 4))  # pure noise
        outcomes.append(int(rng.random() < p))
    model = fit_market_anchored(market, disagreement, outcomes)
    assert model.market_coef == pytest.approx(1.0, abs=0.15)
    assert abs(model.disagreement_coef) < 0.01
    assert model.home_cover_probability(0.6, 0.0) == pytest.approx(0.6, abs=0.02)
    assert math.isclose(model.intercept, 0.0, abs_tol=0.05)


def test_market_anchored_needs_data() -> None:
    with pytest.raises(ValueError):
        fit_market_anchored([0.5] * 10, [0.0] * 10, [1] * 10)


def test_paired_log_loss() -> None:
    from ttk.research.nfl_elo import paired_log_loss

    same = paired_log_loss([0.6, 0.4], [0.6, 0.4], [1, 0])
    assert same.mean_log_loss_diff == 0.0 and same.z == 0.0
    better = paired_log_loss([0.7, 0.3, 0.7], [0.5, 0.5, 0.5], [1, 0, 1])
    assert better.mean_log_loss_diff < 0 and better.standard_error >= 0
