"""Bet and line-movement alerts: one-time events, unlike the health alerts'
open/resolved problems (services/alerts). Each event is sent once - a Windows
notification and a line in data/logs/alerts.log - and remembered in
data/alerts/events.json (pruned after ``FORGET``).

The odds collector checks these on every pass:

- your pending bet's line has moved ``BET_MOVE_POINTS`` or more from your
  number since you bet it, toward you or against you (spreads and totals,
  before kickoff);
- a bet or parlay has settled (result, profit and CLV);
- steam: a game in a watched sport, kicking off within ``STEAM_WINDOW``, whose
  consensus main spread or total moved ``STEAM_POINTS`` / ``STEAM_TOTAL_POINTS``
  or more in the last ``STEAM_LOOKBACK``, across at least ``STEAM_MIN_BOOKS``
  books then and now.

These are information about the market and your own bets, never a bet signal:
nothing here is a model's opinion.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ttk import betting_math as bm
from ttk.db.models import Bet, Game, Parlay, Team, utcnow
from ttk.domain import BetResult, GameStatus, Market, Selection, Sport
from ttk.services.alerts import Alert, deliver
from ttk.services.line_history import consensus_main_line, line_points_gained
from ttk.services.odds_state import book_timelines

EASTERN = ZoneInfo("America/New_York")
BET_MOVE_POINTS = 1.5
SETTLED_RECENT = timedelta(hours=24)
STEAM_WINDOW = timedelta(hours=24)
STEAM_LOOKBACK = timedelta(minutes=60)
STEAM_POINTS = 1.0
STEAM_TOTAL_POINTS = 1.5
STEAM_MIN_BOOKS = 3
FORGET = timedelta(days=14)


def _points(x: float) -> str:
    return f"{x:g} point" + ("" if x == 1 else "s")


def _fmt_line(market: Market, line: float) -> str:
    if market is Market.TOTAL:
        return f"{line:g}"
    return "PK" if line == 0 else f"{line:+g}"


def bet_moves(session: Session, *, now: datetime) -> list[Alert]:
    alerts = []
    pending = session.execute(
        select(Bet, Game)
        .join(Game, Game.id == Bet.game_id)
        .where(
            Bet.result == BetResult.PENDING,
            Bet.parlay_id.is_(None),
            Bet.market.in_([Market.SPREAD, Market.TOTAL]),
            Game.commence_time > now,
        )
    ).all()
    for bet, game in pending:
        market, selection = Market(bet.market), Selection(bet.selection)
        ((current, books),) = consensus_main_line(session, game.id, market, selection, [now])
        gained = line_points_gained(market, selection, bet.line, current)
        if current is None or gained is None or abs(gained) < BET_MOVE_POINTS:
            continue
        way = "your way" if gained > 0 else "against you"
        alerts.append(
            Alert(
                f"bet-move-{bet.id}-{'for' if gained > 0 else 'against'}",
                f"Your bet {bet.description}: the market moved {_points(abs(gained))} {way} "
                f"(now {_fmt_line(market, current)}, {books} books).",
            )
        )
    return alerts


def settlements(session: Session, *, now: datetime) -> list[Alert]:
    alerts = []
    since = now - SETTLED_RECENT
    for bet in session.scalars(
        select(Bet).where(
            Bet.parlay_id.is_(None), Bet.settled_at.is_not(None), Bet.settled_at >= since
        )
    ):
        clv = "" if bet.clv is None else f", CLV {bet.clv:+.1%}"
        alerts.append(
            Alert(
                f"bet-settled-{bet.id}",
                f"Bet settled: {bet.result} {bet.description} "
                f"{bm.format_american(bet.american_odds)}, P/L {bet.profit_loss or 0:+.2f}{clv}.",
            )
        )
    for parlay in session.scalars(
        select(Parlay).where(Parlay.settled_at.is_not(None), Parlay.settled_at >= since)
    ):
        count = len(session.scalars(select(Bet.id).where(Bet.parlay_id == parlay.id)).all())
        alerts.append(
            Alert(
                f"parlay-settled-{parlay.id}",
                f"Parlay settled: {parlay.result} ({count} legs), "
                f"P/L {parlay.profit_loss or 0:+.2f}.",
            )
        )
    return alerts


def steam(session: Session, *, now: datetime, sports: Sequence[Sport]) -> list[Alert]:
    if not sports:
        return []
    alerts = []
    games = session.scalars(
        select(Game).where(
            Game.sport.in_([str(s) for s in sports]),
            Game.status == GameStatus.SCHEDULED,
            Game.commence_time > now,
            Game.commence_time <= now + STEAM_WINDOW,
            # Preseason lines swing on rest decisions, and no model covers them.
            or_(Game.season_type.is_(None), Game.season_type != "PRE"),
        )
    ).all()
    for game in games:
        home = session.get_one(Team, game.home_team_id).name
        away = session.get_one(Team, game.away_team_id).name
        kick = game.commence_time.astimezone(EASTERN)
        timelines = book_timelines(session, game.id)  # every market, loaded once
        for market, selection, threshold in (
            (Market.SPREAD, Selection.HOME, STEAM_POINTS),
            (Market.TOTAL, Selection.OVER, STEAM_TOTAL_POINTS),
        ):
            (before, n_before), (after, n_after) = consensus_main_line(
                session,
                game.id,
                market,
                selection,
                [now - STEAM_LOOKBACK, now],
                timelines=timelines,
            )
            if before is None or after is None or min(n_before, n_after) < STEAM_MIN_BOOKS:
                continue
            moved = after - before
            if abs(moved) < threshold:
                continue
            what = "home spread" if market is Market.SPREAD else "total"
            minutes = int(STEAM_LOOKBACK.total_seconds() // 60)
            alerts.append(
                Alert(
                    f"steam-{game.id}-{market}-{after:g}",
                    f"{game.sport} {away} @ {home}: {what} moved {_points(abs(moved))} in "
                    f"{minutes} min ({_fmt_line(market, before)} -> "
                    f"{_fmt_line(market, after)}, {n_after} books), "
                    f"{kick:%a %I:%M %p} ET.",
                )
            )
    return alerts


def run_bet_alerts(
    session: Session,
    *,
    data_dir: Path,
    steam_sports: Sequence[Sport] = (),
    now: datetime | None = None,
    notify: Callable[[str, str], None] | None = None,
) -> list[str]:
    """Send every event not sent before. Returns the lines sent."""
    now = now or utcnow()
    path = data_dir / "alerts" / "events.json"
    try:
        sent_before: dict[str, str] = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        sent_before = {}
    events = [
        *bet_moves(session, now=now),
        *settlements(session, now=now),
        *steam(session, now=now, sports=steam_sports),
    ]
    sent = []
    for event in events:
        if event.key in sent_before:
            continue
        sent_before[event.key] = now.isoformat()
        sent.append(f"EVENT {event.message}")
    kept = {k: t for k, t in sent_before.items() if now - datetime.fromisoformat(t) < FORGET}
    if sent or kept != sent_before:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(kept, indent=1), "utf-8")
    if sent:
        deliver(sent, data_dir=data_dir, notify=notify)
    return sent
