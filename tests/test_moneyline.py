import pytest

from ttk import betting_math as bm
from ttk.models.anchored import MarketAnchoredModel
from ttk.models.margin import KeyNumberMarginModel, MarginModel
from ttk.research.moneyline import (
    MlRow,
    center_for,
    evaluate,
    home_win_excluding_tie,
    no_vig,
    probabilities,
)

KEY = KeyNumberMarginModel(MarginModel(0.0, 0.0, 13.5), {3: 1.6, 7: 1.3, 0: 0.4})
PMF = KEY.pmf_at_mean
NEUTRAL = MarketAnchoredModel(0.0, 1.0, 0.0, 1000)  # follows the market exactly


def test_home_win_leaves_ties_out() -> None:
    assert home_win_excluding_tie({-1: 0.3, 0: 0.2, 2: 0.5}) == pytest.approx(0.5 / 0.8)
    assert home_win_excluding_tie(PMF(0.0)) == pytest.approx(0.5, abs=1e-9)


def test_center_for_hits_the_cover_target() -> None:
    mu = center_for(PMF, -3.5, 0.55)
    p = PMF(mu)
    win = sum(v for k, v in p.items() if k - 3.5 > 0)
    assert win == pytest.approx(0.55, abs=1e-6)  # a half-point line cannot push


def test_spread_implied_moneyline_carries_no_model_opinion() -> None:
    row = MlRow(
        1,
        mu=10.0,
        home_spread=-3.5,
        market_home_cover=0.5,
        home_moneyline=-180,
        away_moneyline=160,
        home_score=24,
        away_score=20,
    )
    probs = probabilities(row, PMF, NEUTRAL)
    # The model's own margin (+10) moves the standalone price but not the others.
    assert probs["standalone"] > probs["spread_implied"]
    assert probs["anchored"] == pytest.approx(probs["spread_implied"], abs=1e-6)
    assert 0.6 < probs["spread_implied"] < 0.7  # a 3.5-point favorite


def test_evaluate_scores_paired_and_bets_the_favored_side() -> None:
    rows = [
        MlRow(1, 10.0, -3.5, 0.5, -110, -110, 30, 10),  # home wins at even money
        MlRow(2, 10.0, -3.5, 0.5, -110, -110, 10, 30),
        MlRow(3, 0.0, 0.0, 0.5, -110, -110, 20, 20),  # tie: left out
        MlRow(4, 0.0, None, None, -110, -110, 20, 10),  # no spread: left out
        MlRow(5, 0.0, -3.0, 0.5, 50, -110, 20, 10),  # not a real price: left out
    ]
    scores = evaluate(rows, PMF, NEUTRAL)
    s = scores["standalone"]
    assert s.games == 2
    assert s.bets[0].bets == 2 and s.bets[0].wins == 1  # home both times
    assert s.bets[0].units == pytest.approx(bm.american_to_decimal(-110) - 2)
    assert scores["spread_implied"].games == 2
    assert no_vig(50, -110) is None
