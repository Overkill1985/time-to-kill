"""Parlay Lab: evaluate, save, list."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from ttk.api.common import (
    ParlayIn,
    ParlaySave,
    SessionDep,
    _cached_predictor,
    _legs,
    _parlay_out,
    _simulators,
)
from ttk.config import Settings
from ttk.db.models import (
    Parlay,
)
from ttk.services.parlay_lab import (
    ParlayError,
    evaluate_parlay,
    save_parlay,
)

router = APIRouter()


@router.post("/api/parlays/evaluate")
def parlay_evaluate(body: ParlayIn, session: SessionDep, request: Request) -> dict[str, object]:
    """Price and analyze a slip. Read-only (POST only because it takes a body)."""
    settings: Settings = request.app.state.settings
    try:
        analysis = evaluate_parlay(
            session,
            _legs(body),
            body.sportsbook,
            predictor=_cached_predictor(request.app, session),
            simulators=_simulators(request.app),
            max_odds_age=settings.qualification_rules().max_odds_age,
        )
    except ParlayError as exc:
        raise HTTPException(422, str(exc)) from None
    finally:
        session.rollback()
    return asdict(analysis)


@router.post("/api/parlays", status_code=201)
def parlay_save(body: ParlaySave, session: SessionDep, request: Request) -> dict[str, object]:
    try:
        parlay = save_parlay(
            session,
            _legs(body),
            body.sportsbook,
            body.stake,
            american_odds=body.american_odds,
            placed_at=body.placed_at,
            notes=body.notes,
            predictor=_cached_predictor(request.app, session),
            limit_override=body.limit_override,
            simulators=_simulators(request.app),
        )
    except ParlayError as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from None
    session.commit()
    return _parlay_out(session, parlay)


@router.get("/api/parlays")
def list_parlays(session: SessionDep) -> list[dict[str, object]]:
    rows = session.scalars(select(Parlay).order_by(Parlay.placed_at.desc(), Parlay.id.desc()))
    return [_parlay_out(session, p) for p in rows]
