"""Games, their markets, line history and book offers; sportsbooks; health."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import aliased

from ttk.api.common import (
    GameOut,
    LineHistoryOut,
    MarketOut,
    SessionDep,
    _market_out,
    _point_out,
)
from ttk.db.models import (
    Game,
    IngestionRun,
    Sportsbook,
    Team,
    utcnow,
)
from ttk.domain import Market, Selection, Sport
from ttk.services.daily_card import card_window
from ttk.services.line_history import line_history
from ttk.services.market import main_lines, side_markets
from ttk.services.parlay_lab import (
    ParlayError,
    book_offers,
)

router = APIRouter()


@router.get("/api/health")
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


@router.get("/api/games")
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


@router.get("/api/games/{game_id}/market")
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


@router.get("/api/games/{game_id}/line-history")
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


@router.get("/api/sportsbooks")
def sportsbooks(session: SessionDep, request: Request) -> list[dict[str, object]]:
    bettable = request.app.state.settings.bettable_book_keys()
    return [
        {"key": key, "name": name, "bettable": bettable is None or key in bettable}
        for key, name in session.execute(
            select(Sportsbook.key, Sportsbook.name).order_by(Sportsbook.key)
        ).all()
    ]


@router.get("/api/games/{game_id}/offers")
def game_offers(game_id: int, book: str, session: SessionDep) -> list[dict[str, object]]:
    """Every line ``book`` currently quotes for a game, main line flagged."""
    if session.get(Game, game_id) is None:
        raise HTTPException(404, "Game not found")
    try:
        return [asdict(o) for o in book_offers(session, game_id, book)]
    except ParlayError as exc:
        raise HTTPException(422, str(exc)) from None
