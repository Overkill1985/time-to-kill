"""Import NFL history (games, results, reported lines) from nflverse."""

from __future__ import annotations

from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import IngestionRun, ReportedLine
from ttk.domain import Sport
from ttk.providers.nflverse import HistoricalGame, NflverseProvider
from ttk.services.identity import resolve_game
from ttk.services.runs import RunResult, audited_run


def store_history(session: Session, games: list[HistoricalGame], stats: Counter[str]) -> int:
    for item in games:
        resolved = resolve_game(session, item.game, stats)
        if item.lines is None:
            continue
        line = session.scalar(
            select(ReportedLine).where(
                ReportedLine.game_id == resolved.game.id,
                ReportedLine.provider == item.game.provider,
            )
        )
        if line is None:
            line = ReportedLine(game_id=resolved.game.id, provider=item.game.provider)
            session.add(line)
        lines = item.lines
        if resolved.swapped:  # provider's home is our away: swap sides, negate the spread
            line.home_spread = None if lines.home_spread is None else -lines.home_spread
            line.home_spread_odds, line.away_spread_odds = (
                lines.away_spread_odds,
                lines.home_spread_odds,
            )
            line.home_moneyline, line.away_moneyline = (
                lines.away_moneyline,
                lines.home_moneyline,
            )
            stats["lines_swapped"] += 1
        else:
            line.home_spread = lines.home_spread
            line.home_spread_odds, line.away_spread_odds = (
                lines.home_spread_odds,
                lines.away_spread_odds,
            )
            line.home_moneyline, line.away_moneyline = (
                lines.home_moneyline,
                lines.away_moneyline,
            )
        line.total, line.over_odds, line.under_odds = (
            lines.total,
            lines.over_odds,
            lines.under_odds,
        )
    return len(games)


def run_nfl_history_import(
    session_factory: sessionmaker[Session],
    provider: NflverseProvider,
    *,
    first_season: int,
    last_season: int | None = None,
) -> IngestionRun:
    """Idempotent: games are matched by nflverse game id (or ESPN event id), and
    each game's reported line row is replaced rather than duplicated."""

    def work(session: Session, run: IngestionRun) -> RunResult:
        games, skipped = provider.fetch_history(first_season=first_season, last_season=last_season)
        stats: Counter[str] = Counter()
        return RunResult(store_history(session, games, stats), skipped, stats)

    return audited_run(
        session_factory, provider=provider.name, kind="history", sport=Sport.NFL, work=work
    )
