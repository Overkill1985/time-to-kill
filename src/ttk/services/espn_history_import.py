"""Import a sport's history from ESPN: games and results (scoreboards) plus every
market sportsbook's closing - and, where recorded, opening - line (core odds).

Lines go to reported_lines, one row per (game, provider) with provider
``espn:<book>`` (close) or ``espn-open:<book>`` (open). They are evaluation data
with the same rules as nflverse lines (docs/MODEL-GOVERNANCE.md), except that
openers are timestamped by construction: the opener precedes the close.

Resumable: a game that already has any ``espn:`` line is skipped; progress is
committed every ``batch`` games. Paced (``delay`` seconds between requests) -
this is an unofficial API, and a season is ~1,300 odds requests for the NBA.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import Game, GameSourceId, IngestionRun, ReportedLine
from ttk.domain import GameStatus, Sport
from ttk.providers.base import ProviderError
from ttk.providers.espn import EspnScheduleProvider
from ttk.providers.espn_odds import BookLine, EspnCoreOdds, parse_item
from ttk.services.runs import RunResult, audited_run
from ttk.services.schedule_ingest import run_schedule_ingestion


@dataclass(frozen=True)
class SeasonWindow:
    start: date
    end: date


def season_window(sport: Sport, season: int) -> SeasonWindow:
    """ESPN season years are the year a season ends (2025-26 = 2026)."""
    if sport == Sport.NBA:
        if season == 2020:  # the bubble: the 2019-20 season finished in October 2020
            return SeasonWindow(date(2019, 9, 25), date(2020, 10, 15))
        if season == 2021:  # shortened 2020-21 season started in December 2020
            return SeasonWindow(date(2020, 12, 1), date(2021, 7, 25))
    if sport in (Sport.NBA, Sport.NCAAB):
        # College basketball: early November to early April, so this covers every
        # season, including 2020-21 (from November 25, 2020).
        return SeasonWindow(date(season - 1, 9, 25), date(season, 6, 30))
    return SeasonWindow(date(season, 8, 20), date(season + 1, 2, 20))  # football


def _store(session: Session, game_id: int, lines: list[BookLine]) -> int:
    written = 0
    for line in lines:
        provider = f"{'espn-open' if line.moment == 'open' else 'espn'}:{line.book}"
        row = session.scalar(
            select(ReportedLine).where(
                ReportedLine.game_id == game_id, ReportedLine.provider == provider
            )
        )
        if row is None:
            row = ReportedLine(game_id=game_id, provider=provider)
            session.add(row)
        row.home_spread = line.home_spread
        row.home_spread_odds, row.away_spread_odds = line.home_spread_odds, line.away_spread_odds
        row.total, row.over_odds, row.under_odds = line.total, line.over_odds, line.under_odds
        row.home_moneyline, row.away_moneyline = line.home_moneyline, line.away_moneyline
        written += 1
    return written


def import_season_odds(
    session_factory: sessionmaker[Session],
    sport: Sport,
    season: int,
    *,
    odds: EspnCoreOdds,
    delay: float = 0.25,
    batch: int = 10,
) -> IngestionRun:
    """Closing/opening lines for every final game of the season lacking ESPN lines."""

    def work(session: Session, run: IngestionRun) -> RunResult:
        has_lines = select(ReportedLine.game_id).where(ReportedLine.provider.like("espn:%"))
        pending = session.execute(
            select(Game.id, GameSourceId.source_identifier)
            .join(
                GameSourceId, (GameSourceId.game_id == Game.id) & (GameSourceId.provider == "espn")
            )
            .where(
                Game.sport == sport,
                Game.status == GameStatus.FINAL,
                Game.season == season,
                Game.id.not_in(has_lines),
            )
            .order_by(Game.commence_time)
        ).all()
        stats: Counter[str] = Counter({"season": season, "games_pending": len(pending)})
        written = 0
        for i, (game_id, event_id) in enumerate(pending, start=1):
            lines = [line for item in odds.fetch(sport, event_id) for line in parse_item(item)]
            if lines:
                written += _store(session, game_id, lines)
                stats["games_with_lines"] += 1
                stats["games_with_openers"] += any(x.moment == "open" for x in lines)
            else:
                stats["games_without_lines"] += 1
            if i % batch == 0:
                session.commit()  # resumable: finished games are skipped next time
            time.sleep(delay)
        return RunResult(written, {}, stats)

    return audited_run(
        session_factory, provider="espn-core", kind="odds-history", sport=sport, work=work
    )


def import_season(
    session_factory: sessionmaker[Session],
    sport: Sport,
    season: int,
    *,
    delay: float = 0.25,
) -> tuple[int, IngestionRun]:
    """Schedule/results for the season window, then its odds history. Returns the
    number of games written by the schedule runs, and the odds run."""
    window = season_window(sport, season)
    provider = EspnScheduleProvider(delay=delay)
    # A month per transaction: a whole season in one held the database write lock
    # for minutes and starved the collector (2026-09-27).
    start = window.start
    games = 0
    while start <= window.end:
        end = min(start + timedelta(days=30), window.end)
        schedule = run_schedule_ingestion(session_factory, provider, sport, start, end)
        if schedule.status != "SUCCESS":
            raise ProviderError(schedule.error or "schedule import failed")
        games += schedule.records_written
        start = end + timedelta(days=1)
    odds_run = import_season_odds(session_factory, sport, season, odds=EspnCoreOdds(), delay=delay)
    return games, odds_run
