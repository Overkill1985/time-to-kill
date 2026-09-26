from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from ttk.api.app import create_app
from ttk.config import Settings
from ttk.domain import Market, Selection, Sport
from ttk.providers.base import NormalizedGame, NormalizedOddsQuote, OddsFetch, TeamRef
from ttk.services.odds_ingest import run_odds_ingestion


class OneShot:
    name = "fake"

    def __init__(self, fetch: OddsFetch) -> None:
        self._fetch = fetch

    def fetch_odds(self, sport: Sport) -> OddsFetch:
        return self._fetch


@pytest.fixture
def client(database_url: str) -> TestClient:
    return TestClient(create_app(Settings(database_url=database_url)), base_url="http://localhost")


def seed(session_factory: sessionmaker[Session], kickoff: datetime) -> None:
    game = NormalizedGame(
        "fake",
        "g1",
        Sport.NFL,
        TeamRef("Baltimore Ravens"),
        TeamRef("Cleveland Browns"),
        kickoff,
        None,
    )
    quotes = [
        NormalizedOddsQuote("fake", "g1", "a", "Book A", m, sel, line, price, None)
        for m, sel, line, price in [
            (Market.SPREAD, Selection.HOME, -2.5, -105),
            (Market.SPREAD, Selection.AWAY, 2.5, -115),
        ]
    ]
    run_odds_ingestion(
        session_factory,
        OneShot(OddsFetch([game], quotes, {})),
        Sport.NFL,
    )


def test_health(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body == {"status": "ok", "last_ingestion": None}


def test_games_and_market(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    seed(session_factory, datetime.now(UTC) + timedelta(days=1))
    games = client.get("/api/games", params={"sport": "NFL"}).json()
    assert [(g["away_team"], g["home_team"]) for g in games] == [
        ("Cleveland Browns", "Baltimore Ravens")
    ]

    market = client.get(f"/api/games/{games[0]['id']}/market").json()
    home = next(m for m in market if m["selection"] == "HOME")
    assert home["line"] == -2.5
    implied_home, implied_away = 105 / 205, 115 / 215
    assert home["best"] == {
        "sportsbook": "a",
        "american_odds": "-105",
        "no_vig_probability": pytest.approx(implied_home / (implied_home + implied_away)),
    }
    assert home["odds_current"] is True
    assert client.get("/api/games/999/market").status_code == 404


def test_started_games_hidden_by_default(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    seed(session_factory, datetime.now(UTC) - timedelta(hours=1))
    assert client.get("/api/games").json() == []
    assert len(client.get("/api/games", params={"include_started": True}).json()) == 1


def test_evaluate(client: TestClient) -> None:
    body = client.post(
        "/api/math/evaluate",
        json={"model_probability": 0.587, "american_odds": -105, "no_vig_probability": 0.532},
    ).json()
    assert body["edge"] == pytest.approx(0.055)
    assert body["ev_percent"] == pytest.approx((0.587 * 100 / 105 - 0.413) * 100)
    assert body["fair_american_odds"] == "-142"


def test_evaluate_rejects_bad_odds(client: TestClient) -> None:
    r = client.post("/api/math/evaluate", json={"model_probability": 0.5, "american_odds": 50})
    assert r.status_code == 422


def test_rejects_foreign_host(database_url: str) -> None:
    app = create_app(Settings(database_url=database_url))
    evil = TestClient(app, base_url="http://attacker.example")
    assert evil.get("/api/health").status_code == 400
