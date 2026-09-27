"""Import player box scores for final games (ESPN summaries -> player_game_stats).

Resumable: games that already have rows are skipped; progress is committed every
``batch`` games. Paced by ``delay`` seconds between requests (unofficial API).
A game whose teams can't be matched to ESPN team ids is counted, never guessed.
"""

from __future__ import annotations

import time
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import Game, GameSourceId, IngestionRun, PlayerGameStat, Team
from ttk.domain import GameStatus, Sport
from ttk.providers.espn_boxscore import EspnBoxscores
from ttk.services.runs import RunResult, audited_run

PROVIDER = "espn"


def import_boxscores(
    session_factory: sessionmaker[Session],
    sport: Sport,
    seasons: tuple[int, int],
    *,
    boxscores: EspnBoxscores,
    delay: float = 0.25,
    batch: int = 50,
) -> IngestionRun:
    def work(session: Session, run: IngestionRun) -> RunResult:
        team_ids = {
            espn_id: team_id
            for team_id, espn_id in session.execute(
                select(Team.id, Team.espn_id).where(Team.sport == sport, Team.espn_id.is_not(None))
            )
        }
        done = select(PlayerGameStat.game_id).distinct()
        pending = session.execute(
            select(Game.id, GameSourceId.source_identifier, Game.home_team_id, Game.away_team_id)
            .join(
                GameSourceId, (GameSourceId.game_id == Game.id) & (GameSourceId.provider == "espn")
            )
            .where(
                Game.sport == sport,
                Game.status == GameStatus.FINAL,
                Game.season.between(*seasons),
                Game.id.not_in(done),
            )
            .order_by(Game.commence_time)
        ).all()
        stats: Counter[str] = Counter({"games_pending": len(pending)})
        written = 0
        for i, (game_id, event_id, home_id, away_id) in enumerate(pending, start=1):
            lines = boxscores.fetch(sport, event_id)
            teams = {team_ids.get(line.team_espn_id) for line in lines}
            if not lines:
                stats["games_without_boxscore"] += 1
            elif teams != {home_id, away_id}:
                stats["games_team_mismatch"] += 1
            else:
                seen: set[str] = set()
                for line in lines:
                    if line.player_id in seen:
                        stats["duplicate_player"] += 1
                        continue
                    seen.add(line.player_id)
                    session.add(
                        PlayerGameStat(
                            game_id=game_id,
                            team_id=team_ids[line.team_espn_id],
                            player_id=line.player_id,
                            player_name=line.player_name,
                            provider=PROVIDER,
                            starter=line.starter,
                            played=line.played,
                            dnp_reason=line.dnp_reason,
                            minutes=line.minutes,
                            points=line.points,
                            fgm=line.fgm,
                            fga=line.fga,
                            ftm=line.ftm,
                            fta=line.fta,
                            oreb=line.oreb,
                            dreb=line.dreb,
                            ast=line.ast,
                            stl=line.stl,
                            blk=line.blk,
                            tov=line.tov,
                            pf=line.pf,
                            plus_minus=line.plus_minus,
                        )
                    )
                written += len(seen)
                stats["games_imported"] += 1
            if i % batch == 0:
                session.commit()  # resumable: finished games are skipped next time
            time.sleep(delay)
        return RunResult(written, {}, stats)

    return audited_run(
        session_factory, provider="espn-summary", kind="boxscores", sport=sport, work=work
    )
