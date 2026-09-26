"""Persist normalized odds as immutable snapshots, with an ingestion-run audit row."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ttk import betting_math as bm
from ttk.db.models import IngestionRun, OddsSnapshot, Sportsbook, utcnow
from ttk.domain import Selection, Sport
from ttk.providers.base import OddsFetch, OddsProvider
from ttk.services.identity import resolve_game
from ttk.services.runs import RunResult, audited_run

_FLIP = {Selection.HOME: Selection.AWAY, Selection.AWAY: Selection.HOME}


def _sportsbook(session: Session, cache: dict[str, Sportsbook], key: str, name: str) -> Sportsbook:
    if key not in cache:
        book = session.scalar(select(Sportsbook).where(Sportsbook.key == key))
        if book is None:
            book = Sportsbook(key=key, name=name)
            session.add(book)
            session.flush()
        cache[key] = book
    return cache[key]


def store_odds(
    session: Session,
    fetch: OddsFetch,
    *,
    run: IngestionRun,
    observed_at: datetime,
    stats: Counter[str],
) -> int:
    games = {g.source_identifier: resolve_game(session, g, stats) for g in fetch.games}
    books: dict[str, Sportsbook] = {}
    written = 0
    for q in fetch.quotes:
        resolved = games.get(q.game_source_identifier)
        if resolved is None:
            continue
        # A spread line is from the selection's own perspective, so flipping the
        # side (provider's HOME is our AWAY) leaves the line unchanged.
        selection = _FLIP.get(q.selection, q.selection) if resolved.swapped else q.selection
        decimal_odds = bm.american_to_decimal(q.american_odds)
        session.add(
            OddsSnapshot(
                game_id=resolved.game.id,
                sportsbook_id=_sportsbook(session, books, q.sportsbook_key, q.sportsbook_name).id,
                market=q.market,
                selection=selection,
                line=q.line,
                american_odds=q.american_odds,
                decimal_odds=decimal_odds,
                implied_probability=bm.decimal_implied_probability(decimal_odds),
                provider=q.provider,
                source_timestamp=q.source_timestamp,
                observed_at=observed_at,
                ingestion_run_id=run.id,
            )
        )
        written += 1
    return written


def run_odds_ingestion(
    session_factory: sessionmaker[Session], provider: OddsProvider, sport: Sport
) -> IngestionRun:
    """Fetch and store one sport's odds. A provider failure is recorded, not raised."""

    def work(session: Session, run: IngestionRun) -> RunResult:
        fetch = provider.fetch_odds(sport)
        stats: Counter[str] = Counter()
        written = store_odds(session, fetch, run=run, observed_at=utcnow(), stats=stats)
        return RunResult(written, fetch.skipped, stats)

    return audited_run(session_factory, provider=provider.name, kind="odds", sport=sport, work=work)
