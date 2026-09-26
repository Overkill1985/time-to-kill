"""Pregame NFL features from play-by-play aggregates, computed walk-forward.

Games are processed in time order: a game's features are read from the state
built by earlier games, then the game's own stats update the state. No game's
features can see its own or any later result.

Features (home minus away, in approximate points):
- ``epa_net_diff_pts``: team strength from EPA. Each team keeps exponentially
  weighted offensive EPA/play and defensive EPA/play allowed (its opponents'
  offense). net = offense - defense, x ``PLAYS_PER_GAME``.
- ``qb_change_diff_pts``: today's starting QB versus the QBs who produced the
  team's recent offense. A QB rating is shrunken EPA per dropback; the team's
  recent QB level is the dropback-weighted average of its recent passers. The
  difference x ``DROPBACKS_PER_GAME`` is how much better or worse the offense
  should be than its EPA history implies. The starter is the one known at
  kickoff (docs/MODEL-GOVERNANCE.md).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ttk.models.elo import EloGame

PLAYS_PER_GAME = 62.0
DROPBACKS_PER_GAME = 36.0


@dataclass(frozen=True)
class TeamGameEpa:
    game_id: int
    team_id: int
    plays: int
    epa_total: float
    dropbacks: int


@dataclass(frozen=True)
class QbGame:
    game_id: int
    team_id: int
    player_id: str
    dropbacks: int
    qb_epa_total: float


@dataclass(frozen=True)
class FeatureParams:
    team_half_life_games: float = 8.0
    """A team game's weight halves after this many further games by that team."""
    season_carryover: float = 0.5
    """Extra weight multiplier on all history when a new season starts."""
    team_prior_plays: float = 60.0
    """Pseudo-plays at league-average (0 EPA) shrinking thin team histories."""
    qb_half_life_games: float = 16.0
    qb_prior_dropbacks: float = 150.0
    qb_prior_epa: float = -0.10
    """Shrinkage target for QBs with little history (below-average backup level)."""
    team_qb_half_life_games: float = 4.0
    """How quickly the team's 'recent QB' mix follows a new passer."""


@dataclass(frozen=True)
class GameFeatures:
    game_id: int
    epa_net_diff_pts: float
    qb_change_diff_pts: float
    home_qb_rating: float | None
    away_qb_rating: float | None
    home_team_games: int
    away_team_games: int
    """Games of EPA history each side had; 0 means the feature is only the prior."""
    home_qb_history: float = 0.0
    away_qb_history: float = 0.0
    """Decayed dropbacks behind each starter's rating; 0 means prior only (or unknown)."""


def _decay(half_life: float) -> float:
    return math.pow(0.5, 1.0 / half_life)


@dataclass
class _Weighted:
    total: float = 0.0
    weight: float = 0.0

    def scale(self, factor: float) -> None:
        self.total *= factor
        self.weight *= factor

    def mean(self, prior_weight: float, prior_mean: float = 0.0) -> float:
        return (self.total + prior_weight * prior_mean) / (self.weight + prior_weight)


@dataclass
class _Team:
    offense: _Weighted = field(default_factory=_Weighted)
    defense: _Weighted = field(default_factory=_Weighted)
    qb_mix: dict[str, float] = field(default_factory=dict)
    season: int | None = None
    games: int = 0


@dataclass
class _Qb:
    epa: _Weighted = field(default_factory=_Weighted)
    season: int | None = None


def compute_features(
    games: Sequence[EloGame],
    team_stats: Sequence[TeamGameEpa],
    qb_stats: Sequence[QbGame],
    starters: Mapping[tuple[int, int], str],
    params: FeatureParams | None = None,
) -> dict[int, GameFeatures]:
    """``starters``: (game_id, team_id) -> starting QB player id."""
    params = params or FeatureParams()
    stats_by_game: dict[int, dict[int, TeamGameEpa]] = {}
    for s in team_stats:
        stats_by_game.setdefault(s.game_id, {})[s.team_id] = s
    qbs_by_game: dict[int, list[QbGame]] = {}
    for q in qb_stats:
        qbs_by_game.setdefault(q.game_id, []).append(q)

    teams: dict[int, _Team] = {}
    qbs: dict[str, _Qb] = {}
    team_decay = _decay(params.team_half_life_games)
    qb_decay = _decay(params.qb_half_life_games)
    mix_decay = _decay(params.team_qb_half_life_games)

    def team(tid: int, season: int) -> _Team:
        t = teams.setdefault(tid, _Team())
        if t.season is not None and t.season != season:
            t.offense.scale(params.season_carryover)
            t.defense.scale(params.season_carryover)
        t.season = season
        return t

    def qb_rating(pid: str) -> float:
        q = qbs.get(pid)
        epa = q.epa if q else _Weighted()
        return epa.mean(params.qb_prior_dropbacks, params.qb_prior_epa)

    def team_recent_qb(t: _Team) -> float | None:
        total = sum(t.qb_mix.values())
        if total == 0:
            return None
        return sum(w * qb_rating(pid) for pid, w in t.qb_mix.items()) / total

    def strength(t: _Team) -> float:
        prior = params.team_prior_plays
        return t.offense.mean(prior) - t.defense.mean(prior)

    def qb_change(t: _Team, starter: str | None) -> tuple[float, float | None, float]:
        if starter is None:
            return 0.0, None, 0.0
        rating = qb_rating(starter)
        recent = team_recent_qb(t)
        history = qbs[starter].epa.weight if starter in qbs else 0.0
        return (0.0 if recent is None else rating - recent), rating, history

    out: dict[int, GameFeatures] = {}
    for g in sorted(games, key=lambda g: (g.commence_time, g.game_id)):
        home, away = team(g.home_id, g.season), team(g.away_id, g.season)
        home_change, home_rating, home_hist = qb_change(home, starters.get((g.game_id, g.home_id)))
        away_change, away_rating, away_hist = qb_change(away, starters.get((g.game_id, g.away_id)))
        out[g.game_id] = GameFeatures(
            game_id=g.game_id,
            epa_net_diff_pts=(strength(home) - strength(away)) * PLAYS_PER_GAME,
            qb_change_diff_pts=(home_change - away_change) * DROPBACKS_PER_GAME,
            home_qb_rating=home_rating,
            away_qb_rating=away_rating,
            home_team_games=home.games,
            away_team_games=away.games,
            home_qb_history=home_hist,
            away_qb_history=away_hist,
        )

        # Update state with this game's own stats (after its features were taken).
        game_stats = stats_by_game.get(g.game_id, {})
        for tid, opp_id, t in ((g.home_id, g.away_id, home), (g.away_id, g.home_id, away)):
            own, opp = game_stats.get(tid), game_stats.get(opp_id)
            if own is None or opp is None:
                continue
            t.offense.scale(team_decay)
            t.defense.scale(team_decay)
            t.offense.total += own.epa_total
            t.offense.weight += own.plays
            t.defense.total += opp.epa_total
            t.defense.weight += opp.plays
            t.games += 1
            for pid in t.qb_mix:
                t.qb_mix[pid] *= mix_decay
        for q in qbs_by_game.get(g.game_id, []):
            state = qbs.setdefault(q.player_id, _Qb())
            if state.season is not None and state.season != g.season:
                state.epa.scale(params.season_carryover)
            state.season = g.season
            state.epa.scale(qb_decay)
            state.epa.total += q.qb_epa_total
            state.epa.weight += q.dropbacks
            mix = teams[q.team_id].qb_mix if q.team_id in teams else None
            if mix is not None:
                mix[q.player_id] = mix.get(q.player_id, 0.0) + q.dropbacks
    return out
