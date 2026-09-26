from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ttk import betting_math as bm
from ttk.config import Settings, get_settings
from ttk.db.models import Game, IngestionRun, Team, utcnow
from ttk.db.session import make_engine, make_session_factory
from ttk.domain import Market, Selection, Sport
from ttk.services.line_history import PricePoint, line_history
from ttk.services.market import SideMarket, main_lines, side_markets


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Time-to-Kill", version="0.1.0")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.state.settings = settings
    app.state.session_factory = make_session_factory(make_engine(settings.database_url))
    _register_routes(app)
    return app


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
