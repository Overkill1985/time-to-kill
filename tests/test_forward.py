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

    def view(
        game_id: int, home_line: float, market: float, at: datetime, horizon: int
    ) -> ModelView:
        return ModelView(p_home, 0.01, 6.0, {"x": 1.0, "line_seen": home_line, "h": horizon})

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


def test_forward_api(database_url: str, session_factory: sessionmaker[Session]) -> None:
    from fastapi.testclient import TestClient

    from ttk.api.app import create_app
    from ttk.config import Settings

    now = KICK - timedelta(hours=20)
    game_id = odds(session_factory, now - timedelta(minutes=5))
    with session_factory() as s:
        snapshot(s, [model(s)], now=now)
        game = s.get_one(Game, game_id)
        game.status, game.home_score, game.away_score = GameStatus.FINAL, 30, 20
        s.commit()
    app = create_app(Settings(database_url=database_url))
    app.state.predictor = (now, None)  # skip model fitting in tests
    client = TestClient(app, base_url="http://localhost")
    body = client.get("/api/forward").json()
    assert body["models"] == [{"model": "cfb-spread-test 1", "snapshots": 1, "finished": 1}]
    (score,) = body["scores"]
    assert (score["horizon_hours"], score["decided"]) == (24, 1)
    assert score["bets"][1]["wins"] == 1  # edge 0.10 clears the 2% threshold
    (row,) = body["recent"]
    assert row["matchup"] == "Auburn @ Alabama" and row["result"] == "home"
    assert row["edge"] == pytest.approx(0.60 - row["market_home_cover"])
    assert "DEVELOPMENT" in body["note"]
    assert client.get("/api/forward", params={"sport": "NBA"}).json()["recent"] == []


def test_closing_snapshot_matches_closing_line_value(
    session_factory: sessionmaker[Session],
) -> None:
    from ttk.services.line_history import closing_line_value, closing_snapshot

    game_id = odds(session_factory, KICK - timedelta(hours=20))
    odds(session_factory, KICK - timedelta(minutes=30), home_line=-4.5)  # the close moved
    with session_factory() as s:
        snap = closing_snapshot(s, game_id, Market.SPREAD)
        for side, line, price in (
            (Selection.HOME, -3.5, -105),  # bet line no longer at the close: no price CLV
            (Selection.HOME, -4.5, -110),
            (Selection.AWAY, 4.5, -115),
        ):
            old = closing_line_value(s, game_id, Market.SPREAD, side, line, price)
            assert snap.price_clv(side, line, price) == old.price_clv
            assert snap.points_gained(side, line) == old.points_gained
        assert snap.price_clv(Selection.HOME, -3.5, -105) is None
        assert snap.points_gained(Selection.HOME, -3.5) == pytest.approx(1.0)


def test_a_model_with_its_own_long_horizon_is_snapshot_at_first_sight(
    session_factory: sessionmaker[Session],
) -> None:
    from dataclasses import replace

    now = KICK - timedelta(hours=100)  # outside 24 h, inside a week
    odds(session_factory, now - timedelta(minutes=5))
    with session_factory() as s:
        default = model(s)
        opener = replace(default, horizons=(168,))
        stats = snapshot(s, [default], now=now)
        assert stats.written == 0  # the default horizons (24, 1) aren't due yet
        stats = snapshot(s, [opener], now=now)
        s.commit()
        assert stats.written == 1
        fp = s.scalars(select(ForwardPrediction)).one()
        assert fp.horizon_hours == 168
        # Once only: later passes inside the week add nothing for this horizon.
        assert snapshot(s, [opener], now=now + timedelta(hours=1)).written == 0


def test_registering_without_an_artifact_stores_sql_null(
    session_factory: sessionmaker[Session],
) -> None:
    from ttk.db.models import ModelVersion

    with session_factory() as s:
        register(
            s,
            name="nfl-spread-test-opener",
            version="1",
            sport=Sport.NFL,
            algorithm="test",
            features=["x"],
            artifact=None,
            training_window="2002-2017",
            validation_window="2018-2021",
            log_loss=None,
            brier=None,
        )
        s.commit()
        frozen = s.scalars(select(ModelVersion).where(ModelVersion.artifact.is_not(None))).all()
        assert frozen == []


def test_register_opener_copies_a_key_spread_artifact_with_its_horizon(
    session_factory: sessionmaker[Session],
) -> None:
    from ttk.services.forward_models import register_opener

    def base(name: str, artifact: dict[str, object]) -> None:
        register(
            s,
            name=name,
            version="1",
            sport=Sport.NBA,
            algorithm="test",
            features=["x"],
            artifact=artifact,
            training_window="2019-2022",
            validation_window="2023-2024",
            log_loss=None,
            brier=None,
        )

    with session_factory() as s:
        base("nba-spread-key-test", {"artifact_version": 1, "variant": "key"})
        base("nba-spread-anchored-test", {"artifact_version": 1, "variant": "anchored"})
        base(
            "nba-spread-key-injury-test",
            {"artifact_version": 1, "variant": "key", "live_substitution": {"x": "y"}},
        )
        row = register_opener(s, "nba-spread-key-test", 48)
        assert (row.name, row.version) == ("nba-spread-key-test-opener", "1")
        assert row.artifact == {"artifact_version": 1, "variant": "key", "opener_hours": 48}
        assert register_opener(s, "nba-spread-key-test", 48).id == row.id  # idempotent
        for bad in ("nba-spread-anchored-test", "nba-spread-key-injury-test", "missing"):
            with pytest.raises(ValueError):
                register_opener(s, bad, 48)


def test_snapshot_writes_once_at_the_end_not_row_by_row(
    session_factory: sessionmaker[Session],
) -> None:
    # Autoflushing each row took SQLite's write lock for the whole pass and failed
    # the collector's odds writes (2026-10-20..23): rows wait in the session.
    now = KICK - timedelta(hours=20)
    odds(session_factory, now - timedelta(minutes=5))
    pending_seen: list[int] = []
    with session_factory() as s:
        first = model(s)
        row = register(
            s,
            name="cfb-spread-test-2",
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
        s.commit()

        def view(
            game_id: int, home_line: float, market: float, at: datetime, horizon: int
        ) -> ModelView:
            pending_seen.append(sum(isinstance(o, ForwardPrediction) for o in s.new))
            return ModelView(0.55, 0.01, 4.0, {"x": 1.0})

        second = ForwardModel(row, Sport.CFB, view, KICK - timedelta(days=7))
        stats = snapshot(s, [first, second], now=now)
        assert stats.written == 2 and pending_seen == [1]  # the first row not yet written
        assert not s.new  # both written by the final flush
