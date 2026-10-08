"""The daily card."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request

from ttk import betting_math as bm
from ttk.api.common import (
    EASTERN,
    SessionDep,
    _cached_predictor,
    _card_models,
)
from ttk.config import Settings
from ttk.domain import BetClassification, Sport
from ttk.services.bankroll import (
    bankroll_state,
    stake_guidance,
)
from ttk.services.daily_card import CardFilter, build_card, filter_entries

router = APIRouter()


@router.get("/api/card")
def daily_card(
    session: SessionDep,
    request: Request,
    day: Annotated[date | None, Query(alias="date")] = None,
    sport: Annotated[list[Sport] | None, Query()] = None,
    classification: Annotated[list[BetClassification] | None, Query()] = None,
    min_edge: Annotated[float | None, Query(description="points, e.g. 2")] = None,
    book: Annotated[list[str] | None, Query()] = None,
) -> dict[str, object]:
    """The daily card. Read-only: predictions are only persisted by `ttk card`.
    Filters narrow ``entries`` only; the summaries cover the whole card."""
    card_filter = CardFilter(
        sports=frozenset(sport) if sport else None,
        classifications=frozenset(classification) if classification else None,
        min_edge=min_edge / 100 if min_edge is not None else None,
        books=frozenset(b.lower() for b in book) if book else None,
    )
    settings: Settings = request.app.state.settings
    models, loading = _card_models(request.app)
    for model in models.values():
        model.refresh(session)  # the injury report as of now
    card = build_card(
        session,
        day or datetime.now(EASTERN).date(),
        settings.qualification_rules(),
        predictor=_cached_predictor(request.app, session),
        persist=False,
        bettable_books=settings.bettable_book_keys(),
        models=models,
        loading=loading,
    )
    session.rollback()  # nothing from a GET is kept (e.g. a first registry row)
    bankroll = bankroll_state(session)
    return {
        "date": card.day,
        "generated_at": card.generated_at,
        "headline": card.headline,
        "bettable_books": sorted(card.bettable_books) if card.bettable_books else None,
        "by_sport": {s: asdict(v) for s, v in card.by_sport.items()},
        "bankroll_configured": bankroll.configured,
        "entries": [
            {
                **asdict(e),
                "stake": asdict(
                    stake_guidance(
                        bankroll,
                        e.model_probability,
                        bm.american_to_decimal(e.american_odds),
                        push_probability=e.push_probability,
                        qualified=e.classification is BetClassification.QUALIFIED,
                    )
                ),
            }
            for e in filter_entries(card.entries, card_filter)
        ],
        "total_entries": len(card.entries),
        "unmodeled": [asdict(u) for u in card.unmodeled],
        "unbettable": card.unbettable,
    }
