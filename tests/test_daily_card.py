from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ttk.api.app import create_app
from ttk.config import Settings
from ttk.db.models import IngestionRun, ModelVersion, Prediction
from ttk.domain import BetClassification, DataQuality, Market, Selection, Sport, Uncertainty
from ttk.models.nfl_features import GameFeatures
from ttk.providers.base import NormalizedGame, NormalizedOddsQuote, OddsFetch, TeamRef
from ttk.qualification import QualificationRules
from ttk.services.daily_card import build_card, card_window
from ttk.services.data_quality import QualityInputs, assess
from ttk.services.nfl_spread_predictor import SpreadView
from ttk.services.odds_ingest import store_odds

DAY = date(2026, 9, 27)
KICK = datetime(2026, 9, 27, 17, 0, tzinfo=UTC)  # 1:00 PM ET
NOW = KICK - timedelta(hours=2)
RULES = QualificationRules()


# --------------------------------------------------------------------------- data quality


def inputs(**kw: object) -> QualityInputs:
    base = QualityInputs(
        odds_age=timedelta(minutes=5),
        books=12,
        schedule_linked=True,
        home_team_games=5,
        away_team_games=5,
        home_starter_known=True,
        away_starter_known=True,
        home_starter_seen=True,
        away_starter_seen=True,
        disagreement_points=1.0,
    )
    return replace(base, **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "quality"),
    [
        ({}, DataQuality.EXCELLENT),
        ({"books": 6}, DataQuality.GOOD),
        ({"books": 4}, DataQuality.ACCEPTABLE),
        ({"home_starter_known": False}, DataQuality.ACCEPTABLE),
        ({"odds_age": timedelta(minutes=45)}, DataQuality.POOR),
        ({"books": 2}, DataQuality.POOR),
        ({"schedule_linked": False}, DataQuality.POOR),
        ({"away_team_games": 1}, DataQuality.POOR),
        ({"odds_age": timedelta(hours=7)}, DataQuality.UNUSABLE),
        ({"odds_age": None, "books": 0}, DataQuality.UNUSABLE),
    ],
)
def test_data_quality(changes: dict[str, object], quality: DataQuality) -> None:
    result = assess(inputs(**changes), max_odds_age=timedelta(minutes=30))
    assert result.data_quality is quality
    if quality is not DataQuality.EXCELLENT:
        assert result.reasons  # every downgrade is explained


@pytest.mark.parametrize(
    ("changes", "uncertainty"),
    [
        ({}, Uncertainty.LOW),
        ({"disagreement_points": -5.0}, Uncertainty.MODERATE),
        ({"away_starter_seen": False}, Uncertainty.HIGH),
        ({"home_starter_known": False}, Uncertainty.HIGH),
        ({"home_team_games": 0}, Uncertainty.HIGH),
        ({"disagreement_points": None}, Uncertainty.INSUFFICIENT_DATA),
    ],
)
def test_uncertainty(changes: dict[str, object], uncertainty: Uncertainty) -> None:
    assert assess(inputs(**changes), max_odds_age=timedelta(minutes=30)).uncertainty is uncertainty


# --------------------------------------------------------------------------- card


class FakePredictor:
    """Stands in for NflSpreadPredictor: fixed P(home covers)."""

    def __init__(self, home_cover: float, status: str = "DEVELOPMENT") -> None:
        self.home_cover = home_cover
        self.status = status
        self.starters: dict[tuple[int, int], str] = {}

    def spread(self, game_id: int, home_line: float, market: float) -> SpreadView:
        f = GameFeatures(game_id, 3.0, 0.0, 0.1, 0.1, 5, 5, 400.0, 400.0)
        return SpreadView(self.home_cover, 0.0, -home_line + 1.0, f, 40.0)

    def ensure_registered(self, session: Session) -> ModelVersion:
        row = session.scalar(select(ModelVersion).where(ModelVersion.name == "fake"))
        if row is None:
            row = ModelVersion(
                name="fake",
                version="1",
                sport="NFL",
                market="SPREAD",
                algorithm="fake",
                status=self.status,
            )
            session.add(row)
            session.flush()
        return row


def seed(
    sf: sessionmaker[Session], *, sport: Sport = Sport.NFL, kick: datetime = KICK, sid: str = "g1"
) -> None:
    game = NormalizedGame(
        "espn", sid, sport, TeamRef(f"Home {sid}"), TeamRef(f"Away {sid}"), kick, None
    )
    quotes = [
        NormalizedOddsQuote("fake", sid, book, book, Market.SPREAD, sel, line, -110, None)
        for book in ("a", "b", "c", "d", "e")
        for sel, line in ((Selection.HOME, -3.5), (Selection.AWAY, 3.5))
    ]
    with sf() as s:
        run = IngestionRun(provider="fake", kind="odds", sport=sport)
        s.add(run)
        s.flush()
        store_odds(
            s,
            OddsFetch([game], quotes, {}),
            run=run,
            observed_at=NOW - timedelta(minutes=5),
            stats=Counter(),
        )
        s.commit()


def card_for(sf: sessionmaker[Session], predictor: FakePredictor, **kw: object):  # type: ignore[no-untyped-def]
    predictor.starters = {}
    with sf() as s:
        from ttk.db.models import Game

        for g in s.scalars(select(Game)):
            predictor.starters[(g.id, g.home_team_id)] = "QB-H"
            predictor.starters[(g.id, g.away_team_id)] = "QB-A"
        return build_card(s, DAY, RULES, predictor=predictor, now=NOW, **kw)  # type: ignore[arg-type]


def test_card_window_is_eastern_6am_to_6am() -> None:
    start, end = card_window(DAY)
    assert start == datetime(2026, 9, 27, 10, 0, tzinfo=UTC)  # 06:00 EDT
    assert end - start == timedelta(days=1)


def test_development_model_never_qualifies(session_factory: sessionmaker[Session]) -> None:
    seed(session_factory)
    card = card_for(session_factory, FakePredictor(0.60))
    home = next(e for e in card.entries if e.selection is Selection.HOME)
    assert home.classification is BetClassification.LEAN
    assert any("model_validated" in n for n in home.notes)
    assert card.headline == "NO QUALIFIED BETS TODAY"
    assert card.entries[0].selection is Selection.HOME  # LEAN sorts before PASS


def test_active_model_can_qualify(session_factory: sessionmaker[Session]) -> None:
    seed(session_factory)
    card = card_for(session_factory, FakePredictor(0.60, status="ACTIVE"))
    home = next(e for e in card.entries if e.selection is Selection.HOME)
    away = next(e for e in card.entries if e.selection is Selection.AWAY)
    assert home.classification is BetClassification.QUALIFIED
    assert home.ev_percent == pytest.approx((0.60 * 100 / 110 - 0.40) * 100)
    assert home.bet.endswith("-3.5") and home.data_quality == "GOOD"
    assert away.classification is BetClassification.PASS  # 40% side
    assert card.headline == "1 QUALIFIED OPPORTUNITIES"
    assert card.by_sport[Sport.NFL].qualified == 1


def test_bettable_books_filter(session_factory: sessionmaker[Session]) -> None:
    seed(session_factory)
    card = card_for(session_factory, FakePredictor(0.60), bettable_books=frozenset({"zzz"}))
    assert card.entries == [] and len(card.unbettable) == 2


def test_persist_writes_snapshots_only_when_asked(session_factory: sessionmaker[Session]) -> None:
    seed(session_factory)
    card_for(session_factory, FakePredictor(0.55), persist=False)
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Prediction)) == 0
    card_for(session_factory, FakePredictor(0.55))
    with session_factory() as s:
        rows = s.scalars(select(Prediction)).all()
    assert {(r.selection, r.line) for r in rows} == {("HOME", -3.5), ("AWAY", 3.5)}
    assert all(r.features and "expected_margin" in r.features for r in rows)


def test_started_and_unmodeled_games(session_factory: sessionmaker[Session]) -> None:
    seed(session_factory, sid="early", kick=NOW - timedelta(minutes=30))  # already started
    seed(session_factory, sport=Sport.CFB, sid="cfb")
    card = card_for(session_factory, FakePredictor(0.6))
    assert card.entries == []
    assert [u.sport for u in card.unmodeled] == [Sport.CFB]
    assert card.by_sport[Sport.NFL].games == 1 and card.by_sport[Sport.NFL].with_market == 0


def test_card_api_is_read_only(database_url: str, session_factory: sessionmaker[Session]) -> None:
    seed(session_factory)
    app = create_app(Settings(database_url=database_url))
    fake = FakePredictor(0.60)
    with session_factory() as s:
        from ttk.db.models import Game

        g = s.scalars(select(Game)).one()
        fake.starters = {(g.id, g.home_team_id): "H", (g.id, g.away_team_id): "A"}
    app.state.predictor = (datetime.now(UTC), fake)
    client = TestClient(app, base_url="http://localhost")
    body = client.get("/api/card", params={"date": "2026-09-27"}).json()
    assert body["headline"] == "NO QUALIFIED BETS TODAY"
    assert body["by_sport"]["NFL"]["games"] == 1
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Prediction)) == 0
        assert s.scalar(select(func.count()).select_from(ModelVersion)) == 0
