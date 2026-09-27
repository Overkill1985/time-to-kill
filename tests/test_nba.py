import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from ttk.db.models import ReportedLine
from ttk.domain import Sport
from ttk.models.elo import EloGame, EloPrediction
from ttk.providers.base import ProviderError
from ttk.providers.espn_odds import EspnCoreOdds, parse_item
from ttk.research.nba_model import (
    REST_CAP,
    _representative,
    opener_test,
    rest_features,
)
from ttk.research.nfl_elo import PricedSpread
from ttk.services.espn_history_import import season_window

# Real ESPN core-odds items captured 2026-09-26.
FIX = json.loads(
    (Path(__file__).parent / "fixtures" / "espn_core_odds_nba.json").read_text("utf-8")
)


def test_modern_item_has_open_and_close() -> None:
    espn_bet, live = FIX["modern_nyk_at_phi_2025"]
    lines = {line.moment: line for line in parse_item(espn_bet)}
    assert set(lines) == {"open", "close"}
    # Knicks at 76ers: Philadelphia opened +6.5 and closed +5.5.
    assert lines["open"].home_spread == 6.5 and lines["close"].home_spread == 5.5
    assert lines["close"].home_moneyline == 175 and lines["close"].home_spread_odds == -110
    assert lines["open"].total == 219.5
    assert parse_item(live) == []  # in-game odds are not pregame lines


def test_old_item_uses_item_spread_and_skips_projection_sites() -> None:
    parsed = [line for item in FIX["old_bos_at_cle_2017"] for line in parse_item(item)]
    books = {line.book for line in parsed}
    assert {"accuscore", "numberfire", "teamrankings"}.isdisjoint(books)
    assert "consensus" in books and "caesars-sportsbook" in books
    assert all(line.moment == "close" for line in parsed)
    caesars = next(line for line in parsed if line.book == "caesars-sportsbook")
    # Cleveland (home) -4.5 and the moneyline favorite: negative home line.
    assert caesars.home_spread == -4.5 and caesars.home_moneyline is not None
    assert caesars.home_moneyline < 0


def test_core_odds_http() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert "/basketball/leagues/nba/events/42/competitions/42/odds" in str(request.url)
        return httpx.Response(200, json={"items": FIX["modern_nyk_at_phi_2025"]})

    core = EspnCoreOdds(client=httpx.Client(transport=httpx.MockTransport(handle)))
    assert len(core.fetch(Sport.NBA, "42")) == 2
    missing = EspnCoreOdds(
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    )
    assert missing.fetch(Sport.NBA, "1") == []
    broken = EspnCoreOdds(
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
        backoff=0,
    )
    with pytest.raises(ProviderError):
        broken.fetch(Sport.NBA, "1")

    calls: list[int] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"items": FIX["modern_nyk_at_phi_2025"]})

    retried = EspnCoreOdds(client=httpx.Client(transport=httpx.MockTransport(flaky)), backoff=0)
    assert len(retried.fetch(Sport.NBA, "42")) == 2 and len(calls) == 3


def test_season_windows() -> None:
    w = season_window(Sport.NBA, 2025)
    assert (w.start, w.end) == (date(2024, 9, 25), date(2025, 6, 30))
    assert season_window(Sport.NBA, 2020).end == date(2020, 10, 15)  # the bubble
    assert season_window(Sport.NBA, 2021).start == date(2020, 12, 1)


def line(
    provider: str,
    spread: float | None,
    odds: tuple[float | None, float | None] = (-110, -110),
    total: float | None = 220.0,
) -> ReportedLine:
    return ReportedLine(
        game_id=1,
        provider=provider,
        home_spread=spread,
        home_spread_odds=odds[0],
        away_spread_odds=odds[1],
        total=total,
    )


def test_representative_line() -> None:
    rows = [
        line("espn:unibet", -4.5, (-119, -102)),
        line("espn:caesars", -4.5),
        line("espn:westgate", -5.0),
        line("espn:draftkings", -4.5, (None, None), 218.0),
    ]
    r = _representative(rows)
    assert r is not None
    assert r.home_spread == -4.5  # the most common number
    assert r.provider == "espn:unibet" or r.provider == "espn:caesars"
    assert r.provider != "espn:draftkings"  # priority book, but without prices
    assert r.total == 220.0
    assert _representative([line("espn:x", None)]) is None


T0 = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)


def g(gid: int, home: int, away: int, day: int) -> EloGame:
    return EloGame(gid, 2024, T0 + timedelta(days=day), home, away, False, 110, 100)


def test_rest_features() -> None:
    games = [g(1, 1, 2, 0), g(2, 1, 3, 1), g(3, 2, 1, 5)]
    rest = rest_features(games)
    assert rest[1] == (REST_CAP, REST_CAP, False, False)  # first games: capped rest
    assert rest[2] == (1.0, REST_CAP, True, False)  # team 1 on a back-to-back
    assert rest[3] == (REST_CAP, REST_CAP, False, False)  # team 2 rested 5 days (capped)


def test_opener_test_reports_price_clv_and_points_separately() -> None:
    game = g(1, 1, 2, 0)
    pred = EloPrediction(1, 2024, 1500, 1500, 0.0, 0.5)
    # Opener: home -4.5 at -110/-110. We like home (p=0.60 vs market 0.50).
    row = PricedSpread(game, pred, -4.5, -110, -110, 0.5)
    closes_same = {1: line("espn:x", -4.5, (-130, 110))}
    same = opener_test("m", lambda r: (0.60, 0.0), [row], closes_same)
    assert same.price_clv_n == 1 and same.moved_n == 0
    assert same.avg_price_clv is not None and same.avg_price_clv > 0  # -110 beat a -130 close
    closes_moved = {1: line("espn:x", -6.0)}
    moved = opener_test("m", lambda r: (0.60, 0.0), [row], closes_moved)
    assert moved.price_clv_n == 0 and moved.moved_n == 1
    assert moved.avg_points_gained == pytest.approx(1.5)  # we hold -4.5, the close was -6


def test_zero_prices_mean_not_offered() -> None:
    # ESPN sends 0 where a book listed a spread but no price (e.g. Wynn, 2017-18).
    item = {
        "provider": {"name": "Wynn"},
        "spread": -1.5,
        "overUnder": 210.5,
        "homeTeamOdds": {"moneyLine": 0, "spreadOdds": 0.0},
        "awayTeamOdds": {"moneyLine": 0, "spreadOdds": -110},
    }
    (parsed,) = parse_item(item)
    assert parsed.home_spread == -1.5 and parsed.total == 210.5
    assert parsed.home_moneyline is None and parsed.away_moneyline is None
    assert parsed.home_spread_odds is None and parsed.away_spread_odds == -110
    rows = [line("espn:consensus", -1.5, (0.0, 0.0)), line("espn:wynn", -1.5, (-105, -115))]
    r = _representative(rows)
    assert r is not None and r.provider == "espn:wynn"  # consensus ranks first but has no price
