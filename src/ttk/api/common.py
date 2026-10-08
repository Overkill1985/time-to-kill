"""Shared by the API routes: request and response models, conversions, the
cached models (NFL predictor, the other sports' card models and simulators)
and the database session dependency."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ttk import betting_math as bm
from ttk.db.models import (
    Bet,
    Game,
    Parlay,
    Sportsbook,
    utcnow,
)
from ttk.domain import Market, Selection, Sport
from ttk.services.bankroll import (
    BankrollState,
    EntryKind,
    max_allowed,
)
from ttk.services.line_history import PricePoint
from ttk.services.market import SideMarket
from ttk.services.nfl_spread_predictor import NflSpreadPredictor
from ttk.services.parlay_lab import (
    LegInput,
)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


PREDICTOR_TTL = timedelta(minutes=30)


EASTERN = ZoneInfo("America/New_York")


def _cached_predictor(app: FastAPI, session: Session) -> NflSpreadPredictor | None:
    """Fitting takes ~10 s, so the fitted predictor is reused for PREDICTOR_TTL."""
    cached = getattr(app.state, "predictor", None)
    if cached is not None and utcnow() - cached[0] < PREDICTOR_TTL:
        return cast("NflSpreadPredictor | None", cached[1])
    predictor = NflSpreadPredictor.build(session)
    app.state.predictor = (utcnow(), predictor)
    return predictor


CARD_MODELS_TTL = timedelta(hours=6)


_card_lock = threading.Lock()


def _simulators(app: FastAPI) -> dict[Sport, Any]:
    """Monte Carlo per non-NFL sport, from the card models (none until they load)."""
    models, _ = _card_models(app)
    return {s: m.simulator for s, m in models.items() if getattr(m, "simulator", None)}


def _card_models(app: FastAPI) -> tuple[dict[Sport, Any], frozenset[Sport]]:
    """The non-NFL card models and the sports still loading. Building them loads
    each sport's history (minutes), so it runs in a background thread, started by
    the first card request and again every CARD_MODELS_TTL; until a build
    finishes, those sports are reported as loading. Tests may preset
    ``app.state.card_models`` to skip the build."""
    from ttk.services.forward_models import CARD_MODEL_NAMES, build_card_models

    state = app.state
    cached = getattr(state, "card_models", None)
    if cached is not None and (cached[0] is None or utcnow() - cached[0] < CARD_MODELS_TTL):
        return cached[1], frozenset()

    def build() -> None:
        try:
            with state.session_factory() as session:
                models = build_card_models(session)
            state.card_models = (utcnow(), models)
        except Exception:  # keep serving the NFL card; retry on a later request
            import logging

            logging.getLogger(__name__).exception("card model build failed")
        finally:
            state.card_models_building = False

    with _card_lock:
        if not getattr(state, "card_models_building", False):
            state.card_models_building = True
            threading.Thread(target=build, name="card-models", daemon=True).start()
    previous = cached[1] if cached is not None else {}
    return previous, frozenset() if previous else frozenset(CARD_MODEL_NAMES)


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


class GameOut(BaseModel):
    id: int
    sport: Sport
    home_team: str
    away_team: str
    commence_time: datetime


class BookPriceOut(BaseModel):
    sportsbook: str
    american_odds: str
    no_vig_probability: float


class MarketOut(BaseModel):
    market: str
    selection: str
    line: float | None
    books_reporting: int
    consensus_no_vig_probability: float
    consensus_american_odds: str
    best: BookPriceOut
    no_vig_range: tuple[float, float]
    oldest_observation: datetime
    odds_current: bool


class BetIn(BaseModel):
    game_id: int
    market: Market
    selection: Selection
    line: float | None = None
    american_odds: float
    sportsbook: str
    stake: float = Field(gt=0)
    placed_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=2000)
    limit_override: str | None = Field(default=None, max_length=500)
    """Reason to record the bet although it breaks a bankroll limit."""


class BetPatch(BaseModel):
    notes: str | None = Field(default=None, max_length=2000)
    void: bool = False
    """Mark a pending bet VOID (e.g. the book voided it)."""


class BetOut(BaseModel):
    id: int
    placed_at: datetime
    sport: str
    game_id: int | None
    commence_time: datetime | None
    description: str
    market: str
    selection: str
    line: float | None
    american_odds: str
    sportsbook: str | None
    stake: float | None
    model_probability: float | None
    market_probability: float | None
    edge: float | None
    expected_value: float | None
    result: str
    profit_loss: float | None
    closing_line: float | None
    closing_no_vig_probability: float | None
    clv: float | None
    closing_points_gained: float | None
    settled_at: datetime | None
    notes: str | None
    bankroll_at_bet: float | None
    limit_override: str | None


def _bet_out(session: Session, bet: Bet) -> BetOut:
    book = session.get(Sportsbook, bet.sportsbook_id) if bet.sportsbook_id else None
    game = session.get(Game, bet.game_id) if bet.game_id else None
    return BetOut(
        id=bet.id,
        placed_at=bet.placed_at,
        sport=bet.sport,
        game_id=bet.game_id,
        commence_time=game.commence_time if game else None,
        description=bet.description,
        market=bet.market,
        selection=bet.selection,
        line=bet.line,
        american_odds=bm.format_american(bet.american_odds),
        sportsbook=book.key if book else None,
        stake=bet.stake,
        model_probability=bet.model_probability,
        market_probability=bet.market_probability,
        edge=bet.edge,
        expected_value=bet.expected_value,
        result=bet.result,
        profit_loss=bet.profit_loss,
        closing_line=bet.closing_line,
        closing_no_vig_probability=bet.closing_no_vig_probability,
        clv=bet.clv,
        closing_points_gained=bet.closing_points_gained,
        settled_at=bet.settled_at,
        notes=bet.notes,
        bankroll_at_bet=bet.bankroll_at_bet,
        limit_override=bet.limit_override,
    )


class LegIn(BaseModel):
    game_id: int
    market: Market
    selection: Selection
    line: float | None = None
    american_odds: float | None = None


class ParlayIn(BaseModel):
    sportsbook: str
    legs: list[LegIn] = Field(min_length=2, max_length=12)


class ParlaySave(ParlayIn):
    stake: float = Field(gt=0)
    american_odds: float | None = None
    """The price actually taken; default is the product of the legs."""
    placed_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=2000)
    limit_override: str | None = Field(default=None, max_length=500)


class BankrollEntryIn(BaseModel):
    kind: EntryKind
    amount: float
    """Positive for deposits and withdrawals; signed for an adjustment."""
    note: str | None = Field(default=None, max_length=500)


class PolicyIn(BaseModel):
    kelly_multiplier: float = Field(gt=0, le=1)
    max_stake_fraction: float = Field(gt=0, le=1)
    max_daily_fraction: float = Field(gt=0, le=1)
    max_open_fraction: float = Field(gt=0, le=1)
    stop_drawdown_fraction: float = Field(gt=0, lt=1)
    note: str | None = Field(default=None, max_length=500)


class GuidanceIn(BaseModel):
    model_probability: float = Field(ge=0, le=1)
    """P(win | no push)."""
    american_odds: float
    push_probability: float = Field(default=0.0, ge=0, lt=1)
    qualified: bool = False


def _bankroll_out(state: BankrollState) -> dict[str, object]:
    out = asdict(state)
    out["max_allowed"] = max_allowed(state)
    return out


def _legs(body: ParlayIn) -> list[LegInput]:
    return [LegInput(**leg.model_dump()) for leg in body.legs]


def _parlay_out(session: Session, parlay: Parlay) -> dict[str, object]:
    book = session.get(Sportsbook, parlay.sportsbook_id) if parlay.sportsbook_id else None
    legs = session.scalars(select(Bet).where(Bet.parlay_id == parlay.id).order_by(Bet.id))
    return {
        "id": parlay.id,
        "placed_at": parlay.placed_at,
        "sportsbook": book.key if book else None,
        "american_odds": bm.format_american(parlay.american_odds)
        if parlay.american_odds is not None
        else None,
        "stake": parlay.stake,
        "joint_probability": parlay.model_joint_probability,
        "market_joint_probability": parlay.market_joint_probability,
        "expected_value": parlay.expected_value,
        "correlation_risk": parlay.correlation_risk,
        "result": parlay.result,
        "profit_loss": parlay.profit_loss,
        "settled_at": parlay.settled_at,
        "notes": parlay.notes,
        "legs": [
            {
                "description": leg.description,
                "american_odds": bm.format_american(leg.american_odds),
                "result": leg.result,
                "clv": leg.clv,
            }
            for leg in legs
        ],
    }


class SimulationIn(BaseModel):
    game_id: int
    preset: Literal["quick", "detailed", "research"] | None = "quick"
    iterations: int | None = Field(default=None, ge=1_000, le=200_000)
    """Overrides the preset."""
    seed: int | None = None


class EvaluateIn(BaseModel):
    model_probability: float = Field(ge=0, le=1)
    american_odds: float
    no_vig_probability: float | None = Field(default=None, gt=0, lt=1)
    push_probability: float = Field(default=0.0, ge=0, le=1)


class EvaluateOut(BaseModel):
    decimal_odds: float
    implied_probability: float
    break_even_probability: float
    fair_american_odds: str | None
    edge: float | None
    ev_per_unit: float
    ev_percent: float
    full_kelly_fraction: float


class PricePointOut(BaseModel):
    observed_at: datetime
    line: float | None
    american_odds: str
    no_vig_probability: float | None


class LineHistoryOut(BaseModel):
    sportsbook: str
    opening: PricePointOut
    previous: PricePointOut | None
    current: PricePointOut | None
    """Null when the book has taken the market down."""
    closing: PricePointOut | None
    """Last price at or before kickoff; null until the game has started."""
    observations: int


def _point_out(p: PricePoint) -> PricePointOut:
    return PricePointOut(
        observed_at=p.observed_at,
        line=p.line,
        american_odds=bm.format_american(p.american_odds),
        no_vig_probability=p.no_vig_probability,
    )


def _market_out(sm: SideMarket, max_age: timedelta) -> MarketOut:
    c = sm.consensus
    return MarketOut(
        market=sm.market,
        selection=sm.selection,
        line=sm.line,
        books_reporting=c.books_reporting,
        consensus_no_vig_probability=c.consensus_no_vig_probability,
        consensus_american_odds=bm.format_american(
            bm.decimal_to_american(c.consensus_decimal_odds)
        ),
        best=BookPriceOut(
            sportsbook=c.best.sportsbook,
            american_odds=bm.format_american(bm.decimal_to_american(c.best.decimal_odds)),
            no_vig_probability=c.best.no_vig_probability,
        ),
        no_vig_range=c.no_vig_range,
        oldest_observation=sm.oldest_observation,
        odds_current=utcnow() - sm.oldest_observation <= max_age,
    )
