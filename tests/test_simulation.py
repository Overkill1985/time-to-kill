import math
from collections import Counter
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ttk import betting_math as bm
from ttk.db.models import GameSourceId, IngestionRun
from ttk.domain import Market, Selection, Sport
from ttk.models.margin import KeyNumberMarginModel, MarginModel, key_number_weights_for_means
from ttk.models.nfl_features import GameFeatures
from ttk.models.simulation import (
    GameSimulation,
    Leg,
    randomized_pit,
    simulate_game,
)
from ttk.providers.base import NormalizedGame, NormalizedOddsQuote, OddsFetch, TeamRef
from ttk.services.nfl_spread_predictor import SpreadView
from ttk.services.odds_ingest import store_odds
from ttk.services.parlay_lab import LegInput, evaluate_parlay
from ttk.services.simulation_service import SimulationError, run_simulation

MODEL = KeyNumberMarginModel(MarginModel(0.0, 0.0, 13.5), {3: 2.7, 7: 1.9, 0: 0.2})
PMF = MODEL.pmf_at_mean(-3.0)  # home is a 3-point underdog


def independent_pairs(n: int = 4000, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    residual = rng.normal(0, 13, n)
    return np.column_stack([rng.random(n), residual - np.median(residual)])


def test_seed_reproducible_and_seeds_differ() -> None:
    pairs = independent_pairs()
    a = simulate_game(PMF, 44.5, pairs, home_favored=False, iterations=5000, seed=3)
    b = simulate_game(PMF, 44.5, pairs, home_favored=False, iterations=5000, seed=3)
    c = simulate_game(PMF, 44.5, pairs, home_favored=False, iterations=5000, seed=4)
    assert np.array_equal(a.home, b.home) and np.array_equal(a.away, b.away)
    assert not np.array_equal(a.home, c.home)


def test_margin_distribution_matches_model() -> None:
    sim = simulate_game(
        PMF, 44.5, independent_pairs(), home_favored=False, iterations=200_000, seed=5
    )
    for margin in (-3, -7, 0, 3):
        observed = float(np.mean(sim.margin == margin))
        se = math.sqrt(PMF[margin] * (1 - PMF[margin]) / sim.iterations)
        assert abs(observed - PMF[margin]) < 4 * se + 1e-3


def test_scores_are_valid_and_total_centered_on_line() -> None:
    sim = simulate_game(
        PMF, 44.5, independent_pairs(), home_favored=False, iterations=100_000, seed=6
    )
    assert (sim.home >= 0).all() and (sim.away >= 0).all()
    assert np.array_equal(sim.home - sim.away, sim.margin)
    over = sim.side(Leg("TOTAL", "OVER", 44.5)).win
    assert abs(over.value - 0.5) < 0.02  # a market total is a 50/50 point


def test_moneyline_and_spread_same_team_are_nested() -> None:
    sim = simulate_game(
        PMF, 44.5, independent_pairs(), home_favored=False, iterations=50_000, seed=7
    )
    cover = Leg("SPREAD", "AWAY", -3.5)  # away laying 3.5
    win = Leg("MONEYLINE", "AWAY", None)
    joint = sim.joint([cover, win]).value
    assert joint == pytest.approx(sim.side(cover).win.value)  # covering -3.5 implies winning
    assert joint > sim.side(cover).win.value * sim.side(win).win.value  # not independent


def test_copula_carries_favorite_over_association() -> None:
    n = 6000
    rng = np.random.default_rng(8)
    u = rng.random(n)
    residual = (u - 0.5) * 40 + rng.normal(0, 3, n)  # favorite outperforming -> over
    pairs = np.column_stack([u, residual - np.median(residual)])
    for home_favored, fav in ((True, "HOME"), (False, "AWAY")):
        pmf = MODEL.pmf_at_mean(6.0 if home_favored else -6.0)
        sim = simulate_game(pmf, 44.5, pairs, home_favored=home_favored, iterations=40_000, seed=9)
        line = -6.5
        legs = [Leg("SPREAD", fav, line), Leg("TOTAL", "OVER", 44.5)]
        joint = sim.joint(legs).value
        product = sim.side(legs[0]).win.value * sim.side(legs[1]).win.value
        assert joint > product + 0.05


def test_randomized_pit_is_uniform() -> None:
    rng = np.random.default_rng(10)
    margins = np.array(sorted(PMF))
    probs = np.array([PMF[m] for m in margins])
    draws = rng.choice(margins, size=20_000, p=probs / probs.sum())
    u = np.array([randomized_pit(PMF, int(m), rng) for m in draws])
    assert abs(u.mean() - 0.5) < 0.01 and abs(u.std() - math.sqrt(1 / 12)) < 0.01


def test_max_acceptable_line() -> None:
    sim = GameSimulation(np.array([20, 24, 27, 30] * 250), np.array([17, 17, 17, 17] * 250))
    # home margins: 3, 7, 10, 13 equally likely
    d = bm.american_to_decimal(-110)
    candidates = [-2.5, -3.0, -3.5, -6.5, -7.0, -9.5, -12.5]
    worst = sim.max_acceptable_line("SPREAD", "HOME", d, candidates)
    # -6.5: wins 75% -> +EV; -7: 50% win, 25% push, 25% loss -> +EV; -9.5: 50% -> -EV
    assert worst == -7.0
    assert sim.max_acceptable_line("TOTAL", "OVER", d, [30.5, 40.5, 50.5]) == 40.5


def test_simulate_game_validates_inputs() -> None:
    with pytest.raises(ValueError):
        simulate_game(PMF, 44.5, np.empty((0, 2)), home_favored=True)
    with pytest.raises(ValueError):
        simulate_game(PMF, 44.5, independent_pairs(), home_favored=True, iterations=0)


def test_key_number_weights_for_means_shape() -> None:
    weights = key_number_weights_for_means([0.0] * 300, 13.5, [3] * 100 + [5] * 200)
    assert weights[5] > weights[3] > 0  # 5 was observed twice as often as 3


# --------------------------------------------------------------------------- integration

KICK = datetime.now(UTC) + timedelta(days=2)


def seed_game(sf: sessionmaker[Session], sport: Sport = Sport.NFL) -> int:
    game = NormalizedGame("espn", "s1", sport, TeamRef("Home"), TeamRef("Away"), KICK, None)
    quotes = [
        NormalizedOddsQuote("fake", "s1", book, book, market, sel, line, price, None)
        for book in ("draftkings", "fanduel", "betmgm")
        for market, sel, line, price in [
            (Market.SPREAD, Selection.HOME, 3.5, -110),
            (Market.SPREAD, Selection.AWAY, -3.5, -110),
            (Market.MONEYLINE, Selection.HOME, None, 150),
            (Market.MONEYLINE, Selection.AWAY, None, -180),
            (Market.TOTAL, Selection.OVER, 44.5, -110),
            (Market.TOTAL, Selection.UNDER, 44.5, -110),
        ]
    ]
    with sf() as s:
        run = IngestionRun(provider="fake", kind="odds", sport=sport)
        s.add(run)
        s.flush()
        store_odds(
            s,
            OddsFetch([game], quotes, {}),
            run=run,
            observed_at=datetime.now(UTC) - timedelta(minutes=2),
            stats=Counter(),
        )
        s.commit()
        return int(s.scalar(select(GameSourceId.game_id)) or 0)


class FakeSimPredictor:
    def __init__(self) -> None:
        self.anchors: list[tuple[float, float] | None] = []

    def spread(self, game_id: int, home_line: float, market: float) -> SpreadView:
        return SpreadView(0.45, 0.0, -3.0, GameFeatures(game_id, 0, 0, None, None, 5, 5), 0.0)

    def simulate(
        self,
        game_id: int,
        total_line: float,
        *,
        iterations: int = 10_000,
        seed: int | None = None,
        anchor: tuple[float, float] | None = None,
    ) -> GameSimulation:
        self.anchors.append(anchor)
        return simulate_game(
            PMF,
            total_line,
            independent_pairs(),
            home_favored=False,
            iterations=iterations,
            seed=seed,
        )


def test_parlay_lab_simulates_same_game_nfl(session_factory: sessionmaker[Session]) -> None:
    gid = seed_game(session_factory)
    fake = FakeSimPredictor()
    with session_factory() as s:
        a = evaluate_parlay(
            s,
            [
                LegInput(gid, Market.SPREAD, Selection.AWAY, -3.5),
                LegInput(gid, Market.MONEYLINE, Selection.AWAY),
            ],
            "draftkings",
            predictor=fake,
        )
    assert len(a.simulated) == 1 and a.simulated[0].lift > 1.2
    wins = [leg.probability * (1 - leg.push_probability) for leg in a.legs]
    assert a.joint_probability <= min(wins) + 1e-9  # nested legs: joint <= weaker leg
    assert a.joint_probability > wins[0] * wins[1]  # far above the independence product
    assert any("simulations" in w for w in a.warnings)
    (anchor,) = fake.anchors  # centered on the main spread
    assert anchor is not None and anchor[0] == 3.5 and anchor[1] == pytest.approx(0.5)


def test_simulation_service(session_factory: sessionmaker[Session]) -> None:
    gid = seed_game(session_factory)
    with session_factory() as s:
        summary = run_simulation(s, gid, FakeSimPredictor(), iterations=20_000, seed=1)
    assert summary.total_line == 44.5 and summary.total_line_source == "market main total"
    assert len(summary.spread_sensitivity) == 13 and summary.spread_sensitivity[6].line == 3.5
    assert set(summary.joint) == {
        "home_covers_and_over",
        "home_covers_and_under",
        "away_covers_and_over",
        "away_covers_and_under",
    }
    assert sum(p.value for p in summary.joint.values()) <= 1.0
    assert {side.sportsbook for side in summary.spread_sides} <= {"draftkings", "fanduel", "betmgm"}
    with session_factory() as s, pytest.raises(SimulationError, match="between"):
        run_simulation(s, gid, FakeSimPredictor(), iterations=10)


def test_simulation_service_rejects_other_sports(session_factory: sessionmaker[Session]) -> None:
    gid = seed_game(session_factory, sport=Sport.CFB)
    with session_factory() as s, pytest.raises(SimulationError, match="NFL"):
        run_simulation(s, gid, FakeSimPredictor())
