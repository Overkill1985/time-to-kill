from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from ttk.api.app import create_app
from ttk.config import Settings
from ttk.db.models import Game, IngestionRun, Team


def test_status_reports_collection_models_and_upcoming(
    database_url: str, session_factory: sessionmaker[Session]
) -> None:
    now = datetime.now(UTC)
    with session_factory() as s:
        home, away = (
            Team(sport="NBA", name="Home", espn_id="1"),
            Team(sport="NBA", name="Away", espn_id="2"),
        )
        s.add_all([home, away])
        s.flush()
        s.add_all(
            [
                Game(
                    sport="NBA",
                    home_team_id=home.id,
                    away_team_id=away.id,
                    commence_time=now + timedelta(hours=5),
                    season_type="REG",
                ),
                Game(
                    sport="NBA",
                    home_team_id=away.id,
                    away_team_id=home.id,
                    commence_time=now + timedelta(hours=2),
                    season_type="PRE",
                ),
                IngestionRun(
                    provider="propline",
                    kind="odds",
                    sport="NBA",
                    status="SUCCESS",
                    finished_at=now - timedelta(minutes=10),
                ),
            ]
        )
        s.commit()
    client = TestClient(
        create_app(Settings(database_url=database_url)), base_url="http://localhost"
    )
    body = client.get("/api/status").json()
    assert 9 < body["collection"]["minutes_since_poll"] < 11
    assert body["collection"]["alerts"] == []
    nba = next(u for u in body["upcoming"] if u["sport"] == "NBA")
    assert nba["next_24h"] == 1  # the preseason game doesn't count
    assert body["models"] == [] and body["min_decided"] == 30
    assert body["bankroll"]["configured"] is False


def test_card_says_which_sports_are_loading(database_url: str) -> None:
    app = create_app(Settings(database_url=database_url))
    app.state.predictor = (datetime.now(UTC), None)
    app.state.card_models = (None, {})
    body = (
        TestClient(app, base_url="http://localhost")
        .get("/api/card", params={"date": "2026-09-27"})
        .json()
    )
    assert body["loading"] == []
