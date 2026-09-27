"""One-off data repairs, each reproducible and dry-run first.

``split_merged_espn_games``: before the identity fix of 2026-09-27, an ESPN event
could be matched by teams and time to another ESPN event's game (NBA teams that
meet twice within 24 hours, home and away reversed). The game row then carried the
later event's time and score, and ESPN line rows written for it are unreliable.
The repair unlinks every ESPN event but the first (the unswapped link, which
created the game), drops that game's ESPN-derived rows (lines, box scores), and
returns the dates to re-ingest: re-running the schedule restores the kept game
from its own event and creates a new game for each unlinked event.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ttk.db.models import Game, GameSourceId, PlayerGameStat, ReportedLine
from ttk.services.identity import SCHEDULE_AUTHORITY


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
