"""NFL prop model v0.2: v0.1's recent usage, adjusted for the opposing defense.

A defense's factor for a stat and a position group (QB, RB, WR, TE) is what it has
allowed that group per game in its earlier games - exponentially weighted (half-life
``defense_half_life`` games, last season's x``defense_carryover``) and shrunk toward
the league average by ``shrink`` pseudo-games - divided by that league average
(TRAIN seasons). The player's expected stat is v0.1's usage mean x factor^beta, with
``beta`` per stat fitted on TRAIN (0 = matchups don't matter, 1 = fully
proportional). Distributions refitted on TRAIN around the adjusted means.

Passing stats are matched to the defense's passing allowed to QBs; rushing and
receiving to the player's own position group; TDs likewise.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from ttk.research import nba_props as nb
from ttk.research import prop_model as pm

ARTIFACT_VERSION = 2
"""1: v0.2 (usage x defense). 2: v0.3, also shrinking a player's usage toward his
position's average by ``shrink[stat]`` pseudo-games (few games: mostly the average)."""
GRID_PLAYER_SHRINK = (0.0, 1.0, 2.0, 4.0, 8.0)
GROUPS = {"QB": "QB", "RB": "RB", "FB": "RB", "WR": "WR", "TE": "TE"}
GRID_DEFENSE_HALF_LIFE = (4.0, 8.0, 16.0)
GRID_SHRINK = (2.0, 4.0, 8.0)
GRID_BETA = (0.0, 0.25, 0.5, 0.75, 1.0, 1.25)
DEFENSE_CARRYOVER = 0.5


def group_of(position: str) -> str | None:
    return GROUPS.get(position.upper())


@dataclass(frozen=True)
class DefenseParams:
    half_life: float
    shrink: float
    carryover: float = DEFENSE_CARRYOVER


class Defenses:
    """Walk-forward defense factors per (defense team, group, stat)."""

    def __init__(
        self, games: Sequence[pm.PlayerGame], params: DefenseParams, league: Mapping[str, float]
    ) -> None:
        self.params = params
        self.league = dict(league)
        """'group|stat' -> league mean allowed per team-game (TRAIN)."""
        # (defense, group) -> [(kickoff, season, game_id, {stat: allowed in that game})]
        per_game: dict[tuple[int, str, int], dict[str, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        meta: dict[tuple[int, str, int], tuple[datetime, int]] = {}
        for g in games:
            group = group_of(g.position)
            if group is None or not g.opponent_id:
                continue
            key = (g.opponent_id, group, g.game_id)
            for stat, v in g.values.items():
                per_game[key][stat] += v
            meta[key] = (g.kickoff, g.season)
        self.series: dict[tuple[int, str], list[tuple[datetime, int, dict[str, float]]]] = (
            defaultdict(list)
        )
        for (defense, group, _), allowed in per_game.items():
            kick, season = meta[(defense, group, _)]
            self.series[(defense, group)].append((kick, season, dict(allowed)))
        for s in self.series.values():
            s.sort(key=lambda x: x[0])

    def factor(self, defense: int, group: str, stat: str, before: datetime, season: int) -> float:
        lg = self.league.get(f"{group}|{stat}")
        if not lg:
            return 1.0
        decay = 0.5 ** (1 / self.params.half_life)
        total = weight = 0.0
        w = 1.0
        for kick, s, allowed in reversed(self.series.get((defense, group), [])):
            if kick >= before:
                continue
            ww = w * (1.0 if s == season else self.params.carryover)
            total += ww * allowed.get(stat, 0.0)
            weight += ww
            w *= decay
            if w < 1e-3:
                break
        n0 = self.params.shrink
        # Clamped: kneel-downs can make rushing yards allowed negative.
        return float(np.clip(((total + n0 * lg) / (weight + n0)) / lg, 0.25, 4.0))


def league_means(games: Iterable[pm.PlayerGame], seasons: tuple[int, int]) -> dict[str, float]:
    """Mean allowed per team-game by group and stat, over ``seasons`` only."""
    sums: dict[tuple[int, str, int], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for g in games:
        group = group_of(g.position)
        if group is None or not g.opponent_id or not seasons[0] <= g.season <= seasons[1]:
            continue
        for stat, v in g.values.items():
            sums[(g.opponent_id, group, g.game_id)][stat] += v
    by: dict[str, list[float]] = defaultdict(list)
    for (_, group, _), allowed in sums.items():
        for stat, v in allowed.items():
            by[f"{group}|{stat}"].append(v)
    return {k: float(np.mean(v)) for k, v in by.items()}


def position_priors(games: Iterable[pm.PlayerGame], seasons: tuple[int, int]) -> dict[str, float]:
    """Mean of each stat per player-game by position group, over ``seasons``."""
    by: dict[str, list[float]] = defaultdict(list)
    for g in games:
        group = group_of(g.position)
        if group is None or not seasons[0] <= g.season <= seasons[1]:
            continue
        for stat, v in g.values.items():
            by[f"{group}|{stat}"].append(v)
    return {k: float(np.mean(v)) for k, v in by.items()}


def shrunk(mean: float, games: int, prior: float | None, k: float) -> float:
    if prior is None or k <= 0:
        return mean
    n = min(games, 17)
    return (n * mean + k * prior) / (n + k)


def defense_group(market: str, position: str) -> str | None:
    """Which allowed-stat a player's prop is matched to."""
    if market.startswith("player_pass_"):
        return "QB"
    return group_of(position)


@dataclass
class MatchupBacktest:
    usage: pm.Params
    defense: DefenseParams
    beta: dict[str, float]
    dists: dict[str, pm.Dist]
    league: dict[str, float]
    validate: list[pm.StatReport] = field(default_factory=list)
    """Against the previous version on VALIDATE (z > 0: this one is better)."""
    shrink: dict[str, float] = field(default_factory=dict)
    """v0.3: pseudo-games of the position average added to a player's usage, per stat."""
    priors: dict[str, float] = field(default_factory=dict)
    """v0.3: 'group|stat' -> position average per player-game (TRAIN)."""


def _triples(
    games: Sequence[pm.PlayerGame],
    means: pm.Means,
    defenses: Defenses,
    market: str,
    seasons: tuple[int, int],
) -> list[tuple[float, float, float]]:
    """(usage mean, defense factor, actual) for player-games in ``seasons``."""
    out = []
    for g, history in means.walk():
        if not seasons[0] <= g.season <= seasons[1]:
            continue
        m = means.mean(history, g.season, market)
        if m is None or m[0] < pm.MIN_MEAN[market]:
            continue
        group = defense_group(market, g.position)
        f = (
            defenses.factor(g.opponent_id, group, market, g.kickoff, g.season)
            if group and g.opponent_id
            else 1.0
        )
        out.append((m[0], f, g.values[market]))
    return out


def backtest(games: Sequence[pm.PlayerGame], usage: pm.Params) -> MatchupBacktest:
    league = league_means(games, pm.TRAIN)
    means = pm.Means(games, usage)
    best: tuple[float, DefenseParams, dict[str, float], dict[str, pm.Dist]] | None = None
    for h in GRID_DEFENSE_HALF_LIFE:
        for n0 in GRID_SHRINK:
            dp = DefenseParams(h, n0)
            defenses = Defenses(games, dp, league)
            total = 0.0
            betas: dict[str, float] = {}
            dists: dict[str, pm.Dist] = {}
            for market in pm.STATS:
                triples = _triples(games, means, defenses, market, pm.TRAIN)
                best_m: tuple[float, float, pm.Dist] | None = None
                for beta in GRID_BETA:
                    pairs = [(m * f**beta, y) for m, f, y in triples]
                    dist = pm.fit_dist(market, pairs)
                    ll = sum(dist.log_lik(m, y) for m, y in pairs) / max(1, len(pairs))
                    if best_m is None or ll > best_m[0]:
                        best_m = (ll, beta, dist)
                assert best_m is not None
                total += best_m[0]
                betas[market], dists[market] = best_m[1], best_m[2]
            if best is None or total > best[0]:
                best = (total, dp, betas, dists)
    assert best is not None
    _, dp, betas, dists = best
    result = MatchupBacktest(usage, dp, betas, dists, league)
    # VALIDATE: v0.2 against v0.1 (v0.1's own distributions, refit on TRAIN).
    defenses = Defenses(games, dp, league)
    for market in pm.STATS:
        base_pairs = pm._pairs(means, market, pm.TRAIN)
        base = pm.fit_dist(market, base_pairs)
        triples = _triples(games, means, defenses, market, pm.VALIDATE)
        d, new_ll, old_ll = [], [], []
        for m, f, y in triples:
            a = max(dists[market].log_lik(m * f ** betas[market], y), pm.LOG_LIK_FLOOR)
            b = max(base.log_lik(m, y), pm.LOG_LIK_FLOOR)
            new_ll.append(a)
            old_ll.append(b)
            d.append(a - b)
        arr = np.array(d)
        z = float(arr.mean() / (arr.std(ddof=1) / math.sqrt(len(arr)))) if len(arr) > 1 else 0.0
        result.validate.append(
            pm.StatReport(market, len(arr), float(np.mean(new_ll)), float(np.mean(old_ll)), z)
        )
    return result


def backtest_v03(
    games: Sequence[pm.PlayerGame],
    previous: MatchupBacktest,
) -> MatchupBacktest:
    """v0.3: v0.2's usage and defense settings; per stat, the player shrink and beta
    chosen together by TRAIN log-likelihood; checked on VALIDATE against v0.2."""
    priors = position_priors(games, pm.TRAIN)
    means = pm.Means(games, previous.usage)
    defenses = Defenses(games, previous.defense, previous.league)

    def rows(market: str, seasons: tuple[int, int]) -> list[tuple[float, int, float, float, str]]:
        out = []
        for g, history in means.walk():
            if not seasons[0] <= g.season <= seasons[1]:
                continue
            m = means.mean(history, g.season, market)
            if m is None or m[0] < pm.MIN_MEAN[market]:
                continue
            group = defense_group(market, g.position)
            f = (
                defenses.factor(g.opponent_id, group, market, g.kickoff, g.season)
                if group and g.opponent_id
                else 1.0
            )
            out.append((m[0], m[1], f, g.values[market], group_of(g.position) or ""))
        return out

    betas: dict[str, float] = {}
    shrinks: dict[str, float] = {}
    dists: dict[str, pm.Dist] = {}
    for market in pm.STATS:
        train = rows(market, pm.TRAIN)
        best: tuple[float, float, float, pm.Dist] | None = None
        for k in GRID_PLAYER_SHRINK:
            for beta in GRID_BETA:
                pairs = [
                    (shrunk(m, n, priors.get(f"{grp}|{market}"), k) * f**beta, y)
                    for m, n, f, y, grp in train
                ]
                dist = pm.fit_dist(market, pairs)
                ll = sum(dist.log_lik(mm, y) for mm, y in pairs) / max(1, len(pairs))
                if best is None or ll > best[0]:
                    best = (ll, k, beta, dist)
        assert best is not None
        _, shrinks[market], betas[market], dists[market] = best
    result = MatchupBacktest(
        previous.usage,
        previous.defense,
        betas,
        dists,
        previous.league,
        shrink=shrinks,
        priors=priors,
    )
    for market in pm.STATS:
        d, new_ll, old_ll = [], [], []
        for m, n, f, y, grp in rows(market, pm.VALIDATE):
            new = shrunk(m, n, priors.get(f"{grp}|{market}"), shrinks[market]) * f ** betas[market]
            a = max(dists[market].log_lik(new, y), pm.LOG_LIK_FLOOR)
            b = max(
                previous.dists[market].log_lik(m * f ** previous.beta[market], y),
                pm.LOG_LIK_FLOOR,
            )
            new_ll.append(a)
            old_ll.append(b)
            d.append(a - b)
        arr = np.array(d)
        z = float(arr.mean() / (arr.std(ddof=1) / math.sqrt(len(arr)))) if len(arr) > 1 else 0.0
        result.validate.append(
            pm.StatReport(market, len(arr), float(np.mean(new_ll)), float(np.mean(old_ll)), z)
        )
    return result


def freeze(result: MatchupBacktest) -> dict[str, Any]:
    return {
        "artifact_version": ARTIFACT_VERSION,
        "kind": "nfl_prop_matchup",
        "market": "PLAYER_PROP",
        "sport": "NFL",
        "splits": {"train": list(pm.TRAIN), "validate": list(pm.VALIDATE)},
        "min_games": pm.MIN_GAMES,
        "usage": asdict(result.usage),
        "defense": asdict(result.defense),
        "beta": result.beta,
        "dists": {m: asdict(d) for m, d in result.dists.items()},
        "league": result.league,
        "min_mean": pm.MIN_MEAN,
        "validation_vs_previous": [asdict(r) for r in result.validate],
        "shrink": result.shrink,
        "priors": result.priors,
    }


def from_artifact(artifact: Mapping[str, Any]) -> MatchupBacktest:
    """The fitted settings of a registered matchup artifact (to build the next version on)."""
    return MatchupBacktest(
        pm.Params(**artifact["usage"]),
        DefenseParams(**artifact["defense"]),
        {k: float(v) for k, v in artifact["beta"].items()},
        {m: pm.Dist(**d) for m, d in artifact["dists"].items()},
        {k: float(v) for k, v in artifact["league"].items()},
        shrink={k: float(v) for k, v in (artifact.get("shrink") or {}).items()},
        priors={k: float(v) for k, v in (artifact.get("priors") or {}).items()},
    )


@dataclass(frozen=True)
class MatchupView:
    mean: float
    usage_mean: float
    defense_factor: float
    p_over: float
    games: int


class FrozenMatchupModel:
    def __init__(self, artifact: Mapping[str, Any], games: Sequence[pm.PlayerGame]) -> None:
        if artifact.get("artifact_version") not in (1, 2, STARTER_ARTIFACT_VERSION) or (
            artifact.get("kind") != "nfl_prop_matchup"
        ):
            raise ValueError("not an NFL prop matchup artifact of a known version")
        # Version 3 (v0.4) scales the passing stats to a full-game starter.
        self.starter = {k: float(v) for k, v in (artifact.get("starter_factor") or {}).items()}
        # Version 1 (v0.2) has no shrink: its predictions are unchanged.
        self.shrink = {k: float(v) for k, v in (artifact.get("shrink") or {}).items()}
        self.priors = {k: float(v) for k, v in (artifact.get("priors") or {}).items()}
        self.usage = pm.Params(**artifact["usage"])
        self.defense = DefenseParams(**artifact["defense"])
        self.beta = {k: float(v) for k, v in artifact["beta"].items()}
        self.dists = {m: pm.Dist(**d) for m, d in artifact["dists"].items()}
        self.min_mean = dict(artifact["min_mean"])
        self.means = pm.Means(games, self.usage)
        self.defenses = Defenses(games, self.defense, artifact["league"])
        self.position = {g.player_id: g.position for g in games}

    def view(
        self,
        player_id: str,
        market: str,
        line: float,
        before: datetime,
        season: int,
        opponent_id: int,
    ) -> MatchupView | None:
        dist = self.dists.get(market)
        if dist is None:
            return None
        history = [g for g in self.means.by_player.get(player_id, []) if g.kickoff < before]
        m = self.means.mean(history[-40:], season, market)
        if m is None or m[0] < self.min_mean[market]:
            return None
        position = self.position.get(player_id, "")
        group = defense_group(market, position)
        f = (
            self.defenses.factor(opponent_id, group, market, before, season)
            if group and opponent_id
            else 1.0
        )
        usage = shrunk(
            m[0],
            m[1],
            self.priors.get(f"{group_of(position)}|{market}"),
            self.shrink.get(market, 0.0),
        )
        mean = usage * f ** self.beta.get(market, 0.0) * self.starter.get(market, 1.0)
        return MatchupView(mean, usage, f, dist.p_over(mean, line), m[1])


# ------------------------------------------------------------------ v0.4: full-game starters

STARTER_ARTIFACT_VERSION = 3
STARTER_MARKETS = ("player_pass_yds", "player_pass_attempts", "player_pass_completions")
FULL_GAME_ATTEMPTS = 15.0
"""A quarterback game with this many attempts counts as a start he finished: the
population books offer passing props on. v0.3's history also holds the games he
left early or came in late, so on full games it fell short by 5-8% in every season
(TRAIN included) and its passing props leaned under (forward: 40% said, 57% over)."""


@dataclass
class StarterCheck:
    market: str
    games: int
    ratio_before: float
    """Actual over model mean, full-game starts, before the factor."""
    ratio_after: float
    log_lik_before: float
    log_lik_after: float
    z: float
    """Paired per-game log-likelihood gain, after minus before."""


def _full_starts(
    games: Sequence[pm.PlayerGame],
    model: FrozenMatchupModel,
    market: str,
    seasons: tuple[int, int],
) -> list[tuple[float, float]]:
    """(actual, model mean) on full-game quarterback starts in ``seasons``."""
    out = []
    for g in games:
        if g.position != "QB" or not seasons[0] <= g.season <= seasons[1]:
            continue
        if g.values.get("player_pass_attempts", 0.0) < FULL_GAME_ATTEMPTS:
            continue
        view = model.view(g.player_id, market, 0.5, g.kickoff, g.season, g.opponent_id)
        if view is not None:
            out.append((g.values[market], view.mean))
    return out


def starter_factors(
    games: Sequence[pm.PlayerGame],
    model: FrozenMatchupModel,
    seasons: tuple[int, int] = pm.TRAIN,
) -> dict[str, float]:
    """Per passing stat, total actual over total model mean on full-game starts."""
    factors = {}
    for market in STARTER_MARKETS:
        rows = _full_starts(games, model, market, seasons)
        factors[market] = sum(a for a, _ in rows) / sum(m for _, m in rows)
    return factors


def check_starter(
    games: Sequence[pm.PlayerGame],
    model: FrozenMatchupModel,
    factors: Mapping[str, float],
    seasons: tuple[int, int] = pm.VALIDATE,
) -> list[StarterCheck]:
    out = []
    for market in STARTER_MARKETS:
        rows = _full_starts(games, model, market, seasons)
        dist, f = model.dists[market], factors[market]
        before = [dist.log_lik(m, a) for a, m in rows]
        after = [dist.log_lik(m * f, a) for a, m in rows]
        gains = np.array(after) - np.array(before)
        sd = float(np.std(gains, ddof=1))
        actual, mean = sum(a for a, _ in rows), sum(m for _, m in rows)
        out.append(
            StarterCheck(
                market,
                len(rows),
                actual / mean,
                actual / (mean * f),
                float(np.mean(before)),
                float(np.mean(after)),
                float(np.mean(gains)) / (sd / math.sqrt(len(gains))) if sd > 0 else 0.0,
            )
        )
    return out


def starter_artifact(base: Mapping[str, Any], factors: Mapping[str, float]) -> dict[str, Any]:
    """v0.4: the v0.3 artifact unchanged, plus the passing stats' starter factors."""
    return {**base, "artifact_version": STARTER_ARTIFACT_VERSION, "starter_factor": dict(factors)}


# ------------------------------------------------------------------ market-anchored

ANCHORED_KIND = "nfl_prop_anchored"
"""v0.3 anchored to the market, as ``nba_props.anchored`` does for the NBA, on the
receiving props only. On v0.3's forward props (2026-10-18..30) its lean carried
information there (weight about 0.5, t about +2.3, at 24 h and 1 h) and none, or a
reversed one, on rushing, passing and touchdowns, where it trails the market."""
ANCHORED_MARKETS = ("player_receptions", "player_reception_yds")


class FrozenAnchoredMatchupModel:
    """v0.3's view on ``markets``, its chance pulled to the market's by ``weight``."""

    def __init__(self, artifact: Mapping[str, Any], games: Sequence[pm.PlayerGame]) -> None:
        if artifact.get("artifact_version") != ARTIFACT_VERSION or artifact.get("kind") != (
            ANCHORED_KIND
        ):
            raise ValueError("not an anchored NFL prop model artifact of a known version")
        self.base = FrozenMatchupModel(artifact["base"], games)
        self.weight = float(artifact["weight"])
        markets = set(artifact["markets"])
        self.dists = {m: d for m, d in self.base.dists.items() if m in markets}

    def view(
        self,
        player_id: str,
        market: str,
        line: float,
        before: datetime,
        season: int,
        opponent_id: int,
        market_over: float | None,
    ) -> MatchupView | None:
        if market_over is None or market not in self.dists:
            return None  # anchored to the market, on its own markets only
        v = self.base.view(player_id, market, line, before, season, opponent_id)
        if v is None:
            return None
        p = nb.anchored(market_over, v.p_over, self.weight)
        return MatchupView(v.mean, v.usage_mean, v.defense_factor, p, v.games)
