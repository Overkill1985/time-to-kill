from collections import Counter
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from test_forward import KICK
from ttk import betting_math as bm
from ttk.db.models import ForwardPrediction, Game, GameSourceId, IngestionRun
from ttk.domain import GameStatus, Market, Selection, Sport
from ttk.providers.base import NormalizedGame, NormalizedOddsQuote, OddsFetch, TeamRef
from ttk.research.moneyline import no_vig
from ttk.services.forward_test import (
    ForwardModel,
    ModelView,
    forward_report,
    register,
    score_finished,
    scored_rows,
    snapshot,
)
from ttk.services.odds_ingest import store_odds


def odds(sf: sessionmaker[Session], observed: datetime, *, moneyline: bool = True) -> int:
    game = NormalizedGame("espn", "g1", Sport.NCAAB, TeamRef("Duke"), TeamRef("UNC"), KICK, None)

    def q(market: Market, sel: Selection, line: float | None, price: float, book: str):  # type: ignore[no-untyped-def]
        return NormalizedOddsQuote("fake", "g1", book, book, market, sel, line, price, None)

    quotes = [
        q(Market.SPREAD, Selection.HOME, -4.5, -110, "draftkings"),
        q(Market.SPREAD, Selection.AWAY, 4.5, -110, "draftkings"),
    ]
    if moneyline:
        quotes += [
            q(Market.MONEYLINE, Selection.HOME, None, -200, "draftkings"),
            q(Market.MONEYLINE, Selection.AWAY, None, 170, "draftkings"),
            q(Market.MONEYLINE, Selection.HOME, None, -190, "fanduel"),
            q(Market.MONEYLINE, Selection.AWAY, None, 160, "fanduel"),
        ]
    with sf() as s:
        run = IngestionRun(provider="fake", kind="odds", sport=Sport.NCAAB)
        s.add(run)
        s.flush()
        store_odds(s, OddsFetch([game], quotes, {}), run=run, observed_at=observed, stats=Counter())
        s.commit()
        gid = s.scalar(select(GameSourceId.game_id).where(GameSourceId.source_identifier == "g1"))
        assert gid is not None
        return gid


def ml_model(session: Session, p_home: float, seen: list[tuple[float, float]]) -> ForwardModel:
    row = register(
        session,
        name="ncaab-ml-test",
        version="1",
        sport=Sport.NCAAB,
        algorithm="test",
        features=["x"],
        artifact={"market": "MONEYLINE"},
        training_window="2017-2022",
        validation_window="2023-2024",
        log_loss=0.54,
        brier=None,
    )

    def view(game_id: int, line: float, market: float, at: datetime, h: int) -> ModelView:
        seen.append((line, market))
        return ModelView(p_home, 0.0, 6.0, {"x": 1.0})

    return ForwardModel(row, Sport.NCAAB, view, KICK, market=Market.MONEYLINE)


def test_moneyline_snapshot_records_the_moneyline_market(
    session_factory: sessionmaker[Session],
) -> None:
    now = KICK - timedelta(hours=20)
    gid = odds(session_factory, now - timedelta(minutes=5))
    seen: list[tuple[float, float]] = []
    with session_factory() as s:
        stats = snapshot(s, [ml_model(s, 0.75, seen)], now=now)
        s.commit()
        fp = s.scalars(select(ForwardPrediction)).one()
    assert stats.written == 1
    ((line, market),) = seen  # centered on the main spread and its market
    assert line == -4.5 and market == pytest.approx(0.5)
    assert fp.market == "MONEYLINE" and fp.home_line == -4.5
    consensus = (no_vig(-200, 170) + no_vig(-190, 160)) / 2  # type: ignore[operator]
    assert fp.market_home_cover == pytest.approx(consensus, abs=0.01)
    assert fp.best_home_odds == pytest.approx(-190) and fp.best_home_book == "fanduel"
    assert fp.best_away_odds == pytest.approx(170) and fp.best_away_book == "draftkings"

    with session_factory() as s:
        game = s.get_one(Game, gid)
        game.status, game.home_score, game.away_score = GameStatus.FINAL, 70, 68  # won by 2
        s.commit()
        (row,) = scored_rows(s)
        assert row.market is Market.MONEYLINE
        assert row.cover_margin == 2 and row.won  # the spread (-4.5) did not cover
        (score,) = forward_report(s)
        assert score.bets[0].wins == 1
        assert score.bets[0].units == pytest.approx(bm.american_to_decimal(-190) - 1)
        assert score_finished(s) == 1


def test_moneyline_model_needs_a_moneyline_market(session_factory: sessionmaker[Session]) -> None:
    now = KICK - timedelta(hours=20)
    odds(session_factory, now - timedelta(minutes=5), moneyline=False)
    with session_factory() as s:
        stats = snapshot(s, [ml_model(s, 0.75, [])], now=now)
    assert stats.written == 0 and stats.skipped["no two-sided moneyline"] == 1
