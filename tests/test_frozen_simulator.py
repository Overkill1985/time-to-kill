from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from ttk.domain import Sport
from ttk.models.margin import KeyNumberMarginModel, MarginModel
from ttk.models.simulation import Leg
from ttk.research.espn_simulation import independent_pairs, without_ties
from ttk.services.forward_test import ForwardModel, ModelView
from ttk.services.frozen_simulator import FrozenSimulator, home_cover_excluding_push

NOW = datetime(2026, 10, 20, 18, 0, tzinfo=UTC)
TIP = NOW + timedelta(hours=5)


def fake_frozen(sport: Sport) -> Any:
    key = KeyNumberMarginModel(MarginModel(0.0, 0.0, 13.0), {0: 0.5, 1: 0.9})
    return SimpleNamespace(
        artifact={"variant": "anchored"},
        margin=SimpleNamespace(key=key),
        data=SimpleNamespace(
            config=SimpleNamespace(sport=sport),
            games=[SimpleNamespace(game_id=1, commence_time=TIP)],
        ),
    )


def card_model(home_cover: float, expected_margin: float = 4.0) -> tuple[ForwardModel, list[Any]]:
    calls: list[Any] = []

    def view(game_id: int, line: float, market: float, at: datetime, horizon: int) -> ModelView:
        calls.append((game_id, line, market, at, horizon))
        return ModelView(home_cover, 0.0, expected_margin, {})

    return ForwardModel(SimpleNamespace(name="m"), Sport.NBA, view, NOW), calls  # type: ignore[arg-type]


def pairs() -> np.ndarray:
    rng = np.random.default_rng(1)
    return np.column_stack([rng.uniform(size=2000), rng.normal(0, 18, size=2000)])


def test_without_ties_moves_the_zero_mass_proportionally() -> None:
    out = without_ties({-1: 0.3, 0: 0.4, 2: 0.3})
    assert out == {-1: pytest.approx(0.5), 2: pytest.approx(0.5)}


def test_independent_pairs_keep_both_marginals() -> None:
    p = pairs()
    q = independent_pairs(p)
    assert np.array_equal(p[:, 0], q[:, 0])
    assert np.array_equal(np.sort(p[:, 1]), np.sort(q[:, 1]))
    assert not np.array_equal(p[:, 1], q[:, 1])


def test_basketball_simulation_never_ties_and_holds_the_anchor() -> None:
    model, calls = card_model(home_cover=0.58)
    sim = FrozenSimulator(model, fake_frozen(Sport.NBA), pairs(), clock=lambda: NOW)
    assert sim.pmf(0.0).get(0) is None  # no tie mass
    run = sim.simulate(1, 221.5, iterations=40_000, seed=3, anchor=(-4.5, 0.5))
    assert run is not None
    assert not (run.margin == 0).any()
    # The center makes the model's anchored cover probability hold at the main line.
    mu = sim.center_for(-4.5, 0.58)
    assert home_cover_excluding_push(sim.pmf(mu), -4.5) == pytest.approx(0.58, abs=1e-6)
    win, push = run.outcome(Leg("SPREAD", "HOME", -4.5))
    assert float(win[~push].mean()) == pytest.approx(0.58, abs=0.01)
    assert calls[-1][1:3] == (-4.5, 0.5) and calls[-1][4] == 24  # five hours out: 24 h view
    assert abs(float(np.median(run.total)) - 221.5) <= 1.5


def test_without_a_market_the_center_is_the_models_margin() -> None:
    model, _ = card_model(home_cover=0.9, expected_margin=6.0)
    sim = FrozenSimulator(model, fake_frozen(Sport.NBA), pairs(), clock=lambda: NOW)
    run = sim.simulate(1, 220.0, iterations=40_000, seed=5)
    assert run is not None
    assert float(run.margin.mean()) == pytest.approx(6.0, abs=0.3)
    assert sim.simulate(99, 220.0) is None  # unknown game


def test_standalone_card_models_are_refused() -> None:
    frozen = fake_frozen(Sport.CFB)
    frozen.artifact = {"variant": "key"}
    model, _ = card_model(0.5)
    with pytest.raises(ValueError, match="market-anchored"):
        FrozenSimulator(model, frozen, pairs())


def test_simulation_api_routes_by_sport(database_url: str, session_factory: Any) -> None:
    from fastapi.testclient import TestClient

    from test_forward import KICK, odds
    from ttk.api.app import create_app
    from ttk.config import Settings
    from ttk.db.models import utcnow

    game_id = odds(session_factory, KICK - timedelta(hours=20))  # a CFB game, spreads only
    app = create_app(Settings(database_url=database_url))
    client = TestClient(app, base_url="http://localhost")
    body = {"game_id": game_id, "preset": "quick", "seed": 1}

    app.state.card_models = (utcnow(), {})  # loaded, but no CFB model
    r = client.post("/api/simulations/run", json=body)
    assert r.status_code == 422 and "No simulation model for CFB" in r.json()["detail"]

    model, _ = card_model(0.55)
    sim = FrozenSimulator(model, fake_frozen(Sport.CFB), pairs(), clock=lambda: NOW)
    app.state.card_models = (utcnow(), {Sport.CFB: SimpleNamespace(simulator=sim)})
    r = client.post("/api/simulations/run", json=body)
    assert r.status_code == 422 and "No total market" in r.json()["detail"]
    assert client.post("/api/simulations/run", json={**body, "game_id": 999}).status_code == 404
