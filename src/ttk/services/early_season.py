"""Forward results split by how far into its season a game was.

A frozen rating model starts each season on last season's ratings, regressed; in
college basketball, where rosters turn over most, its early-season records say more
about the carryover than about the model. Each record is put in a bucket by the
games the less-experienced team had finished that season before kickoff, so that a
November verdict can be read against the rest. Descriptive only: it picks nothing.
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ttk.db.models import Game
from ttk.domain import GameStatus, Sport
from ttk.services.forward_test import ScoredRow, scored_rows

BUCKETS: tuple[tuple[int, int | None, str], ...] = (
    (0, 4, "0-4 games"),
    (5, 14, "5-14 games"),
    (15, None, "15+ games"),
)


@dataclass
class BucketScore:
    model: str
    horizon_hours: int
    bucket: str
    decided: int = 0
    wins: int = 0
    losses: int = 0
    paired_diff: float | None = None
    """Model minus market log loss (negative = the model is better)."""
    z: float | None = None
    points_vs_close: float | None = None
    points_n: int = 0


def bucket_of(games_played: int) -> str:
    for low, high, label in BUCKETS:
        if games_played >= low and (high is None or games_played <= high):
            return label
    raise ValueError(games_played)


def games_played_before(session: Session, game_ids: Iterable[int]) -> dict[int, int]:
    """Per game: the fewer finished games either team had played that season before
    its kickoff."""
    targets = list(session.scalars(select(Game).where(Game.id.in_(set(game_ids)))))
    if not targets:
        return {}
    keys = {(t, g.season) for g in targets for t in (g.home_team_id, g.away_team_id)}
    teams = {t for t, _ in keys}
    played: dict[tuple[int, int | None], list[datetime]] = defaultdict(list)
    for g in session.scalars(
        select(Game).where(
            Game.status == GameStatus.FINAL,
            Game.home_team_id.in_(teams) | Game.away_team_id.in_(teams),
        )
    ):
        for t in (g.home_team_id, g.away_team_id):
            if (t, g.season) in keys:
                played[(t, g.season)].append(g.commence_time)
    for times in played.values():
        times.sort()
    return {
        g.id: min(
            bisect.bisect_left(played[(t, g.season)], g.commence_time)
            for t in (g.home_team_id, g.away_team_id)
        )
        for g in targets
    }


def _ll(p: float, y: int) -> float:
    p = min(max(p, 1e-9), 1 - 1e-9)
    return -math.log(p if y else 1 - p)


def split(rows: Sequence[ScoredRow], played: dict[int, int]) -> list[BucketScore]:
    groups: dict[tuple[str, int, str], list[ScoredRow]] = defaultdict(list)
    for r in rows:
        if r.game_id in played:
            groups[(r.model, r.horizon_hours, bucket_of(played[r.game_id]))].append(r)
    order = {label: i for i, (_, _, label) in enumerate(BUCKETS)}
    out = []
    for (model, h, bucket), rs in sorted(
        groups.items(), key=lambda kv: (kv[0][0], -kv[0][1], order[kv[0][2]])
    ):
        score = BucketScore(model, h, bucket)
        diffs = []
        for r in rs:
            if r.cover_margin == 0:
                continue
            y = int(r.cover_margin > 0)
            diffs.append(_ll(r.model_home_cover, y) - _ll(r.market_home_cover, y))
            if r.won:
                score.wins += 1
            else:
                score.losses += 1
        score.decided = len(diffs)
        if diffs:
            score.paired_diff = statistics.fmean(diffs)
            if len(diffs) > 1 and (sd := statistics.stdev(diffs)) > 0:
                score.z = score.paired_diff / (sd / math.sqrt(len(diffs)))
        points = [r.points_vs_close for r in rs if r.points_vs_close is not None]
        if points:
            score.points_vs_close, score.points_n = statistics.fmean(points), len(points)
        out.append(score)
    return out


def games_played_split(session: Session, sport: Sport | None = Sport.NCAAB) -> list[BucketScore]:
    rows = scored_rows(session, sport)
    return split(rows, games_played_before(session, {r.game_id for r in rows}))
