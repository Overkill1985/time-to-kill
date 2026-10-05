"""Bet tracker: record real wagers, settle them from final scores, measure CLV, and
summarize performance.

Recording captures beliefs as of ``placed_at``:
- market probability: consensus no-vig at the bet's exact line from every
  book's state at that moment (odds change log);
- model probability: the latest prediction snapshot for the same game, market,
  side and line created at or before ``placed_at`` (none if the model never
  priced that number - never interpolated);
- edge and EV at the bet's own price.
A bet placed at or after kickoff is rejected: live betting is not modeled, and
QB-starter features assume a pregame bet (docs/MODEL-GOVERNANCE.md). A bet over
a bankroll limit is rejected unless it carries a reason (services/bankroll).

Settlement grades from final scores (status FINAL), voids canceled games,
leaves postponed ones pending, and records CLV against the close
(services/line_history.closing_line_value).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ttk import betting_math as bm
from ttk.db.models import Bet, Game, Prediction, Sportsbook, Team, utcnow
from ttk.domain import BetResult, GameStatus, Market, Selection, Sport
from ttk.services.bankroll import LimitError, enforce_limits
from ttk.services.line_history import closing_line_value, consensus_at_line

VALID_SIDES = {
    Market.MONEYLINE: {Selection.HOME, Selection.AWAY},
    Market.SPREAD: {Selection.HOME, Selection.AWAY},
    Market.TOTAL: {Selection.OVER, Selection.UNDER},
}


class BetError(ValueError):
    """An invalid bet. The message is safe to show the user."""


@dataclass(frozen=True)
class NewBet:
    game_id: int
    market: Market
    selection: Selection
    line: float | None
    american_odds: float
    sportsbook: str
    stake: float
    placed_at: datetime | None = None
    notes: str | None = None
    limit_override: str | None = None
    """Reason to record the bet although it breaks a bankroll limit."""


def _describe(
    session: Session, game: Game, market: Market, selection: Selection, line: float | None
) -> str:
    home = session.get_one(Team, game.home_team_id).name
    away = session.get_one(Team, game.away_team_id).name
    if market is Market.TOTAL:
        return f"{selection.value.title()} {line:g} ({away} @ {home})"
    team = home if selection is Selection.HOME else away
    if market is Market.MONEYLINE:
        return f"{team} ML ({away} @ {home})"
    return f"{team} {'PK' if line == 0 else f'{line:+g}'} ({away} @ {home})"


def record_bet(session: Session, new: NewBet, *, now: datetime | None = None) -> Bet:
    game = session.get(Game, new.game_id)
    if game is None:
        raise BetError(f"Game {new.game_id} not found")
    if new.selection not in VALID_SIDES.get(new.market, set()):
        raise BetError(f"{new.selection} is not a side of {new.market}")
    if new.market is Market.MONEYLINE:
        line = None
    elif new.line is None:
        raise BetError(f"{new.market} bets need a line")
    else:
        line = new.line
    if new.stake <= 0:
        raise BetError("Stake must be positive")
    try:
        decimal_odds = bm.american_to_decimal(new.american_odds)
    except ValueError as exc:
        raise BetError(str(exc)) from None
    placed_at = new.placed_at or now or utcnow()
    if placed_at >= game.commence_time:
        raise BetError("Bet placed at or after kickoff; live bets are not tracked")
    book = session.scalar(select(Sportsbook).where(Sportsbook.key == new.sportsbook.lower()))
    if book is None:
        raise BetError(f"Unknown sportsbook {new.sportsbook!r}")
    try:
        bankroll_at_bet, override = enforce_limits(
            session, new.stake, placed_at, new.limit_override
        )
    except LimitError as exc:
        raise BetError(str(exc)) from None

    market_view = consensus_at_line(session, game.id, new.market, new.selection, line, placed_at)
    prediction = session.scalar(
        select(Prediction)
        .where(
            Prediction.game_id == game.id,
            Prediction.market == new.market,
            Prediction.selection == new.selection,
            Prediction.line == line if line is not None else Prediction.line.is_(None),
            Prediction.created_at <= placed_at,
        )
        .order_by(Prediction.created_at.desc(), Prediction.id.desc())
        .limit(1)
    )
    model_p = prediction.probability if prediction else None
    push = prediction.push_probability if prediction else None
    market_p = market_view.consensus_no_vig_probability if market_view else None
    ev = (
        bm.expected_value(
            model_p * (1 - (push or 0.0)), decimal_odds, push_probability=push or 0.0
        ).per_unit
        if model_p is not None
        else None
    )
    bet = Bet(
        placed_at=placed_at,
        sport=game.sport,
        game_id=game.id,
        prediction_id=prediction.id if prediction else None,
        model_version_id=prediction.model_version_id if prediction else None,
        sportsbook_id=book.id,
        market=new.market,
        selection=new.selection,
        description=_describe(session, game, new.market, new.selection, line),
        line=line,
        american_odds=new.american_odds,
        model_probability=model_p,
        model_push_probability=push,
        market_probability=market_p,
        edge=bm.edge(model_p, market_p) if model_p is not None and market_p is not None else None,
        expected_value=ev,
        stake=new.stake,
        result=BetResult.PENDING,
        notes=new.notes,
        bankroll_at_bet=bankroll_at_bet,
        limit_override=override,
    )
    session.add(bet)
    session.flush()
    return bet


def grade(
    market: Market, selection: Selection, line: float | None, home_score: int, away_score: int
) -> BetResult:
    if market is Market.TOTAL:
        assert line is not None
        diff = (home_score + away_score) - line
        diff = diff if selection is Selection.OVER else -diff
    else:
        margin = home_score - away_score
        diff = margin if selection is Selection.HOME else -margin
        if market is Market.SPREAD:
            assert line is not None
            diff += line
    if diff > 0:
        return BetResult.WIN
    return BetResult.LOSS if diff < 0 else BetResult.PUSH


def settle_bets(session: Session, *, now: datetime | None = None) -> list[Bet]:
    """Settle every pending single bet whose game is final or canceled."""
    now = now or utcnow()
    settled = []
    pending = session.scalars(
        select(Bet).where(Bet.result == BetResult.PENDING, Bet.parlay_id.is_(None))
    ).all()
    for bet in pending:
        game = session.get(Game, bet.game_id) if bet.game_id else None
        if game is None:
            continue
        if game.status == GameStatus.CANCELED:
            bet.result = BetResult.VOID
        elif game.status == GameStatus.FINAL and game.home_score is not None:
            assert game.away_score is not None
            bet.result = grade(
                Market(bet.market),
                Selection(bet.selection),
                bet.line,
                game.home_score,
                game.away_score,
            )
        else:
            continue
        bet.profit_loss = bm.settle_payout(
            bet.stake or 0.0, bm.american_to_decimal(bet.american_odds), bet.result
        )
        clv = closing_line_value(
            session,
            game.id,
            Market(bet.market),
            Selection(bet.selection),
            bet.line,
            bet.american_odds,
        )
        bet.closing_no_vig_probability = clv.closing_no_vig_at_bet_line
        bet.clv = clv.price_clv
        bet.closing_line = clv.closing_main_line
        bet.closing_points_gained = clv.points_gained
        bet.settled_at = now
        settled.append(bet)
    session.flush()
    return settled


@dataclass(frozen=True)
class Performance:
    bets: int
    wins: int
    losses: int
    pushes: int
    voids: int
    pending: int
    pending_stake: float
    staked: float
    profit: float
    roi: float | None
    hit_rate: float | None
    units: float
    unit_size: float
    avg_edge: float | None
    edge_n: int
    avg_ev: float | None
    ev_n: int
    avg_clv: float | None
    clv_n: int
    avg_points_gained: float | None
    points_n: int
    max_drawdown: float
    """Largest peak-to-trough fall in cumulative profit (currency, <= 0)."""


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def performance(
    session: Session,
    *,
    sport: Sport | None = None,
    market: Market | None = None,
    unit_size: float = 1.0,
) -> Performance:
    stmt = select(Bet).where(Bet.parlay_id.is_(None))
    if sport is not None:
        stmt = stmt.where(Bet.sport == sport)
    if market is not None:
        stmt = stmt.where(Bet.market == market)
    all_bets = session.scalars(stmt.order_by(Bet.placed_at, Bet.id)).all()
    pending = [b for b in all_bets if b.result == BetResult.PENDING]
    graded = [b for b in all_bets if b.result in (BetResult.WIN, BetResult.LOSS, BetResult.PUSH)]
    wins = sum(b.result == BetResult.WIN for b in graded)
    losses = sum(b.result == BetResult.LOSS for b in graded)
    staked = sum(b.stake or 0.0 for b in graded)
    profit = sum(b.profit_loss or 0.0 for b in graded)

    running = peak = drawdown = 0.0
    for b in sorted(graded, key=lambda b: (b.settled_at or b.placed_at, b.id)):
        running += b.profit_loss or 0.0
        peak = max(peak, running)
        drawdown = min(drawdown, running - peak)

    edges = [b.edge for b in graded if b.edge is not None]
    evs = [b.expected_value for b in graded if b.expected_value is not None]
    clvs = [b.clv for b in graded if b.clv is not None]
    points = [b.closing_points_gained for b in graded if b.closing_points_gained is not None]
    return Performance(
        bets=len(graded),
        wins=wins,
        losses=losses,
        pushes=sum(b.result == BetResult.PUSH for b in graded),
        voids=sum(b.result == BetResult.VOID for b in all_bets),
        pending=len(pending),
        pending_stake=sum(b.stake or 0.0 for b in pending),
        staked=staked,
        profit=profit,
        roi=profit / staked if staked else None,
        hit_rate=wins / (wins + losses) if wins + losses else None,
        units=profit / unit_size,
        unit_size=unit_size,
        avg_edge=_mean(edges),
        edge_n=len(edges),
        avg_ev=_mean(evs),
        ev_n=len(evs),
        avg_clv=_mean(clvs),
        clv_n=len(clvs),
        avg_points_gained=_mean(points),
        points_n=len(points),
        max_drawdown=drawdown,
    )
