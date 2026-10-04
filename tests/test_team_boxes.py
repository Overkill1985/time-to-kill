import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import TeamGameBox
from ttk.domain import GameStatus, Sport
from ttk.models.elo import EloGame
from ttk.providers.base import NormalizedGame, TeamRef
from ttk.providers.espn_boxscore import EspnBoxscores, parse_team_totals
from ttk.research.espn_models import _possession_efficiency
from ttk.research.ncaab_model import NCAAB
from ttk.services.boxscore_import import import_team_boxes
from ttk.services.identity import resolve_game

# Real ESPN summary (team totals only): Kentucky 74, Duke 63, 2015-11-17 (event 400809203).
BOX = json.loads(
    (Path(__file__).parent / "fixtures" / "espn_ncaab_team_box.json").read_text("utf-8")
)


def test_team_totals_points_and_possessions() -> None:
    teams = {b.team_espn_id: b for b in parse_team_totals(BOX)}
    uk, duke = teams["96"], teams["150"]
    assert (uk.fgm, uk.fga, uk.fg3m, uk.ftm, uk.fta, uk.oreb, uk.tov) == (30, 67, 3, 11, 18, 17, 9)
    assert (uk.points, duke.points) == (74, 63)  # the final score
    assert uk.possessions == pytest.approx(67 - 17 + 9 + 0.475 * 18)
    broken = {"boxscore": {"teams": [BOX["boxscore"]["teams"][0]]}}
    assert parse_team_totals(broken) == []  # both teams or nothing


def seed(sf: sessionmaker[Session]) -> int:
    g = NormalizedGame(
        "espn",
        "400809203",
        Sport.NCAAB,
        TeamRef("Duke Blue Devils", espn_id="150"),
        TeamRef("Kentucky Wildcats", espn_id="96"),
        datetime(2015, 11, 18, 0, 30, tzinfo=UTC),
        None,
        espn_event_id="400809203",
        status=GameStatus.FINAL,
        home_score=63,
        away_score=74,
        season=2016,
        season_type="REG",
    )
    with sf() as session:
        game_id = resolve_game(session, g, Counter()).game.id
        session.commit()
        return game_id


def test_import_team_boxes_is_resumable(session_factory: sessionmaker[Session]) -> None:
    game_id = seed(session_factory)
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=BOX)

    source = EspnBoxscores(client=httpx.Client(transport=httpx.MockTransport(handle)))
    run = import_team_boxes(session_factory, Sport.NCAAB, (2016, 2016), boxscores=source, delay=0)
    assert run.status == "SUCCESS" and run.records_written == 2
    assert "mens-college-basketball" in calls[0]
    with session_factory() as session:
        rows = session.scalars(select(TeamGameBox)).all()
        assert {r.game_id for r in rows} == {game_id}
    again = import_team_boxes(session_factory, Sport.NCAAB, (2016, 2016), boxscores=source, delay=0)
    assert again.records_written == 0 and len(calls) == 1


def test_possession_efficiency_feeds_later_games(session_factory: sessionmaker[Session]) -> None:
    game_id = seed(session_factory)
    source = EspnBoxscores(
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=BOX)))
    )
    import_team_boxes(session_factory, Sport.NCAAB, (2016, 2016), boxscores=source, delay=0)
    with session_factory() as session:
        duke, uk = (
            session.scalar(select(TeamGameBox.team_id).where(TeamGameBox.fgm == fgm))
            for fgm in (22, 30)
        )
        assert duke is not None and uk is not None
        t0 = datetime(2015, 11, 18, 0, 30, tzinfo=UTC)
        games = [
            EloGame(game_id, 2016, t0, duke, uk, True, 63, 74),
            EloGame(999, 2016, t0 + timedelta(days=7), uk, duke, False, None, None),
        ]
        feats = _possession_efficiency(session, NCAAB, games)
    assert feats[game_id]["eff_raw_diff"] == 0.0  # nothing known before the first game
    # A week later Kentucky (home) has the better per-possession history.
    assert feats[999]["eff_raw_diff"] > 0 and feats[999]["eff_diff"] > 0


def test_collector_imports_team_boxes_on_its_cadence(
    session_factory: sessionmaker[Session],
) -> None:
    from sqlalchemy import func

    from ttk.db.models import IngestionRun
    from ttk.services.collector import refresh_team_boxes

    source = EspnBoxscores(
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    )
    now = datetime(2026, 11, 10, tzinfo=UTC)
    first = refresh_team_boxes(session_factory, source, now=now)
    assert [(r.sport, r.status) for r in first] == [("NCAAB", "SUCCESS")]
    with session_factory() as session:
        last = session.scalar(select(func.max(IngestionRun.finished_at)))
    assert last is not None
    assert refresh_team_boxes(session_factory, source, now=last + timedelta(hours=1)) == []
    assert len(refresh_team_boxes(session_factory, source, now=last + timedelta(hours=7))) == 1
