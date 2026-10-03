"""College football preseason features per game (pure; no I/O).

From team_season_features (CollegeFootballData, services/cfbd_import.py). For each
game, home minus away, using only facts with ``known_at`` before kickoff:

- **Roster quality, all season:** ``talent_diff`` (247 talent composite) and
  ``recruiting_diff`` (mean of the last four recruiting classes' points), each
  z-scored across that season's teams.
- **Change since last season, early season:** ``returning_diff`` (share of last
  season's production returning), ``returning_passing_diff`` (passing share - is the
  quarterback back?), ``new_coach_diff``, ``portal_diff`` (incoming minus outgoing
  transfer ratings, z-scored) and ``ap_diff`` (preseason AP points / 1,000). These
  are multiplied by 0.5 ** (games the team has played this season / 4): Elo learns
  from results as the season goes on.

A fact a team lacks (FCS teams have little of this) contributes 0 for that team.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from ttk.models.elo import EloGame

PRESEASON_FEATURES = (
    "talent_diff",
    "recruiting_diff",
    "returning_diff",
    "returning_passing_diff",
    "new_coach_diff",
    "portal_diff",
    "ap_diff",
)
EARLY_HALF_LIFE_GAMES = 4.0
RECRUITING_CLASSES = 4


@dataclass(frozen=True)
class SeasonFact:
    season: int
    team_id: int
    name: str
    value: float
    known_at: datetime | None


def _zscores(values: dict[int, float]) -> dict[int, float]:
    if len(values) < 2:
        return {}
    mean = statistics.fmean(values.values())
    sd = statistics.pstdev(values.values())
    return {team: (v - mean) / sd for team, v in values.items()} if sd else {}


def _team_season_values(
    facts: Sequence[SeasonFact],
) -> dict[tuple[int, int], dict[str, tuple[float, datetime | None]]]:
    """(season, team) -> derived feature -> (value, known_at)."""
    raw: dict[tuple[int, int], dict[str, SeasonFact]] = defaultdict(dict)
    for fact in facts:
        raw[(fact.season, fact.team_id)][fact.name] = fact
    out: dict[tuple[int, int], dict[str, tuple[float, datetime | None]]] = defaultdict(dict)
    seasons = sorted({s for s, _ in raw})
    for season in seasons:
        teams = [t for s, t in raw if s == season]

        def get(team: int, name: str, year: int = season) -> SeasonFact | None:
            return raw.get((year, team), {}).get(name)

        talent = {t: f.value for t in teams if (f := get(t, "talent")) is not None}
        recruiting: dict[int, float] = {}
        recruiting_known: dict[int, datetime | None] = {}
        for t in teams:
            classes = [
                f
                for y in range(season - RECRUITING_CLASSES + 1, season + 1)
                if (f := get(t, "recruiting_points", y)) is not None
            ]
            if classes:
                recruiting[t] = statistics.fmean(c.value for c in classes)
                recruiting_known[t] = max(
                    (c.known_at for c in classes if c.known_at is not None), default=None
                )
        portal = {
            t: (fi.value if (fi := get(t, "portal_in_rating")) else 0.0)
            - (fo.value if (fo := get(t, "portal_out_rating")) else 0.0)
            for t in teams
            if get(t, "portal_in_rating") or get(t, "portal_out_rating")
        }
        for name, values in (("talent", talent), ("recruiting", recruiting), ("portal", portal)):
            for t, z in _zscores(values).items():
                if name == "recruiting":
                    known = recruiting_known.get(t)
                else:
                    sources = (
                        [get(t, "talent")]
                        if name == "talent"
                        else [get(t, "portal_in_rating"), get(t, "portal_out_rating")]
                    )
                    known = max(
                        (f.known_at for f in sources if f is not None and f.known_at), default=None
                    )
                out[(season, t)][name] = (z, known)
        for t in teams:
            for src, name, scale in (
                ("returning_ppa_pct", "returning", 1.0),
                ("returning_passing_ppa_pct", "returning_passing", 1.0),
                ("new_head_coach", "new_coach", 1.0),
                ("preseason_ap_points", "ap", 1 / 1000),
            ):
                f = get(t, src)
                if f is not None:
                    out[(season, t)][name] = (f.value * scale, f.known_at)
    return out


EARLY = {"returning", "returning_passing", "new_coach", "portal", "ap"}


def preseason_features(
    games: Sequence[EloGame], facts: Sequence[SeasonFact]
) -> dict[int, dict[str, float]]:
    """Per game: each PRESEASON_FEATURES value (home minus away)."""
    values = _team_season_values(facts)
    played: dict[tuple[int, int], int] = defaultdict(int)
    out: dict[int, dict[str, float]] = {}
    for g in sorted(games, key=lambda g: (g.commence_time, g.game_id)):
        feats: dict[str, float] = {}
        sides = []
        for team in (g.home_id, g.away_id):
            n = played[(g.season, team)]
            weight = 0.5 ** (n / EARLY_HALF_LIFE_GAMES)
            known = {
                name: v * (weight if name in EARLY else 1.0)
                for name, (v, at) in values.get((g.season, team), {}).items()
                if at is not None and at <= g.commence_time  # undated: never known
            }
            sides.append(known)
        for name in (
            "talent",
            "recruiting",
            "returning",
            "returning_passing",
            "new_coach",
            "portal",
            "ap",
        ):
            feats[f"{name}_diff"] = sides[0].get(name, 0.0) - sides[1].get(name, 0.0)
        out[g.game_id] = feats
        for team in (g.home_id, g.away_id):
            played[(g.season, team)] += 1
    return out
