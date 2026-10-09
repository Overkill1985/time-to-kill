import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import Game, GameSourceId, PropPull, PropQuote, Sportsbook, Team
from ttk.domain import GameStatus, Sport
from ttk.providers.base import ProviderError
from ttk.providers.espn_roster import parse_roster
from ttk.providers.propline import parse_props
from ttk.services.props import collect_props, coverage, due_pulls, is_sportsbook, player_key

FIXTURE = Path(__file__).parent / "fixtures" / "propline_event_props_nfl.json"
KICK = datetime(2026, 10, 11, 17, 0, tzinfo=UTC)
ROSTERS = {
    "18": ["Alvin Kamara", "Tyler Shough", "Barion Brown"],  # Saints
    "16": ["Aaron Jones", "Kyler Murray"],  # Vikings
}


def test_player_keys_and_book_filter() -> None:
    assert player_key("Aaron Jones Sr.") == player_key("Aaron Jones") == "aaron jones"
    assert player_key("Ja'Marr Chase") == "jamarr chase"
    assert player_key("Dak Prescott (DAL)") == player_key("Dak Prescott") == "dak prescott"
    assert player_key("Chris Godwin Jr. (TB)") == "chris godwin"
    quotes = parse_props(json.loads(FIXTURE.read_text("utf-8")))
    assert len(quotes) == 24
    kept = {q.book for q in quotes if is_sportsbook(q)}
    assert kept == {"fanatics", "rebet"}  # PrizePicks (flat payout) and Kalshi dropped


def test_roster_parse_flat_and_grouped() -> None:
    assert parse_roster({"athletes": [{"displayName": "A"}, {"displayName": "B"}]}) == ["A", "B"]
    grouped = {"athletes": [{"position": "offense", "items": [{"displayName": "C"}]}]}
    assert parse_roster(grouped) == ["C"]


class FakeProps:
    name = "propline"

    def __init__(self, remaining: int | None = 800) -> None:
        self.daily_remaining = remaining
        self.calls = 0

    def event_markets(self, sport: Sport, event_id: str) -> list[str]:
        self.calls += 1
        return ["h2h", "player_1st_td", "player_anytime_td", "player_pass_yds"]

    def event_props(self, sport: Sport, event_id: str, markets: list[str]):  # type: ignore[no-untyped-def]
        self.calls += 1
        assert "h2h" not in markets
        return parse_props(json.loads(FIXTURE.read_text("utf-8")))


def seed(sf: sessionmaker[Session], **game: object) -> int:
    with sf() as s:
        saints = Team(sport="NFL", name="New Orleans Saints", espn_id="18")
        vikings = Team(sport="NFL", name="Minnesota Vikings", espn_id="16")
        s.add_all([saints, vikings])
        s.flush()
        g = Game(
            sport="NFL",
            home_team_id=saints.id,
            away_team_id=vikings.id,
            commence_time=KICK,
            season_type="REG",
            **game,
        )
        s.add(g)
        s.flush()
        s.add(GameSourceId(provider="propline", source_identifier="32681", game_id=g.id))
        s.commit()
        return g.id


def test_pull_keeps_sportsbook_props_on_the_rosters(
    session_factory: sessionmaker[Session],
) -> None:
    gid = seed(session_factory)
    provider = FakeProps()
    assert (
        collect_props(
            session_factory, provider, lambda s, t: ROSTERS[t], now=KICK - timedelta(hours=30)
        )
        == []
    )  # not yet due
    lines = collect_props(
        session_factory, provider, lambda s, t: ROSTERS[t], now=KICK - timedelta(hours=20)
    )
    assert provider.calls == 2 and "12 quotes from 2 books" in lines[0]
    with session_factory() as s:
        pull = s.scalars(select(PropPull)).one()
        assert (pull.game_id, pull.horizon_hours, pull.event_id) == (gid, 24, "32681")
        assert pull.stats is not None
        assert pull.stats["dropped_not_a_sportsbook"] == 12 and pull.stats["player_markets"] == 3
        sides = {q.player: q.team_side for q in s.scalars(select(PropQuote))}
        assert sides == {"Aaron Jones": "AWAY", "Alvin Kamara": "HOME", "Barion Brown": "HOME"}
        assert {b.key for b in s.scalars(select(Sportsbook))} == {"fanatics", "rebet"}
        assert due_pulls(s, KICK - timedelta(hours=19)) == []  # one pull per horizon
        assert [d.horizon for d in due_pulls(s, KICK - timedelta(minutes=50))] == [1]
        (c,) = coverage(s)
        assert (c.sport, c.horizon, c.pulls, c.quotes) == ("NFL", 24, 1, 12)
        assert c.books["fanatics"] == (1, 6)
        with pytest.raises(DatabaseError, match="append-only"):
            s.execute(text("DELETE FROM prop_quotes"))


def test_off_roster_players_are_dropped(session_factory: sessionmaker[Session]) -> None:
    seed(session_factory)
    rosters = {"18": ["Alvin Kamara"], "16": []}
    collect_props(
        session_factory, FakeProps(), lambda s, t: rosters[t], now=KICK - timedelta(hours=20)
    )
    with session_factory() as s:
        pull = s.scalars(select(PropPull)).one()
    assert pull.quotes == 4 and pull.stats is not None
    assert pull.stats["dropped_not_on_one_roster"] == 8
    # Aaron Jones has 4 quotes in the fixture, Barion Brown 4 (Fanatics and Rebet).
    assert pull.stats["off_roster_names"] == {"Aaron Jones": 4, "Barion Brown": 4}
    with session_factory() as s:
        (c,) = coverage(s)
    assert c.off_roster_names == {"Aaron Jones": 4, "Barion Brown": 4}


def test_quota_floor_duplicates_and_failures(session_factory: sessionmaker[Session]) -> None:
    gid = seed(session_factory)
    now = KICK - timedelta(hours=20)
    low = FakeProps(remaining=50)
    assert "paused" in collect_props(session_factory, low, lambda s, t: ROSTERS[t], now=now)[0]
    assert low.calls == 0

    def broken(sport: Sport, team: str) -> list[str]:
        raise ProviderError("espn roster: HTTP 503")

    lines = collect_props(session_factory, FakeProps(), broken, now=now)
    assert "failed" in lines[0]
    with session_factory() as s:
        assert s.scalars(select(PropPull)).all() == []  # nothing stored: retried next pass
        s.get_one(Game, gid).status = GameStatus.DUPLICATE
        s.commit()
        assert due_pulls(s, now) == []
