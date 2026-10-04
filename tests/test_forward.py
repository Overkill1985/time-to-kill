import math
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session, sessionmaker

from ttk import betting_math as bm
from ttk.db.models import ForwardPrediction, Game, GameSourceId, IngestionRun
from ttk.domain import GameStatus, Market, Selection, Sport
from ttk.providers.base import NormalizedGame, NormalizedOddsQuote, OddsFetch, TeamRef
from ttk.services.forward_test import (
    ForwardModel,
    ModelView,
    due_horizon,
    forward_report,
    register,
    snapshot,
)
from ttk.services.odds_ingest import store_odds

KICK = datetime(2026, 10, 10, 19, 30, tzinfo=UTC)


def q(line: float, sel: Selection, price: float, book: str) -> NormalizedOddsQuote:
    return NormalizedOddsQuote("fake", "g1", book, book, Market.SPREAD, sel, line, price, None)


def odds(sf: sessionmaker[Session], observed: datetime, home_line: float = -3.5) -> int:
    game = NormalizedGame(
        "espn", "g1", Sport.CFB, TeamRef("Alabama"), TeamRef("Auburn"), KICK, None
    )
    quotes = [
        q(home_line, Selection.HOME, -110, "draftkings"),
        q(-home_line, Selection.AWAY, -110, "draftkings"),
        q(home_line, Selection.HOME, -105, "fanduel"),
        q(-home_line, Selection.AWAY, -115, "fanduel"),
    ]
    with sf() as s:
        run = IngestionRun(provider="fake", kind="odds", sport=Sport.CFB)
        s.add(run)
        s.flush()
        store_odds(s, OddsFetch([game], quotes, {}), run=run, observed_at=observed, stats=Counter())
        s.commit()
        game_id = s.scalar(
            select(GameSourceId.game_id).where(GameSourceId.source_identifier == "g1")
        )
        assert game_id is not None
        return game_id


def model(session: Session, p_home: float = 0.60) -> ForwardModel:
    row = register(
        session,
        name="cfb-spread-test",
        version="1",
        sport=Sport.CFB,
        algorithm="test",
        features=["x"],
        artifact={"artifact_version": 1},
        training_window="2015-2022",
        validation_window="2023-2024",
        log_loss=0.69,
        brier=0.25,
    )

    def view(game_id: int, home_line: float, market: float) -> ModelView:
        return ModelView(p_home, 0.01, 6.0, {"x": 1.0, "line_seen": home_line})

    return ForwardModel(row, Sport.CFB, view, KICK - timedelta(days=7))


def test_due_horizon() -> None:
    assert due_horizon(KICK, KICK - timedelta(hours=30)) is None  # not inside 24 h yet
    assert due_horizon(KICK, KICK - timedelta(hours=20)) == 24
    assert due_horizon(KICK, KICK - timedelta(minutes=40)) == 1
    assert due_horizon(KICK, KICK + timedelta(minutes=1)) is None  # started


def test_snapshot_once_per_horizon_with_fresh_odds_only(
    session_factory: sessionmaker[Session],
) -> None:
    now = KICK - timedelta(hours=20)
    game_id = odds(session_factory, now - timedelta(minutes=5))
    with session_factory() as s:
        m = model(s)
        first = snapshot(s, [m], now=now)
        again = snapshot(s, [m], now=now + timedelta(minutes=30))
        s.commit()
        assert (first.written, again.written) == (1, 0)
        fp = s.scalars(select(ForwardPrediction)).one()
        assert (fp.game_id, fp.horizon_hours, fp.home_line) == (game_id, 24, -3.5)
        assert fp.home_cover_probability == 0.60 and fp.books == 2
        no_vig_dk = 0.5
        no_vig_fd = bm.no_vig_probabilities(
            [bm.american_to_decimal(-105), bm.american_to_decimal(-115)]
        ).probabilities[0]
        assert fp.market_home_cover == pytest.approx((no_vig_dk + no_vig_fd) / 2)
        assert fp.best_home_odds == pytest.approx(-105) and fp.best_home_book == "fanduel"
        assert fp.best_away_odds == pytest.approx(-110) and fp.best_away_book == "draftkings"
        # An hour before kickoff, the last odds are 19 hours old: not a real market.
        late = snapshot(s, [m], now=KICK - timedelta(minutes=30))
        assert late.written == 0 and late.skipped["odds older than 2 hours"] == 1
    odds(session_factory, KICK - timedelta(minutes=40), home_line=-4.5)  # the line moved
    with session_factory() as s:
        late = snapshot(s, [model(s)], now=KICK - timedelta(minutes=30))
        s.commit()
        assert late.written == 1
        rows = s.scalars(select(ForwardPrediction).order_by(ForwardPrediction.horizon_hours)).all()
        assert [(r.horizon_hours, r.home_line) for r in rows] == [(1, -4.5), (24, -3.5)]


def test_forward_predictions_are_append_only(session_factory: sessionmaker[Session]) -> None:
    now = KICK - timedelta(hours=20)
    odds(session_factory, now - timedelta(minutes=5))
    with session_factory() as s:
        snapshot(s, [model(s)], now=now)
        s.commit()
        row = s.scalars(select(ForwardPrediction)).one()
        row.home_cover_probability = 0.99
        with pytest.raises(DatabaseError):
            s.commit()


def test_report_scores_finished_games(session_factory: sessionmaker[Session]) -> None:
    now = KICK - timedelta(hours=20)
    game_id = odds(session_factory, now - timedelta(minutes=5))
    with session_factory() as s:
        snapshot(s, [model(s)], now=now)
        game = s.get_one(Game, game_id)
        assert forward_report(s) == []  # not final yet
        game.status, game.home_score, game.away_score = GameStatus.FINAL, 30, 20
        s.commit()
        (score,) = forward_report(s, Sport.CFB)
    # Home -3.5 won by 10: covered. Model 0.60 vs market ~0.5.
    assert score.decided == 1 and score.horizon_hours == 24
    assert score.model_log_loss == pytest.approx(-math.log(0.60))
    assert score.paired_diff is not None and score.paired_diff < 0  # the model did better
    bet = score.bets[0]
    assert (bet.bets, bet.wins, bet.losses) == (1, 1, 0)
    assert bet.units == pytest.approx(bm.american_to_decimal(-105) - 1.0)  # best price, fanduel
