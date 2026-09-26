"""Import NFL history (games, results, reported lines) from nflverse."""

from __future__ import annotations

from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import GameStarter, IngestionRun, ReportedLine
from ttk.domain import Sport
from ttk.providers.nflverse import HistoricalGame, NflverseProvider, Starter
from ttk.services.identity import resolve_game
from ttk.services.runs import RunResult, audited_run


def _store_starter(
    session: Session, game_id: int, team_id: int, starter: Starter | None, provider: str
) -> None:
    if starter is None:
        return
    row = session.scalar(
        select(GameStarter).where(
            GameStarter.game_id == game_id,
            GameStarter.team_id == team_id,
            GameStarter.position == "QB",
            GameStarter.provider == provider,
        )
    )
    if row is None:
        row = GameStarter(game_id=game_id, team_id=team_id, position="QB", provider=provider)
        session.add(row)
    row.player_id, row.player_name = starter.player_id, starter.player_name


def store_history(session: Session, games: list[HistoricalGame], stats: Counter[str]) -> int:
    for item in games:
        resolved = resolve_game(session, item.game, stats)
        game = resolved.game
        # Starters are keyed by team, so a swapped link just maps sides to teams.
        provider_home, provider_away = (
            (game.away_team_id, game.home_team_id)
            if resolved.swapped
            else (game.home_team_id, game.away_team_id)
        )
        _store_starter(session, game.id, provider_home, item.home_qb, item.game.provider)
        _store_starter(session, game.id, provider_away, item.away_qb, item.game.provider)
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
