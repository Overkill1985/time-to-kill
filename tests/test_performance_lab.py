from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session, sessionmaker

from test_forward import KICK, model, odds
from ttk import betting_math as bm
from ttk.db.models import ForwardScore, Game
from ttk.domain import GameStatus, Selection
from ttk.services import forward_test
from ttk.services.forward_test import ScoredRow, score_finished, scored_rows, snapshot
from ttk.services.performance_lab import (
    calibration,
    lab_choices,
    performance_lab,
    threshold_lab,
    weekly_trend,
)


def row(
    p_home: float,
    margin: float,
    *,
    market: float = 0.5,
    odds: float | None = -110,
    clv: float | None = 0.01,
    kick: datetime = datetime(2026, 10, 6, 0, 0, tzinfo=UTC),
) -> ScoredRow:
    side = Selection.HOME if p_home >= market else Selection.AWAY
    return ScoredRow(
        model="m 1",
        horizon_hours=1,
        game_id=1,
        sport="NFL",
        commence_time=kick,
        home_line=-3.5,
        model_home_cover=p_home,
        market_home_cover=market,
        cover_margin=margin,
        side=side,
        side_odds=odds,
        price_clv=clv,
        points_vs_close=0.5,
    )


def test_threshold_lab_counts_the_models_side_by_its_probability() -> None:
    rows = [
        row(0.53, 3),  # home, won
        row(0.53, -3),  # home, lost
        row(0.45, -3),  # away at 0.55, won
        row(0.58, 0),  # home push
        row(0.61, 5, odds=None),  # no bettable price: never a bet
    ]
    t50, t52, t54, *_ = threshold_lab(rows)
    assert (t50.bets, t50.wins, t50.losses, t50.pushes) == (4, 2, 1, 1)
    win = bm.american_to_decimal(-110) - 1
    assert t50.units == pytest.approx(2 * win - 1)
    assert t50.roi == pytest.approx((2 * win - 1) / 4)
    assert t50.break_even == pytest.approx(1 / bm.american_to_decimal(-110))
    assert t50.hit_rate == pytest.approx(2 / 3) and not t50.enough
    assert (t52.bets, t54.bets) == (4, 2)  # 0.53 is below 54%


def test_calibration_bins_leave_out_pushes() -> None:
    rows = [row(0.52, 3), row(0.53, -3), row(0.54, 0), row(0.70, 2, market=0.68)]
    bins = {(b.low, b.high): b for b in calibration(rows)}
    mid = bins[(0.50, 0.55)]
    assert mid.n == 2 and mid.observed == 0.5
    assert mid.mean_predicted == pytest.approx(0.525)
    assert bins[(0.65, 1.0)].n == 1
    market = {(b.low, b.high): b.n for b in calibration(rows, market=True)}
    assert market[(0.50, 0.55)] == 2 and market[(0.45, 0.50)] == 0  # 0.50 opens its bin


def test_weekly_trend_runs_by_eastern_monday() -> None:
    # Sunday 23:30 Eastern is Monday 03:30 UTC: still the earlier week.
    sunday_night = datetime(2026, 10, 12, 3, 30, tzinfo=UTC)
    rows = [
        row(0.6, 3, clv=0.02, kick=sunday_night),
        row(0.6, -3, clv=-0.04, kick=sunday_night + timedelta(days=1)),
    ]
    w1, w2 = weekly_trend(rows)
    assert str(w1.week) == "2026-10-05" and str(w2.week) == "2026-10-12"
    assert w1.avg_clv == pytest.approx(0.02) and w2.cumulative_clv == pytest.approx(-0.01)
    assert w2.cumulative_diff == pytest.approx(((w1.diff or 0) + (w2.diff or 0)) / 2)


def finished(sf: sessionmaker[Session]) -> int:
    now = KICK - timedelta(hours=20)
    game_id = odds(sf, now - timedelta(minutes=5))
    with sf() as s:
        snapshot(s, [model(s)], now=now)
        game = s.get_one(Game, game_id)
        game.status, game.home_score, game.away_score = GameStatus.FINAL, 30, 20
        s.commit()
    return game_id


def test_finished_snapshots_are_scored_once_and_stored(
    session_factory: sessionmaker[Session],
) -> None:
    finished(session_factory)
    with session_factory() as s:
        replayed = scored_rows(s)
        forward_test._CLV_CACHE.clear()
        assert score_finished(s) == 1
        s.commit()
        assert score_finished(s) == 0
        stored = s.scalars(select(ForwardScore)).one()
        assert (stored.price_clv, stored.points_vs_close) == (
            replayed[0].price_clv,
            replayed[0].points_vs_close,
        )
        assert scored_rows(s) == replayed
        with pytest.raises(DatabaseError, match="append-only"):
            s.execute(text("DELETE FROM forward_scores"))


def test_lab_and_api(database_url: str, session_factory: sessionmaker[Session]) -> None:
    from fastapi.testclient import TestClient

    from ttk.api.app import create_app
    from ttk.config import Settings

    finished(session_factory)
    with session_factory() as s:
        (choice,) = lab_choices(s)
        assert choice == {"model": "cfb-spread-test 1", "horizon_hours": 24, "games": 1}
        lab = performance_lab(s, "cfb-spread-test 1", 24)
    assert lab["thresholds"][0]["bets"] == 1 and lab["thresholds"][0]["wins"] == 1
    assert len(lab["weeks"]) == 1 and "overfit" in lab["note"]

    client = TestClient(
        create_app(Settings(database_url=database_url)), base_url="http://localhost"
    )
    body = client.get("/api/lab").json()
    assert body["lab"]["model"] == "cfb-spread-test 1"
    assert client.get("/api/lab", params={"sport": "NBA"}).json() == {"choices": [], "lab": None}
