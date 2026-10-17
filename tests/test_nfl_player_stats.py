from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import Game, GameSourceId, NflPlayerGameStat, Team
from ttk.domain import Sport
from ttk.providers.nflverse import parse_player_weeks
from ttk.services.nfl_player_stats import store_player_weeks


def row(**kw: str) -> dict[str, str]:
    base = {
        "player_id": "00-0036389",
        "player_name": "J.Hurts",
        "player_display_name": "Jalen Hurts",
        "position": "QB",
        "season": "2026",
        "week": "6",
        "season_type": "REG",
        "game_id": "2026_06_DAL_PHI",
        "team": "PHI",
        "completions": "21",
        "attempts": "30",
        "passing_yards": "254",
        "passing_tds": "2",
        "passing_interceptions": "1",
        "carries": "8",
        "rushing_yards": "41",
        "rushing_tds": "1",
        "targets": "0",
        "receptions": "0",
        "receiving_yards": "0",
        "receiving_tds": "0",
    }
    return {**base, **kw}


def test_parse_skips_unknown_teams_and_reads_counts() -> None:
    weeks = parse_player_weeks([row(), row(team="XXX"), row(receiving_yards="NA")])
    assert len(weeks) == 2
    assert weeks[0].player_name == "Jalen Hurts"
    assert weeks[0].counts["passing_yards"] == 254 and weeks[0].counts["rushing_tds"] == 1
    assert weeks[1].counts["receiving_yards"] == 0  # NA -> 0


def seed(sf: sessionmaker[Session]) -> int:
    with sf() as s:
        phi, dal = (
            Team(sport="NFL", name="Eagles", espn_id="21"),
            Team(sport="NFL", name="Cowboys", espn_id="6"),
        )
        s.add_all([phi, dal])
        s.flush()
        g = Game(
            sport=Sport.NFL,
            season=2026,
            home_team_id=phi.id,
            away_team_id=dal.id,
            commence_time=datetime(2026, 10, 18, 17, tzinfo=UTC),
        )
        s.add(g)
        s.flush()
        s.add(GameSourceId(provider="nflverse", source_identifier="2026_06_DAL_PHI", game_id=g.id))
        s.commit()
        return g.id


def test_store_links_games_and_updates_on_reimport(session_factory: sessionmaker[Session]) -> None:
    game_id = seed(session_factory)
    with session_factory() as s:
        stats = store_player_weeks(
            s, parse_player_weeks([row(), row(game_id="2026_07_PHI_NYG", player_id="x")])
        )
        s.commit()
        assert (stats["new"], stats["unknown_game"]) == (1, 1)
        stored = s.scalars(select(NflPlayerGameStat)).one()
        assert (stored.game_id, stored.player_name, stored.passing_yards) == (
            game_id,
            "Jalen Hurts",
            254,
        )
        assert stored.interceptions == 1 and stored.position == "QB"
    with session_factory() as s:  # nflverse revises a stat: the row is updated, not added
        stats = store_player_weeks(s, parse_player_weeks([row(passing_yards="261")]))
        s.commit()
        assert stats["updated"] == 1
        assert s.scalars(select(NflPlayerGameStat)).one().passing_yards == 261
