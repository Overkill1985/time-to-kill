"""Import nflverse play-by-play aggregates (team and QB EPA per game).

Requires games to exist first (``ttk import-nfl-history``): play-by-play game ids
are nflverse game ids and resolve through game_source_ids. One audited run per
season, so an interrupted import keeps the seasons already done.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import GameSourceId, IngestionRun, QbGameStat, Team, TeamGameStat
from ttk.domain import Sport
from ttk.providers.nflverse import TEAMS
from ttk.providers.nflverse_pbp import NflversePbpProvider, SeasonAggregates
from ttk.services.runs import RunResult, audited_run

PROVIDER = "nflverse"


def store_season(session: Session, agg: SeasonAggregates, stats: Counter[str]) -> int:
    game_ids = dict(
        session.execute(
            select(GameSourceId.source_identifier, GameSourceId.game_id).where(
                GameSourceId.provider == PROVIDER,
                GameSourceId.source_identifier.in_({t.game_source_id for t in agg.teams}),
            )
        ).all()
    )
    team_ids = dict(
        session.execute(
            select(Team.espn_id, Team.id).where(Team.sport == Sport.NFL, Team.espn_id.is_not(None))
        ).all()
    )

    def team_id(code: str) -> int | None:
        entry = TEAMS.get(code)
        return team_ids.get(entry[0]) if entry else None

    # Replace this season's rows for the games we can resolve.
    resolved = list(game_ids.values())
    for model in (TeamGameStat, QbGameStat):
        session.execute(
            delete(model).where(model.game_id.in_(resolved), model.provider == PROVIDER)
        )

    written = 0
    for t in agg.teams:
        gid, tid = game_ids.get(t.game_source_id), team_id(t.team_code)
        if gid is None or tid is None:
            stats["unresolved_team_game"] += 1
            continue
        session.add(
            TeamGameStat(
                game_id=gid,
                team_id=tid,
                provider=PROVIDER,
                plays=t.plays,
                epa_total=t.epa_total,
                successes=t.successes,
                dropbacks=t.dropbacks,
                dropback_epa_total=t.dropback_epa_total,
                rushes=t.rushes,
                rush_epa_total=t.rush_epa_total,
            )
        )
        written += 1
    for q in agg.qbs:
        gid, tid = game_ids.get(q.game_source_id), team_id(q.team_code)
        if gid is None or tid is None:
            stats["unresolved_qb_game"] += 1
            continue
        session.add(
            QbGameStat(
                game_id=gid,
                team_id=tid,
                player_id=q.player_id,
                player_name=q.player_name,
                provider=PROVIDER,
                dropbacks=q.dropbacks,
                qb_epa_total=q.qb_epa_total,
            )
        )
    return written


def run_pbp_import(
    session_factory: sessionmaker[Session], provider: NflversePbpProvider, seasons: Iterable[int]
) -> list[IngestionRun]:
    runs = []
    for season in seasons:

        def work(session: Session, run: IngestionRun, season: int = season) -> RunResult:
            agg = provider.fetch_season(season)
            stats: Counter[str] = Counter({"season": season})
            return RunResult(store_season(session, agg, stats), agg.skipped, stats)

        runs.append(
            audited_run(session_factory, provider=PROVIDER, kind="pbp", sport=Sport.NFL, work=work)
        )
    return runs
