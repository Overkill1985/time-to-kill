"""Home page status: is data collection healthy, what is under test, what is next."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Request
from sqlalchemy import func, or_, select

from ttk.api.common import SessionDep
from ttk.config import Settings
from ttk.db.models import Game, IngestionRun, ModelVersion, PropPull, utcnow
from ttk.domain import GameStatus, Sport
from ttk.services.alerts import check_health
from ttk.services.bankroll import bankroll_state
from ttk.services.forward_test import MIN_DECIDED_FOR_Z, forward_dashboard
from ttk.services.health import last_quota

router = APIRouter()


def _data_dir(settings: Settings) -> Path:
    return Path(settings.database_url.removeprefix("sqlite:///")).parent


@router.get("/api/status")
def status(session: SessionDep, request: Request) -> dict[str, object]:
    """Everything the Home page shows, in one read-only call."""
    settings: Settings = request.app.state.settings
    now = utcnow()
    data_dir = _data_dir(settings)

    last_poll = session.scalar(
        select(func.max(IngestionRun.finished_at)).where(
            IngestionRun.kind == "odds", IngestionRun.status == "SUCCESS"
        )
    )
    quota = last_quota(data_dir / "logs" / "collector.log")
    alerts = [
        {"key": a.key, "message": a.message}
        for a in check_health(session, now=now, data_dir=data_dir)
    ]

    dashboard = forward_dashboard(session, recent=0)
    progress: dict[str, dict[str, object]] = {}
    for row in session.scalars(
        select(ModelVersion)
        .where(ModelVersion.artifact.is_not(None))
        .order_by(ModelVersion.sport, ModelVersion.name)
    ):
        market = (row.artifact or {}).get("market", "SPREAD")
        progress[f"{row.name} {row.version}"] = {
            "model": f"{row.name} {row.version}",
            "name": row.name,
            "sport": row.sport,
            "market": market,
            "status": row.status,
            "snapshots": 0,
            "decided": 0,
            "z": None,
        }
    nfl = next(
        (m["model"] for m in dashboard["models"] if str(m["model"]).startswith("nfl-")), None
    )
    if nfl is not None and nfl not in progress:
        progress[nfl] = {
            "model": nfl,
            "name": str(nfl).rsplit(" ", 1)[0],
            "sport": "NFL",
            "market": "SPREAD",
            "status": "DEVELOPMENT",
            "snapshots": 0,
            "decided": 0,
            "z": None,
        }
    for m in dashboard["models"]:
        if m["model"] in progress:
            progress[m["model"]]["snapshots"] = m["snapshots"]
    for sc in dashboard["scores"]:
        p = progress.get(sc["model"])
        if p is None:
            continue
        # The horizon with fewer decided games sets the pace (both are judged).
        if p["decided"] == 0 or sc["decided"] < p["decided"]:
            p["decided"] = sc["decided"]
            p["z"] = sc["z"]

    upcoming = []
    for sport in Sport:
        base = select(Game).where(
            Game.sport == sport,
            Game.commence_time > now,
            Game.status != GameStatus.DUPLICATE,
            or_(Game.season_type.is_(None), Game.season_type != "PRE"),
        )
        nxt = session.scalars(base.order_by(Game.commence_time).limit(1)).first()
        within = session.scalar(
            select(func.count()).select_from(
                base.where(Game.commence_time <= now + timedelta(hours=24)).subquery()
            )
        )
        upcoming.append(
            {
                "sport": sport,
                "next_kickoff": nxt.commence_time if nxt else None,
                "next_24h": int(within or 0),
            }
        )

    last_props = session.scalar(select(func.max(PropPull.pulled_at)))
    bankroll = bankroll_state(session)
    return {
        "now": now,
        "collection": {
            "last_odds_poll": last_poll,
            "minutes_since_poll": (now - last_poll).total_seconds() / 60 if last_poll else None,
            "quota_remaining": quota[1] if quota else None,
            "alerts": alerts,
            "last_props_pull": last_props,
        },
        "models": list(progress.values()),
        "min_decided": MIN_DECIDED_FOR_Z,
        "upcoming": upcoming,
        "bankroll": {
            "configured": bankroll.configured,
            "balance": bankroll.balance,
            "open_exposure": bankroll.open_exposure,
            "open_wagers": bankroll.open_wagers,
            "drawdown": bankroll.drawdown,
        },
    }
