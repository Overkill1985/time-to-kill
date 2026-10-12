from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import Game, GameSourceId, GameWeather, ReportedLine, Team
from ttk.domain import Sport
from ttk.providers.base import ProviderError
from ttk.providers.cfbd import parse_game_venues, parse_venues
from ttk.services.weather_history import import_cfb_weather

VENUES = [
    {"id": 1, "name": "Beaver Stadium", "dome": False, "latitude": 40.81, "longitude": -77.86},
    {"id": 2, "name": "Caesars Superdome", "dome": True, "latitude": 29.95, "longitude": -90.08},
    {"id": 3, "name": "Nowhere Field", "dome": False, "latitude": None, "longitude": None},
]


class FakeCfbd:
    calls_remaining: int | None = 800

    def __init__(self, games: list[dict[str, Any]]) -> None:
        self.games = games
        self.paths: list[tuple[str, dict[str, int | str]]] = []

    def get(self, path: str, **params: int | str) -> list[dict[str, Any]]:
        self.paths.append((path, params))
        return VENUES if path == "/venues" else self.games


class FakeArchive:
    name = "fake-archive"

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[list[tuple[float, float]], date]] = []
        self.fail = fail

    def day(self, points: list[tuple[float, float]], day: date) -> list[dict[str, Any]]:
        if self.fail:
            raise ProviderError("open-meteo archive: HTTP 429")
        self.calls.append((points, day))
        hours = [f"{day.isoformat()}T{h:02d}:00" for h in range(24)]
        return [
            {
                "hourly": {
                    "time": hours,
                    "wind_speed_10m": [float(h) for h in range(24)],
                    "wind_gusts_10m": [None] * 24,
                    "temperature_2m": [50.0] * 24,
                    "precipitation": [0.0] * 24,
                }
            }
            for _ in points
        ]


def setup(sf: sessionmaker[Session]) -> list[int]:
    """Four finished, lined games: outdoor (twice), dome, a venue without coordinates;
    plus one with no reported total (never imported)."""
    with sf() as s:
        a, b = Team(sport="CFB", name="A"), Team(sport="CFB", name="B")
        s.add_all([a, b])
        s.flush()
        ids = []
        for i, kick in enumerate(
            [
                datetime(2023, 10, 21, 19, 40, tzinfo=UTC),  # rounds to 20:00
                datetime(2023, 10, 21, 16, 0, tzinfo=UTC),
                datetime(2023, 10, 21, 17, 0, tzinfo=UTC),
                datetime(2023, 10, 28, 17, 0, tzinfo=UTC),
                datetime(2023, 10, 28, 18, 0, tzinfo=UTC),
            ]
        ):
            g = Game(
                sport=Sport.CFB,
                season=2023,
                home_team_id=a.id,
                away_team_id=b.id,
                commence_time=kick,
                home_score=21,
                away_score=17,
            )
            s.add(g)
            s.flush()
            s.add(GameSourceId(provider="espn", source_identifier=f"40{i}", game_id=g.id))
            if i < 4:
                s.add(ReportedLine(game_id=g.id, provider="espn:test", total=50.5))
            ids.append(g.id)
        s.commit()
        return ids


GAMES = [
    {"id": 400, "venueId": 1},
    {"id": 401, "venueId": 1},
    {"id": 402, "venueId": 2},
    {"id": 403, "venueId": 3},
    {"id": 404, "venueId": 1},
]


def test_parsers() -> None:
    venues = parse_venues(VENUES)
    assert venues[2].dome and venues[3].latitude is None
    assert parse_game_venues([{"id": 1, "venueId": 5}, {"id": 2}]) == {"1": 5}


def test_import_is_resumable_and_stores_the_kickoff_hour(
    session_factory: sessionmaker[Session],
) -> None:
    ids = setup(session_factory)
    cfbd, archive = FakeCfbd(GAMES), FakeArchive()
    report = import_cfb_weather(
        session_factory,
        cfbd,
        archive,
        first=2023,
        last=2023,
        budget=100,
        sleep=lambda _: None,
    )
    assert (report.requested, report.stored, report.indoor, report.remaining) == (2, 2, 1, 0)
    assert report.skipped == {"venue_without_coordinates": 1}
    assert [p for p, _ in cfbd.paths] == ["/venues", "/games"]
    assert archive.calls == [([(40.81, -77.86), (40.81, -77.86)], date(2023, 10, 21))]
    with session_factory() as s:
        rows = {r.game_id: r for r in s.scalars(select(GameWeather))}
        assert set(rows) == {ids[0], ids[1], ids[2]}
        assert rows[ids[0]].wind_mph == 20.0  # 19:40 kickoff -> the 20:00 hour
        assert rows[ids[1]].wind_mph == 16.0
        assert rows[ids[2]].indoor and rows[ids[2]].wind_mph is None

    again = import_cfb_weather(
        session_factory,
        cfbd,
        FakeArchive(),
        first=2023,
        last=2023,
        budget=100,
        sleep=lambda _: None,
    )
    assert (again.requested, again.stored, again.indoor) == (0, 0, 0)


def test_budget_and_provider_errors_stop_the_run(session_factory: sessionmaker[Session]) -> None:
    setup(session_factory)
    report = import_cfb_weather(
        session_factory,
        FakeCfbd(GAMES),
        FakeArchive(),
        first=2023,
        last=2023,
        budget=1,
        sleep=lambda _: None,
    )
    assert report.requested == 0 and report.remaining == 2
    assert report.stopped == "budget of 1 places reached"
    failed = import_cfb_weather(
        session_factory,
        FakeCfbd(GAMES),
        FakeArchive(fail=True),
        first=2023,
        last=2023,
        budget=100,
        sleep=lambda _: None,
    )
    assert failed.stopped == "open-meteo archive: HTTP 429" and failed.remaining == 2
