"""The simulator and the betting-math calculator."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ttk import betting_math as bm
from ttk.api.common import (
    EvaluateIn,
    EvaluateOut,
    SessionDep,
    SimulationIn,
    _cached_predictor,
    _card_models,
    _simulators,
)
from ttk.config import Settings
from ttk.db.models import (
    Game,
)
from ttk.domain import Sport
from ttk.models.simulation import PRESETS
from ttk.services.simulation_service import SimulationError, run_simulation

router = APIRouter()


@router.post("/api/simulations/run")
def simulations_run(body: SimulationIn, session: SessionDep, request: Request) -> dict[str, object]:
    """Monte Carlo for one game. Read-only: nothing is stored."""
    settings: Settings = request.app.state.settings
    iterations = body.iterations or PRESETS[body.preset or "quick"]
    game = session.get(Game, body.game_id)
    if game is None:
        raise HTTPException(404, "Game not found")
    if game.sport == Sport.NFL:
        simulator: Any = _cached_predictor(request.app, session)
    else:
        _, loading = _card_models(request.app)
        if Sport(game.sport) in loading:
            raise HTTPException(503, f"The {game.sport} model is still loading; try again shortly")
        simulator = _simulators(request.app).get(Sport(game.sport))
    try:
        summary = run_simulation(
            session,
            body.game_id,
            simulator,
            iterations=iterations,
            seed=body.seed,
            bettable_books=settings.bettable_book_keys(),
        )
    except SimulationError as exc:
        raise HTTPException(422, str(exc)) from None
    return asdict(summary)


@router.post("/api/math/evaluate")
def evaluate(body: EvaluateIn) -> EvaluateOut:
    try:
        decimal_odds = bm.american_to_decimal(body.american_odds)
        ev = bm.expected_value(
            body.model_probability, decimal_odds, push_probability=body.push_probability
        )
        kelly = bm.kelly_fraction(
            body.model_probability, decimal_odds, push_probability=body.push_probability
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    fair = (
        bm.format_american(bm.fair_american_odds(body.model_probability))
        if 0 < body.model_probability < 1
        else None
    )
    return EvaluateOut(
        decimal_odds=decimal_odds,
        implied_probability=bm.decimal_implied_probability(decimal_odds),
        break_even_probability=bm.break_even_probability(decimal_odds),
        fair_american_odds=fair,
        edge=None
        if body.no_vig_probability is None
        else bm.edge(body.model_probability, body.no_vig_probability),
        ev_per_unit=ev.per_unit,
        ev_percent=ev.percent,
        full_kelly_fraction=kelly,
    )
