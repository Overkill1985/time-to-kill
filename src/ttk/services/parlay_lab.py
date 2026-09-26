"""Parlay Lab: price, analyze, save and settle parlays built by hand, across sports.

Pipeline (docs spec 73): validate legs -> price each at the chosen book as of a
moment -> leg probabilities -> correlation -> joint probability -> book implied
probability -> fair odds -> EV -> strongest/weakest/costliest legs.

- A parlay is placed at ONE book: every leg is that book's price at the leg's
  exact line (or a price the user enters). A book not quoting a leg's line
  cannot price it.
- Leg probability: a validated model where one exists (NFL spreads), otherwise
  the market's own no-vig probability, labeled "market" - so a market-only
  parlay shows the compounded book margin as negative EV, never an edge.
- Joint probability is the product of leg probabilities across games. Legs from
  the same NFL game are joined by Monte Carlo: the simulation's lift,
  P(all legs) / product of P(each leg) in the same simulated games, multiplies
  the product of the displayed leg probabilities, so each leg keeps its own
  probability and only the dependence is simulated. Same-game groups that
  cannot be simulated (other sports for now) stay an independence assumption,
  flagged. A book's same-game-parlay price will differ from the product of legs.
- Pushes: joint probability uses each leg's P(win) = p x (1 - P(push)); the
  upside of a push (the parlay shrinks rather than loses) is ignored, so EV is
  slightly understated when push chances are material.
- The lab never changes a parlay; it only reports which legs hurt it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ttk import betting_math as bm
from ttk.db.models import Bet, Game, Parlay, Sportsbook, Team, utcnow
from ttk.domain import BetResult, CorrelationRisk, GameStatus, Market, Selection, Sport
from ttk.models.simulation import PRESETS, GameSimulation
from ttk.models.simulation import Leg as SimLeg
from ttk.services.bets import VALID_SIDES, grade
from ttk.services.line_history import (
    OPPOSITE,
    closing_line_value,
    consensus_at_line,
    opposite_line,
)
from ttk.services.market import main_lines, side_markets
from ttk.services.nfl_spread_predictor import SpreadView
from ttk.services.odds_state import book_timelines

_RISK_ORDER = {
    CorrelationRisk.LOW: 0,
    CorrelationRisk.UNKNOWN: 1,
    CorrelationRisk.MODERATE: 2,
    CorrelationRisk.HIGH: 3,
}


class ParlayError(ValueError):
    """An invalid parlay. The message is safe to show the user."""


class SpreadModel(Protocol):
    def spread(
        self, game_id: int, home_line: float, market_home_cover: float
    ) -> SpreadView | None: ...


class GameSimulator(Protocol):
    def simulate(
        self,
        game_id: int,
        total_line: float,
        *,
        iterations: int = ...,
        seed: int | None = ...,
        anchor: tuple[float, float] | None = ...,
    ) -> GameSimulation | None: ...


SIMULATION_ITERATIONS = PRESETS["quick"]
DEFAULT_TOTAL_LINE = 44.0
"""Only used to simulate margin-only groups (spreads/moneylines) with no total market."""


@dataclass(frozen=True)
class LegInput:
    game_id: int
    market: Market
    selection: Selection
    line: float | None = None
    american_odds: float | None = None
    """Override the book's price (e.g. what you were actually offered)."""


@dataclass(frozen=True)
class LegAnalysis:
    index: int
    game_id: int
    sport: Sport
    matchup: str
    commence_time: datetime
    market: Market
    selection: Selection
    line: float | None
    description: str
    american_odds: float
    decimal_odds: float
    price_source: str
    """'book' (the chosen book's quote) or 'manual'."""
    odds_age_minutes: float | None
    market_probability: float | None
    market_books: int
    probability: float
    """P(win | no push) used for the leg."""
    probability_source: str
    """'model', 'market' (all-book no-vig consensus) or 'book' (this book's no-vig)."""
    push_probability: float
    edge: float | None
    ev_per_unit: float
    """This leg as a straight bet at this price."""
    correlation_risk: CorrelationRisk = CorrelationRisk.LOW


@dataclass(frozen=True)
class Correlation:
    legs: tuple[int, int]
    risk: CorrelationRisk
    reason: str


@dataclass(frozen=True)
class SimulatedGroup:
    game_id: int
    legs: tuple[int, ...]
    iterations: int
    lift: float
    """P(all legs) / product of P(each leg) in the simulation (1.0 = independent)."""
    total_line: float


@dataclass(frozen=True)
class LegImpact:
    index: int
    ev_without: float | None
    """Parlay EV per unit if this leg were removed (None for a one-leg parlay)."""


@dataclass
class ParlayAnalysis:
    sportsbook: str
    legs: list[LegAnalysis]
    decimal_odds: float
    american_odds: float
    book_implied_probability: float
    joint_probability: float
    market_joint_probability: float | None
    fair_american_odds: float | None
    ev_per_unit: float
    correlation_risk: CorrelationRisk
    correlations: list[Correlation]
    simulated: list[SimulatedGroup] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    impacts: list[LegImpact] = field(default_factory=list)
    strongest_leg: int | None = None
    weakest_leg: int | None = None
    lowest_edge_leg: int | None = None
    highest_correlation_leg: int | None = None
    reduces_ev_most: int | None = None
    """The leg whose removal raises EV the most (only reported if removal helps)."""


def _describe(
    home: str, away: str, market: Market, selection: Selection, line: float | None
) -> str:
    if market is Market.TOTAL:
        return f"{selection.value.title()} {line:g} ({away} @ {home})"
    team = home if selection is Selection.HOME else away
    if market is Market.MONEYLINE:
        return f"{team} ML"
    return f"{team} {'PK' if line == 0 else f'{line:+g}'}"


def _correlate(a: LegAnalysis, b: LegAnalysis) -> Correlation | None:
    if a.game_id != b.game_id:
        return None
    pair = (a.index, b.index)
    if a.market is b.market:
        if a.selection is b.selection:
            return Correlation(
                pair, CorrelationRisk.HIGH, "two lines on the same side of the same market"
            )
        raise ParlayError(
            f"Legs {a.index + 1} and {b.index + 1} take both sides of the same market"
        )
    sides = {Market.MONEYLINE, Market.SPREAD}
    if {a.market, b.market} == sides:
        if a.selection is b.selection:
            return Correlation(
                pair, CorrelationRisk.HIGH, "moneyline and spread on the same team move together"
            )
        return Correlation(
            pair,
            CorrelationRisk.HIGH,
            "moneyline and spread on opposite teams work against each other",
        )
    if Market.TOTAL in (a.market, b.market):
        return Correlation(
            pair,
            CorrelationRisk.MODERATE,
            "a side and the total in one game are linked by game script",
        )
    return Correlation(pair, CorrelationRisk.UNKNOWN, "same game; relationship not modeled")


def _win(leg: LegAnalysis) -> float:
    return leg.probability * (1 - leg.push_probability)


def _sim_leg(leg: LegAnalysis) -> SimLeg:
    return SimLeg(str(leg.market), str(leg.selection), leg.line)


def _joint(
    legs: Sequence[LegAnalysis], sims: dict[int, tuple[GameSimulation, float]] | None = None
) -> float:
    """Product across games; a simulated same-game group gets its simulation lift."""
    by_game: dict[int, list[LegAnalysis]] = {}
    for leg in legs:
        by_game.setdefault(leg.game_id, []).append(leg)
    result = 1.0
    for game_id, group in by_game.items():
        base = bm.independent_parlay_probability([_win(leg) for leg in group])
        if len(group) >= 2 and sims and game_id in sims:
            base = min(base * _lift(sims[game_id][0], group), min(_win(leg) for leg in group))
        result *= base
    return result


def _lift(sim: GameSimulation, group: Sequence[LegAnalysis]) -> float:
    legs = [_sim_leg(leg) for leg in group]
    each = 1.0
    for leg in legs:
        each *= sim.side(leg).win.value
    return sim.joint(legs).value / each if each > 0 else 1.0


def evaluate_parlay(
    session: Session,
    inputs: Sequence[LegInput],
    sportsbook: str,
    *,
    at: datetime | None = None,
    predictor: SpreadModel | None = None,
    max_odds_age: timedelta = timedelta(minutes=30),
) -> ParlayAnalysis:
    at = at or utcnow()
    if not 2 <= len(inputs) <= 12:
        raise ParlayError("A parlay needs 2 to 12 legs")
    book = session.scalar(select(Sportsbook).where(Sportsbook.key == sportsbook.lower()))
    if book is None:
        raise ParlayError(f"Unknown sportsbook {sportsbook!r}")
    seen: set[tuple[int, Market, Selection, float | None]] = set()
    legs: list[LegAnalysis] = []
    warnings: list[str] = []

    for i, leg in enumerate(inputs):
        game = session.get(Game, leg.game_id)
        if game is None:
            raise ParlayError(f"Leg {i + 1}: game {leg.game_id} not found")
        if leg.selection not in VALID_SIDES.get(leg.market, set()):
            raise ParlayError(f"Leg {i + 1}: {leg.selection} is not a side of {leg.market}")
        line = None if leg.market is Market.MONEYLINE else leg.line
        if leg.market is not Market.MONEYLINE and line is None:
            raise ParlayError(f"Leg {i + 1}: {leg.market} needs a line")
        if at >= game.commence_time:
            raise ParlayError(f"Leg {i + 1}: the game has started")
        key = (game.id, leg.market, leg.selection, line)
        if key in seen:
            raise ParlayError(f"Leg {i + 1} repeats an earlier leg")
        seen.add(key)

        home = session.get_one(Team, game.home_team_id).name
        away = session.get_one(Team, game.away_team_id).name
        timeline = book_timelines(session, game.id, leg.market).get(book.id)
        state = timeline.state_at(at) if timeline else {}
        quote = state.get((leg.market, leg.selection, line))
        other = state.get((leg.market, OPPOSITE[leg.selection], opposite_line(leg.market, line)))
        last_seen = timeline.last_seen(at) if timeline else None

        if leg.american_odds is not None:
            american, price_source = leg.american_odds, "manual"
        elif quote is not None:
            american, price_source = quote.american_odds, "book"
        else:
            raise ParlayError(
                f"Leg {i + 1}: {book.key} does not quote "
                f"{_describe(home, away, leg.market, leg.selection, line)}; enter the price"
            )
        try:
            decimal = bm.american_to_decimal(american)
        except ValueError as exc:
            raise ParlayError(f"Leg {i + 1}: {exc}") from None
        odds_age = (at - last_seen) if (last_seen and price_source == "book") else None
        if odds_age is not None and odds_age > max_odds_age:
            warnings.append(
                f"Leg {i + 1}: {book.key} price is {odds_age.total_seconds() / 60:.0f} min old"
            )

        consensus = consensus_at_line(session, game.id, leg.market, leg.selection, line, at)
        market_p = consensus.consensus_no_vig_probability if consensus else None
        push = 0.0
        if (
            predictor is not None
            and game.sport == Sport.NFL
            and leg.market is Market.SPREAD
            and line is not None
        ):
            home_line = line if leg.selection is Selection.HOME else -line
            home_consensus = consensus_at_line(
                session, game.id, Market.SPREAD, Selection.HOME, home_line, at
            )
            view = (
                predictor.spread(game.id, home_line, home_consensus.consensus_no_vig_probability)
                if home_consensus
                else None
            )
        else:
            view = None
        if view is not None:
            probability = (
                view.home_cover if leg.selection is Selection.HOME else 1 - view.home_cover
            )
            push, source = view.push, "model"
        elif market_p is not None:
            probability, source = market_p, "market"
        elif quote is not None and other is not None:
            probability = bm.no_vig_probabilities(
                [quote.decimal_odds, other.decimal_odds]
            ).probabilities[0]
            source = "book"
        else:
            raise ParlayError(
                f"Leg {i + 1}: no two-sided market at this line to estimate a probability"
            )
        edge = bm.edge(probability, market_p) if market_p is not None else None
        legs.append(
            LegAnalysis(
                index=i,
                game_id=game.id,
                sport=Sport(game.sport),
                matchup=f"{away} @ {home}",
                commence_time=game.commence_time,
                market=leg.market,
                selection=leg.selection,
                line=line,
                description=_describe(home, away, leg.market, leg.selection, line),
                american_odds=american,
                decimal_odds=decimal,
                price_source=price_source,
                odds_age_minutes=None if odds_age is None else odds_age.total_seconds() / 60,
                market_probability=market_p,
                market_books=consensus.books_reporting if consensus else 0,
                probability=probability,
                probability_source=source,
                push_probability=push,
                edge=edge,
                ev_per_unit=bm.expected_value(
                    probability * (1 - push), decimal, push_probability=push
                ).per_unit,
            )
        )

    correlations = [
        c
        for x in range(len(legs))
        for y in range(x + 1, len(legs))
        if (c := _correlate(legs[x], legs[y])) is not None
    ]
    leg_risk = {leg.index: CorrelationRisk.LOW for leg in legs}
    for c in correlations:
        for idx in c.legs:
            if _RISK_ORDER[c.risk] > _RISK_ORDER[leg_risk[idx]]:
                leg_risk[idx] = c.risk
    legs = [replace(la, correlation_risk=leg_risk[la.index]) for la in legs]
    overall = max(leg_risk.values(), key=lambda r: _RISK_ORDER[r])

    # Same-game NFL groups: simulate the dependence.
    sims: dict[int, tuple[GameSimulation, float]] = {}
    simulate = getattr(predictor, "simulate", None)
    groups: dict[int, list[LegAnalysis]] = {}
    for la in legs:
        groups.setdefault(la.game_id, []).append(la)
    same_game = {gid: g for gid, g in groups.items() if len(g) >= 2}
    for gid, group in same_game.items():
        if simulate is None or group[0].sport is not Sport.NFL:
            continue
        mains = {(m.market, m.selection): m for m in main_lines(side_markets(session, gid))}
        main_total = mains.get((Market.TOTAL, Selection.OVER))
        main_spread = mains.get((Market.SPREAD, Selection.HOME))
        total_line = next((x.line for x in group if x.market is Market.TOTAL and x.line), None)
        if total_line is None:
            total_line = main_total.line if main_total and main_total.line else DEFAULT_TOTAL_LINE
        # Center the margin on the validated anchored model at the main spread.
        anchor = (
            (main_spread.line, main_spread.consensus.consensus_no_vig_probability)
            if main_spread and main_spread.line is not None
            else None
        )
        sim = simulate(gid, total_line, iterations=SIMULATION_ITERATIONS, seed=gid, anchor=anchor)
        if sim is not None:
            sims[gid] = (sim, total_line)
    simulated = [
        SimulatedGroup(
            gid,
            tuple(x.index for x in same_game[gid]),
            sim.iterations,
            _lift(sim, same_game[gid]),
            total_line,
        )
        for gid, (sim, total_line) in sims.items()
    ]
    for sg in simulated:
        warnings.append(
            f"Legs {', '.join(str(i + 1) for i in sg.legs)} share a game: their joint "
            f"probability comes from {sg.iterations:,} simulations "
            f"(x{sg.lift:.2f} vs independent). The book's same-game parlay price will "
            "differ from the product of legs."
        )
    if any(gid not in sims for gid in same_game):
        warnings.append(
            "Legs share a game that could not be simulated: their joint probability assumes "
            "independence and is likely wrong, and the book's same-game parlay price will "
            "differ from the product of legs."
        )
    if any(leg.probability_source != "model" for leg in legs):
        warnings.append(
            "Legs without a validated model use the market's no-vig probability: they add "
            "the book's margin to the parlay and no edge."
        )

    decimal_odds = bm.parlay_decimal_odds([leg.decimal_odds for leg in legs])
    joint = _joint(legs, sims)
    market_ps = [leg.market_probability for leg in legs]
    analysis = ParlayAnalysis(
        sportsbook=book.key,
        legs=legs,
        decimal_odds=decimal_odds,
        american_odds=bm.decimal_to_american(decimal_odds),
        book_implied_probability=1 / decimal_odds,
        joint_probability=joint,
        market_joint_probability=(
            bm.independent_parlay_probability([p for p in market_ps if p is not None])
            if all(p is not None for p in market_ps)
            else None
        ),
        fair_american_odds=bm.fair_american_odds(joint) if 0 < joint < 1 else None,
        ev_per_unit=joint * decimal_odds - 1,
        correlation_risk=overall,
        correlations=correlations,
        simulated=simulated,
        warnings=warnings,
    )
    analysis.strongest_leg = max(legs, key=lambda leg: leg.probability).index
    analysis.weakest_leg = min(legs, key=lambda leg: leg.probability).index
    with_edge = [leg for leg in legs if leg.edge is not None]
    analysis.lowest_edge_leg = (
        min(with_edge, key=lambda leg: leg.edge or 0.0).index if with_edge else None
    )
    risky = [leg for leg in legs if leg.correlation_risk is not CorrelationRisk.LOW]
    analysis.highest_correlation_leg = (
        max(risky, key=lambda leg: _RISK_ORDER[leg.correlation_risk]).index if risky else None
    )
    for analyzed in legs:
        rest = [other for other in legs if other.index != analyzed.index]
        ev_without = (
            _joint(rest, sims) * bm.parlay_decimal_odds([o.decimal_odds for o in rest]) - 1
            if len(rest) >= 2
            else None
        )
        analysis.impacts.append(LegImpact(analyzed.index, ev_without))
    helps = [
        i
        for i in analysis.impacts
        if i.ev_without is not None and i.ev_without > analysis.ev_per_unit
    ]
    if helps:
        analysis.reduces_ev_most = max(helps, key=lambda i: i.ev_without or 0.0).index
    return analysis


@dataclass(frozen=True)
class Offer:
    market: Market
    selection: Selection
    line: float | None
    american_odds: float
    no_vig_probability: float | None
    """This book's own no-vig probability at the line (both sides posted)."""
    main: bool


def book_offers(
    session: Session, game_id: int, sportsbook: str, *, at: datetime | None = None
) -> list[Offer]:
    """Everything ``sportsbook`` quotes for a game as of ``at``; per market and side,
    the line closest to 50/50 is flagged as the book's main line."""
    at = at or utcnow()
    book = session.scalar(select(Sportsbook).where(Sportsbook.key == sportsbook.lower()))
    if book is None:
        raise ParlayError(f"Unknown sportsbook {sportsbook!r}")
    timeline = book_timelines(session, game_id).get(book.id)
    if timeline is None:
        return []
    state = timeline.state_at(at)
    offers = []
    for (market_s, selection_s, line), row in state.items():
        market, selection = Market(market_s), Selection(selection_s)
        if selection not in OPPOSITE:
            continue
        other = state.get((market_s, OPPOSITE[selection], opposite_line(market, line)))
        p = (
            bm.no_vig_probabilities([row.decimal_odds, other.decimal_odds]).probabilities[0]
            if other is not None
            else None
        )
        offers.append(Offer(market, selection, line, row.american_odds, p, False))
    main: dict[tuple[Market, Selection], Offer] = {}
    for o in offers:
        if o.no_vig_probability is None:
            continue
        best = main.get((o.market, o.selection))
        if best is None or abs(o.no_vig_probability - 0.5) < abs(
            (best.no_vig_probability or 0.5) - 0.5
        ):
            main[(o.market, o.selection)] = o
    chosen = {id(o) for o in main.values()}
    return sorted(
        (replace(o, main=id(o) in chosen) for o in offers),
        key=lambda o: (o.market, o.selection, o.line if o.line is not None else 0.0),
    )


# --------------------------------------------------------------------------- save


def save_parlay(
    session: Session,
    inputs: Sequence[LegInput],
    sportsbook: str,
    stake: float,
    *,
    american_odds: float | None = None,
    placed_at: datetime | None = None,
    notes: str | None = None,
    predictor: SpreadModel | None = None,
) -> Parlay:
    """Record a placed parlay with its legs' beliefs as of ``placed_at``.
    ``american_odds`` is the price actually taken (default: product of legs)."""
    if stake <= 0:
        raise ParlayError("Stake must be positive")
    placed_at = placed_at or utcnow()
    analysis = evaluate_parlay(session, inputs, sportsbook, at=placed_at, predictor=predictor)
    taken = american_odds if american_odds is not None else analysis.american_odds
    try:
        taken_decimal = bm.american_to_decimal(taken)
    except ValueError as exc:
        raise ParlayError(str(exc)) from None
    book = session.scalar(select(Sportsbook).where(Sportsbook.key == analysis.sportsbook))
    assert book is not None
    parlay = Parlay(
        placed_at=placed_at,
        sportsbook_id=book.id,
        american_odds=taken,
        stake=stake,
        model_joint_probability=analysis.joint_probability,
        market_joint_probability=analysis.market_joint_probability,
        expected_value=analysis.joint_probability * taken_decimal - 1,
        correlation_risk=analysis.correlation_risk,
        result=BetResult.PENDING,
        notes=notes,
    )
    session.add(parlay)
    session.flush()
    for leg in analysis.legs:
        game = session.get_one(Game, leg.game_id)
        session.add(
            Bet(
                placed_at=placed_at,
                sport=game.sport,
                game_id=game.id,
                parlay_id=parlay.id,
                sportsbook_id=book.id,
                market=leg.market,
                selection=leg.selection,
                description=f"{leg.description} ({leg.matchup})"
                if leg.market is not Market.TOTAL
                else leg.description,
                line=leg.line,
                american_odds=leg.american_odds,
                model_probability=leg.probability if leg.probability_source == "model" else None,
                model_push_probability=leg.push_probability,
                market_probability=leg.market_probability,
                edge=leg.edge,
                expected_value=leg.ev_per_unit,
                stake=None,
                result=BetResult.PENDING,
            )
        )
    session.flush()
    return parlay


# --------------------------------------------------------------------------- settle


def settle_parlays(session: Session, *, now: datetime | None = None) -> list[Parlay]:
    """Settle parlays whose every leg's game is final or canceled.

    A pushed or voided leg drops out and the parlay is repriced on the remaining
    legs (a common book rule; books differ). The taken price is scaled by the
    same factor, which is exact for a product-of-legs price and approximate for
    a same-game-parlay price."""
    now = now or utcnow()
    settled = []
    for parlay in session.scalars(select(Parlay).where(Parlay.result == BetResult.PENDING)):
        legs = session.scalars(select(Bet).where(Bet.parlay_id == parlay.id)).all()
        games = [session.get(Game, leg.game_id) if leg.game_id else None for leg in legs]
        if not legs or any(
            g is None or g.status not in (GameStatus.FINAL, GameStatus.CANCELED) for g in games
        ):
            continue
        for leg, game in zip(legs, games, strict=True):
            assert game is not None
            if game.status == GameStatus.CANCELED:
                leg.result = BetResult.VOID
            else:
                assert game.home_score is not None and game.away_score is not None
                leg.result = grade(
                    Market(leg.market),
                    Selection(leg.selection),
                    leg.line,
                    game.home_score,
                    game.away_score,
                )
            clv = closing_line_value(
                session,
                game.id,
                Market(leg.market),
                Selection(leg.selection),
                leg.line,
                leg.american_odds,
            )
            leg.closing_no_vig_probability = clv.closing_no_vig_at_bet_line
            leg.clv = clv.price_clv
            leg.closing_line = clv.closing_main_line
            leg.closing_points_gained = clv.points_gained
            leg.settled_at = now
        full = bm.parlay_decimal_odds([bm.american_to_decimal(leg.american_odds) for leg in legs])
        remaining = bm.parlay_settled_decimal_odds(
            [(bm.american_to_decimal(leg.american_odds), leg.result) for leg in legs]
        )
        stake = parlay.stake or 0.0
        taken = bm.american_to_decimal(parlay.american_odds or bm.decimal_to_american(full))
        if remaining is None:
            parlay.result, parlay.profit_loss = BetResult.LOSS, -stake
        elif remaining == 1.0:
            all_void = all(leg.result == BetResult.VOID for leg in legs)
            parlay.result = BetResult.VOID if all_void else BetResult.PUSH
            parlay.profit_loss = 0.0
        else:
            parlay.result = BetResult.WIN
            parlay.profit_loss = stake * (taken * remaining / full - 1)
        parlay.settled_at = now
        settled.append(parlay)
    session.flush()
    return settled


@dataclass(frozen=True)
class ParlayPerformance:
    parlays: int
    wins: int
    losses: int
    pushes: int
    pending: int
    staked: float
    profit: float
    roi: float | None
    avg_ev: float | None
    ev_n: int


def parlay_performance(session: Session) -> ParlayPerformance:
    rows = session.scalars(select(Parlay)).all()
    graded = [p for p in rows if p.result in (BetResult.WIN, BetResult.LOSS, BetResult.PUSH)]
    staked = sum(p.stake or 0.0 for p in graded)
    profit = sum(p.profit_loss or 0.0 for p in graded)
    evs = [p.expected_value for p in graded if p.expected_value is not None]
    return ParlayPerformance(
        parlays=len(graded),
        wins=sum(p.result == BetResult.WIN for p in graded),
        losses=sum(p.result == BetResult.LOSS for p in graded),
        pushes=sum(p.result == BetResult.PUSH for p in graded),
        pending=sum(p.result == BetResult.PENDING for p in rows),
        staked=staked,
        profit=profit,
        roi=profit / staked if staked else None,
        avg_ev=sum(evs) / len(evs) if evs else None,
        ev_n=len(evs),
    )
