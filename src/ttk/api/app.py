from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, cast
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
from ttk.db.models import Bet, Game, IngestionRun, Sportsbook, Team, utcnow
from ttk.db.session import make_engine, make_session_factory
from ttk.domain import BetResult, Market, Selection, Sport
from ttk.services.bets import BetError, NewBet, performance, record_bet, settle_bets
from ttk.services.daily_card import build_card
from ttk.services.line_history import PricePoint, line_history
from ttk.services.market import SideMarket, main_lines, side_markets
from ttk.services.nfl_spread_predictor import NflSpreadPredictor

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
    )


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
        card = build_card(
            session,
            day or datetime.now(EASTERN).date(),
            settings.qualification_rules(),
            predictor=_cached_predictor(request.app, session),
            persist=False,
            bettable_books=settings.bettable_book_keys(),
        )
        session.rollback()  # nothing from a GET is kept (e.g. a first registry row)
        return {
            "date": card.day,
            "generated_at": card.generated_at,
            "headline": card.headline,
            "bettable_books": sorted(card.bettable_books) if card.bettable_books else None,
            "by_sport": {s: asdict(v) for s, v in card.by_sport.items()},
            "entries": [asdict(e) for e in card.entries],
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
    def settle(session: SessionDep) -> list[BetOut]:
        settled = settle_bets(session)
        session.commit()
        return [_bet_out(session, b) for b in settled]

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
        return asdict(perf)

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
