from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ttk import betting_math as bm
from ttk.config import Settings, get_settings
from ttk.db.models import (
    BankrollEntry,
    Bet,
    Game,
    IngestionRun,
    Parlay,
    Sportsbook,
    Team,
    utcnow,
)
from ttk.db.session import make_engine, make_session_factory
from ttk.domain import BetClassification, BetResult, Market, Selection, Sport
from ttk.models.simulation import PRESETS
from ttk.services.bankroll import (
    BankrollState,
    EntryKind,
    Policy,
    add_entry,
    bankroll_state,
    max_allowed,
    save_policy,
    stake_guidance,
)
from ttk.services.bets import BetError, NewBet, performance, record_bet, settle_bets
from ttk.services.daily_card import build_card, card_window
from ttk.services.forward_test import forward_dashboard
from ttk.services.line_history import PricePoint, line_history
from ttk.services.market import SideMarket, main_lines, side_markets
from ttk.services.nfl_spread_predictor import NflSpreadPredictor
from ttk.services.parlay_lab import (
    LegInput,
    ParlayError,
    book_offers,
    evaluate_parlay,
    parlay_performance,
    save_parlay,
    settle_parlays,
)
from ttk.services.performance_lab import lab_choices, performance_lab
from ttk.services.simulation_service import SimulationError, run_simulation

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Time-to-Kill", version="0.1.0")
    allowed_hosts = settings.allowed_hosts

    @app.middleware("http")
    async def same_origin_writes(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """No auth, so block cross-site writes: another website open in the browser
        must not be able to POST to this loopback API. Writes need a JSON body,
        and any Origin header must be this app's own host."""
        if request.method in WRITE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and urlsplit(origin).hostname not in {
                h.strip("[]") for h in allowed_hosts
            }:
                return JSONResponse({"detail": "Cross-origin write refused"}, status_code=403)
            content_type = request.headers.get("content-type", "")
            if request.headers.get("content-length", "0") != "0" and not content_type.startswith(
                "application/json"
            ):
                return JSONResponse({"detail": "Writes must be application/json"}, status_code=415)
        return await call_next(request)

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.state.settings = settings
    app.state.session_factory = make_session_factory(make_engine(settings.database_url))
    _register_routes(app)
    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


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


# ----------------------------------------------------------------------- schemas


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


# ----------------------------------------------------------------------- routes


def _register_routes(app: FastAPI) -> None:
    @app.get("/api/health")
    def health(session: SessionDep) -> dict[str, object]:
        last = session.scalar(select(IngestionRun).order_by(IngestionRun.id.desc()).limit(1))
        return {
            "status": "ok",
            "last_ingestion": None
            if last is None
            else {
                "provider": last.provider,
                "sport": last.sport,
                "status": last.status,
                "finished_at": last.finished_at,
            },
        }

    @app.get("/api/games")
    def list_games(
        session: SessionDep,
        sport: Sport | None = None,
        include_started: bool = False,
        day: Annotated[date | None, Query(alias="date")] = None,
    ) -> list[GameOut]:
        home, away = aliased(Team), aliased(Team)
        stmt = (
            select(Game, home.name, away.name)
            .join(home, Game.home_team_id == home.id)
            .join(away, Game.away_team_id == away.id)
            .order_by(Game.commence_time)
        )
        if sport is not None:
            stmt = stmt.where(Game.sport == sport)
        if not include_started:
            stmt = stmt.where(Game.commence_time > utcnow())
        if day is not None:
            start, end = card_window(day)
            stmt = stmt.where(Game.commence_time >= start, Game.commence_time < end)
        return [
            GameOut(
                id=g.id,
                sport=Sport(g.sport),
                home_team=h,
                away_team=a,
                commence_time=g.commence_time,
            )
            for g, h, a in session.execute(stmt)
        ]

    @app.get("/api/games/{game_id}/market")
    def game_market(
        game_id: int,
        session: SessionDep,
        request: Request,
        all_lines: Annotated[bool, Query(description="Include alternate lines")] = False,
    ) -> list[MarketOut]:
        if session.get(Game, game_id) is None:
            raise HTTPException(404, "Game not found")
        markets = side_markets(session, game_id)
        if not all_lines:
            markets = main_lines(markets)
        max_age = request.app.state.settings.qualification_rules().max_odds_age
        out = [_market_out(sm, max_age) for sm in markets]
        return sorted(out, key=lambda m: (m.market, m.selection, m.line or 0.0))

    @app.get("/api/games/{game_id}/line-history")
    def game_line_history(
        game_id: int, market: Market, selection: Selection, session: SessionDep
    ) -> list[LineHistoryOut]:
        if session.get(Game, game_id) is None:
            raise HTTPException(404, "Game not found")
        try:
            histories = line_history(session, game_id, market, selection)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return [
            LineHistoryOut(
                sportsbook=h.sportsbook,
                opening=_point_out(h.opening),
                previous=_point_out(h.previous) if h.previous else None,
                current=_point_out(h.current) if h.current else None,
                closing=_point_out(h.closing) if h.closing else None,
                observations=h.observations,
            )
            for h in histories
        ]

    @app.get("/api/card")
    def daily_card(
        session: SessionDep,
        request: Request,
        day: Annotated[date | None, Query(alias="date")] = None,
    ) -> dict[str, object]:
        """The daily card. Read-only: predictions are only persisted by `ttk card`."""
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
                for e in card.entries
            ],
            "unmodeled": [asdict(u) for u in card.unmodeled],
            "unbettable": card.unbettable,
        }

    @app.get("/api/sportsbooks")
    def sportsbooks(session: SessionDep, request: Request) -> list[dict[str, object]]:
        bettable = request.app.state.settings.bettable_book_keys()
        return [
            {"key": key, "name": name, "bettable": bettable is None or key in bettable}
            for key, name in session.execute(
                select(Sportsbook.key, Sportsbook.name).order_by(Sportsbook.key)
            ).all()
        ]

    @app.get("/api/bets")
    def list_bets(
        session: SessionDep, status: Annotated[str | None, Query()] = None
    ) -> list[BetOut]:
        stmt = select(Bet).where(Bet.parlay_id.is_(None)).order_by(Bet.placed_at.desc())
        if status == "pending":
            stmt = stmt.where(Bet.result == BetResult.PENDING)
        elif status == "settled":
            stmt = stmt.where(Bet.result != BetResult.PENDING)
        return [_bet_out(session, b) for b in session.scalars(stmt)]

    @app.post("/api/bets", status_code=201)
    def create_bet(body: BetIn, session: SessionDep) -> BetOut:
        try:
            bet = record_bet(session, NewBet(**body.model_dump()))
        except BetError as exc:
            raise HTTPException(422, str(exc)) from None
        session.commit()
        return _bet_out(session, bet)

    @app.patch("/api/bets/{bet_id}")
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

    @app.post("/api/bets/settle")
    def settle(session: SessionDep) -> dict[str, object]:
        """Settle finished single bets and parlays."""
        bets = settle_bets(session)
        parlays = settle_parlays(session)
        session.commit()
        return {
            "bets": [_bet_out(session, b) for b in bets],
            "parlays": [_parlay_out(session, p) for p in parlays],
        }

    @app.get("/api/bankroll")
    def get_bankroll(session: SessionDep) -> dict[str, object]:
        entries = session.scalars(select(BankrollEntry).order_by(BankrollEntry.at.desc())).all()
        return {
            **_bankroll_out(bankroll_state(session)),
            "entries": [
                {"id": e.id, "at": e.at, "kind": e.kind, "amount": e.amount, "note": e.note}
                for e in entries
            ],
        }

    @app.post("/api/bankroll/entries", status_code=201)
    def create_entry(body: BankrollEntryIn, session: SessionDep) -> dict[str, object]:
        try:
            add_entry(session, body.kind, body.amount, note=body.note)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        session.commit()
        return _bankroll_out(bankroll_state(session))

    @app.put("/api/bankroll/policy")
    def put_policy(body: PolicyIn, session: SessionDep) -> dict[str, object]:
        policy = Policy(**body.model_dump(exclude={"note"}))
        try:
            save_policy(session, policy, note=body.note)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        session.commit()
        return _bankroll_out(bankroll_state(session))

    @app.post("/api/bankroll/guidance")
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

    @app.get("/api/performance")
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

    @app.get("/api/forward")
    def get_forward(session: SessionDep, sport: Sport | None = None) -> dict[str, object]:
        """Forward-test scores per model and horizon, and the latest snapshots."""
        return forward_dashboard(session, sport)

    @app.get("/api/lab")
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

    @app.get("/api/games/{game_id}/offers")
    def game_offers(game_id: int, book: str, session: SessionDep) -> list[dict[str, object]]:
        """Every line ``book`` currently quotes for a game, main line flagged."""
        if session.get(Game, game_id) is None:
            raise HTTPException(404, "Game not found")
        try:
            return [asdict(o) for o in book_offers(session, game_id, book)]
        except ParlayError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/parlays/evaluate")
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

    @app.post("/api/parlays", status_code=201)
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

    @app.get("/api/parlays")
    def list_parlays(session: SessionDep) -> list[dict[str, object]]:
        rows = session.scalars(select(Parlay).order_by(Parlay.placed_at.desc(), Parlay.id.desc()))
        return [_parlay_out(session, p) for p in rows]

    @app.post("/api/simulations/run")
    def simulations_run(
        body: SimulationIn, session: SessionDep, request: Request
    ) -> dict[str, object]:
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
                raise HTTPException(
                    503, f"The {game.sport} model is still loading; try again shortly"
                )
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

    @app.post("/api/math/evaluate")
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
