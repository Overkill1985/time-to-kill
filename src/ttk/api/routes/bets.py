"""Bet tracker: record, list, void, settle; performance."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select

from ttk.api.common import (
    BetIn,
    BetOut,
    BetPatch,
    SessionDep,
    _bet_out,
    _parlay_out,
)
from ttk.db.models import (
    Bet,
    utcnow,
)
from ttk.domain import BetResult, Market, Sport
from ttk.services.bets import BetError, NewBet, performance, record_bet, settle_bets
from ttk.services.parlay_lab import (
    parlay_performance,
    settle_parlays,
)

router = APIRouter()


@router.get("/api/bets")
def list_bets(session: SessionDep, status: Annotated[str | None, Query()] = None) -> list[BetOut]:
    stmt = select(Bet).where(Bet.parlay_id.is_(None)).order_by(Bet.placed_at.desc())
    if status == "pending":
        stmt = stmt.where(Bet.result == BetResult.PENDING)
    elif status == "settled":
        stmt = stmt.where(Bet.result != BetResult.PENDING)
    return [_bet_out(session, b) for b in session.scalars(stmt)]


@router.post("/api/bets", status_code=201)
def create_bet(body: BetIn, session: SessionDep) -> BetOut:
    try:
        bet = record_bet(session, NewBet(**body.model_dump()))
    except BetError as exc:
        raise HTTPException(422, str(exc)) from None
    session.commit()
    return _bet_out(session, bet)


@router.patch("/api/bets/{bet_id}")
def update_bet(bet_id: int, body: BetPatch, session: SessionDep) -> BetOut:
    bet = session.get(Bet, bet_id)
    if bet is None:
        raise HTTPException(404, "Bet not found")
    if body.void:
        if bet.result != BetResult.PENDING:
            raise HTTPException(409, "Only a pending bet can be voided")
        bet.result, bet.profit_loss, bet.settled_at = BetResult.VOID, 0.0, utcnow()
    if body.notes is not None:
        bet.notes = body.notes
    session.commit()
    return _bet_out(session, bet)


@router.post("/api/bets/settle")
def settle(session: SessionDep) -> dict[str, object]:
    """Settle finished single bets and parlays."""
    bets = settle_bets(session)
    parlays = settle_parlays(session)
    session.commit()
    return {
        "bets": [_bet_out(session, b) for b in bets],
        "parlays": [_parlay_out(session, p) for p in parlays],
    }


@router.get("/api/performance")
def get_performance(
    session: SessionDep,
    request: Request,
    sport: Sport | None = None,
    market: Market | None = None,
) -> dict[str, object]:
    perf = performance(
        session, sport=sport, market=market, unit_size=request.app.state.settings.unit_size
    )
    return {**asdict(perf), "parlays": asdict(parlay_performance(session))}
