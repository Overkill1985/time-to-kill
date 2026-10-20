"""One-off data repairs, each reproducible and dry-run first.

``split_merged_espn_games``: before the identity fix of 2026-09-27, an ESPN event
could be matched by teams and time to another ESPN event's game (NBA teams that
meet twice within 24 hours, home and away reversed). The game row then carried the
later event's time and score, and ESPN line rows written for it are unreliable.
The repair unlinks every ESPN event but the first (the unswapped link, which
created the game), drops that game's ESPN-derived rows (lines, box scores), and
returns the dates to re-ingest: re-running the schedule restores the kept game
from its own event and creates a new game for each unlinked event.

``set_aside_phantom_games``: odds events pairing teams that were not playing each
other (2026-10-17 on, from two offshore books: eleven NBA teams "@ San Antonio
Spurs" at the Spurs' real tip-off) each created a game. A game no schedule
authority knows, one of whose teams has an authority game within BUSY_WINDOW, is
set aside as DUPLICATE: hidden and never modeled, its odds rows kept.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ttk.db.models import Game, GameSourceId, PlayerGameStat, ReportedLine
from ttk.domain import GameStatus
from ttk.services.identity import AUTHORITY_RANK, BUSY_WINDOW, SCHEDULE_AUTHORITY


@dataclass(frozen=True)
class MergedGame:
    game_id: int
    sport: str
    season: int | None
    kept_event: str
    removed_events: tuple[str, ...]
    window: tuple[date, date]
    """ESPN dates to re-ingest (covers both events; ESPN days are US local dates)."""
    reported_lines: int
    player_rows: int


def split_merged_espn_games(session: Session, *, apply: bool = False) -> list[MergedGame]:
    merged_ids = session.scalars(
        select(GameSourceId.game_id)
        .where(GameSourceId.provider == SCHEDULE_AUTHORITY)
        .group_by(GameSourceId.game_id)
        .having(func.count() > 1)
    ).all()
    out = []
    for game_id in merged_ids:
        game = session.get_one(Game, game_id)
        links = session.scalars(
            select(GameSourceId)
            .where(GameSourceId.game_id == game_id, GameSourceId.provider == SCHEDULE_AUTHORITY)
            .order_by(GameSourceId.swapped, GameSourceId.id)
        ).all()
        kept, removed = links[0], links[1:]
        espn_lines = (ReportedLine.game_id == game_id) & (
            ReportedLine.provider.like("espn:%") | ReportedLine.provider.like("espn-open:%")
        )
        n_lines = session.scalar(select(func.count()).select_from(ReportedLine).where(espn_lines))
        n_players = session.scalar(
            select(func.count())
            .select_from(PlayerGameStat)
            .where(PlayerGameStat.game_id == game_id)
        )
        day = game.commence_time.date()
        out.append(
            MergedGame(
                game_id,
                game.sport,
                game.season,
                kept.source_identifier,
                tuple(link.source_identifier for link in removed),
                (date.fromordinal(day.toordinal() - 2), date.fromordinal(day.toordinal() + 1)),
                n_lines or 0,
                n_players or 0,
            )
        )
        if apply:
            for link in removed:
                session.delete(link)
            session.execute(delete(ReportedLine).where(espn_lines))
            session.execute(delete(PlayerGameStat).where(PlayerGameStat.game_id == game_id))
    if apply:
        session.flush()
    return out


@dataclass(frozen=True)
class PhantomGame:
    game_id: int
    sport: str
    commence_time: object
    home_team_id: int
    away_team_id: int


def set_aside_phantom_games(session: Session, *, apply: bool = False) -> list[PhantomGame]:
    authorities = tuple(AUTHORITY_RANK)
    known = select(GameSourceId.game_id).where(GameSourceId.provider.in_(authorities))
    out = []
    for game in session.scalars(
        select(Game)
        .where(Game.status != GameStatus.DUPLICATE, Game.id.not_in(known))
        .order_by(Game.id)
    ):
        teams = (game.home_team_id, game.away_team_id)
        clash = session.scalar(
            select(Game.id)
            .where(
                Game.id != game.id,
                Game.sport == game.sport,
                Game.id.in_(known),
                Game.status != GameStatus.DUPLICATE,
                (Game.home_team_id.in_(teams)) | (Game.away_team_id.in_(teams)),
                Game.commence_time.between(
                    game.commence_time - BUSY_WINDOW, game.commence_time + BUSY_WINDOW
                ),
            )
            .limit(1)
        )
        if clash is None:
            continue
        out.append(
            PhantomGame(
                game.id, game.sport, game.commence_time, game.home_team_id, game.away_team_id
            )
        )
        if apply:
            game.status = GameStatus.DUPLICATE
    if apply:
        session.flush()
    return out


@dataclass(frozen=True)
class MalformedLines:
    sport: str
    season: int | None
    games: int
    rows: int


def drop_malformed_espn_lines(session: Session, *, apply: bool = False) -> list[MalformedLines]:
    """Games with an ESPN line holding a price where the line belongs (see
    providers/espn_odds._valid): a spread of 100+ points, or a total that is not
    positive or equals its own over/under price. All of those games' ESPN lines
    are dropped so the resumable importer re-fetches them with the fixed parser."""
    bad = (ReportedLine.provider.like("espn:%") | ReportedLine.provider.like("espn-open:%")) & (
        (func.abs(ReportedLine.home_spread) >= 100)
        | (ReportedLine.total <= 0)
        | (ReportedLine.total == ReportedLine.over_odds)
        | (ReportedLine.total == ReportedLine.under_odds)
    )
    game_ids = set(session.scalars(select(ReportedLine.game_id).where(bad)).all())
    if not game_ids:
        return []
    espn_rows = (ReportedLine.game_id.in_(game_ids)) & (
        ReportedLine.provider.like("espn:%") | ReportedLine.provider.like("espn-open:%")
    )
    summary = session.execute(
        select(Game.sport, Game.season, func.count(func.distinct(Game.id)), func.count())
        .join(ReportedLine, ReportedLine.game_id == Game.id)
        .where(espn_rows)
        .group_by(Game.sport, Game.season)
        .order_by(Game.sport, Game.season)
    ).all()
    if apply:
        session.execute(delete(ReportedLine).where(espn_rows))
        session.flush()
    return [MalformedLines(sport, season, games, rows) for sport, season, games, rows in summary]
