"""Run and summarize a Monte Carlo simulation for one game (docs spec 27-28).

Currently NFL only: the simulation needs the validated margin model. The margin
is centered where the validated market-anchored model's cover probability holds
at the market main spread; the total is centered on the market main total; line sensitivity and the
maximum acceptable line are evaluated at the best bettable price at the main
line (the price is held fixed across nearby lines - books reprice alternates,
so treat far lines as indicative).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np
from sqlalchemy.orm import Session

from ttk import betting_math as bm
from ttk.db.models import Game, Team
from ttk.domain import Market, Selection, Sport
from ttk.models.simulation import (
    PRESETS,
    GameSimulation,
    Leg,
    Probability,
    SensitivityRow,
)
from ttk.services.market import SideMarket, main_lines, side_markets

LINE_STEPS = [x / 2 for x in range(-6, 7)]  # main line +-3 points in half points


class SimulationError(ValueError):
    """The game cannot be simulated. The message is safe to show the user."""


class Simulator(Protocol):
    def simulate(
        self,
        game_id: int,
        total_line: float,
        *,
        iterations: int = ...,
        seed: int | None = ...,
        anchor: tuple[float, float] | None = ...,
    ) -> GameSimulation | None: ...


@dataclass(frozen=True)
class MarketSide:
    selection: Selection
    line: float | None
    sportsbook: str | None
    american_odds: float | None
    max_acceptable_line: float | None
    """Worst line with positive EV at this price (None: none in range, or no price)."""


@dataclass(frozen=True)
class SimulationSummary:
    game_id: int
    matchup: str
    iterations: int
    seed: int | None
    total_line: float
    total_line_source: str
    home_score_mean: float
    away_score_mean: float
    margin_quantiles: dict[str, float]
    total_quantiles: dict[str, float]
    home_win: Probability
    tie: Probability
    spread_sensitivity: list[SensitivityRow]
    """Home side at lines around the market main spread."""
    total_sensitivity: list[SensitivityRow]
    """Over at lines around the market main total."""
    spread_sides: list[MarketSide]
    total_sides: list[MarketSide]
    joint: dict[str, Probability]
    """(home covers | away covers) x (over | under) at the main lines."""


def _main(markets: list[SideMarket], market: Market, selection: Selection) -> SideMarket | None:
    return next(
        (m for m in main_lines(markets) if m.market is market and m.selection is selection), None
    )


def _side(
    sim: GameSimulation,
    sm: SideMarket | None,
    market: Market,
    selection: Selection,
    bettable: frozenset[str] | None,
) -> MarketSide:
    if sm is None or sm.line is None:
        return MarketSide(selection, None, None, None, None)
    prices = [b for b in sm.consensus.books if bettable is None or b.sportsbook in bettable]
    if not prices:
        return MarketSide(selection, sm.line, None, None, None)
    best = max(prices, key=lambda b: b.decimal_odds)
    candidates = [sm.line + step for step in LINE_STEPS]
    worst = sim.max_acceptable_line(market, selection, best.decimal_odds, candidates)
    return MarketSide(
        selection, sm.line, best.sportsbook, bm.decimal_to_american(best.decimal_odds), worst
    )


def run_simulation(
    session: Session,
    game_id: int,
    simulator: Simulator | None,
    *,
    iterations: int = PRESETS["quick"],
    seed: int | None = None,
    bettable_books: frozenset[str] | None = None,
) -> SimulationSummary:
    game = session.get(Game, game_id)
    if game is None:
        raise SimulationError(f"Game {game_id} not found")
    if simulator is None:
        raise SimulationError(
            f"No simulation model for {game.sport} is available (NFL, college football, "
            "NBA and college basketball have one; they may still be loading)"
        )
    if not 1_000 <= iterations <= 200_000:
        raise SimulationError("Iterations must be between 1,000 and 200,000")
    markets = side_markets(session, game.id)
    home_spread = _main(markets, Market.SPREAD, Selection.HOME)
    away_spread = _main(markets, Market.SPREAD, Selection.AWAY)
    over = _main(markets, Market.TOTAL, Selection.OVER)
    under = _main(markets, Market.TOTAL, Selection.UNDER)
    if over and over.line is not None:
        total_line, source = over.line, "market main total"
    elif game.sport == Sport.NFL:
        total_line, source = 44.0, "default (no total market)"
    else:
        raise SimulationError("No total market for this game yet")
    anchor = (
        (home_spread.line, home_spread.consensus.consensus_no_vig_probability)
        if home_spread and home_spread.line is not None
        else None
    )
    sim = simulator.simulate(game.id, total_line, iterations=iterations, seed=seed, anchor=anchor)
    if sim is None:
        if getattr(game, "season_type", None) == "PRE":
            raise SimulationError("Preseason games are not modeled")
        raise SimulationError("No model inputs for this game")

    spread_center = home_spread.line if home_spread and home_spread.line is not None else 0.0
    home_name = session.get_one(Team, game.home_team_id).name
    away_name = session.get_one(Team, game.away_team_id).name
    joint = {}
    if home_spread and home_spread.line is not None:
        for cover_sel, cover_line in (
            (Selection.HOME, home_spread.line),
            (Selection.AWAY, -home_spread.line),
        ):
            for total_sel in (Selection.OVER, Selection.UNDER):
                joint[f"{cover_sel.value.lower()}_covers_and_{total_sel.value.lower()}"] = (
                    sim.joint(
                        [Leg("SPREAD", cover_sel, cover_line), Leg("TOTAL", total_sel, total_line)]
                    )
                )
    return SimulationSummary(
        game_id=game.id,
        matchup=f"{away_name} @ {home_name}",
        iterations=sim.iterations,
        seed=seed,
        total_line=total_line,
        total_line_source=source,
        home_score_mean=float(np.mean(sim.home)),
        away_score_mean=float(np.mean(sim.away)),
        margin_quantiles=sim.quantiles(sim.margin),
        total_quantiles=sim.quantiles(sim.total),
        home_win=Probability.of(sim.margin > 0),
        tie=Probability.of(sim.margin == 0),
        spread_sensitivity=sim.sensitivity(
            "SPREAD", "HOME", [spread_center + s for s in LINE_STEPS]
        ),
        total_sensitivity=sim.sensitivity("TOTAL", "OVER", [total_line + s for s in LINE_STEPS]),
        spread_sides=[
            _side(sim, home_spread, Market.SPREAD, Selection.HOME, bettable_books),
            _side(sim, away_spread, Market.SPREAD, Selection.AWAY, bettable_books),
        ],
        total_sides=[
            _side(sim, over, Market.TOTAL, Selection.OVER, bettable_books),
            _side(sim, under, Market.TOTAL, Selection.UNDER, bettable_books),
        ],
        joint=joint,
    )
