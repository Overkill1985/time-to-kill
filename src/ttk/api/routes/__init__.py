"""API routers, one per area; ``ROUTERS`` is the order ``create_app`` includes them."""

from fastapi import APIRouter

from ttk.api.routes import bankroll, bets, card, forward, games, parlays, props, status, tools

ROUTERS: tuple[APIRouter, ...] = (
    games.router,
    card.router,
    bets.router,
    bankroll.router,
    forward.router,
    parlays.router,
    props.router,
    tools.router,
    status.router,
)
