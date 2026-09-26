from collections import Counter
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ttk import betting_math as bm
from ttk.api.app import create_app
from ttk.config import Settings
from ttk.db.models import Bet, Game, GameSourceId, IngestionRun
from ttk.domain import BetResult, CorrelationRisk, GameStatus, Market, Selection, Sport
from ttk.models.nfl_features import GameFeatures
from ttk.providers.base import NormalizedGame, NormalizedOddsQuote, OddsFetch, TeamRef
from ttk.services.bets import performance, settle_bets
from ttk.services.nfl_spread_predictor import SpreadView
from ttk.services.parlay_lab import (
    LegInput,
    ParlayError,
    book_offers,
    evaluate_parlay,
    parlay_performance,
    save_parlay,
    settle_parlays,
)

KICK = datetime(2026, 9, 27, 17, 0, tzinfo=UTC)
NOW = KICK - timedelta(hours=3)
DK = "draftkings"


def q(
    sid: str, market: Market, sel: Selection, line: float | None, price: float, book: str = DK
) -> NormalizedOddsQuote:
    return NormalizedOddsQuote("fake", sid, book, book, market, sel, line, price, None)


def seed(
    sf: sessionmaker[Session], *, kick: datetime = KICK, observed: datetime | None = None
) -> dict[str, int]:
    games = [
        NormalizedGame("espn", "g1", Sport.NFL, TeamRef("Ravens"), TeamRef("Browns"), kick, None),
        NormalizedGame("espn", "g2", Sport.NFL, TeamRef("Bills"), TeamRef("Jets"), kick, None),
        NormalizedGame(
            "espn",
            "g3",
            Sport.CFB,
            TeamRef("Alabama"),
            TeamRef("Auburn"),
            kick + timedelta(hours=3),
            None,
        ),
    ]
    quotes = []
    for book in (DK, "fanduel"):
        for sid in ("g1", "g2", "g3"):
            quotes += [
                q(sid, Market.SPREAD, Selection.HOME, -3.5, -110, book),
                q(sid, Market.SPREAD, Selection.AWAY, 3.5, -110, book),
            ]
        quotes += [
            q("g1", Market.MONEYLINE, Selection.HOME, None, -180, book),
            q("g1", Market.MONEYLINE, Selection.AWAY, None, 150, book),
            q("g1", Market.TOTAL, Selection.OVER, 44.5, -110, book),
            q("g1", Market.TOTAL, Selection.UNDER, 44.5, -110, book),
        ]
    quotes += [
        q("g1", Market.SPREAD, Selection.HOME, -1.5, -150),  # DK alt line only
        q("g1", Market.SPREAD, Selection.AWAY, 1.5, 125),
    ]
    with sf() as s:
        run = IngestionRun(provider="fake", kind="odds", sport=Sport.NFL)
        s.add(run)
        s.flush()
        store_odds_fetch = OddsFetch(games, quotes, {})
        from ttk.services.odds_ingest import store_odds

        store_odds(
            s,
            store_odds_fetch,
            run=run,
            observed_at=observed or NOW - timedelta(minutes=5),
            stats=Counter(),
        )
        s.commit()
        ids = dict(s.execute(select(GameSourceId.source_identifier, GameSourceId.game_id)).all())
    return {k: int(v) for k, v in ids.items()}


def leg(
    gid: int,
    market: Market = Market.SPREAD,
    sel: Selection = Selection.HOME,
    line: float | None = -3.5,
    odds: float | None = None,
) -> LegInput:
    return LegInput(gid, market, sel, line, odds)


def test_two_game_market_only_parlay(session_factory: sessionmaker[Session]) -> None:
    g = seed(session_factory)
    with session_factory() as s:
        a = evaluate_parlay(s, [leg(g["g1"]), leg(g["g2"])], DK, at=NOW)
    d = bm.american_to_decimal(-110)
    assert a.decimal_odds == pytest.approx(d * d)
    assert a.joint_probability == pytest.approx(0.25)  # two 50/50 legs
    assert a.market_joint_probability == pytest.approx(0.25)
    assert a.ev_per_unit == pytest.approx(0.25 * d * d - 1)  # the compounded vig
    assert a.ev_per_unit < 0
    assert a.correlation_risk is CorrelationRisk.LOW and a.correlations == []
    assert all(x.probability_source == "market" for x in a.legs)
    assert any("no edge" in w for w in a.warnings)


def test_cross_sport_parlay(session_factory: sessionmaker[Session]) -> None:
    g = seed(session_factory)
    with session_factory() as s:
        a = evaluate_parlay(s, [leg(g["g1"]), leg(g["g3"])], DK, at=NOW)
    assert {x.sport for x in a.legs} == {Sport.NFL, Sport.CFB}


@pytest.mark.parametrize(
    ("second", "risk"),
    [
        (dict(market=Market.TOTAL, sel=Selection.OVER, line=44.5), CorrelationRisk.MODERATE),
        (dict(market=Market.MONEYLINE, sel=Selection.HOME, line=None), CorrelationRisk.HIGH),
        (dict(market=Market.SPREAD, sel=Selection.HOME, line=-1.5), CorrelationRisk.HIGH),
    ],
)
def test_same_game_correlation(
    session_factory: sessionmaker[Session], second: dict[str, object], risk: CorrelationRisk
) -> None:
    g = seed(session_factory)
    with session_factory() as s:
        a = evaluate_parlay(s, [leg(g["g1"]), leg(g["g1"], **second)], DK, at=NOW)  # type: ignore[arg-type]
    assert a.correlation_risk is risk
    assert a.highest_correlation_leg is not None
    assert any("assumes independence" in w for w in a.warnings)


@pytest.mark.parametrize(
    ("legs", "message"),
    [
        (lambda g: [leg(g["g1"])], "2 to 12 legs"),
        (lambda g: [leg(g["g1"]), leg(g["g1"], sel=Selection.AWAY, line=3.5)], "both sides"),
        (lambda g: [leg(g["g1"]), leg(g["g1"])], "repeats"),
        (lambda g: [leg(g["g1"]), leg(g["g2"], line=-7.5)], "does not quote"),
        (
            lambda g: [leg(g["g1"]), leg(g["g2"], market=Market.TOTAL, sel=Selection.HOME)],
            "not a side",
        ),
    ],
)
def test_rejections(session_factory: sessionmaker[Session], legs, message: str) -> None:  # type: ignore[no-untyped-def]
    g = seed(session_factory)
    with session_factory() as s, pytest.raises(ParlayError, match=message):
        evaluate_parlay(s, legs(g), DK, at=NOW)


def test_started_game_rejected(session_factory: sessionmaker[Session]) -> None:
    g = seed(session_factory)
    with session_factory() as s, pytest.raises(ParlayError, match="started"):
        evaluate_parlay(s, [leg(g["g1"]), leg(g["g2"])], DK, at=KICK)


def test_manual_price_and_costliest_leg(session_factory: sessionmaker[Session]) -> None:
    g = seed(session_factory)
    with session_factory() as s:
        a = evaluate_parlay(s, [leg(g["g1"]), leg(g["g2"]), leg(g["g3"], odds=-400)], DK, at=NOW)
    assert a.legs[2].price_source == "manual" and a.legs[2].american_odds == -400
    assert a.reduces_ev_most == 2  # dropping the -400 leg helps most
    impact = {i.index: i.ev_without for i in a.impacts}
    assert impact[2] is not None and impact[2] > a.ev_per_unit


class FakeModel:
    def spread(self, game_id: int, home_line: float, market: float) -> SpreadView:
        f = GameFeatures(game_id, 0.0, 0.0, None, None, 5, 5)
        return SpreadView(0.60, 0.0, 0.0, f, 0.0)


def test_model_probability_used_for_nfl_spreads(session_factory: sessionmaker[Session]) -> None:
    g = seed(session_factory)
    with session_factory() as s:
        a = evaluate_parlay(
            s,
            [leg(g["g1"]), leg(g["g2"], sel=Selection.AWAY, line=3.5), leg(g["g3"])],
            DK,
            at=NOW,
            predictor=FakeModel(),
        )
    sources = [x.probability_source for x in a.legs]
    assert sources == ["model", "model", "market"]  # CFB has no model
    assert a.legs[0].probability == pytest.approx(0.60)
    assert a.legs[1].probability == pytest.approx(0.40)  # away side of a 60% home
    assert a.legs[0].edge == pytest.approx(0.10)
    assert a.strongest_leg == 0 and a.weakest_leg == 1


def test_book_offers_flag_main_line(session_factory: sessionmaker[Session]) -> None:
    g = seed(session_factory)
    with session_factory() as s:
        offers = book_offers(s, g["g1"], DK, at=NOW)
    home_spreads = {
        o.line: o.main
        for o in offers
        if o.market is Market.SPREAD and o.selection is Selection.HOME
    }
    assert home_spreads == {-3.5: True, -1.5: False}


def finish(
    sf: sessionmaker[Session], sid_scores: dict[int, tuple[GameStatus, int | None, int | None]]
) -> None:
    with sf() as s:
        for gid, (status, home, away) in sid_scores.items():
            game = s.get_one(Game, gid)
            game.status, game.home_score, game.away_score = status, home, away
        s.commit()


def saved(sf: sessionmaker[Session], g: dict[str, int], stake: float = 10.0) -> int:
    with sf() as s:
        p = save_parlay(s, [leg(g["g1"]), leg(g["g2"])], DK, stake, placed_at=NOW)
        s.commit()
        return p.id


def test_save_and_settle_win(session_factory: sessionmaker[Session]) -> None:
    g = seed(session_factory)
    pid = saved(session_factory, g)
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Bet).where(Bet.parlay_id == pid)) == 2
        assert settle_parlays(s) == []  # games not final yet
    finish(
        session_factory, {g["g1"]: (GameStatus.FINAL, 24, 17), g["g2"]: (GameStatus.FINAL, 30, 20)}
    )
    with session_factory() as s:
        (p,) = settle_parlays(s)
        s.commit()
        assert settle_bets(s) == []  # parlay legs are not single bets
    d = bm.american_to_decimal(-110)
    assert p.result == BetResult.WIN and p.profit_loss == pytest.approx(10 * (d * d - 1))


def test_settle_push_reprices_and_loss(session_factory: sessionmaker[Session]) -> None:
    g = seed(session_factory)
    pid = saved(session_factory, g)
    finish(
        session_factory,
        {g["g1"]: (GameStatus.FINAL, 24, 17), g["g2"]: (GameStatus.CANCELED, None, None)},
    )
    with session_factory() as s:
        (p,) = settle_parlays(s)
        legs = s.scalars(select(Bet).where(Bet.parlay_id == pid)).all()
    d = bm.american_to_decimal(-110)
    assert {leg.result for leg in legs} == {"WIN", "VOID"}
    assert p.result == BetResult.WIN and p.profit_loss == pytest.approx(10 * (d - 1))


def test_settle_loss_and_performance(session_factory: sessionmaker[Session]) -> None:
    g = seed(session_factory)
    saved(session_factory, g, stake=20.0)
    finish(
        session_factory, {g["g1"]: (GameStatus.FINAL, 24, 17), g["g2"]: (GameStatus.FINAL, 17, 20)}
    )
    with session_factory() as s:
        (p,) = settle_parlays(s)
        s.commit()
        perf = parlay_performance(s)
        singles = performance(s)
    assert p.result == BetResult.LOSS and p.profit_loss == -20.0
    assert (perf.parlays, perf.losses, perf.profit) == (1, 1, -20.0)
    assert singles.bets == 0  # legs never count as single bets


def test_parlay_api(database_url: str, session_factory: sessionmaker[Session]) -> None:
    # The API prices "now", so this fixture lives on the real clock.
    real_now = datetime.now(UTC)
    kick = real_now + timedelta(days=2)
    g = seed(session_factory, kick=kick, observed=real_now - timedelta(minutes=5))
    app = create_app(Settings(database_url=database_url))
    app.state.predictor = (real_now, None)  # skip model fitting in tests
    client = TestClient(app, base_url="http://localhost")
    legs = [
        {"game_id": g["g1"], "market": "SPREAD", "selection": "HOME", "line": -3.5},
        {"game_id": g["g2"], "market": "SPREAD", "selection": "HOME", "line": -3.5},
    ]
    body = client.post("/api/parlays/evaluate", json={"sportsbook": DK, "legs": legs}).json()
    assert len(body["legs"]) == 2 and body["correlation_risk"] == "LOW"
    bad = client.post("/api/parlays/evaluate", json={"sportsbook": DK, "legs": legs[:1]})
    assert bad.status_code == 422
    created = client.post("/api/parlays", json={"sportsbook": DK, "legs": legs, "stake": 5})
    assert created.status_code == 201 and len(created.json()["legs"]) == 2
    assert [p["id"] for p in client.get("/api/parlays").json()] == [created.json()["id"]]
    offers = client.get(f"/api/games/{g['g1']}/offers", params={"book": DK}).json()
    assert any(o["main"] for o in offers)
    assert client.get("/api/performance").json()["parlays"]["pending"] == 1
    assert client.post("/api/bets/settle").json() == {"bets": [], "parlays": []}
    day = kick.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    games = client.get("/api/games", params={"date": day, "sport": "NFL"}).json()
    assert len(games) == 2
