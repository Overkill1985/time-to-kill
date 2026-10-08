"""Forward tests and the Performance Lab."""

from __future__ import annotations

from fastapi import APIRouter

from ttk.api.common import (
    SessionDep,
)
from ttk.domain import Sport
from ttk.services.forward_test import forward_dashboard
from ttk.services.performance_lab import lab_choices, performance_lab

router = APIRouter()


@router.get("/api/forward")
def get_forward(session: SessionDep, sport: Sport | None = None) -> dict[str, object]:
    """Forward-test scores per model and horizon, and the latest snapshots."""
    return forward_dashboard(session, sport)


@router.get("/api/lab")
def get_lab(
    session: SessionDep,
    sport: Sport | None = None,
    model: str | None = None,
    horizon_hours: int | None = None,
) -> dict[str, object]:
    """Performance Lab for one forward-tested model and horizon (default: the
    one with the most finished games)."""
    choices = lab_choices(session, sport)
    pick = next(
        (
            c
            for c in choices
            if (model is None or c["model"] == model)
            and (horizon_hours is None or c["horizon_hours"] == horizon_hours)
        ),
        None,
    )
    lab = performance_lab(session, pick["model"], pick["horizon_hours"]) if pick else None
    return {"choices": choices, "lab": lab}
