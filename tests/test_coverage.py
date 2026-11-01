from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import IngestionRun
from ttk.domain import Market, Selection, Sport
from ttk.providers.base import NormalizedGame, NormalizedOddsQuote, OddsFetch, TeamRef
from ttk.services.coverage import odds_coverage
from ttk.services.odds_ingest import store_odds

T0 = datetime(2026, 11, 3, 23, tzinfo=UTC)


def test_counts_games_with_a_spread_before_kickoff(session_factory: sessionmaker[Session]) -> None:
    quoted = NormalizedGame("espn", "g1", Sport.NCAAB, TeamRef("A"), TeamRef("B"), T0, None)
    late = NormalizedGame("espn", "g2", Sport.NCAAB, TeamRef("C"), TeamRef("D"), T0, None)
    bare = NormalizedGame("espn", "g3", Sport.NCAAB, TeamRef("E"), TeamRef("F"), T0, None)

    def spread(game: str) -> list[NormalizedOddsQuote]:
        return [
            NormalizedOddsQuote("fake", game, "dk", "DK", Market.SPREAD, sel, line, -110, None)
            for sel, line in ((Selection.HOME, -3.5), (Selection.AWAY, 3.5))
        ]

    def poll(quotes: list[NormalizedOddsQuote], at: datetime) -> None:
        with session_factory() as s:
            run = IngestionRun(provider="fake", kind="odds", sport=Sport.NCAAB)
            s.add(run)
            s.flush()
            fetch = OddsFetch([quoted, late, bare], quotes, {})
            store_odds(s, fetch, run=run, observed_at=at, stats=Counter())
            s.commit()

    poll(spread("g1"), T0 - timedelta(hours=3))  # before tip: counts
    poll(spread("g2"), T0 + timedelta(minutes=5))  # only after tip: doesn't
    with session_factory() as s:
        (line,) = odds_coverage(s, T0 - timedelta(hours=12), T0 + timedelta(hours=12))
    assert (line.sport, line.games, line.with_spread) == ("NCAAB", 3, 1)
    assert line.share == 1 / 3
