"""Prop legs for the parlay lab: a player prop's price at one book, the market's
no-vig chance at its line, and its result from the player's stat line.

Prices come from the latest props pull taken at or before the moment asked about
(services/props: 24 h and 1 h before kickoff) - props are snapshots, not a
stream, so a price can be up to a day old; the lab says how old. An anytime-TD
Yes/No is over/under 0.5. There is no prop model: a prop leg's probability is
the market's (median no-vig across the books quoting both sides at the line).

Grading: once the game is final and its player stats are in (NFL: nflverse,
usually within a day or two; NBA: ESPN box scores, every 6 h), the stat is
compared with the line. A player with no stat line for a finished game whose
stats are in did not play: the leg is void, as books void props for players who
don't take part. NBA box scores list every player (those who didn't play with a
reason), so a missing NBA player is void. nflverse lists only players who recorded a
stat, so a missing NFL player may have been inactive (void) or played without
recording one (over loses): that leg waits to be settled by hand.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ttk.db.models import Game, NflPlayerGameStat, PlayerGameStat, PropPull, PropQuote, Sportsbook
from ttk.domain import BetResult, Selection, Sport
from ttk.research.moneyline import no_vig
from ttk.services.player_props import LADDER, NBA_STATS, NFL_STATS, YES_NO, _implied
from ttk.services.props import player_key, roster_sides

SIDE = {Selection.OVER: "Over", Selection.UNDER: "Under"}


def prop_label(market: str) -> str:
    """'player_pass_yds' -> 'pass yds' (the page shows friendlier names)."""
    return market.removeprefix("player_").replace("_", " ")


@dataclass(frozen=True)
class PropOffer:
    pulled_at: datetime
    book_price: float | None
    """The book's price on this side at the line (None if it doesn't quote it)."""
    book_other: float | None
    market_over: float | None
    """Median no-vig P(over) across books quoting both sides at the line."""
    books: int
    implied_over: float | None = None
    """Median implied P(over) of every book's over price, cut included: the only chance
    a one-sided milestone has."""


def _point(q: PropQuote) -> float | None:
    if q.point is not None:
        return q.point
    return 0.5 if q.selection in YES_NO else None


def prop_offer(
    session: Session,
    game_id: int,
    player: str,
    market: str,
    selection: Selection,
    line: float,
    sportsbook_id: int,
    at: datetime,
) -> PropOffer | None:
    """The latest pull at or before ``at``; None if there is none for this prop."""
    pull = session.scalars(
        select(PropPull)
        .where(PropPull.game_id == game_id, PropPull.pulled_at <= at)
        .order_by(PropPull.pulled_at.desc())
        .limit(1)
    ).first()
    if pull is None:
        return None
    by_book: dict[int, dict[str, float]] = {}
    # Books spell a player differently ("C.J. Stroud", "C.J Stroud (HOU)"): match the key.
    key = player_key(player)
    for q in session.scalars(
        select(PropQuote).where(PropQuote.pull_id == pull.id, PropQuote.market == market)
    ):
        if player_key(q.player) != key:
            continue
        point: float | None
        rung = LADDER.match(q.selection) if q.point is None else None
        if rung:  # a milestone ("200+ Passing Yards") is an over at 199.5
            side, point = "Over", int(rung.group(1)) - 0.5
        else:
            side, point = YES_NO.get(q.selection, q.selection), _point(q)
        if side not in ("Over", "Under") or point != line:
            continue
        sides = by_book.setdefault(q.sportsbook_id, {})
        if side not in sides or q.american_odds > sides[side]:
            sides[side] = q.american_odds
    if not by_book:
        return None
    overs = [
        p
        for sides in by_book.values()
        if len(sides) == 2 and (p := no_vig(sides["Over"], sides["Under"])) is not None
    ]
    mine = by_book.get(sportsbook_id, {})
    want = SIDE[selection]
    other = SIDE[Selection.UNDER if selection is Selection.OVER else Selection.OVER]
    implied = [_implied(sides["Over"]) for sides in by_book.values() if "Over" in sides]
    return PropOffer(
        pull.pulled_at,
        mine.get(want),
        mine.get(other),
        statistics.median(overs) if overs else None,
        sum(1 for sides in by_book.values() if len(sides) == 2),
        statistics.median(implied) if implied else None,
    )


def book_key(session: Session, sportsbook_id: int) -> str:
    return session.get_one(Sportsbook, sportsbook_id).key


@dataclass(frozen=True)
class PropResult:
    result: BetResult | None
    """None: not gradable yet (stats not in), or needs settling by hand."""
    actual: float | None
    manual: bool = False
    """True: the stats can't decide it; settle it by hand."""


def _stat_rows(session: Session, game: Game) -> list[Any]:
    model: Any = NflPlayerGameStat if game.sport == Sport.NFL else PlayerGameStat
    return list(session.scalars(select(model).where(model.game_id == game.id)))


def grade_prop(
    session: Session, game: Game, player: str, market: str, selection: Selection, line: float
) -> PropResult:
    fns: dict[str, Any] = NFL_STATS if game.sport == Sport.NFL else NBA_STATS
    fn = fns.get(market)
    if fn is None:
        return PropResult(None, None, manual=True)  # its stat isn't recorded
    rows = _stat_rows(session, game)
    if not rows:
        return PropResult(None, None)  # stats not imported yet
    names = {player_key(r.player_name): r for r in rows if r.player_name}
    key = player_key(player)
    row = names.get(key)
    if row is None and " " in key:
        fits = [k for k in names if roster_sides(key, {"x": {k}})]
        row = names[fits[0]] if len(fits) == 1 else None
    if row is None and game.sport == Sport.NFL:
        return PropResult(None, None, manual=True)  # inactive, or played without a stat
    if row is None or (game.sport == Sport.NBA and not row.played):
        return PropResult(BetResult.VOID, None)
    actual = fn(row)
    if actual is None:
        return PropResult(None, None)
    if actual == line:
        return PropResult(BetResult.PUSH, actual)
    over = actual > line
    won = over if selection is Selection.OVER else not over
    return PropResult(BetResult.WIN if won else BetResult.LOSS, actual)
