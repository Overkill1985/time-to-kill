"""Read the current market for a game from stored snapshots.

"Current" = each book's most recent observation of this game. Lines a book
posted in an earlier poll but not in its latest one are treated as withdrawn.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ttk.consensus import MarketConsensus, TwoSidedQuote, market_consensus
from ttk.db.models import OddsSnapshot, Sportsbook
from ttk.domain import Market, Selection

# Selection -> its opposite side, and how the opposite side's line relates.
_OPPOSITE = {
    Selection.HOME: Selection.AWAY,
    Selection.AWAY: Selection.HOME,
    Selection.OVER: Selection.UNDER,
    Selection.UNDER: Selection.OVER,
}


def _opposite_line(market: Market, line: float | None) -> float | None:
    if market is Market.SPREAD and line is not None:
        return -line  # home -3.5 pairs with away +3.5
    return line  # totals share the number; moneyline has none


@dataclass(frozen=True)
class SideMarket:
    market: Market
    selection: Selection
    line: float | None
    consensus: MarketConsensus
    oldest_observation: datetime
    """Oldest price used; the market is only as fresh as this."""


def current_snapshots(session: Session, game_id: int) -> list[OddsSnapshot]:
    latest_per_book = (
        select(OddsSnapshot.sportsbook_id, func.max(OddsSnapshot.observed_at).label("latest"))
        .where(OddsSnapshot.game_id == game_id)
        .group_by(OddsSnapshot.sportsbook_id)
        .subquery()
    )
    stmt = (
        select(OddsSnapshot)
        .join(
            latest_per_book,
            (OddsSnapshot.sportsbook_id == latest_per_book.c.sportsbook_id)
            & (OddsSnapshot.observed_at == latest_per_book.c.latest),
        )
        .where(OddsSnapshot.game_id == game_id)
    )
    return list(session.scalars(stmt))


def side_markets(session: Session, game_id: int) -> list[SideMarket]:
    """Consensus for every (market, selection, line) that at least one book prices on
    both sides. Alternate lines are included; see ``main_lines`` to pick one per market."""
    snaps = current_snapshots(session, game_id)
    book_keys = dict(session.execute(select(Sportsbook.id, Sportsbook.key)).all())
    index = {(s.sportsbook_id, s.market, s.selection, s.line): s for s in snaps}
    grouped: dict[tuple[Market, Selection, float | None], list[OddsSnapshot]] = defaultdict(list)
    pairs: dict[tuple[Market, Selection, float | None], list[TwoSidedQuote]] = defaultdict(list)
    for s in snaps:
        market, selection = Market(s.market), Selection(s.selection)
        if selection not in _OPPOSITE:
            continue
        other = index.get(
            (s.sportsbook_id, market, _OPPOSITE[selection], _opposite_line(market, s.line))
        )
        if other is None:
            continue
        key = (market, selection, s.line)
        pairs[key].append(
            TwoSidedQuote(book_keys[s.sportsbook_id], s.decimal_odds, other.decimal_odds)
        )
        grouped[key].extend([s, other])
    return [
        SideMarket(
            market=m,
            selection=sel,
            line=line,
            consensus=market_consensus(quotes),
            oldest_observation=min(s.observed_at for s in grouped[(m, sel, line)]),
        )
        for (m, sel, line), quotes in pairs.items()
    ]


def main_lines(markets: list[SideMarket]) -> list[SideMarket]:
    """One line per (market, selection): the most widely offered, ties broken by
    closest to a 50/50 price - the book's main line rather than an alternate."""
    best: dict[tuple[Market, Selection], SideMarket] = {}
    for sm in markets:
        key = (sm.market, sm.selection)
        current = best.get(key)
        rank = (sm.consensus.books_reporting, -abs(sm.consensus.consensus_no_vig_probability - 0.5))
        if current is None or rank > (
            current.consensus.books_reporting,
            -abs(current.consensus.consensus_no_vig_probability - 0.5),
        ):
            best[key] = sm
    return list(best.values())
