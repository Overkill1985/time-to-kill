"""Player props: each prop's market line next to the player's hit rate against it."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from sqlalchemy.orm import Session

from ttk.api.common import EASTERN, SessionDep
from ttk.db.models import utcnow
from ttk.domain import Sport
from ttk.research import prop_model as pm
from ttk.services.player_props import props_for_games, upcoming_games
from ttk.services.prop_forward import PropModel, load_model, model_view, prop_versions

router = APIRouter()


PROP_MODEL_TTL = timedelta(hours=6)


def _prop_model(app: Any, session: Session) -> tuple[str, PropModel] | None:
    """The newest frozen NFL prop model (the matchup one when registered), loaded
    once and kept ``PROP_MODEL_TTL`` (its state - the players' games - moves on)."""
    cached = getattr(app.state, "prop_model", None)
    if cached is not None and utcnow() - cached[0] < PROP_MODEL_TTL:
        hit: tuple[str, PropModel] | None = cached[1]
        return hit
    versions = prop_versions(session)
    if not versions:
        app.state.prop_model = (utcnow(), None)
        return None
    matchup = [v for v in versions if (v.artifact or {}).get("kind") == "nfl_prop_matchup"]
    version = (matchup or versions)[-1]
    loaded = (
        f"{version.name} {version.version}",
        load_model(version.artifact or {}, pm.load_player_games(session)),
    )
    app.state.prop_model = (utcnow(), loaded)
    return loaded


@router.get("/api/props")
def player_props(
    request: Request,
    session: SessionDep,
    day: Annotated[date | None, Query(alias="date")] = None,
    sport: Annotated[Sport | None, Query()] = None,
    kind: Annotated[Literal["main", "milestones"], Query()] = "main",
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
    rows = props_for_games(session, games, milestones=kind == "milestones")
    by_id = {g.id: g for g in games}
    loaded = _prop_model(request.app, session) if any(r.sport == Sport.NFL for r in rows) else None
    if loaded is not None:
        name, model = loaded
        for r in rows:
            if r.sport != Sport.NFL or r.player_id is None or r.opponent_id is None:
                continue
            view = model_view(model, r.player_id, r.market, r.line, by_id[r.game_id], r.opponent_id)
            if view is None:
                continue
            r.model_name, r.model_over, r.model_mean = name, view.p_over, view.mean
            r.defense_factor = getattr(view, "defense_factor", None)
    return {
        "date": day.isoformat() if day else None,
        "kind": kind,
        "games": len(games),
        "props": [
            {
                **asdict(r),
                "season_over_share": r.season.over_share if r.season else None,
                "last10_over_share": r.last10.over_share if r.last10 else None,
            }
            for r in rows
        ],
        "model": loaded[0] if loaded else None,
        "note": (
            "Hit rates describe past games; the prop model is in testing (it has not "
            "beaten the market), so none of these is a recommendation."
        ),
    }
