"""Does the NFL spread move after injury news, and would a bet at the news beat
the close? (docs/MODELS.md, "NFL injury reports": the close prices the final
report, so any edge is in timing.)

An event is a change in a player's ESPN injury status, as first seen by our
poll (``injury_reports`` change log), for a regular (``MIN_SHARE`` of snaps in
his team's earlier games this season, nflverse snap counts). Its weight is the
change in his chance of sitting (``P_SIT``, measured on 2012-2017 reports) x his
snap share; positive is bad news for his team.

For the team's next game, the consensus main spread is read 6 h before the news,
at it, 3 h after and at kickoff (median of books seen within 2 h). ``gain`` is
the points a bet against the news (against the team on bad news, on it on good
news) at the line when we saw it would have gained by kickoff; ``pre_move`` is
how far the line had already moved that way in the 6 h before.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ttk.db.models import Game, InjuryReport, Team
from ttk.domain import GameStatus, Market, Selection, Sport
from ttk.providers.nflverse import TEAMS, SnapRow
from ttk.services.line_history import consensus_main_line
from ttk.services.odds_state import BookTimeline, book_timelines
from ttk.services.props import player_key

P_SIT = {"Out": 1.0, "Injured Reserve": 1.0, "Doubtful": 0.99, "Questionable": 0.34}
"""P(sits | final status), nflverse reports 2012-2017 (docs/MODELS.md); any other
status (Active, cleared) counts as 0."""
MIN_SHARE = 0.5
MIN_WEIGHT = 0.15
MIN_LEAD = timedelta(minutes=30)
MAX_LEAD = timedelta(days=4)
"""News is within the game week; earlier flips are mostly the weekly rollover."""
ROLLOVER = timedelta(hours=48)
"""ESPN rolls every listed status over to the next week on the Monday or Tuesday
after a game (many flips at one poll, e.g. Out -> Questionable): changes within
this long after the team's previous kickoff are not news and are skipped."""


@dataclass(frozen=True)
class Shares:
    """(ESPN team id, player key) -> [(week, share)] and the player's position."""

    weeks: dict[tuple[str, str], list[tuple[int, float]]]
    position: dict[tuple[str, str], str]

    def before(self, espn_team: str, key: str, week: int) -> float | None:
        prior = [s for w, s in self.weeks.get((espn_team, key), []) if w < week]
        return sum(prior) / len(prior) if prior else None


def snap_shares(rows: Iterable[SnapRow]) -> Shares:
    weeks: dict[tuple[str, str], list[tuple[int, float]]] = defaultdict(list)
    position: dict[tuple[str, str], str] = {}
    for r in rows:
        if r.game_type != "REG" or r.team not in TEAMS:
            continue
        k = (TEAMS[r.team][0], player_key(r.player))
        weeks[k].append((r.week, r.share))
        position[k] = r.position
    return Shares(dict(weeks), position)


@dataclass(frozen=True)
class InjuryMove:
    player: str
    position: str
    status: str
    previous: str | None
    observed_at: datetime
    game_id: int
    home: bool
    share: float
    weight: float
    line_before: float | None
    line_at: float
    line_3h: float | None
    line_close: float
    gain: float
    pre_move: float | None


def _p(status: str | None) -> float:
    return P_SIT.get(status or "", 0.0)


def injury_moves(
    session: Session,
    shares: Shares,
    *,
    min_share: float = MIN_SHARE,
    min_weight: float = MIN_WEIGHT,
) -> list[InjuryMove]:
    rows = session.scalars(
        select(InjuryReport)
        .where(InjuryReport.sport == Sport.NFL, InjuryReport.provider == "espn")
        .order_by(InjuryReport.observed_at, InjuryReport.id)
    ).all()
    if not rows:
        return []
    first_poll = rows[0].observed_at  # the initial load is a state, not news
    espn_team = dict(session.execute(select(Team.id, Team.espn_id)).all())
    last: dict[tuple[int | None, str], str | None] = {}
    out: list[InjuryMove] = []
    timelines_cache: dict[int, dict[int, BookTimeline]] = {}
    for row in rows:
        pkey = player_key(row.player_name)
        k = (row.team_id, pkey)
        previous = last.get(k)
        status = None if row.cleared else row.status
        last[k] = status
        if row.observed_at == first_poll or row.team_id is None:
            continue
        delta = _p(status) - _p(previous)
        if abs(delta) < min_weight:
            continue
        game = session.scalars(
            select(Game)
            .where(
                Game.sport == Sport.NFL,
                or_(Game.home_team_id == row.team_id, Game.away_team_id == row.team_id),
                Game.commence_time > row.observed_at + MIN_LEAD,
                Game.commence_time <= row.observed_at + MAX_LEAD,
                Game.status != GameStatus.DUPLICATE,
            )
            .order_by(Game.commence_time)
            .limit(1)
        ).first()
        team = espn_team.get(row.team_id)
        if game is None or game.week is None or team is None:
            continue
        previous_kick = session.scalar(
            select(Game.commence_time)
            .where(
                Game.sport == Sport.NFL,
                or_(Game.home_team_id == row.team_id, Game.away_team_id == row.team_id),
                Game.commence_time <= row.observed_at,
                Game.status != GameStatus.DUPLICATE,
            )
            .order_by(Game.commence_time.desc())
            .limit(1)
        )
        if previous_kick is not None and row.observed_at - previous_kick < ROLLOVER:
            continue
        share = shares.before(team, pkey, game.week)
        if share is None or share < min_share:
            continue
        if game.id not in timelines_cache:
            timelines_cache[game.id] = book_timelines(session, game.id, Market.SPREAD)
        t = row.observed_at
        (before, _), (at, _), (after, _), (close, _) = consensus_main_line(
            session,
            game.id,
            Market.SPREAD,
            Selection.HOME,
            [t - timedelta(hours=6), t, t + timedelta(hours=3), game.commence_time],
            timelines=timelines_cache[game.id],
        )
        if at is None or close is None:
            continue
        home = game.home_team_id == row.team_id
        # Bad news for the home team pushes the home line up (less favored).
        toward = (1.0 if home else -1.0) * (1.0 if delta > 0 else -1.0)
        out.append(
            InjuryMove(
                row.player_name,
                shares.position.get((team, pkey), ""),
                status or "cleared",
                previous,
                t,
                game.id,
                home,
                share,
                delta * share,
                before,
                at,
                after,
                close,
                toward * (close - at),
                toward * (at - before) if before is not None else None,
            )
        )
    return out


@dataclass(frozen=True)
class MoveSummary:
    label: str
    events: int
    games: int
    mean_gain: float | None
    se_gain: float | None
    mean_pre_move: float | None
    positive: int
    """Events where the bet at the news beat the close by any amount."""
    negative: int


def summarize(label: str, moves: Sequence[InjuryMove]) -> MoveSummary:
    gains = [m.gain for m in moves]
    pre = [m.pre_move for m in moves if m.pre_move is not None]
    se = statistics.stdev(gains) / math.sqrt(len(gains)) if len(gains) > 1 else None
    return MoveSummary(
        label,
        len(moves),
        len({m.game_id for m in moves}),
        statistics.fmean(gains) if gains else None,
        se,
        statistics.fmean(pre) if pre else None,
        sum(1 for g in gains if g > 0),
        sum(1 for g in gains if g < 0),
    )


def summaries(moves: Sequence[InjuryMove]) -> list[MoveSummary]:
    groups: Mapping[str, list[InjuryMove]] = {
        "all": list(moves),
        "bad news, out-type (Out/Doubtful/IR)": [
            m for m in moves if m.weight > 0 and _p(m.status) >= 0.99
        ],
        "bad news, questionable": [m for m in moves if m.weight > 0 and m.status == "Questionable"],
        "good news (upgraded or cleared)": [m for m in moves if m.weight < 0],
        "quarterbacks": [m for m in moves if m.position == "QB"],
    }
    return [summarize(label, ms) for label, ms in groups.items()]
