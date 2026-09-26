"""Cross-provider identity: ESPN schedule rows and odds-provider rows must meet
on one games.id, with no guessing."""

from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import Game, GameSourceId, OddsSnapshot, Team
from ttk.domain import GameStatus, Market, Selection, Sport
from ttk.providers.base import (
    NormalizedGame,
    NormalizedOddsQuote,
    OddsFetch,
    ScheduleFetch,
    TeamRef,
)
from ttk.services.identity import resolve_team
from ttk.services.odds_ingest import run_odds_ingestion
from ttk.services.schedule_ingest import run_schedule_ingestion
from ttk.teams import normalize_team_name

KICK = datetime(2026, 9, 26, 23, 0, tzinfo=UTC)
BOISE = TeamRef("Boise State Broncos", "68", ("Boise State",))
AIR_FORCE = TeamRef("Air Force Falcons", "2005", ("Air Force",))


def espn_game(home: TeamRef = BOISE, away: TeamRef = AIR_FORCE, **kw: object) -> NormalizedGame:
    base = NormalizedGame(
        "espn",
        "401000001",
        Sport.CFB,
        home,
        away,
        KICK,
        None,
        espn_event_id="401000001",
        status=GameStatus.SCHEDULED,
        neutral_site=False,
        season=2026,
    )
    return replace(base, **kw)  # type: ignore[arg-type]


class Schedule:
    name = "espn"

    def __init__(self, *games: NormalizedGame) -> None:
        self.games = list(games)

    def fetch_games(self, sport: Sport, start: date, end: date) -> ScheduleFetch:
        return ScheduleFetch(self.games, {})


class Odds:
    name = "books"

    def __init__(self, game: NormalizedGame, *quotes: tuple[Selection, float, float]) -> None:
        self.fetch = OddsFetch(
            [game],
            [
                NormalizedOddsQuote(
                    "books",
                    game.source_identifier,
                    "dk",
                    "DK",
                    Market.SPREAD,
                    sel,
                    line,
                    price,
                    None,
                )
                for sel, line, price in quotes
            ],
            {},
        )

    def fetch_odds(self, sport: Sport) -> OddsFetch:
        return self.fetch


def books_game(home: str, away: str, **kw: object) -> NormalizedGame:
    base = NormalizedGame(
        "books", "ev-9", Sport.CFB, TeamRef(home), TeamRef(away), KICK + timedelta(minutes=5), None
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def ingest_schedule(sf: sessionmaker[Session], *games: NormalizedGame) -> None:
    run = run_schedule_ingestion(
        sf, Schedule(*games), Sport.CFB, date(2026, 9, 26), date(2026, 9, 26)
    )
    assert run.status == "SUCCESS", run.error


def count(sf: sessionmaker[Session], model: type) -> int:
    with sf() as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


def test_normalize_team_name() -> None:
    assert normalize_team_name("Boise St.") == "boise state"
    assert normalize_team_name("St. John's") == "saint john s"
    assert normalize_team_name("Texas A&M") == "texas a and m"
    assert normalize_team_name("San José State") == "san jose state"


def test_odds_link_to_espn_game_by_normalized_name(session_factory: sessionmaker[Session]) -> None:
    ingest_schedule(session_factory, espn_game())
    odds = Odds(
        books_game("Boise St.", "Air Force"),
        (Selection.HOME, -7.5, -110),
        (Selection.AWAY, 7.5, -110),
    )
    run = run_odds_ingestion(session_factory, odds, Sport.CFB)

    assert run.stats == {"linked_by_teams_and_time": 1}
    assert count(session_factory, Game) == 1
    assert count(session_factory, Team) == 2  # no provisional duplicates
    with session_factory() as s:
        assert {g.provider for g in s.scalars(select(GameSourceId))} == {"espn", "books"}


def test_espn_event_id_link_wins(session_factory: sessionmaker[Session]) -> None:
    ingest_schedule(session_factory, espn_game())
    # Names that match nothing, but the provider supplies ESPN's event id.
    odds = Odds(
        books_game("BSU", "AFA", espn_event_id="401000001"),
        (Selection.HOME, -7.5, -110),
        (Selection.AWAY, 7.5, -110),
    )
    run = run_odds_ingestion(session_factory, odds, Sport.CFB)
    assert run.stats is not None and run.stats["linked_by_espn_event_id"] == 1
    assert count(session_factory, Game) == 1


def test_swapped_orientation_flips_selections(session_factory: sessionmaker[Session]) -> None:
    ingest_schedule(session_factory, espn_game(neutral_site=True))
    # The book lists Air Force as home. Its "HOME -3" is Air Force -3 = our AWAY -3.
    odds = Odds(
        books_game("Air Force", "Boise St."),
        (Selection.HOME, -3.0, -110),
        (Selection.AWAY, 3.0, -110),
    )
    run_odds_ingestion(session_factory, odds, Sport.CFB)
    with session_factory() as s:
        link = s.scalars(select(GameSourceId).where(GameSourceId.provider == "books")).one()
        assert link.swapped is True
        snaps = {(o.selection, o.line) for o in s.scalars(select(OddsSnapshot))}
        assert snaps == {(Selection.AWAY, -3.0), (Selection.HOME, 3.0)}


def test_results_update_in_place(session_factory: sessionmaker[Session]) -> None:
    ingest_schedule(session_factory, espn_game())
    ingest_schedule(
        session_factory,
        espn_game(
            status=GameStatus.FINAL,
            home_score=31,
            away_score=17,
            commence_time=KICK + timedelta(minutes=30),
        ),
    )
    with session_factory() as s:
        game = s.scalars(select(Game)).one()
        assert (game.status, game.home_score, game.away_score) == ("FINAL", 31, 17)
        assert game.commence_time == KICK + timedelta(minutes=30)


def test_odds_provider_never_writes_results(session_factory: sessionmaker[Session]) -> None:
    ingest_schedule(session_factory, espn_game())
    odds = Odds(books_game("Boise St.", "Air Force", commence_time=KICK + timedelta(hours=2)))
    run_odds_ingestion(session_factory, odds, Sport.CFB)
    with session_factory() as s:
        assert s.scalars(select(Game)).one().commence_time == KICK


def test_odds_first_then_espn_adopts_team_and_game(session_factory: sessionmaker[Session]) -> None:
    run_odds_ingestion(session_factory, Odds(books_game("Boise St.", "Air Force")), Sport.CFB)
    assert count(session_factory, Team) == 2
    ingest_schedule(session_factory, espn_game())
    assert count(session_factory, Team) == 2
    assert count(session_factory, Game) == 1
    with session_factory() as s:
        assert {t.espn_id for t in s.scalars(select(Team))} == {"68", "2005"}


def test_curated_alias(session_factory: sessionmaker[Session]) -> None:
    miami = TeamRef("Miami Hurricanes", "2390", ("Miami",))
    miami_oh = TeamRef("Miami (OH) RedHawks", "193", ("Miami (OH)", "Miami OH"))
    ingest_schedule(session_factory, espn_game(home=miami, away=miami_oh))
    stats: Counter[str] = Counter()
    with session_factory() as s:
        team = resolve_team(
            s, provider="books", sport=Sport.CFB, ref=TeamRef("Miami (FL)"), stats=stats
        )
        assert team.espn_id == "2390"
    assert stats == Counter()


def test_ambiguous_name_is_not_guessed(session_factory: sessionmaker[Session]) -> None:
    # ESPN reports two different teams that share the short name "Springfield".
    a = TeamRef("Springfield Lasers", "901", ("Springfield",))
    b = TeamRef("Springfield Owls", "902", ("Springfield",))
    ingest_schedule(session_factory, espn_game(home=a, away=b))
    stats: Counter[str] = Counter()
    with session_factory() as s:
        team = resolve_team(
            s, provider="books", sport=Sport.CFB, ref=TeamRef("Springfield"), stats=stats
        )
        assert team.espn_id is None  # a new unmatched team, not one of the two
        assert team.name == "Springfield"
    assert stats["unmatched_team"] == 1


def test_unmatched_name_clashing_with_espn_team_gets_distinct_row(
    session_factory: sessionmaker[Session],
) -> None:
    a = TeamRef("Springfield", "901", ("Springfield Lasers",))
    b = TeamRef("Springfield Owls", "902", ("Springfield",))
    ingest_schedule(session_factory, espn_game(home=a, away=b))
    stats: Counter[str] = Counter()
    with session_factory() as s:
        team = resolve_team(
            s, provider="books", sport=Sport.CFB, ref=TeamRef("Springfield"), stats=stats
        )
        assert team.espn_id is None
        assert team.name == "Springfield [unmatched]"


def test_game_outside_window_is_not_linked(session_factory: sessionmaker[Session]) -> None:
    ingest_schedule(session_factory, espn_game())
    far = books_game("Boise St.", "Air Force", commence_time=KICK + timedelta(days=3))
    run_odds_ingestion(session_factory, Odds(far), Sport.CFB)
    assert count(session_factory, Game) == 2


@pytest.mark.parametrize("url_fixture", ["database_url"])
def test_migration_0002_backfills_normalized(url_fixture: str, tmp_path: object) -> None:
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    from ttk.cli import ROOT
    from ttk.db.session import make_engine

    url = f"sqlite:///{(Path(str(tmp_path)) / 'mig.db').as_posix()}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "0001")
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO teams (sport, name) VALUES ('CFB', 'Boise St.')"))
        conn.execute(
            text(
                "INSERT INTO team_aliases (provider, sport, alias, team_id) "
                "VALUES ('books', 'CFB', 'Boise St.', 1)"
            )
        )
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT normalized FROM team_aliases")).scalar() == "boise state"
    command.downgrade(cfg, "0001")
    engine.dispose()
