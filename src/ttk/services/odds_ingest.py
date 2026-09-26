"""Persist normalized odds as immutable snapshots, with an ingestion-run audit row."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ttk import betting_math as bm
from ttk.db.models import (
    Game,
    GameSourceId,
    IngestionRun,
    OddsSnapshot,
    Sportsbook,
    Team,
    TeamAlias,
    utcnow,
)
from ttk.domain import Sport
from ttk.providers.base import NormalizedGame, OddsFetch, OddsProvider, ProviderError


def resolve_team(session: Session, *, provider: str, sport: Sport, name: str) -> Team:
    alias = session.scalar(
        select(TeamAlias).where(
            TeamAlias.provider == provider, TeamAlias.sport == sport, TeamAlias.alias == name
        )
    )
    if alias is not None:
        return session.get_one(Team, alias.team_id)
    team = session.scalar(select(Team).where(Team.sport == sport, Team.name == name))
    if team is None:
        team = Team(sport=sport, name=name)
        session.add(team)
        session.flush()
    session.add(TeamAlias(provider=provider, sport=sport, alias=name, team_id=team.id))
    return team


def resolve_game(session: Session, g: NormalizedGame) -> Game:
    link = session.scalar(
        select(GameSourceId).where(
            GameSourceId.provider == g.provider,
            GameSourceId.source_identifier == g.source_identifier,
        )
    )
    if link is not None:
        game = session.get_one(Game, link.game_id)
        if game.commence_time != g.commence_time:
            game.commence_time = g.commence_time  # schedule changes are reference data
        return game
    home = resolve_team(session, provider=g.provider, sport=g.sport, name=g.home_team)
    away = resolve_team(session, provider=g.provider, sport=g.sport, name=g.away_team)
    game = Game(
        sport=g.sport,
        home_team_id=home.id,
        away_team_id=away.id,
        commence_time=g.commence_time,
    )
    session.add(game)
    session.flush()
    session.add(
        GameSourceId(provider=g.provider, source_identifier=g.source_identifier, game_id=game.id)
    )
    return game


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
    session: Session, fetch: OddsFetch, *, run: IngestionRun, observed_at: datetime
) -> int:
    games = {g.source_identifier: resolve_game(session, g) for g in fetch.games}
    books: dict[str, Sportsbook] = {}
    written = 0
    for q in fetch.quotes:
        game = games.get(q.game_source_identifier)
        if game is None:
            continue
        decimal_odds = bm.american_to_decimal(q.american_odds)
        session.add(
            OddsSnapshot(
                game_id=game.id,
                sportsbook_id=_sportsbook(session, books, q.sportsbook_key, q.sportsbook_name).id,
                market=q.market,
                selection=q.selection,
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
    with session_factory() as session:
        run = IngestionRun(provider=provider.name, kind="odds", sport=sport)
        session.add(run)
        session.commit()

        try:
            fetch = provider.fetch_odds(sport)
            run.records_written = store_odds(session, fetch, run=run, observed_at=utcnow())
            run.skipped = fetch.skipped
            run.status = "SUCCESS"
        except ProviderError as exc:
            session.rollback()
            run.status = "FAILED"
            run.error = str(exc)
        run.finished_at = utcnow()
        session.add(run)
        session.commit()
        return run
