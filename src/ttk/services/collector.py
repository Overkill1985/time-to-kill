"""Odds collection passes: build our own timestamped line history, poll by poll.

Each pass first refreshes the schedule authority's games (``refresh_schedules``,
at most every 6 hours; ESPN is free), then polls every requested sport with a
game in the look-ahead window. A sport with no games on file is polled only if
no recent schedule refresh has shown it idle. Snapshots are
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
from ttk.providers.base import OddsProvider, ScheduleProvider
from ttk.providers.espn_injuries import EspnInjuries
from ttk.services.injury_ingest import ingest_injuries
from ttk.services.odds_ingest import run_odds_ingestion
from ttk.services.schedule_ingest import run_schedule_ingestion

LOOKAHEAD = timedelta(days=7)
"""Schedule refresh window, and the default odds window."""
SCHEDULE_REFRESH = timedelta(hours=6)

# How far ahead to poll odds, per sport. Wider where books post lines early, so
# openers are captured. Verified 2026-09-26: PropLine listed NBA regular-season
# games from Oct 20 to Dec 25 (up to ~90 days out, 9-16 books each), but NFL and
# CFB only about a week ahead.
ODDS_LOOKAHEAD: dict[Sport, timedelta] = {Sport.NBA: timedelta(days=90)}

# Injury lists (ESPN, free), polled on their own cadence. NBA's list is ~0.8 MB,
# so every pass; NFL's is ~9 MB, so hourly. College lists are near-empty
# (2026-09-27: CFB 3 entries, NCAAB none) and are not polled.
INJURY_REFRESH: dict[Sport, timedelta] = {
    Sport.NBA: timedelta(minutes=10),
    Sport.NFL: timedelta(minutes=55),
}


def odds_lookahead(sport: Sport) -> timedelta:
    return ODDS_LOOKAHEAD.get(sport, LOOKAHEAD)


def _last_schedule_run(session: Session, sport: Sport) -> datetime | None:
    return session.scalar(
        select(func.max(IngestionRun.finished_at)).where(
            IngestionRun.kind == "schedule",
            IngestionRun.sport == sport,
            IngestionRun.status == "SUCCESS",
        )
    )


def refresh_schedules(
    session_factory: sessionmaker[Session],
    provider: ScheduleProvider,
    sports: Sequence[Sport],
    *,
    now: datetime | None = None,
    every: timedelta = SCHEDULE_REFRESH,
    lookahead: timedelta = LOOKAHEAD,
) -> list[IngestionRun]:
    """Load the schedule authority's games before polling odds, so odds attach to
    its teams and games (and curated aliases can apply). Refreshes a sport at
    most every ``every``."""
    now = now or utcnow()
    runs = []
    for sport in sports:
        with session_factory() as session:
            last = _last_schedule_run(session, sport)
        if last is not None and now - last < every:
            continue
        runs.append(
            run_schedule_ingestion(
                session_factory,
                provider,
                sport,
                (now - timedelta(days=1)).date(),
                (now + lookahead).date(),
            )
        )
    return runs


def _last_run(session: Session, sport: Sport, kind: str) -> datetime | None:
    return session.scalar(
        select(func.max(IngestionRun.finished_at)).where(
            IngestionRun.kind == kind,
            IngestionRun.sport == sport,
            IngestionRun.status == "SUCCESS",
        )
    )


def refresh_injuries(
    session_factory: sessionmaker[Session],
    source: EspnInjuries,
    *,
    now: datetime | None = None,
    every: dict[Sport, timedelta] | None = None,
) -> list[IngestionRun]:
    """Store changes to each sport's injury list, at most every ``every[sport]``.
    Each run's ``observed_at`` is ``now``: what we knew, and when."""
    now = now or utcnow()
    runs = []
    for sport, interval in (every or INJURY_REFRESH).items():
        with session_factory() as session:
            last = _last_run(session, sport, "injuries")
        if last is not None and now - last < interval:
            continue
        runs.append(ingest_injuries(session_factory, sport, source=source, now=now))
    return runs


def sports_to_poll(
    session: Session,
    sports: Sequence[Sport],
    now: datetime,
    lookahead: dict[Sport, timedelta] | None = None,
) -> tuple[list[Sport], dict[Sport, str]]:
    """Sports worth a request now, and why the others were skipped."""
    poll, skipped = [], {}
    for sport in sports:
        window = (lookahead or {}).get(sport, odds_lookahead(sport))
        known = session.scalar(select(func.count()).select_from(Game).where(Game.sport == sport))
        upcoming = session.scalar(
            select(func.count())
            .select_from(Game)
            .where(
                Game.sport == sport,
                Game.commence_time > now - timedelta(hours=4),  # include games in progress
                Game.commence_time <= now + window,
            )
        )
        # With no games on file, poll to bootstrap - unless a recent schedule
        # refresh already says the sport has nothing coming up (off-season).
        # The schedule only covers LOOKAHEAD, so it can't vouch for a wider window.
        recent_schedule = _last_schedule_run(session, sport)
        schedule_says_idle = (
            recent_schedule is not None
            and now - recent_schedule < 2 * SCHEDULE_REFRESH
            and window <= LOOKAHEAD
        )
        if upcoming or (not known and not schedule_says_idle):
            poll.append(sport)
        else:
            skipped[sport] = f"no games in the next {window.days} days"
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
