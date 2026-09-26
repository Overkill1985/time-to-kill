"""Odds collection passes: build our own timestamped line history, poll by poll.

Each pass polls every requested sport that has a game in the look-ahead window
(or has no schedule data yet, so a fresh database can bootstrap). Snapshots are
immutable, so repeated passes accumulate openers, line moves and - with a poll
shortly before kickoff - closing lines.

Quota: one request per sport per pass. PropLine free = 1,000/day, so four
sports every 15 minutes (384/day) fits; the pass stops early when the
provider reports fewer remaining requests than ``min_remaining``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import Game, IngestionRun, utcnow
from ttk.domain import Sport
from ttk.providers.base import OddsProvider
from ttk.services.odds_ingest import run_odds_ingestion

LOOKAHEAD = timedelta(days=7)


def sports_to_poll(
    session: Session, sports: Sequence[Sport], now: datetime, lookahead: timedelta = LOOKAHEAD
) -> tuple[list[Sport], dict[Sport, str]]:
    """Sports worth a request now, and why the others were skipped."""
    poll, skipped = [], {}
    for sport in sports:
        known = session.scalar(select(func.count()).select_from(Game).where(Game.sport == sport))
        upcoming = session.scalar(
            select(func.count())
            .select_from(Game)
            .where(
                Game.sport == sport,
                Game.commence_time > now - timedelta(hours=4),  # include games in progress
                Game.commence_time <= now + lookahead,
            )
        )
        if not known or upcoming:
            poll.append(sport)
        else:
            skipped[sport] = f"no games in the next {lookahead.days} days"
    return poll, skipped


@dataclass
class PassResult:
    runs: list[IngestionRun] = field(default_factory=list)
    skipped: dict[Sport, str] = field(default_factory=dict)
    remaining: int | None = None


def collect_once(
    session_factory: sessionmaker[Session],
    provider: OddsProvider,
    sports: Sequence[Sport],
    *,
    min_remaining: int = 20,
    remaining: int | None = None,
    now: datetime | None = None,
) -> PassResult:
    """One pass. ``remaining`` is the provider's latest reported daily balance."""
    result = PassResult(remaining=remaining)
    with session_factory() as session:
        poll, result.skipped = sports_to_poll(session, sports, now or utcnow())
    for sport in poll:
        if result.remaining is not None and result.remaining < min_remaining:
            result.skipped[sport] = f"quota: {result.remaining} requests left"
            continue
        run = run_odds_ingestion(session_factory, provider, sport)
        result.runs.append(run)
        result.remaining = getattr(provider, "daily_remaining", None)
    return result
