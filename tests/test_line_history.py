import json
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from ttk import betting_math as bm
from ttk.api.app import create_app
from ttk.config import Settings
from ttk.db.models import Game, IngestionRun
from ttk.domain import Market, Selection, Sport
from ttk.providers.base import (
    NormalizedGame,
    NormalizedOddsQuote,
    OddsFetch,
    ScheduleFetch,
    TeamRef,
)
from ttk.providers.propline import BASE_URL, PropLineProvider, RateLimited
from ttk.services.collector import collect_once, refresh_schedules, sports_to_poll
from ttk.services.line_history import closing_line_value, line_history
from ttk.services.odds_ingest import store_odds

KICK = datetime(2026, 9, 27, 17, 0, tzinfo=UTC)
GAME = NormalizedGame("fake", "g1", Sport.NFL, TeamRef("Home"), TeamRef("Away"), KICK, None)
SECRET = "propline-test-key-not-real"


# --------------------------------------------------------------------------- PropLine adapter


def _propline(handler: httpx.MockTransport) -> PropLineProvider:
    return PropLineProvider(SECRET, client=httpx.Client(base_url=BASE_URL, transport=handler))


def test_propline_sends_key_in_header_and_parses_real_payload() -> None:
    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "propline_nfl_spreads_2026-09-26.json").read_text(
            "utf-8"
        )
    )
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload, headers={"X-Daily-Remaining": "987"})

    provider = _propline(httpx.MockTransport(handle))
    fetch = provider.fetch_odds(Sport.NFL)
    request = seen[0]
    assert request.url.path == "/v1/sports/football_nfl/odds"
    assert request.headers["X-API-Key"] == SECRET
    assert SECRET not in str(request.url)
    assert len(fetch.games) == 1 and len(fetch.quotes) == 4
    assert fetch.games[0].home.espn_id == "23"
    assert provider.daily_remaining == 987


@pytest.mark.parametrize("status", [429, 503])
def test_propline_rate_limit(status: int) -> None:
    provider = _propline(
        httpx.MockTransport(lambda r: httpx.Response(status, headers={"Retry-After": "12"}))
    )
    with pytest.raises(RateLimited) as info:
        provider.fetch_odds(Sport.CFB)
    assert info.value.retry_after == 12.0
    assert SECRET not in str(info.value)


def test_propline_requires_key() -> None:
    with pytest.raises(ValueError):
        PropLineProvider("")


# --------------------------------------------------------------------------- line history


def q(
    book: str, sel: Selection, line: float, price: float, market: Market = Market.SPREAD
) -> NormalizedOddsQuote:
    return NormalizedOddsQuote("fake", "g1", book, book.upper(), market, sel, line, price, None)


def spread(book: str, home_line: float, home: float, away: float) -> list[NormalizedOddsQuote]:
    return [q(book, Selection.HOME, home_line, home), q(book, Selection.AWAY, -home_line, away)]


def poll(sf: sessionmaker[Session], at: datetime, quotes: list[NormalizedOddsQuote]) -> None:
    with sf() as s:
        run = IngestionRun(provider="fake", kind="odds", sport=Sport.NFL)
        s.add(run)
        s.flush()
        store_odds(s, OddsFetch([GAME], quotes, {}), run=run, observed_at=at, stats=Counter())
        s.commit()


@pytest.fixture
def history(session_factory: sessionmaker[Session]) -> int:
    poll(
        session_factory,
        KICK - timedelta(days=3),
        spread("a", -3.0, -110, -110) + spread("a", -2.5, -125, 105),
    )  # alt line too
    poll(session_factory, KICK - timedelta(days=1), spread("a", -3.5, -105, -115))
    poll(
        session_factory,
        KICK - timedelta(minutes=10),
        spread("a", -3.5, -110, -110) + spread("b", -3.0, -125, 105),
    )
    poll(session_factory, KICK + timedelta(minutes=30), spread("a", -7.0, -110, -110))  # live
    with session_factory() as s:
        return int(s.query(Game.id).one()[0])


def test_open_previous_current_close(session_factory: sessionmaker[Session], history: int) -> None:
    with session_factory() as s:
        books = {h.sportsbook: h for h in line_history(s, history, Market.SPREAD, Selection.HOME)}
    a = books["a"]
    assert (a.opening.line, a.opening.american_odds) == (-3.0, -110)  # main, not the alt line
    assert a.opening.no_vig_probability == pytest.approx(0.5)
    assert a.current is not None
    assert (a.current.line, a.current.observed_at) == (-7.0, KICK + timedelta(minutes=30))
    assert a.previous is not None and (a.previous.line, a.previous.american_odds) == (-3.5, -110)
    assert a.closing is not None
    assert (a.closing.line, a.closing.observed_at) == (-3.5, KICK - timedelta(minutes=10))
    assert a.observations == 4
    assert books["b"].opening == books["b"].closing  # one poll only


def test_clv_at_bet_line_uses_books_closing_there(
    session_factory: sessionmaker[Session], history: int
) -> None:
    with session_factory() as s:
        clv = closing_line_value(s, history, Market.SPREAD, Selection.HOME, -3.0, -110)
    # Only book b closed at exactly -3 (-125/+105).
    expected_p = bm.no_vig_probabilities(
        [bm.american_to_decimal(-125), bm.american_to_decimal(105)]
    ).probabilities[0]
    assert clv.closing_no_vig_at_bet_line == pytest.approx(expected_p)
    assert clv.price_clv == pytest.approx(bm.american_to_decimal(-110) * expected_p - 1)
    assert clv.price_clv is not None and clv.price_clv > 0  # -110 at -3 beat a -125 close
    assert clv.books_at_close == 2
    assert clv.close_minutes_before_kickoff == pytest.approx(10)


def test_clv_without_close_at_bet_line_reports_points_only(
    session_factory: sessionmaker[Session], history: int
) -> None:
    with session_factory() as s:
        clv = closing_line_value(s, history, Market.SPREAD, Selection.HOME, -2.5, -120)
    assert clv.price_clv is None and clv.closing_no_vig_at_bet_line is None
    # closing main lines: a -3.5, b -3.0 -> median -3.0 (upper median); bet -2.5 is +0.5 better
    assert clv.closing_main_line == -3.0
    assert clv.points_gained == pytest.approx(0.5)


def test_points_gained_for_totals(session_factory: sessionmaker[Session]) -> None:
    quotes = [
        q("a", Selection.OVER, 44.5, -110, Market.TOTAL),
        q("a", Selection.UNDER, 44.5, -110, Market.TOTAL),
    ]
    poll(session_factory, KICK - timedelta(minutes=5), quotes)
    with session_factory() as s:
        gid = int(s.query(Game.id).one()[0])
        over = closing_line_value(s, gid, Market.TOTAL, Selection.OVER, 43.5, -110)
        under = closing_line_value(s, gid, Market.TOTAL, Selection.UNDER, 43.5, -110)
    assert over.points_gained == 1.0  # over 43.5 beat a 44.5 close
    assert under.points_gained == -1.0


def test_draw_is_rejected(session_factory: sessionmaker[Session], history: int) -> None:
    with session_factory() as s, pytest.raises(ValueError):
        line_history(s, history, Market.MONEYLINE, Selection.DRAW)


def test_line_history_api(database_url: str, history: int) -> None:
    client = TestClient(
        create_app(Settings(database_url=database_url)), base_url="http://localhost"
    )
    rows = client.get(
        f"/api/games/{history}/line-history", params={"market": "SPREAD", "selection": "HOME"}
    ).json()
    a = next(r for r in rows if r["sportsbook"] == "a")
    assert a["opening"]["line"] == -3.0 and a["closing"]["line"] == -3.5
    assert a["opening"]["american_odds"] == "-110"
    bad = client.get(
        f"/api/games/{history}/line-history", params={"market": "MONEYLINE", "selection": "DRAW"}
    )
    assert bad.status_code == 422


# --------------------------------------------------------------------------- collector


class Counting:
    name = "fake"

    def __init__(self, remaining: int | None = None) -> None:
        self.calls: list[Sport] = []
        self.daily_remaining = remaining

    def fetch_odds(self, sport: Sport) -> OddsFetch:
        self.calls.append(sport)
        if self.daily_remaining is not None:
            self.daily_remaining -= 1
        return OddsFetch([], [], {})


def test_sports_to_poll(session_factory: sessionmaker[Session]) -> None:
    poll(session_factory, KICK - timedelta(days=1), spread("a", -3.0, -110, -110))
    with session_factory() as s:
        soon, _ = sports_to_poll(s, [Sport.NFL, Sport.NBA], KICK - timedelta(days=2))
        later, why = sports_to_poll(s, [Sport.NFL], KICK - timedelta(days=30))
    assert soon == [Sport.NFL, Sport.NBA]  # NFL has a game soon; NBA unknown -> bootstrap
    assert later == [] and "no games" in why[Sport.NFL]


def test_collect_stops_before_exhausting_quota(session_factory: sessionmaker[Session]) -> None:
    provider = Counting(remaining=21)
    result = collect_once(
        session_factory,
        provider,
        [Sport.NFL, Sport.NBA, Sport.CFB],
        min_remaining=20,
        remaining=21,
    )
    # 21 -> NFL (20 left) -> NBA (19 left) -> CFB would dip below 20: skipped.
    assert provider.calls == [Sport.NFL, Sport.NBA]
    assert result.remaining == 19
    assert "quota" in result.skipped[Sport.CFB]


def test_collect_skips_when_quota_low(session_factory: sessionmaker[Session]) -> None:
    provider = Counting(remaining=5)
    result = collect_once(session_factory, provider, [Sport.NFL], min_remaining=20, remaining=5)
    assert provider.calls == []
    assert "quota" in result.skipped[Sport.NFL]


def test_book_that_pulled_its_line_before_kickoff_has_no_close(
    session_factory: sessionmaker[Session],
) -> None:
    poll(
        session_factory,
        KICK - timedelta(hours=2),
        spread("a", -3.0, -110, -110) + spread("b", -3.0, -120, 100),
    )
    # b is gone from the last pre-kickoff poll: its quotes are withdrawn then.
    poll(session_factory, KICK - timedelta(minutes=5), spread("a", -3.0, -110, -110))
    with session_factory() as s:
        gid = int(s.query(Game.id).one()[0])
        books = {h.sportsbook: h for h in line_history(s, gid, Market.SPREAD, Selection.HOME)}
        clv = closing_line_value(s, gid, Market.SPREAD, Selection.HOME, -3.0, -110)
    assert books["b"].closing is None and books["b"].current is None
    assert books["a"].closing is not None
    assert books["a"].closing.observed_at == KICK - timedelta(minutes=5)
    assert clv.books_at_close == 1
    assert clv.closing_no_vig_at_bet_line == pytest.approx(0.5)  # only book a's -110/-110


class OneGameSchedule:
    name = "espn"

    def __init__(self, games: list[NormalizedGame]) -> None:
        self.games = games
        self.calls = 0

    def fetch_games(self, sport: Sport, start: object, end: object) -> ScheduleFetch:
        self.calls += 1
        return ScheduleFetch(self.games, {})


def test_refresh_schedules_respects_interval(session_factory: sessionmaker[Session]) -> None:
    schedule = OneGameSchedule([replace(GAME, provider="espn", source_identifier="e1")])
    now = KICK - timedelta(days=1)
    assert len(refresh_schedules(session_factory, schedule, [Sport.NFL], now=now)) == 1
    # Within 6 hours of the last successful refresh: skipped.
    assert refresh_schedules(session_factory, schedule, [Sport.NFL], now=now) == []
    assert schedule.calls == 1


def test_recent_idle_schedule_stops_bootstrap_polling(
    session_factory: sessionmaker[Session],
) -> None:
    refresh_schedules(session_factory, OneGameSchedule([]), [Sport.NCAAB])
    with session_factory() as s:
        poll_now, why = sports_to_poll(s, [Sport.NCAAB], datetime.now(UTC))
    assert poll_now == [] and "no games" in why[Sport.NCAAB]


def test_nba_window_is_wider_than_nfl(session_factory: sessionmaker[Session]) -> None:
    far = KICK + timedelta(days=60)
    for sport, sid in ((Sport.NFL, "nfl-far"), (Sport.NBA, "nba-far")):
        game = replace(GAME, sport=sport, source_identifier=sid, commence_time=far)
        with session_factory() as s:
            run = IngestionRun(provider="fake", kind="odds", sport=sport)
            s.add(run)
            s.flush()
            store_odds(s, OddsFetch([game], [], {}), run=run, observed_at=KICK, stats=Counter())
            s.commit()
    with session_factory() as s:
        poll_now, why = sports_to_poll(s, [Sport.NFL, Sport.NBA], KICK)
    assert poll_now == [Sport.NBA]  # NBA books post lines ~90 days out; NFL ~1 week
    assert "7 days" in why[Sport.NFL]


def test_empty_short_schedule_does_not_idle_a_wide_window_sport(
    session_factory: sessionmaker[Session],
) -> None:
    # ESPN's 8-day schedule shows no NBA games, but NBA odds are polled 90 days out.
    refresh_schedules(session_factory, OneGameSchedule([]), [Sport.NBA])
    with session_factory() as s:
        poll_now, _ = sports_to_poll(s, [Sport.NBA], datetime.now(UTC))
    assert poll_now == [Sport.NBA]  # bootstraps rather than going quiet
