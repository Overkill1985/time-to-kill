"""Walk-forward NBA player availability from box scores (pure; no I/O).

For each game, before its own box score is seen:

- **Player value**: a recency-weighted mean of Hollinger Game Score over the
  player's previous games (any team), shrunk toward a replacement level for
  players with little history.
- **Rotation weight** a(team, player): how regularly the player has played for
  this team recently (1 = every game), decayed per team game. A player who
  appears for a new team leaves his old team's rotation (trades are not absences).
  Weights are halved at a new season (free agency, retirements).
- **missing_at_tip** = sum over the team's rotation of a x value for players who
  did not play this game. Known only once lineups are out: a model using it
  assumes the bet is placed then (scored against the close, never the opener).
- **missing_prev** = the team's missing_at_tip in its previous game. Known before
  the opener: the legitimate early-line proxy ("who sat out last time").
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ttk.models.elo import EloGame

REPLACEMENT_GAME_SCORE = 5.0
PRIOR_GAMES = 2.0
VALUE_HALF_LIFE = 20.0  # player games
ROTATION_HALF_LIFE = 5.0  # team games
SEASON_CARRYOVER = 0.5


@dataclass(frozen=True)
class BoxRow:
    player_id: str
    team_id: int
    played: bool
    game_score: float


def hollinger_game_score(
    *,
    points: int | None,
    fgm: int | None,
    fga: int | None,
    ftm: int | None,
    fta: int | None,
    oreb: int | None,
    dreb: int | None,
    ast: int | None,
    stl: int | None,
    blk: int | None,
    tov: int | None,
    pf: int | None,
) -> float:
    def v(x: int | None) -> float:
        return float(x or 0)

    return (
        v(points)
        + 0.4 * v(fgm)
        - 0.7 * v(fga)
        - 0.4 * (v(fta) - v(ftm))
        + 0.7 * v(oreb)
        + 0.3 * v(dreb)
        + v(stl)
        + 0.7 * v(ast)
        + 0.7 * v(blk)
        - 0.4 * v(pf)
        - v(tov)
    )


@dataclass(frozen=True)
class Availability:
    home_missing: float
    away_missing: float
    home_missing_prev: float
    away_missing_prev: float

    @property
    def missing_diff(self) -> float:
        """Home minus away value missing at tip (positive = home is weaker)."""
        return self.home_missing - self.away_missing

    @property
    def missing_prev_diff(self) -> float:
        return self.home_missing_prev - self.away_missing_prev


def availability(
    games: Sequence[EloGame],
    box: Mapping[int, Sequence[BoxRow]],
    *,
    value_half_life: float = VALUE_HALF_LIFE,
    rotation_half_life: float = ROTATION_HALF_LIFE,
    snapshots: dict[tuple[int, int], dict[str, tuple[float, float]]] | None = None,
) -> dict[int, Availability]:
    """Features for every game with a box score, computed in time order from
    earlier games only. Games without a box score get no entry (and teach nothing).

    ``snapshots``, if given, is filled for every game (upcoming ones too) with each
    team's rotation before it: (game_id, team_id) -> player -> (rotation weight,
    value). Injury features (research/nba_injuries.py) are built on it."""
    value_decay = 0.5 ** (1.0 / value_half_life)
    rotation_decay = 0.5 ** (1.0 / rotation_half_life)
    value_sum: dict[str, float] = defaultdict(float)
    value_weight: dict[str, float] = defaultdict(float)
    rotation: dict[int, dict[str, float]] = defaultdict(dict)
    current_team: dict[str, int] = {}
    last_missing: dict[int, float] = {}
    season: int | None = None
    out: dict[int, Availability] = {}

    def value(player: str) -> float:
        w = value_weight[player]
        return (value_sum[player] + REPLACEMENT_GAME_SCORE * PRIOR_GAMES) / (w + PRIOR_GAMES)

    for g in sorted(games, key=lambda g: (g.commence_time, g.game_id)):
        rows = box.get(g.game_id)
        if season is not None and g.season != season:
            for team_rotation in rotation.values():
                for player in team_rotation:
                    team_rotation[player] *= SEASON_CARRYOVER
        season = g.season
        if snapshots is not None:  # every game, upcoming ones included
            for team in (g.home_id, g.away_id):
                snapshots[(g.game_id, team)] = {p: (a, value(p)) for p, a in rotation[team].items()}
        if not rows:
            continue
        missing = {}
        played_by_team: dict[int, set[str]] = defaultdict(set)
        for row in rows:
            if row.played:
                played_by_team[row.team_id].add(row.player_id)
        for team in (g.home_id, g.away_id):
            played = played_by_team[team]
            missing[team] = sum(a * value(p) for p, a in rotation[team].items() if p not in played)
        out[g.game_id] = Availability(
            missing[g.home_id],
            missing[g.away_id],
            last_missing.get(g.home_id, 0.0),
            last_missing.get(g.away_id, 0.0),
        )
        # ---- learn from this game (after its features are fixed)
        last_missing.update(missing)
        for team in (g.home_id, g.away_id):
            played = played_by_team[team]
            for p in played:
                previous = current_team.get(p)
                if previous is not None and previous != team:
                    rotation[previous].pop(p, None)  # traded or signed elsewhere
                current_team[p] = team
                rotation[team].setdefault(p, 0.0)
            for p in rotation[team]:
                rotation[team][p] = rotation[team][p] * rotation_decay + (
                    (1.0 - rotation_decay) if p in played else 0.0
                )
        for row in rows:
            if row.played:
                value_sum[row.player_id] = value_sum[row.player_id] * value_decay + row.game_score
                value_weight[row.player_id] = value_weight[row.player_id] * value_decay + 1.0
    return out
