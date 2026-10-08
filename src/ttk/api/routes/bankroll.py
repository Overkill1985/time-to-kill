"""Bankroll: balance, entries, staking limits, Kelly guidance."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from ttk import betting_math as bm
from ttk.api.common import (
    BankrollEntryIn,
    GuidanceIn,
    PolicyIn,
    SessionDep,
    _bankroll_out,
)
from ttk.db.models import (
    BankrollEntry,
)
from ttk.services.bankroll import (
    Policy,
    add_entry,
    bankroll_state,
    save_policy,
    stake_guidance,
)

router = APIRouter()


@router.get("/api/bankroll")
def get_bankroll(session: SessionDep) -> dict[str, object]:
    entries = session.scalars(select(BankrollEntry).order_by(BankrollEntry.at.desc())).all()
    return {
        **_bankroll_out(bankroll_state(session)),
        "entries": [
            {"id": e.id, "at": e.at, "kind": e.kind, "amount": e.amount, "note": e.note}
            for e in entries
        ],
    }


@router.post("/api/bankroll/entries", status_code=201)
def create_entry(body: BankrollEntryIn, session: SessionDep) -> dict[str, object]:
    try:
        add_entry(session, body.kind, body.amount, note=body.note)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    session.commit()
    return _bankroll_out(bankroll_state(session))


@router.put("/api/bankroll/policy")
def put_policy(body: PolicyIn, session: SessionDep) -> dict[str, object]:
    policy = Policy(**body.model_dump(exclude={"note"}))
    try:
        save_policy(session, policy, note=body.note)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    session.commit()
    return _bankroll_out(bankroll_state(session))


@router.post("/api/bankroll/guidance")
def guidance(body: GuidanceIn, session: SessionDep) -> dict[str, object]:
    """Kelly stake within the limits; 0 unless the opportunity is QUALIFIED."""
    try:
        decimal_odds = bm.american_to_decimal(body.american_odds)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    g = stake_guidance(
        bankroll_state(session),
        body.model_probability,
        decimal_odds,
        push_probability=body.push_probability,
        qualified=body.qualified,
    )
    return asdict(g)
