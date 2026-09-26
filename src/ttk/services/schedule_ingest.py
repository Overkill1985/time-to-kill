"""Schedule and results ingestion from the schedule authority (ESPN)."""

from __future__ import annotations

from collections import Counter
from datetime import date

from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import IngestionRun
from ttk.domain import Sport
from ttk.providers.base import ScheduleProvider
from ttk.services.identity import resolve_game
from ttk.services.runs import RunResult, audited_run


def run_schedule_ingestion(
    session_factory: sessionmaker[Session],
    provider: ScheduleProvider,
    sport: Sport,
    start: date,
    end: date,
) -> IngestionRun:
    """Upsert games, status and scores for start..end. Re-running is safe: games
    are matched by the provider's event id and updated in place."""

    def work(session: Session, run: IngestionRun) -> RunResult:
        fetch = provider.fetch_games(sport, start, end)
        stats: Counter[str] = Counter()
        for game in fetch.games:
            resolve_game(session, game, stats)
        return RunResult(len(fetch.games), fetch.skipped, stats)

    return audited_run(
        session_factory, provider=provider.name, kind="schedule", sport=sport, work=work
    )
