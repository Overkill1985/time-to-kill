import json
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import Game, GameSourceId, ReportedLine, Team
from ttk.domain import GameStatus, Sport
from ttk.providers.base import NormalizedGame, ProviderError, ScheduleFetch
from ttk.providers.espn import parse_event
from ttk.providers.nflverse import (
    GAMES_URL,
    TEAMS,
    HistoricalGame,
    NflverseProvider,
    parse_games,
    parse_row,
)
from ttk.services.history_import import run_nfl_history_import, store_history
from ttk.services.schedule_ingest import run_schedule_ingestion

FIXTURES = Path(__file__).parent / "fixtures"
# Real rows from nflverse/nfldata games.csv, captured 2026-09-26.
ROWS: dict[str, dict[str, str]] = json.loads(
    (FIXTURES / "nflverse_games_sample.json").read_text("utf-8")
)
ESPN = json.loads((FIXTURES / "espn_scoreboards_2026.json").read_text("utf-8"))


def test_parse_regular_game() -> None:
    h = parse_row(ROWS["reg_2025"])
    g = h.game
    assert (g.away.name, g.home.name) == ("Tampa Bay Buccaneers", "Atlanta Falcons")
    assert (g.home.espn_id, g.away.espn_id) == ("1", "27")
    assert (g.home_score, g.away_score, g.status) == (20, 23, GameStatus.FINAL)
    # 13:00 US Eastern (EDT) = 17:00 UTC
    assert g.commence_time == datetime(2025, 9, 7, 17, 0, tzinfo=UTC)
    assert (g.season, g.season_type, g.week, g.neutral_site) == (2025, "REG", 1, False)
    assert g.espn_event_id == "401772830"
    # spread_line -1.5 = away favored by 1.5 -> home line +1.5
    assert h.lines is not None and h.lines.home_spread == 1.5
    assert (h.lines.home_moneyline, h.lines.away_moneyline) == (-105, -115)


def test_parse_neutral_playoff_relocated_and_unplayed() -> None:
    assert parse_row(ROWS["neutral_2025"]).game.neutral_site is True
    assert parse_row(ROWS["playoff"]).game.season_type == "POST"
    oak = parse_row(ROWS["oak_1999_no_ml"])
    assert oak.game.home.espn_id == "13" and "OAK" in oak.game.home.other_names
    assert oak.lines is not None and oak.lines.home_moneyline is None
    assert oak.lines.home_spread == -7.0
    unplayed = parse_row(ROWS["unplayed_2026"]).game
    assert unplayed.status is GameStatus.SCHEDULED and unplayed.home_score is None


def test_team_table_is_franchise_level() -> None:
    assert TEAMS["OAK"][0] == TEAMS["LV"][0] == "13"
    assert TEAMS["STL"][0] == TEAMS["LA"][0] == "14"
    assert TEAMS["SD"][0] == TEAMS["LAC"][0] == "24"
    assert len({espn_id for espn_id, _ in TEAMS.values()}) == 32


def test_parse_games_filters_seasons_and_unknown_codes() -> None:
    rows = [ROWS["reg_2025"], ROWS["oak_1999_no_ml"], {**ROWS["reg_2025"], "home_team": "XXX"}]
    games, skipped = parse_games(rows, first_season=2000, last_season=2026)
    assert [g.game.source_identifier for g in games] == ["2025_01_TB_ATL"]
    assert skipped == {"unknown_team_code": 1}


def test_fetch_errors() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(ProviderError, match="HTTP 404"):
        NflverseProvider(client=client).fetch_history()


def test_fetch_parses_csv() -> None:
    header = list(ROWS["reg_2025"].keys())
    body = ",".join(header) + "\n" + ",".join(ROWS["reg_2025"][c] for c in header) + "\n"

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == GAMES_URL
        return httpx.Response(200, text=body)

    provider = NflverseProvider(client=httpx.Client(transport=httpx.MockTransport(handle)))
    games, _ = provider.fetch_history(first_season=2025, last_season=2025)
    assert len(games) == 1


class FakeNflverse(NflverseProvider):
    def __init__(self, games: list[HistoricalGame]) -> None:
        self.games = games

    def fetch_history(
        self, *, first_season: int = 1999, last_season: int | None = None
    ) -> tuple[list[HistoricalGame], dict[str, int]]:
        return self.games, {}


def _count(sf: sessionmaker[Session], model: type) -> int:
    with sf() as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


def test_import_is_idempotent_and_stores_lines(session_factory: sessionmaker[Session]) -> None:
    games = [parse_row(r) for r in ROWS.values()]
    for _ in range(2):
        run = run_nfl_history_import(session_factory, FakeNflverse(games), first_season=1999)
        assert run.status == "SUCCESS", run.error
    assert _count(session_factory, Game) == 5
    assert _count(session_factory, ReportedLine) == 5
    # OAK (1999) and LV are one franchise; the team table holds ESPN ids.
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Team).where(Team.espn_id == "13")) == 1


def test_links_to_espn_game_and_espn_outranks_nflverse(
    session_factory: sessionmaker[Session],
) -> None:
    parsed = parse_event(ESPN["nfl_scheduled"][0], Sport.NFL)  # LAC @ BUF, 401872953
    assert parsed is not None
    espn_game: NormalizedGame = parsed

    class OneDay:
        name = "espn"

        def fetch_games(self, sport: Sport, start: date, end: date) -> ScheduleFetch:
            return ScheduleFetch([espn_game], {})

    run_schedule_ingestion(
        session_factory, OneDay(), Sport.NFL, date(2026, 9, 27), date(2026, 9, 27)
    )
    # nflverse reports a (stale) different kickoff; ESPN must win.
    nflv = parse_row(ROWS["unplayed_2026"])
    stale = replace(
        nflv, game=replace(nflv.game, commence_time=datetime(2026, 9, 27, 20, tzinfo=UTC))
    )
    stats: Counter[str] = Counter()
    with session_factory() as s:
        store_history(s, [stale], stats)
        s.commit()
    assert stats["linked_by_espn_event_id"] == 1
    with session_factory() as s:
        game = s.scalars(select(Game)).one()
        assert game.commence_time == espn_game.commence_time
        assert {link.provider for link in s.scalars(select(GameSourceId))} == {"espn", "nflverse"}
        assert s.scalars(select(ReportedLine)).one().home_spread == -7.0


def test_swapped_link_swaps_lines(session_factory: sessionmaker[Session]) -> None:
    neutral = parse_row(ROWS["neutral_2025"])  # nflverse: KC @ LAC (Brazil)
    g = neutral.game
    # ESPN (hypothetically) lists it the other way round.
    flipped = replace(
        g,
        provider="espn",
        source_identifier=g.espn_event_id or "",
        home=g.away,
        away=g.home,
        home_score=g.away_score,
        away_score=g.home_score,
    )

    class OneDay:
        name = "espn"

        def fetch_games(self, sport: Sport, start: date, end: date) -> ScheduleFetch:
            return ScheduleFetch([flipped], {})

    run_schedule_ingestion(session_factory, OneDay(), Sport.NFL, date(2025, 9, 5), date(2025, 9, 5))
    stats: Counter[str] = Counter()
    with session_factory() as s:
        store_history(s, [neutral], stats)
        s.commit()
        line = s.scalars(select(ReportedLine)).one()
    assert stats["lines_swapped"] == 1
    assert neutral.lines is not None and neutral.lines.home_spread is not None
    assert line.home_spread == -neutral.lines.home_spread
    assert line.home_moneyline == neutral.lines.away_moneyline
