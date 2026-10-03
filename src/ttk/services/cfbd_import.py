"""Import CollegeFootballData preseason facts into team_season_features.

Teams resolve by CFBD team id = ESPN id (from ``/teams``, which maps each school
name to its id); a school that matches no ESPN team is counted, never guessed.

Transfers are aggregated per team and season from portal entries dated before
that season's preseason cutoff: counts in and out, and the sum of incoming and
outgoing 247 ratings (players without a rating count in the counts only).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import (
    Game,
    GameSourceId,
    IngestionRun,
    Team,
    TeamGameStat,
    TeamSeasonFeature,
)
from ttk.domain import Sport
from ttk.providers.cfbd import (
    CfbdClient,
    GameTeamPpa,
    TeamFact,
    Transfer,
    parse_advanced,
    parse_coaches,
    parse_portal,
    parse_preseason_poll,
    parse_recruiting,
    parse_returning,
    parse_talent,
    preseason_cutoff,
)
from ttk.services.runs import RunResult, audited_run

PROVIDER = "cfbd"


def portal_facts(transfers: list[Transfer], season: int) -> list[TeamFact]:
    cutoff = preseason_cutoff(season)
    agg: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    last: dict[str, datetime] = {}
    for t in transfers:
        if t.season != season or t.transfer_date is None or t.transfer_date > cutoff:
            continue  # undated or after the season started: not known preseason
        for team, side in ((t.destination, "in"), (t.origin, "out")):
            if not team:
                continue
            agg[team][f"portal_{side}_count"] += 1
            agg[team][f"portal_{side}_rating"] += t.rating or 0.0
            last[team] = max(last.get(team, t.transfer_date), t.transfer_date)
    return [
        TeamFact(team, season, name, float(value), last[team])
        for team, counts in agg.items()
        for name, value in counts.items()
    ]


def season_facts(cfbd: CfbdClient, season: int) -> list[TeamFact]:
    facts = [
        *parse_talent(cfbd.get("/talent", year=season), season),
        *parse_returning(cfbd.get("/player/returning", year=season), season),
        *parse_recruiting(cfbd.get("/recruiting/teams", year=season), season),
        *parse_coaches(cfbd.get("/coaches", year=season), season),
        *parse_preseason_poll(cfbd.get("/rankings", year=season), season),
    ]
    if season >= 2021:  # the transfer portal data starts with the 2021 season
        facts += portal_facts(parse_portal(cfbd.get("/player/portal", year=season)), season)
    return facts


def store_facts(
    session: Session, season: int, facts: list[TeamFact], school_ids: dict[str, int]
) -> Counter[str]:
    teams = {
        espn_id: team_id
        for team_id, espn_id in session.execute(
            select(Team.id, Team.espn_id).where(Team.sport == Sport.CFB, Team.espn_id.is_not(None))
        )
    }
    stats: Counter[str] = Counter()
    session.execute(
        delete(TeamSeasonFeature).where(
            TeamSeasonFeature.sport == Sport.CFB,
            TeamSeasonFeature.season == season,
            TeamSeasonFeature.provider == PROVIDER,
        )
    )
    seen: set[tuple[int, str]] = set()
    for f in facts:
        cfbd_id = school_ids.get(f.team)
        team_id = teams.get(str(cfbd_id)) if cfbd_id is not None else None
        if team_id is None:
            stats["unmatched_school"] += 1
            continue
        if (team_id, f.name) in seen:
            stats["duplicate"] += 1
            continue
        seen.add((team_id, f.name))
        session.add(
            TeamSeasonFeature(
                sport=Sport.CFB,
                season=season,
                team_id=team_id,
                provider=PROVIDER,
                name=f.name,
                value=f.value,
                known_at=f.known_at,
            )
        )
        stats[f.name] += 1
    return stats


def import_cfbd(
    session_factory: sessionmaker[Session], cfbd: CfbdClient, seasons: tuple[int, int]
) -> list[IngestionRun]:
    school_ids = {str(t["school"]): int(t["id"]) for t in cfbd.get("/teams") if t.get("id")}
    runs = []
    for season in range(seasons[0], seasons[1] + 1):

        def work(session: Session, run: IngestionRun, season: int = season) -> RunResult:
            stats = store_facts(session, season, season_facts(cfbd, season), school_ids)
            stats["season"] = season
            written = sum(
                v for k, v in stats.items() if k not in ("unmatched_school", "duplicate", "season")
            )
            return RunResult(written, {}, stats)

        runs.append(
            audited_run(
                session_factory, provider=PROVIDER, kind="preseason", sport=Sport.CFB, work=work
            )
        )
    return runs


def store_game_stats(
    session: Session, sport_season: int, rows: list[GameTeamPpa], school_ids: dict[str, int]
) -> Counter[str]:
    """team_game_stats rows (provider ``cfbd``) for one season; replaces that season's."""
    teams = {
        espn_id: team_id
        for team_id, espn_id in session.execute(
            select(Team.id, Team.espn_id).where(Team.sport == Sport.CFB, Team.espn_id.is_not(None))
        )
    }
    games = {
        event: game_id
        for game_id, event in session.execute(
            select(GameSourceId.game_id, GameSourceId.source_identifier)
            .join(Game, Game.id == GameSourceId.game_id)
            .where(
                GameSourceId.provider == "espn",
                Game.sport == Sport.CFB,
                Game.season == sport_season,
            )
        )
    }
    # one season of game ids (~1,700) is far below SQLite's parameter limit
    session.execute(
        delete(TeamGameStat).where(
            TeamGameStat.provider == PROVIDER, TeamGameStat.game_id.in_(list(games.values()))
        )
    )
    stats: Counter[str] = Counter()
    seen: set[tuple[int, int]] = set()
    for r in rows:
        game_id = games.get(r.event_id)
        cfbd_id = school_ids.get(r.team)
        team_id = teams.get(str(cfbd_id)) if cfbd_id is not None else None
        if game_id is None:
            stats["unmatched_game"] += 1
            continue
        if team_id is None:
            stats["unmatched_school"] += 1
            continue
        if (game_id, team_id) in seen:
            stats["duplicate"] += 1
            continue
        seen.add((game_id, team_id))
        session.add(
            TeamGameStat(
                game_id=game_id,
                team_id=team_id,
                provider=PROVIDER,
                plays=r.plays,
                epa_total=r.total_ppa,
                successes=round((r.success_rate or 0.0) * r.plays),
                dropbacks=r.pass_plays,
                dropback_epa_total=r.pass_total_ppa,
                rushes=r.rush_plays,
                rush_epa_total=r.rush_total_ppa,
            )
        )
        stats["team_games"] += 1
    return stats


def import_cfbd_games(
    session_factory: sessionmaker[Session], cfbd: CfbdClient, seasons: tuple[int, int]
) -> list[IngestionRun]:
    """Per-game team efficiency (one call per season)."""
    school_ids = {str(t["school"]): int(t["id"]) for t in cfbd.get("/teams") if t.get("id")}
    runs = []
    for season in range(seasons[0], seasons[1] + 1):

        def work(session: Session, run: IngestionRun, season: int = season) -> RunResult:
            rows = parse_advanced(cfbd.get("/stats/game/advanced", year=season))
            stats = store_game_stats(session, season, rows, school_ids)
            stats["season"] = season
            return RunResult(stats["team_games"], {}, stats)

        runs.append(
            audited_run(
                session_factory, provider=PROVIDER, kind="game-stats", sport=Sport.CFB, work=work
            )
        )
    return runs
