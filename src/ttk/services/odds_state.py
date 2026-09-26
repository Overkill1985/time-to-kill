"""Reconstruct quote state from the change log (odds_snapshots + book_observations).

A book's quotes for a game at time T: replay its snapshot rows with
observed_at <= T in id order (ids are append order); a quote is on the board
iff its latest row is not withdrawn. When the book was last confirmed is its
latest book_observation at or before T.

One provider per book per game: if two providers report the same book, the one
that observed it most recently is used, so their differing coverage (e.g. alt
lines) never interleaves.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ttk.db.models import BookObservation, OddsSnapshot

QuoteKey = tuple[str, str, float | None]
"""(market, selection, line) within one game and book."""


@dataclass
class BookTimeline:
    sportsbook_id: int
    provider: str
    events: list[OddsSnapshot] = field(default_factory=list)
    """Change rows in append order."""
    observations: list[datetime] = field(default_factory=list)
    """Poll times at which the book was seen, ascending."""

    def state_at(self, t: datetime) -> dict[QuoteKey, OddsSnapshot]:
        state: dict[QuoteKey, OddsSnapshot] = {}
        for row in self.events:
            if row.observed_at > t:
                break
            key = (row.market, row.selection, row.line)
            if row.withdrawn:
                state.pop(key, None)
            else:
                state[key] = row
        return state

    def state_now(self) -> dict[QuoteKey, OddsSnapshot]:
        """Every change applied, including withdrawals written when the book was
        absent from a poll (those come after its last observation)."""
        return self.state_at(self.events[-1].observed_at) if self.events else {}

    def last_seen(self, t: datetime | None = None) -> datetime | None:
        if not self.observations:
            return None
        if t is None:
            return self.observations[-1]
        i = bisect_right(self.observations, t)
        return self.observations[i - 1] if i else None

    def change_times(self) -> list[datetime]:
        return sorted({row.observed_at for row in self.events})


def book_timelines(
    session: Session, game_id: int, market: str | None = None
) -> dict[int, BookTimeline]:
    """Per book, the timeline from the provider that saw it most recently."""
    latest = session.execute(
        select(
            BookObservation.sportsbook_id,
            BookObservation.provider,
            func.max(BookObservation.observed_at),
        )
        .where(BookObservation.game_id == game_id)
        .group_by(BookObservation.sportsbook_id, BookObservation.provider)
    ).all()
    chosen: dict[int, tuple[str, datetime]] = {}
    for book_id, provider, seen in latest:
        if book_id not in chosen or seen > chosen[book_id][1]:
            chosen[book_id] = (provider, seen)
    timelines = {b: BookTimeline(b, p) for b, (p, _) in chosen.items()}

    stmt = select(OddsSnapshot).where(OddsSnapshot.game_id == game_id)
    if market is not None:
        stmt = stmt.where(OddsSnapshot.market == market)
    for row in session.scalars(stmt.order_by(OddsSnapshot.id)):
        timeline = timelines.get(row.sportsbook_id)
        if timeline is not None and row.provider == timeline.provider:
            timeline.events.append(row)
    for obs in session.scalars(
        select(BookObservation)
        .where(BookObservation.game_id == game_id)
        .order_by(BookObservation.observed_at)
    ):
        timeline = timelines.get(obs.sportsbook_id)
        if timeline is not None and obs.provider == timeline.provider:
            timeline.observations.append(obs.observed_at)
    return timelines


def latest_rows(
    session: Session, provider: str, game_ids: Iterable[int]
) -> dict[tuple[int, int, str, str, float | None], OddsSnapshot]:
    """(game, book, market, selection, line) -> the key's latest row (withdrawn or not),
    for diffing a new poll against what is on file."""
    ids = list(game_ids)
    if not ids:
        return {}
    latest_ids = (
        select(func.max(OddsSnapshot.id))
        .where(OddsSnapshot.provider == provider, OddsSnapshot.game_id.in_(ids))
        .group_by(
            OddsSnapshot.game_id,
            OddsSnapshot.sportsbook_id,
            OddsSnapshot.market,
            OddsSnapshot.selection,
            OddsSnapshot.line,
        )
    )
    rows = session.scalars(select(OddsSnapshot).where(OddsSnapshot.id.in_(latest_ids)))
    return {(r.game_id, r.sportsbook_id, r.market, r.selection, r.line): r for r in rows}
