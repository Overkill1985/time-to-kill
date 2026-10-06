from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from test_forward import KICK
from ttk import betting_math as bm
from ttk.db.models import ForwardPrediction, Game, GameSourceId, IngestionRun
from ttk.domain import GameStatus, Market, Selection, Sport
from ttk.models.anchored import MarketAnchoredModel
from ttk.models.elo import EloGame
from ttk.providers.base import NormalizedGame, NormalizedOddsQuote, OddsFetch, TeamRef
from ttk.research.totals import (
    Calibration,
    FrozenTotals,
    TeamGame,
    TotalsBacktest,
    TotalsParams,
    freeze_totals,
    p_over,
)
from ttk.services.forward_test import (
    ForwardModel,
    ModelView,
    forward_report,
    register,
    scored_rows,
    snapshot,
)
from ttk.services.odds_ingest import store_odds


def odds(sf: sessionmaker[Session], observed: datetime) -> int:
    game = NormalizedGame("espn", "g1", Sport.NCAAB, TeamRef("Duke"), TeamRef("UNC"), KICK, None)

    def q(sel: Selection, line: float, price: float, book: str) -> NormalizedOddsQuote:
        return NormalizedOddsQuote("fake", "g1", book, book, Market.TOTAL, sel, line, price, None)

    quotes = [
        q(Selection.OVER, 145.5, -110, "draftkings"),
        q(Selection.UNDER, 145.5, -110, "draftkings"),
        q(Selection.OVER, 145.5, -105, "fanduel"),
        q(Selection.UNDER, 145.5, -115, "fanduel"),
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


def totals_model(session: Session, p_over_: float, seen: list[Any]) -> ForwardModel:
    row = register(
        session,
        name="ncaab-total-test",
        version="1",
        sport=Sport.NCAAB,
        algorithm="test",
        features=["x"],
        artifact={"market": "TOTAL"},
        training_window="2017-2022",
        validation_window="2023-2024",
        log_loss=None,
        brier=None,
    )

    def view(game_id: int, line: float, market: float, at: datetime, h: int) -> ModelView:
        seen.append((line, market))
        return ModelView(p_over_, 0.0, 150.0, {})

    return ForwardModel(row, Sport.NCAAB, view, KICK, market=Market.TOTAL)


def test_totals_snapshot_needs_no_spread_and_scores_the_total(
    session_factory: sessionmaker[Session],
) -> None:
    now = KICK - timedelta(hours=20)
    gid = odds(session_factory, now - timedelta(minutes=5))  # totals only
    seen: list[Any] = []
    with session_factory() as s:
        stats = snapshot(s, [totals_model(s, 0.6, seen)], now=now)
        s.commit()
        fp = s.scalars(select(ForwardPrediction)).one()
    assert stats.written == 1 and fp.market == "TOTAL" and fp.home_line == 145.5
    assert seen[0][0] == 145.5 and seen[0][1] == pytest.approx(fp.market_home_cover)
    assert fp.best_home_odds == pytest.approx(-105) and fp.best_home_book == "fanduel"  # over
    assert fp.best_away_odds == pytest.approx(-110) and fp.best_away_book == "draftkings"
    with session_factory() as s:
        game = s.get_one(Game, gid)
        game.status, game.home_score, game.away_score = GameStatus.FINAL, 80, 70  # 150: over
        s.commit()
        (row,) = scored_rows(s)
        assert row.cover_margin == pytest.approx(4.5) and row.won
        (score,) = forward_report(s)
    assert score.bets[0].units == pytest.approx(bm.american_to_decimal(-105) - 1)


def test_frozen_totals_round_trip_and_min_games() -> None:
    t0 = datetime(2024, 11, 5, tzinfo=UTC)
    games = [
        EloGame(i, 2025, t0 + timedelta(days=i), 1 + i % 2, 2 - i % 2, False, 75, 70)
        for i in range(8)
    ]
    team_games = [TeamGame(i, t, 70.0, 72.0) for i in range(7) for t in (1, 2)]
    params = TotalsParams(half_life=10.0, carryover=0.5)
    cal = Calibration(-5.0, 1.04, 17.0)
    anchored = MarketAnchoredModel(0.0, 1.0, 0.02, 1000)
    result: Any = TotalsBacktest(params, cal, anchored, None, None, [])  # type: ignore[arg-type]
    artifact = freeze_totals(result, "NCAAB", {"train": [2017, 2022]})
    frozen = FrozenTotals(artifact, games, team_games)
    assert frozen.view(3, 145.5, 0.5) is None  # under 5 games of history each
    v = frozen.view(7, 145.5, 0.5)
    assert v is not None
    raw = frozen.predictions[7].total
    assert v.predicted == pytest.approx(cal.points(raw))
    assert v.over_standalone == pytest.approx(p_over(cal, raw, 145.5))
    assert v.push == 0.0 and frozen.view(7, 146.0, 0.5).push > 0  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="version"):
        FrozenTotals({**artifact, "artifact_version": 99}, games, team_games)
