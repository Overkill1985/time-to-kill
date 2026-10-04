"""Line movement and closing-line value, reconstructed from the odds change log.

- Books post alternate lines. A book's *main* line at a moment is the one whose
  two sides are closest to 50/50 (moneylines have one "line").
- opening = the main line when the book was first seen; current = when it was
  last seen; previous = the latest earlier main line that differs from current;
  closing = the state at the book's last observation at or before kickoff.
  Timestamps are observation times: the opening is only as early as our first
  poll, the close only as late as our last pre-kickoff poll
  (``close_minutes_before_kickoff``).
- CLV compares the bet's price with the closing no-vig probability *at the bet's
  own line*, from books quoting that exact number in their final pre-kickoff
  state (including alternates). When none did, price CLV is unknown and only
  the points the line moved are reported - never estimated.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ttk import betting_math as bm
from ttk.consensus import MarketConsensus, TwoSidedQuote, market_consensus
from ttk.db.models import Game, OddsSnapshot, Sportsbook
from ttk.domain import Market, Selection
from ttk.services.odds_state import BookTimeline, QuoteKey, book_timelines

OPPOSITE = {
    Selection.HOME: Selection.AWAY,
    Selection.AWAY: Selection.HOME,
    Selection.OVER: Selection.UNDER,
    Selection.UNDER: Selection.OVER,
}


def opposite_line(market: Market, line: float | None) -> float | None:
    return -line if market is Market.SPREAD and line is not None else line


@dataclass(frozen=True)
class PricePoint:
    observed_at: datetime
    line: float | None
    american_odds: float
    no_vig_probability: float | None
    """This side's no-vig probability from the same book and moment, if both sides were posted."""


@dataclass(frozen=True)
class BookLineHistory:
    sportsbook: str
    market: Market
    selection: Selection
    opening: PricePoint
    previous: PricePoint | None
    current: PricePoint | None
    """None when the book has taken the market down (e.g. after kickoff)."""
    closing: PricePoint | None
    observations: int


def _entries(
    state: dict[QuoteKey, OddsSnapshot], market: Market, selection: Selection
) -> list[tuple[OddsSnapshot, OddsSnapshot | None]]:
    """This side's quotes in a state, each with the opposite side at the same line."""
    if selection not in OPPOSITE:
        raise ValueError(f"Line history needs a two-sided selection, got {selection}")
    opposite = OPPOSITE[selection]
    return [
        (row, state.get((market, opposite, opposite_line(market, line))))
        for (m, sel, line), row in state.items()
        if m == market and sel == selection
    ]


def _point(side: OddsSnapshot, other: OddsSnapshot | None, at: datetime) -> PricePoint:
    no_vig = (
        bm.no_vig_probabilities([side.decimal_odds, other.decimal_odds]).probabilities[0]
        if other is not None
        else None
    )
    return PricePoint(at, side.line, side.american_odds, no_vig)


def _main_at(
    timeline: BookTimeline,
    t: datetime,
    market: Market,
    selection: Selection,
    *,
    label: datetime | None = None,
    state: dict[QuoteKey, OddsSnapshot] | None = None,
) -> PricePoint | None:
    """The book's main line for this side in its state as of ``t`` (or ``state``),
    labelled with ``label`` (default ``t``)."""
    entries = _entries(state if state is not None else timeline.state_at(t), market, selection)
    if not entries:
        return None
    points = [_point(side, other, label or t) for side, other in entries]
    two_sided = [p for p in points if p.no_vig_probability is not None]
    if two_sided:
        return min(two_sided, key=lambda p: abs((p.no_vig_probability or 0.5) - 0.5))
    return points[0]


def line_history(
    session: Session, game_id: int, market: Market, selection: Selection
) -> list[BookLineHistory]:
    if selection not in OPPOSITE:
        raise ValueError(f"Line history needs a two-sided selection, got {selection}")
    game = session.get_one(Game, game_id)
    books = dict(session.execute(select(Sportsbook.id, Sportsbook.key)).all())
    out = []
    for book_id, timeline in book_timelines(session, game_id, market).items():
        series = [
            p
            for t in timeline.change_times()
            if (p := _main_at(timeline, t, market, selection)) is not None
        ]
        if not series:
            continue
        first_seen = timeline.observations[0] if timeline.observations else series[0].observed_at
        opening = replace(series[0], observed_at=max(first_seen, series[0].observed_at))
        last_seen = timeline.last_seen()
        current = (
            _main_at(timeline, last_seen, market, selection, state=timeline.state_now())
            if last_seen
            else None
        )
        reference = current or series[-1]
        changed = [
            p
            for p in series
            if (p.line, p.american_odds) != (reference.line, reference.american_odds)
            and p.observed_at < reference.observed_at
        ]
        close_seen = timeline.last_seen(game.commence_time)
        out.append(
            BookLineHistory(
                sportsbook=books[book_id],
                market=market,
                selection=selection,
                opening=opening,
                previous=changed[-1] if changed else None,
                current=current,
                # State as of kickoff (a pre-kickoff withdrawal counts), labelled with
                # the last time the book was actually seen before kickoff.
                closing=(
                    _main_at(timeline, game.commence_time, market, selection, label=close_seen)
                    if close_seen
                    else None
                ),
                observations=len(timeline.observations),
            )
        )
    return sorted(out, key=lambda h: h.sportsbook)


def consensus_at_line(
    session: Session,
    game_id: int,
    market: Market,
    selection: Selection,
    line: float | None,
    at: datetime,
) -> MarketConsensus | None:
    """Consensus of books quoting exactly ``line`` in their state as of ``at``
    (only books seen at or before ``at``). A book that had moved off the number
    by then is not counted, whatever it showed earlier."""
    books = dict(session.execute(select(Sportsbook.id, Sportsbook.key)).all())
    quotes = []
    for book_id, timeline in book_timelines(session, game_id, market).items():
        if timeline.last_seen(at) is None:
            continue
        state = timeline.state_at(at)  # includes withdrawals up to ``at``
        for side, other in _entries(state, market, selection):
            if side.line == line and other is not None:
                quotes.append(TwoSidedQuote(books[book_id], side.decimal_odds, other.decimal_odds))
    return market_consensus(quotes) if quotes else None


def closing_consensus_at_line(
    session: Session, game_id: int, market: Market, selection: Selection, line: float | None
) -> MarketConsensus | None:
    """Consensus at ``line`` in books' final pre-kickoff state."""
    game = session.get_one(Game, game_id)
    return consensus_at_line(session, game_id, market, selection, line, game.commence_time)


@dataclass(frozen=True)
class ClosingLineValue:
    closing_no_vig_at_bet_line: float | None
    price_clv: float | None
    """bet_decimal x closing no-vig - 1 at the bet's own line (None if no book closed there)."""
    closing_main_line: float | None
    points_gained: float | None
    """Spread/total points the bettor gained versus the consensus closing main line."""
    books_at_close: int
    close_minutes_before_kickoff: float | None


def _points_gained(
    market: Market, selection: Selection, bet_line: float | None, close_line: float | None
) -> float | None:
    if bet_line is None or close_line is None or market is Market.MONEYLINE:
        return None
    if market is Market.SPREAD:
        return bet_line - close_line  # home +3.5 vs close +3: +0.5 in the bettor's favor
    return close_line - bet_line if selection is Selection.OVER else bet_line - close_line


def closing_line_value(
    session: Session,
    game_id: int,
    market: Market,
    selection: Selection,
    bet_line: float | None,
    bet_american_odds: float,
) -> ClosingLineValue:
    game = session.get_one(Game, game_id)
    at_line = closing_consensus_at_line(session, game_id, market, selection, bet_line)
    closes = [h.closing for h in line_history(session, game_id, market, selection) if h.closing]
    close_lines = sorted(c.line for c in closes if c.line is not None)
    main_close = close_lines[len(close_lines) // 2] if close_lines else None
    latest_close = max((c.observed_at for c in closes), default=None)
    price_clv = (
        bm.closing_line_value(
            bm.american_to_decimal(bet_american_odds), at_line.consensus_no_vig_probability
        )
        if at_line is not None
        else None
    )
    return ClosingLineValue(
        closing_no_vig_at_bet_line=at_line.consensus_no_vig_probability if at_line else None,
        price_clv=price_clv,
        closing_main_line=main_close,
        points_gained=_points_gained(market, selection, bet_line, main_close),
        books_at_close=len(closes),
        close_minutes_before_kickoff=(
            (game.commence_time - latest_close).total_seconds() / 60 if latest_close else None
        ),
    )


@dataclass(frozen=True)
class ClosingSnapshot:
    """Every book's state at kickoff for one game and market, built once. Answers
    the same questions as ``closing_line_value`` (identical rules) without
    rebuilding the line history at every change - for scoring many bets."""

    market: Market
    states: list[tuple[str, BookTimeline, dict[QuoteKey, OddsSnapshot]]]
    """(sportsbook key, timeline, state at kickoff) for books seen before kickoff."""
    kickoff: datetime

    def consensus_at_line(self, selection: Selection, line: float | None) -> MarketConsensus | None:
        quotes = [
            TwoSidedQuote(book, side.decimal_odds, other.decimal_odds)
            for book, _, state in self.states
            for side, other in _entries(state, self.market, selection)
            if side.line == line and other is not None
        ]
        return market_consensus(quotes) if quotes else None

    def main_close(self, selection: Selection) -> float | None:
        lines = sorted(
            p.line
            for _, timeline, state in self.states
            if (p := _main_at(timeline, self.kickoff, self.market, selection, state=state))
            is not None
            and p.line is not None
        )
        return lines[len(lines) // 2] if lines else None

    def price_clv(self, selection: Selection, line: float | None, american: float) -> float | None:
        at_line = self.consensus_at_line(selection, line)
        if at_line is None:
            return None
        return bm.closing_line_value(
            bm.american_to_decimal(american), at_line.consensus_no_vig_probability
        )

    def points_gained(self, selection: Selection, line: float | None) -> float | None:
        return _points_gained(self.market, selection, line, self.main_close(selection))


def closing_snapshot(session: Session, game_id: int, market: Market) -> ClosingSnapshot:
    game = session.get_one(Game, game_id)
    books = dict(session.execute(select(Sportsbook.id, Sportsbook.key)).all())
    states = [
        (books[book_id], timeline, timeline.state_at(game.commence_time))
        for book_id, timeline in book_timelines(session, game_id, market).items()
        if timeline.last_seen(game.commence_time) is not None
    ]
    return ClosingSnapshot(market, states, game.commence_time)
