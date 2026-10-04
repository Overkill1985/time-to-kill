"""NBA injury features from our own injury-report history (pure; no I/O).

There is no historical source of injury status as of a given time, so these use
only ``injury_reports`` as the collector observed them (since 2026-09-27).

For a game and a horizon h (hours before tip-off), at T = tip - h:

- each rotation player of either team listed at T (latest report observed at or
  before T, not cleared) is expected to sit with probability q(status);
- ``injury_missing_diff_<h>h`` = home minus away of the sum of q x rotation weight
  x player value (the box-score values of research/nba_lineups.py).

q(status) is learned walk-forward: a game's features use the sit rates of
earlier finished games only (Beta prior ``STATUS_PRIORS``, two pseudo-games; a
status never seen gets 50/50). ESPN's NBA list says only "Out" or "Day-To-Day",
and how often a Day-To-Day player sits is exactly what has to be learned.

A model using the 1-hour horizon assumes the bet is placed an hour before tip
(score it against the close); the 24-hour one is an opener-time proxy.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ttk.models.elo import EloGame

HORIZONS_HOURS = (24, 1)
STATUS_PRIORS: dict[str, tuple[float, float]] = {
    # (sat, played) pseudo-counts: a weak prior, replaced by data as games finish
    "Out": (1.9, 0.1),
    "Doubtful": (1.6, 0.4),
    "Questionable": (1.0, 1.0),
    "Day-To-Day": (1.0, 1.0),
    "Probable": (0.4, 1.6),
}
UNKNOWN_PRIOR = (1.0, 1.0)


@dataclass(frozen=True)
class Report:
    player_id: str
    status: str
    cleared: bool
    observed_at: datetime
    team_id: int | None = None
    """The team the report lists him with; it decides which team he is missing from
    (rotations only learn of off-season moves once games are played)."""


@dataclass
class InjuryTimeline:
    """Each player's reports in observation order; ``status_at`` is what we knew."""

    _times: dict[str, list[datetime]] = field(default_factory=dict)
    _reports: dict[str, list[Report]] = field(default_factory=dict)

    @classmethod
    def build(cls, reports: Sequence[Report]) -> InjuryTimeline:
        timeline = cls()
        for r in sorted(reports, key=lambda r: r.observed_at):
            timeline._times.setdefault(r.player_id, []).append(r.observed_at)
            timeline._reports.setdefault(r.player_id, []).append(r)
        return timeline

    def status_at(self, player_id: str, at: datetime, team_id: int | None = None) -> str | None:
        """The player's status at ``at``; None if not listed then, or (with
        ``team_id``) listed with a different team."""
        times = self._times.get(player_id)
        if not times:
            return None
        i = bisect.bisect_right(times, at)
        if i == 0:
            return None  # first report came later
        report = self._reports[player_id][i - 1]
        if report.cleared:
            return None
        if team_id is not None and report.team_id is not None and report.team_id != team_id:
            return None  # he has moved on: not this team's absence
        return report.status

    @property
    def first_observed(self) -> datetime | None:
        firsts = [t[0] for t in self._times.values() if t]
        return min(firsts) if firsts else None


@dataclass
class SitRates:
    """Walk-forward sit counts per (horizon, status)."""

    counts: dict[tuple[int, str], list[float]] = field(default_factory=dict)

    def q(self, horizon: int, status: str) -> float:
        sat, played = self.counts.get((horizon, status), [0.0, 0.0])
        prior_sat, prior_played = STATUS_PRIORS.get(status, UNKNOWN_PRIOR)
        return (sat + prior_sat) / (sat + played + prior_sat + prior_played)

    def add(self, horizon: int, status: str, sat: bool) -> None:
        c = self.counts.setdefault((horizon, status), [0.0, 0.0])
        c[0 if sat else 1] += 1.0

    def observed(self, horizon: int, status: str) -> tuple[int, int]:
        sat, played = self.counts.get((horizon, status), [0.0, 0.0])
        return int(sat), int(sat + played)


def expected_missing(
    rotation: Mapping[str, tuple[float, float]],
    timeline: InjuryTimeline,
    rates: SitRates,
    *,
    team_id: int,
    at: datetime,
    horizon: int,
) -> float:
    """One team's expected missing value at ``at``: sum over its rotation of
    q(status) x rotation weight x value, for players listed then."""
    total = 0.0
    for player, (weight, value) in rotation.items():
        status = timeline.status_at(player, at, team_id)
        if status is not None:
            total += rates.q(horizon, status) * weight * value
    return total


def injury_features(
    games: Sequence[EloGame],
    snapshots: Mapping[tuple[int, int], Mapping[str, tuple[float, float]]],
    timeline: InjuryTimeline,
    played: Mapping[int, set[str]],
    *,
    horizons: Sequence[int] = HORIZONS_HOURS,
) -> tuple[dict[int, dict[str, float]], SitRates]:
    """Per game: ``injury_missing_diff_<h>h`` for games after injury tracking began,
    and the final sit rates. ``played``: game_id -> players who played (box score);
    a finished game with a box score teaches q after its features are taken."""
    rates = SitRates()
    out: dict[int, dict[str, float]] = {}
    start = timeline.first_observed
    if start is None:
        return out, rates
    for g in sorted(games, key=lambda g: (g.commence_time, g.game_id)):
        feats: dict[str, float] = {}
        listed: dict[int, list[tuple[str, str]]] = defaultdict(list)  # horizon -> (player, status)
        for h in horizons:
            at = g.commence_time - timedelta(hours=h)
            if at < start:
                continue  # we weren't watching yet: unknown, not "healthy"
            side = []
            for team in (g.home_id, g.away_id):
                total = 0.0
                for player, (weight, value) in snapshots.get((g.game_id, team), {}).items():
                    status = timeline.status_at(player, at, team)
                    if status is None:
                        continue
                    total += rates.q(h, status) * weight * value
                    listed[h].append((player, status))
                side.append(total)
            feats[f"injury_missing_diff_{h}h"] = side[0] - side[1]
        if feats:
            out[g.game_id] = feats
        who_played = played.get(g.game_id)
        if who_played is not None:
            for h, entries in listed.items():
                for player, status in entries:
                    rates.add(h, status, sat=player not in who_played)
    return out, rates
