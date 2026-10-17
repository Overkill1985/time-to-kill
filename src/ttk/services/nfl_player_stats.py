"""NFL player game stats from nflverse (``stats_player_week``), for the Players
page's prop hit rates. One row per (game, player); a re-import overwrites a row's
values, since nflverse revises stats after games. Games link through their
nflverse game id; teams through nflverse.TEAMS (franchise ESPN ids)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import GameSourceId, IngestionRun, NflPlayerGameStat, Team
from ttk.domain import Sport
from ttk.providers.nflverse import TEAMS, PlayerWeek
from ttk.services.runs import RunResult, audited_run

FIELDS = {
    "completions": "completions",
    "attempts": "attempts",
    "passing_yards": "passing_yards",
    "passing_tds": "passing_tds",
    "passing_interceptions": "interceptions",
    "carries": "carries",
    "rushing_yards": "rushing_yards",
    "rushing_tds": "rushing_tds",
    "targets": "targets",
    "receptions": "receptions",
    "receiving_yards": "receiving_yards",
    "receiving_tds": "receiving_tds",
}


def store_player_weeks(session: Session, weeks: Sequence[PlayerWeek]) -> Counter[str]:
    stats: Counter[str] = Counter()
    games = dict(
        session.execute(
            select(GameSourceId.source_identifier, GameSourceId.game_id).where(
                GameSourceId.provider == "nflverse"
            )
        ).all()
    )
    teams = dict(
        session.execute(
            select(Team.espn_id, Team.id).where(Team.sport == Sport.NFL, Team.espn_id.is_not(None))
        ).all()
    )
    existing = {
        (r.game_id, r.player_id): r
        for r in session.scalars(
            select(NflPlayerGameStat).where(
                NflPlayerGameStat.game_id.in_(
                    {games[w.game_id] for w in weeks if w.game_id in games}
                )
            )
        )
    }
    for w in weeks:
        game_id = games.get(w.game_id)
        team_id = teams.get(TEAMS[w.team][0])
        if game_id is None:
            stats["unknown_game"] += 1
            continue
        if team_id is None:
            stats["unknown_team"] += 1
            continue
        row = existing.get((game_id, w.player_id))
        if row is None:
            row = NflPlayerGameStat(game_id=game_id, player_id=w.player_id)
            session.add(row)
            existing[(game_id, w.player_id)] = row
            stats["new"] += 1
        else:
            stats["updated"] += 1
        row.team_id = team_id
        row.player_name = w.player_name[:100]
        row.position = w.position[:10] or None
        for source, field in FIELDS.items():
            setattr(row, field, w.counts.get(source, 0))
    return stats


def import_nfl_player_stats(
    session_factory: sessionmaker[Session],
    fetch: Callable[[int], list[PlayerWeek]],
    seasons: tuple[int, int],
) -> list[IngestionRun]:
    runs = []
    for season in range(seasons[0], seasons[1] + 1):

        def work(session: Session, run: IngestionRun, season: int = season) -> RunResult:
            stats = store_player_weeks(session, fetch(season))
            written = stats["new"] + stats["updated"]
            stats["season"] = season
            return RunResult(written, {}, stats)

        runs.append(
            audited_run(
                session_factory,
                provider="nflverse",
                kind="player-stats",
                sport=Sport.NFL,
                work=work,
            )
        )
    return runs
