from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session, sessionmaker

from ttk import betting_math as bm
from ttk.db.models import Game, IngestionRun, OddsSnapshot, Team
from ttk.domain import Market, Selection, Sport
from ttk.providers.base import (
    NormalizedGame,
    NormalizedOddsQuote,
    OddsFetch,
    ProviderError,
    TeamRef,
)
from ttk.services.market import main_lines, side_markets
from ttk.services.odds_ingest import run_odds_ingestion

KICKOFF = datetime(2026, 9, 27, 17, 0, tzinfo=UTC)
GAME = NormalizedGame(
    "fake", "g1", Sport.NFL, TeamRef("Baltimore Ravens"), TeamRef("Cleveland Browns"), KICKOFF, None
)


def q(
    book: str, sel: Selection, line: float | None, price: float, market: Market = Market.SPREAD
) -> NormalizedOddsQuote:
    return NormalizedOddsQuote("fake", "g1", book, book.upper(), market, sel, line, price, None)


@dataclass
class FakeProvider:
    fetches: list[OddsFetch | Exception]
    name: str = "fake"
    calls: int = field(default=0)

    def fetch_odds(self, sport: Sport) -> OddsFetch:
        item = self.fetches[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def spread_fetch(*quotes: NormalizedOddsQuote) -> OddsFetch:
    return OddsFetch([GAME], list(quotes), {})


def test_ingest_appends_and_market_uses_latest(session_factory: sessionmaker[Session]) -> None:
    first = spread_fetch(
        q("a", Selection.HOME, -2.5, -110),
        q("a", Selection.AWAY, 2.5, -110),
        q("b", Selection.HOME, -2.5, -105),
        q("b", Selection.AWAY, 2.5, -115),
        q("b", Selection.HOME, -1.5, -125),
        q("b", Selection.AWAY, 1.5, +105),  # alternate
    )
    second = spread_fetch(  # book a moves to -3; book b not in this poll
        q("a", Selection.HOME, -3.0, -110),
        q("a", Selection.AWAY, 3.0, -110),
    )
    provider = FakeProvider([first, second])
    run1 = run_odds_ingestion(session_factory, provider, Sport.NFL)
    run2 = run_odds_ingestion(session_factory, provider, Sport.NFL)
    assert (run1.status, run1.records_written) == ("SUCCESS", 6)
    assert (run2.status, run2.records_written) == ("SUCCESS", 2)

    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(OddsSnapshot)) == 8  # nothing overwritten
        assert s.scalar(select(func.count()).select_from(Game)) == 1  # game resolved once
        assert s.scalar(select(func.count()).select_from(Team)) == 2
        game_id = s.scalars(select(Game.id)).one()

        markets = side_markets(s, game_id)
        home = {m.line: m for m in markets if m.selection is Selection.HOME}
        # Book a's withdrawn -2.5 is gone; book b's latest poll still has -2.5 and -1.5.
        assert set(home) == {-3.0, -2.5, -1.5}
        assert home[-2.5].consensus.books_reporting == 1
        assert home[-2.5].consensus.best.sportsbook == "b"

        main = {m.selection: m for m in main_lines(markets)}
        # Three lines each priced by one book: tie broken by closest to 50/50 (a's -3 at -110).
        assert main[Selection.HOME].line == -3.0
        assert main[Selection.HOME].consensus.consensus_no_vig_probability == pytest.approx(0.5)


def test_consensus_across_books(session_factory: sessionmaker[Session]) -> None:
    fetch = spread_fetch(
        q("a", Selection.HOME, -2.5, -105),
        q("a", Selection.AWAY, 2.5, -115),
        q("b", Selection.HOME, -2.5, -110),
        q("b", Selection.AWAY, 2.5, -110),
        q("c", Selection.HOME, -2.5, -115),
        q("c", Selection.AWAY, 2.5, -105),
        q("c", Selection.HOME, None, -150, Market.MONEYLINE),  # one-sided: no consensus
    )
    run_odds_ingestion(session_factory, FakeProvider([fetch]), Sport.NFL)
    with session_factory() as s:
        game_id = s.scalars(select(Game.id)).one()
        markets = main_lines(side_markets(s, game_id))
        assert {(m.market, m.selection) for m in markets} == {
            (Market.SPREAD, Selection.HOME),
            (Market.SPREAD, Selection.AWAY),
        }
        home = next(m for m in markets if m.selection is Selection.HOME)
        assert home.consensus.books_reporting == 3
        assert home.consensus.best.decimal_odds == pytest.approx(bm.american_to_decimal(-105))


def test_provider_failure_is_recorded(session_factory: sessionmaker[Session]) -> None:
    run = run_odds_ingestion(
        session_factory, FakeProvider([ProviderError("fake: HTTP 500")]), Sport.NFL
    )
    assert run.status == "FAILED" and run.error == "fake: HTTP 500"
    with session_factory() as s:
        stored = s.scalars(select(IngestionRun)).one()
        assert stored.status == "FAILED" and stored.finished_at is not None


def test_snapshots_are_immutable(session_factory: sessionmaker[Session]) -> None:
    fetch = spread_fetch(q("a", Selection.HOME, -2.5, -110), q("a", Selection.AWAY, 2.5, -110))
    run_odds_ingestion(session_factory, FakeProvider([fetch]), Sport.NFL)
    for statement in (
        "UPDATE odds_snapshots SET american_odds = -200",
        "DELETE FROM odds_snapshots",
    ):
        with session_factory() as s, pytest.raises(DatabaseError, match="append-only"):
            s.execute(text(statement))


def test_datetimes_round_trip_as_utc(session_factory: sessionmaker[Session]) -> None:
    run_odds_ingestion(session_factory, FakeProvider([spread_fetch()]), Sport.NFL)
    with session_factory() as s:
        game = s.scalars(select(Game)).one()
        assert game.commence_time == KICKOFF
        assert game.commence_time.tzinfo is UTC
