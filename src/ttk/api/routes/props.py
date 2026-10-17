"""Player props: each prop's market line next to the player's hit rate against it."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from typing import Annotated

from fastapi import APIRouter, Query

from ttk.api.common import EASTERN, SessionDep
from ttk.db.models import utcnow
from ttk.domain import Sport
from ttk.services.player_props import props_for_games, upcoming_games

router = APIRouter()


@router.get("/api/props")
def player_props(
    session: SessionDep,
    day: Annotated[date | None, Query(alias="date")] = None,
    sport: Annotated[Sport | None, Query()] = None,
) -> dict[str, object]:
    """Props for one US Eastern day's games (default: games in the next 36 hours),
    from each game's latest pre-game pull (24 h and 1 h before kickoff)."""
    if day is not None:
        start = datetime.combine(day, time(0), tzinfo=EASTERN)
        end = start + timedelta(days=1)
    else:
        start = utcnow()
        end = start + timedelta(hours=36)
    games = upcoming_games(session, start, end, str(sport) if sport else None)
    rows = props_for_games(session, games)
    return {
        "date": day.isoformat() if day else None,
        "games": len(games),
        "props": [
            {
                **asdict(r),
                "season_over_share": r.season.over_share if r.season else None,
                "last10_over_share": r.last10.over_share if r.last10 else None,
            }
            for r in rows
        ],
        "note": (
            "Hit rates describe past games; there is no validated prop model, "
            "so none of these is a recommendation."
        ),
    }
