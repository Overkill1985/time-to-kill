from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import IngestionRun, OddsSnapshot
from ttk.domain import Market, Selection, Sport
from ttk.providers.base import NormalizedGame, NormalizedOddsQuote, OddsFetch, TeamRef
from ttk.services.odds_ingest import main_line, store_odds, within_window

T0 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def q(market: Market, sel: Selection, line: float | None, price: float) -> NormalizedOddsQuote:
    return NormalizedOddsQuote("fake", "g1", "dk", "DK", market, sel, line, price, None)


def book(*quotes: NormalizedOddsQuote) -> dict[tuple[str, str, float | None], NormalizedOddsQuote]:
    return {(str(x.market), str(x.selection), x.line): x for x in quotes}


SPREADS = [
    # main: home -3.5 at -110/-110; alternates out to -13.5 / +6.5
    q(Market.SPREAD, Selection.HOME, -3.5, -110),
    q(Market.SPREAD, Selection.AWAY, 3.5, -110),
    q(Market.SPREAD, Selection.HOME, -6.5, 150),
    q(Market.SPREAD, Selection.AWAY, 6.5, -180),
    q(Market.SPREAD, Selection.HOME, -13.5, 400),
    q(Market.SPREAD, Selection.AWAY, 13.5, -600),
    q(Market.SPREAD, Selection.HOME, 2.5, -500),
    q(Market.SPREAD, Selection.AWAY, -2.5, 350),
]


def test_main_line_is_the_two_sided_price_nearest_even() -> None:
    quotes = book(*SPREADS, q(Market.TOTAL, Selection.OVER, 47.5, -105))  # one-sided total
    assert main_line(quotes, "SPREAD") == -3.5
    assert main_line(quotes, "TOTAL") is None


def test_window_keeps_lines_near_main_on_both_sides() -> None:
    stats: Counter[str] = Counter()
    kept = within_window(
        book(*SPREADS, q(Market.MONEYLINE, Selection.HOME, None, -170)), stats, window=3.0
    )
    lines = {(market, sel, line) for (market, sel, line) in kept}
    # -3.5 main, -6.5 (3 away) kept; +2.5 home / -2.5 away (6 away) and -13.5 dropped
    assert lines == {
        ("SPREAD", "HOME", -3.5),
        ("SPREAD", "AWAY", 3.5),
        ("SPREAD", "HOME", -6.5),
        ("SPREAD", "AWAY", 6.5),
        ("MONEYLINE", "HOME", None),
    }
    assert stats["outside_line_window"] == 4


def test_lines_leaving_the_window_are_withdrawn(session_factory: sessionmaker[Session]) -> None:
    game = NormalizedGame("espn", "g1", Sport.NFL, TeamRef("Home"), TeamRef("Away"), T0, None)

    def poll(quotes: list[NormalizedOddsQuote], at: datetime) -> Counter[str]:
        stats: Counter[str] = Counter()
        with session_factory() as s:
            run = IngestionRun(provider="fake", kind="odds", sport=Sport.NFL)
            s.add(run)
            s.flush()
            store_odds(s, OddsFetch([game], quotes, {}), run=run, observed_at=at, stats=stats)
            s.commit()
        return stats

    first = poll(SPREADS, T0 - timedelta(hours=5))
    assert first["new"] == 4 and first["outside_line_window"] == 4  # far alternates not stored
    # The main line moves to -9.5: -6.5 stays in the window, -3.5 falls out of it.
    moved = [
        q(Market.SPREAD, Selection.HOME, -9.5, -110),
        q(Market.SPREAD, Selection.AWAY, 9.5, -110),
        q(Market.SPREAD, Selection.HOME, -6.5, -200),
        q(Market.SPREAD, Selection.AWAY, 6.5, 165),
        q(Market.SPREAD, Selection.HOME, -3.5, -400),
        q(Market.SPREAD, Selection.AWAY, 3.5, 300),
    ]
    second = poll(moved, T0 - timedelta(hours=4))
    assert second["withdrawn"] == 2  # -3.5 / +3.5: out of the window now
    with session_factory() as s:
        withdrawn = s.scalars(select(OddsSnapshot).where(OddsSnapshot.withdrawn.is_(True))).all()
        assert sorted((w.selection, w.line) for w in withdrawn) == [("AWAY", 3.5), ("HOME", -3.5)]
