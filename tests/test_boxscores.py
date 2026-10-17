import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import PlayerGameStat
from ttk.domain import GameStatus, Sport
from ttk.providers.base import NormalizedGame, TeamRef
from ttk.providers.espn_boxscore import EspnBoxscores, parse_boxscore
from ttk.services.boxscore_import import backfill_threes, import_boxscores
from ttk.services.identity import resolve_game

# Real ESPN summary (trimmed), Cavaliers at Pacers, 2024-02-10: IND 121, CLE 116.
BOX = json.loads((Path(__file__).parent / "fixtures" / "espn_nba_boxscore.json").read_text("utf-8"))


def test_parse_boxscore() -> None:
    lines = parse_boxscore(BOX)
    by_team = Counter(line.team_espn_id for line in lines)
    assert by_team == {"5": 13, "11": 15}
    for team in ("5", "11"):
        team_lines = [x for x in lines if x.team_espn_id == team]
        assert sum(x.starter for x in team_lines) == 5
        assert sum(x.points or 0 for x in team_lines) == {"5": 116, "11": 121}[team]
        assert all(x.played == bool(x.minutes) for x in team_lines)
    allen = next(x for x in lines if x.player_name == "Jarrett Allen")
    assert (allen.starter, allen.minutes, allen.points, allen.fgm, allen.fga) == (
        True,
        21.0,
        10,
        4,
        6,
    )
    assert (allen.ftm, allen.fta, allen.plus_minus) == (2, 3, -1)
    assert (allen.fg3m, allen.fg3a) == (0, 0)
    # 3-pointers are read right if every player's points add up from his makes.
    played = [x for x in lines if x.played]
    assert any(x.fg3m for x in played)
    for x in played:
        assert x.fgm is not None and x.fg3m is not None and x.ftm is not None
        assert x.points == 2 * x.fgm + x.fg3m + x.ftm, x.player_name
    scratch = next(x for x in lines if x.player_name == "Sam Merrill")
    assert not scratch.played and scratch.dnp_reason == "COACH'S DECISION"
    assert scratch.minutes is None and scratch.points is None


def seed(sf: sessionmaker[Session]) -> int:
    game = NormalizedGame(
        "espn",
        "401584089",
        Sport.NBA,
        TeamRef("Indiana Pacers", espn_id="11"),
        TeamRef("Cleveland Cavaliers", espn_id="5"),
        datetime(2024, 2, 10, 0, 0, tzinfo=UTC),
        None,
        espn_event_id="401584089",
        status=GameStatus.FINAL,
        home_score=121,
        away_score=116,
        season=2024,
        season_type="REG",
    )
    with sf() as session:
        resolved = resolve_game(session, game, Counter())
        session.commit()
        return resolved.game.id


def test_import_boxscores_is_resumable(session_factory: sessionmaker[Session]) -> None:
    game_id = seed(session_factory)
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=BOX)

    box = EspnBoxscores(client=httpx.Client(transport=httpx.MockTransport(handle)))
    run = import_boxscores(session_factory, Sport.NBA, (2024, 2024), boxscores=box, delay=0)
    assert run.status == "SUCCESS" and run.records_written == 28
    assert "event=401584089" in calls[0]
    with session_factory() as session:
        rows = session.scalars(select(PlayerGameStat)).all()
        assert {r.game_id for r in rows} == {game_id} and len(rows) == 28
    again = import_boxscores(session_factory, Sport.NBA, (2024, 2024), boxscores=box, delay=0)
    assert again.records_written == 0 and len(calls) == 1  # finished games are skipped


def test_backfill_threes_fills_rows_imported_without_them(
    session_factory: sessionmaker[Session],
) -> None:
    seed(session_factory)
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=BOX)

    box = EspnBoxscores(client=httpx.Client(transport=httpx.MockTransport(handle)))
    import_boxscores(session_factory, Sport.NBA, (2024, 2024), boxscores=box, delay=0)
    with session_factory() as session:  # as rows imported before 3-pointers were kept
        for row in session.scalars(select(PlayerGameStat)):
            row.fg3m = row.fg3a = None
        session.commit()
    run = backfill_threes(session_factory, Sport.NBA, (2024, 2024), boxscores=box, delay=0)
    # 19 who played; the 9 scratches have no shooting line to fill.
    assert run.status == "SUCCESS" and run.records_written == 19
    with session_factory() as session:
        played = session.scalars(select(PlayerGameStat).where(PlayerGameStat.played)).all()
        assert all(r.fg3m is not None for r in played)
        assert all(r.points == 2 * (r.fgm or 0) + (r.fg3m or 0) + (r.ftm or 0) for r in played)
    again = backfill_threes(session_factory, Sport.NBA, (2024, 2024), boxscores=box, delay=0)
    assert again.records_written == 0 and len(calls) == 2  # nothing left to fill
