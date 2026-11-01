"""Odds coverage: how many of a day's games had a spread before kickoff.

A game with no pregame spread gets no forward snapshot, so a gap here is a gap in
every forward test. The usual cause in college basketball is a provider team name
that links to no ESPN team (``ttk teams-unmatched``); the odds for its games are
dropped until it is linked. Low-major games whose lines are never posted are the
other cause, and nothing to fix.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from ttk.db.models import Game, OddsSnapshot
from ttk.domain import GameStatus, Market
from ttk.services.team_review import unmatched_teams


@dataclass
class CoverageLine:
    sport: str
    games: int
    with_spread: int
    unmatched_teams: int
    """Provider team names in this sport linked to no ESPN team (all time)."""

    @property
    def share(self) -> float | None:
        return self.with_spread / self.games if self.games else None


def odds_coverage(session: Session, start: datetime, end: datetime) -> list[CoverageLine]:
    """Games kicking off in [start, end), and how many had a spread quote before."""
    pregame_spread = exists().where(
        OddsSnapshot.game_id == Game.id,
        OddsSnapshot.market == Market.SPREAD,
        OddsSnapshot.observed_at < Game.commence_time,
    )
    games: Counter[str] = Counter()
    covered: Counter[str] = Counter()
    for sport, has_spread in session.execute(
        select(Game.sport, pregame_spread).where(
            Game.commence_time >= start,
            Game.commence_time < end,
            Game.status != GameStatus.DUPLICATE,
        )
    ):
        games[sport] += 1
        covered[sport] += bool(has_spread)
    unmatched = Counter(t.sport for t in unmatched_teams(session))
    return [
        CoverageLine(sport, games[sport], covered[sport], unmatched[sport])
        for sport in sorted(games)
    ]
