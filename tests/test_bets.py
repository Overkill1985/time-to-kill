from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ttk import betting_math as bm
from ttk.api.app import create_app
from ttk.config import Settings
from ttk.db.models import Game, IngestionRun, ModelVersion, Prediction
from ttk.domain import BetResult, GameStatus, Market, Selection, Sport
from ttk.providers.base import NormalizedGame, NormalizedOddsQuote, OddsFetch, TeamRef
from ttk.services.bets import BetError, NewBet, grade, performance, record_bet, settle_bets
from ttk.services.odds_ingest import store_odds

KICK = datetime(2026, 9, 27, 17, 0, tzinfo=UTC)
GAME = NormalizedGame("espn", "g1", Sport.NFL, TeamRef("Home"), TeamRef("Away"), KICK, None)


@pytest.mark.parametrize(
    ("market", "selection", "line", "home", "away", "result"),
    [
        (Market.SPREAD, Selection.HOME, -3.5, 24, 17, BetResult.WIN),
        (Market.SPREAD, Selection.HOME, -3.0, 20, 17, BetResult.PUSH),
        (Market.SPREAD, Selection.AWAY, 3.0, 24, 20, BetResult.LOSS),
        (Market.SPREAD, Selection.AWAY, 3.5, 20, 17, BetResult.WIN),
        (Market.MONEYLINE, Selection.AWAY, None, 17, 20, BetResult.WIN),
        (Market.MONEYLINE, Selection.HOME, None, 20, 20, BetResult.PUSH),
        (Market.TOTAL, Selection.OVER, 44.5, 24, 21, BetResult.WIN),
        (Market.TOTAL, Selection.UNDER, 45.0, 24, 21, BetResult.PUSH),
        (Market.TOTAL, Selection.UNDER, 44.5, 24, 21, BetResult.LOSS),
    ],
)
def test_grade(
    market: Market,
    selection: Selection,
    line: float | None,
    home: int,
    away: int,
    result: BetResult,
) -> None:
    assert grade(market, selection, line, home, away) is result


def poll(
    sf: sessionmaker[Session],
    at: datetime,
    home_price: float,
    away_price: float,
    line: float = -3.5,
) -> None:
    quotes = [
        NormalizedOddsQuote("fake", "g1", book, book, Market.SPREAD, sel, ln, price, None)
        for book in ("draftkings", "fanduel", "betmgm")
        for sel, ln, price in (
            (Selection.HOME, line, home_price),
            (Selection.AWAY, -line, away_price),
        )
    ]
    with sf() as s:
        run = IngestionRun(provider="fake", kind="odds", sport=Sport.NFL)
        s.add(run)
        s.flush()
        store_odds(s, OddsFetch([GAME], quotes, {}), run=run, observed_at=at, stats=Counter())
        s.commit()


def game_id(sf: sessionmaker[Session]) -> int:
    with sf() as s:
        return int(s.scalars(select(Game.id)).one())


def bet(gid: int, **kw: object) -> NewBet:
    base = dict(
        game_id=gid,
        market=Market.SPREAD,
        selection=Selection.HOME,
        line=-3.5,
        american_odds=-110,
        sportsbook="draftkings",
        stake=100.0,
        placed_at=KICK - timedelta(hours=3),
    )
    base.update(kw)
    return NewBet(**base)  # type: ignore[arg-type]


def add_prediction(
    sf: sessionmaker[Session], gid: int, created: datetime, p: float, line: float = -3.5
) -> None:
    with sf() as s:
        mv = s.scalar(select(ModelVersion)) or ModelVersion(
            name="m", version="1", sport="NFL", market="SPREAD", algorithm="x"
        )
        s.add(mv)
        s.flush()
        s.add(
            Prediction(
                game_id=gid,
                model_version_id=mv.id,
                market="SPREAD",
                selection="HOME",
                line=line,
                probability=p,
                push_probability=0.0,
                uncertainty="LOW",
                data_quality="GOOD",
                inputs_as_of=created,
                created_at=created,
            )
        )
        s.commit()


def test_record_captures_beliefs_as_of_bet_time(session_factory: sessionmaker[Session]) -> None:
    poll(session_factory, KICK - timedelta(hours=5), -110, -110)  # 50/50 at bet time
    poll(session_factory, KICK - timedelta(hours=1), -130, 110)  # moved after the bet
    gid = game_id(session_factory)
    add_prediction(session_factory, gid, KICK - timedelta(hours=4), 0.58)
    add_prediction(session_factory, gid, KICK - timedelta(hours=4), 0.99, line=-7.0)  # other line
    add_prediction(session_factory, gid, KICK - timedelta(hours=2), 0.70)  # after the bet
    with session_factory() as s:
        b = record_bet(s, bet(gid))
    assert b.market_probability == pytest.approx(0.5)
    assert b.model_probability == pytest.approx(0.58)
    assert b.edge == pytest.approx(0.08)
    assert b.expected_value == pytest.approx(0.58 * 100 / 110 - 0.42)
    assert b.description == "Home -3.5 (Away @ Home)" and b.result == BetResult.PENDING


def test_record_without_model_or_market(session_factory: sessionmaker[Session]) -> None:
    poll(session_factory, KICK - timedelta(hours=5), -110, -110)
    gid = game_id(session_factory)
    with session_factory() as s:
        b = record_bet(s, bet(gid, line=-2.5))  # nobody quoted -2.5
    assert b.market_probability is None and b.model_probability is None and b.edge is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"placed_at": KICK}, "after kickoff"),
        ({"sportsbook": "nope"}, "Unknown sportsbook"),
        ({"line": None}, "need a line"),
        ({"selection": Selection.OVER}, "not a side"),
        ({"american_odds": 50}, "American odds"),
        ({"stake": 0}, "Stake"),
        ({"game_id": 999}, "not found"),
    ],
)
def test_record_rejections(
    session_factory: sessionmaker[Session], changes: dict[str, object], message: str
) -> None:
    poll(session_factory, KICK - timedelta(hours=5), -110, -110)
    gid = game_id(session_factory)
    with session_factory() as s, pytest.raises(BetError, match=message):
        record_bet(s, bet(gid, **changes))


def finish(
    sf: sessionmaker[Session], status: GameStatus, home: int | None = None, away: int | None = None
) -> None:
    with sf() as s:
        g = s.scalars(select(Game)).one()
        g.status, g.home_score, g.away_score = status, home, away
        s.commit()


def test_settle_final_with_clv(session_factory: sessionmaker[Session]) -> None:
    poll(session_factory, KICK - timedelta(hours=5), -110, -110)
    poll(session_factory, KICK - timedelta(minutes=10), -130, 110)  # the close
    gid = game_id(session_factory)
    with session_factory() as s:
        record_bet(s, bet(gid))
        s.commit()
    finish(session_factory, GameStatus.FINAL, 27, 20)
    with session_factory() as s:
        (b,) = settle_bets(s)
        s.commit()
    close = bm.no_vig_probabilities(
        [bm.american_to_decimal(-130), bm.american_to_decimal(110)]
    ).probabilities[0]
    assert b.result == BetResult.WIN and b.profit_loss == pytest.approx(100 * 100 / 110)
    assert b.closing_no_vig_probability == pytest.approx(close)
    assert b.clv == pytest.approx(bm.american_to_decimal(-110) * close - 1)
    assert b.clv is not None and b.clv > 0  # bet at -110 before the move to -130
    assert b.closing_points_gained == 0.0 and b.settled_at is not None


def test_settle_void_and_pending(session_factory: sessionmaker[Session]) -> None:
    poll(session_factory, KICK - timedelta(hours=5), -110, -110)
    gid = game_id(session_factory)
    with session_factory() as s:
        record_bet(s, bet(gid))
        s.commit()
    finish(session_factory, GameStatus.POSTPONED)
    with session_factory() as s:
        assert settle_bets(s) == []
    finish(session_factory, GameStatus.CANCELED)
    with session_factory() as s:
        (b,) = settle_bets(s)
    assert b.result == BetResult.VOID and b.profit_loss == 0.0


def test_performance(session_factory: sessionmaker[Session]) -> None:
    poll(session_factory, KICK - timedelta(hours=5), -110, -110)
    gid = game_id(session_factory)
    with session_factory() as s:
        for i, (sel, line) in enumerate(
            [(Selection.HOME, -3.5), (Selection.AWAY, 3.5), (Selection.HOME, -7.0)]
        ):
            record_bet(
                s,
                bet(gid, selection=sel, line=line, placed_at=KICK - timedelta(hours=3, minutes=i)),
            )
        s.commit()
    finish(session_factory, GameStatus.FINAL, 27, 20)  # margin 7: -3.5 wins, +3.5 loses, -7 push
    with session_factory() as s:
        settle_bets(s)
        s.commit()
        p = performance(s, unit_size=100.0)
    assert (p.bets, p.wins, p.losses, p.pushes, p.pending) == (3, 1, 1, 1, 0)
    assert p.profit == pytest.approx(100 * 100 / 110 - 100)
    assert p.roi == pytest.approx(p.profit / 300)
    assert p.hit_rate == 0.5 and p.units == pytest.approx(p.profit / 100)
    assert p.max_drawdown <= 0


def test_bets_api(database_url: str, session_factory: sessionmaker[Session]) -> None:
    poll(session_factory, KICK - timedelta(hours=5), -110, -110)
    gid = game_id(session_factory)
    client = TestClient(
        create_app(Settings(database_url=database_url)), base_url="http://localhost"
    )
    body = {
        "game_id": gid,
        "market": "SPREAD",
        "selection": "HOME",
        "line": -3.5,
        "american_odds": -110,
        "sportsbook": "draftkings",
        "stake": 50,
        "placed_at": (KICK - timedelta(hours=2)).isoformat(),
    }
    created = client.post("/api/bets", json=body)
    assert created.status_code == 201 and created.json()["result"] == "PENDING"
    late = client.post("/api/bets", json={**body, "placed_at": KICK.isoformat()})
    assert late.status_code == 422 and "kickoff" in late.json()["detail"]
    bid = created.json()["id"]
    voided = client.patch(f"/api/bets/{bid}", json={"void": True, "notes": "book voided"})
    assert voided.json()["result"] == "VOID" and voided.json()["notes"] == "book voided"
    assert client.patch(f"/api/bets/{bid}", json={"void": True}).status_code == 409
    assert [b["id"] for b in client.get("/api/bets", params={"status": "settled"}).json()] == [bid]
    assert client.get("/api/performance").json()["voids"] == 1
    books = {b["key"] for b in client.get("/api/sportsbooks").json()}
    assert {"draftkings", "fanduel", "betmgm"} <= books
