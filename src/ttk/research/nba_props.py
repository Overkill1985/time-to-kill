"""NBA player prop model, version 0.1: recent usage x the opponent -> P(over the line).

For a player and a stat (points, rebounds, assists, threes, steals, blocks and the
combined markets), from ESPN box scores of regular-season and playoff games he
played:

- **usage**: an exponentially weighted mean of his earlier games (half-life
  ``half_life`` games; at each new season the running weights are multiplied by
  ``carryover``);
- **shrink**: toward a role prior, the league's rate per minute x his own weighted
  minutes, by ``shrink[stat]`` pseudo-games (a player with few games is mostly his
  role, one with many is mostly himself);
- **opponent**: x factor^``beta[stat]``, where the factor is what the opposing team
  has allowed per game in that stat (all its opponents' players), exponentially
  weighted over its earlier games, shrunk toward the league by ``defense.shrink``
  games and divided by the league's average (pace and defense together; clamped
  0.5 to 2);
- around that mean, a negative binomial per stat with variance = ``phi`` x mean
  (fitted better than variance = mean + mean^2 / k for every stat on TRAIN, and a
  model with both terms put the squared one at zero).

Fitted on TRAIN seasons by log-likelihood of what happened, walk-forward (each
game uses only earlier games); checked on VALIDATE against a season-to-date
average with the same distributions, and against itself without the opponent.
Seasons are ESPN years (2026 = 2025-26). It assumes the player plays: props of a
player who doesn't are void. DEVELOPMENT until it beats the market forward.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
from scipy import optimize, stats
from sqlalchemy import select
from sqlalchemy.orm import Session

from ttk.db.models import Game, PlayerGameStat
from ttk.domain import GameStatus, Sport
from ttk.research import prop_model as pm

ARTIFACT_VERSION = 1
KIND = "nba_prop_usage"
TRAIN = (2023, 2025)
VALIDATE = (2026, 2026)
MIN_GAMES = 5
SEASON_TYPES = ("REG", "POST")
"""Preseason rotations aren't the real ones: preseason games are left out."""
N_CAP = 30
"""Games counted toward the shrink (a veteran's 400 games are not 400 pieces of news)."""


def _reb(r: PlayerGameStat) -> float | None:
    return None if r.oreb is None or r.dreb is None else float(r.oreb + r.dreb)


def _get(name: str) -> Callable[[PlayerGameStat], float | None]:
    def f(r: PlayerGameStat) -> float | None:
        v = getattr(r, name)
        return None if v is None else float(v)

    return f


BASE: dict[str, Callable[[PlayerGameStat], float | None]] = {
    "pts": _get("points"),
    "reb": _reb,
    "ast": _get("ast"),
    "fg3m": _get("fg3m"),
    "stl": _get("stl"),
    "blk": _get("blk"),
}
STATS: dict[str, tuple[str, ...]] = {
    "player_points": ("pts",),
    "player_rebounds": ("reb",),
    "player_assists": ("ast",),
    "player_threes": ("fg3m",),
    "player_steals": ("stl",),
    "player_blocks": ("blk",),
    "player_points_rebounds_assists": ("pts", "reb", "ast"),
    "player_points_rebounds": ("pts", "reb"),
    "player_points_assists": ("pts", "ast"),
    "player_rebounds_assists": ("reb", "ast"),
}
"""PropLine market key -> the box-score parts it adds up."""
MIN_MEAN = {
    "player_points": 3.0,
    "player_rebounds": 1.5,
    "player_assists": 0.8,
    "player_threes": 0.3,
    "player_steals": 0.25,
    "player_blocks": 0.2,
    "player_points_rebounds_assists": 5.0,
    "player_points_rebounds": 4.0,
    "player_points_assists": 4.0,
    "player_rebounds_assists": 2.0,
}
"""A stat a player barely takes part in is noise: modeled from this weighted mean."""
GRID_HALF_LIFE = (5.0, 10.0, 20.0, 40.0)
GRID_CARRYOVER = (0.25, 0.5, 1.0)
GRID_DEFENSE_HALF_LIFE = (10.0, 20.0, 40.0)
GRID_DEFENSE_SHRINK = (5.0, 15.0)
GRID_SHRINK = (0.0, 2.0, 5.0, 10.0)
GRID_BETA = (0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5)
FACTOR_RANGE = (0.5, 2.0)


@dataclass(frozen=True)
class PlayerGame:
    player_id: str
    game_id: int
    season: int
    tipoff: datetime
    team_id: int
    opponent_id: int
    minutes: float
    values: dict[str, float]
    """Market key -> value; NaN where a part is missing (threes before 2025-26)."""


def _values(r: PlayerGameStat) -> dict[str, float]:
    base = {k: f(r) for k, f in BASE.items()}
    out = {}
    for market, parts in STATS.items():
        vs = [base[p] for p in parts]
        out[market] = math.nan if any(v is None for v in vs) else float(sum(v for v in vs if v))
    return out


def load_player_games(session: Session, since_season: int | None = None) -> list[PlayerGame]:
    """Games each player played (minutes > 0), oldest first, from ``since_season``
    (default: the season before TRAIN, for the carryover)."""
    since = since_season if since_season is not None else TRAIN[0] - 1
    rows = session.execute(
        select(
            PlayerGameStat, Game.season, Game.commence_time, Game.home_team_id, Game.away_team_id
        )
        .join(Game, Game.id == PlayerGameStat.game_id)
        .where(
            Game.sport == Sport.NBA,
            Game.status == GameStatus.FINAL,
            Game.season_type.in_(SEASON_TYPES),
            Game.season >= since,
            PlayerGameStat.played.is_(True),
            PlayerGameStat.minutes > 0,
        )
        .order_by(Game.commence_time, PlayerGameStat.id)
    ).all()
    return [
        PlayerGame(
            r.player_id,
            r.game_id,
            int(season or 0),
            tip,
            r.team_id,
            away if r.team_id == home else home,
            float(r.minutes or 0.0),
            _values(r),
        )
        for r, season, tip, home, away in rows
    ]


# ------------------------------------------------------------------ running means


class _Running:
    """Exponentially weighted sums, decayed per game, scaled at a season change."""

    __slots__ = ("count", "season", "total", "weight")

    def __init__(self) -> None:
        self.total = 0.0
        self.weight = 0.0
        self.count = 0
        self.season = -1

    def mean(self) -> float | None:
        return self.total / self.weight if self.weight > 0 else None

    def add(self, value: float, season: int, decay: float, carryover: float) -> None:
        if math.isnan(value):
            return
        if self.season != -1 and season != self.season:
            self.total *= carryover
            self.weight *= carryover
        self.season = season
        self.total = self.total * decay + value
        self.weight = self.weight * decay + 1.0
        self.count += 1


@dataclass(frozen=True)
class Usage:
    half_life: float
    carryover: float


@dataclass(frozen=True)
class Defense:
    half_life: float
    shrink: float
    carryover: float = 0.5


@dataclass(frozen=True)
class Row:
    """One player-game as the model saw it before tip-off."""

    game: PlayerGame
    means: dict[str, tuple[float, int]]
    minutes: float
    """His weighted minutes."""
    season_avg: dict[str, float]
    """Season-to-date averages (the baseline), where he has MIN_GAMES this season."""


def usage_rows(games: Sequence[PlayerGame], usage: Usage) -> list[Row]:
    """Walk-forward: each player-game with the player's means from earlier games."""
    decay = 0.5 ** (1 / usage.half_life)
    run: dict[str, dict[str, _Running]] = defaultdict(lambda: defaultdict(_Running))
    season_sums: dict[tuple[str, int], dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    out = []
    for g in games:
        r = run[g.player_id]
        means = {}
        for market in STATS:
            # (The carryover scales weights only when the next game is added: a mean
            # alone is unchanged by scaling total and weight alike.)
            m = r[market].mean()
            if m is not None and r[market].count >= MIN_GAMES:
                means[market] = (m, r[market].count)
        mins = r["minutes"].mean()
        this = season_sums[(g.player_id, g.season)]
        avg = {
            k: float(np.mean(v)) for k, v in this.items() if len(v) >= MIN_GAMES and k != "minutes"
        }
        if means and mins is not None:
            out.append(Row(g, means, mins, avg))
        for market, v in g.values.items():
            r[market].add(v, g.season, decay, usage.carryover)
            if not math.isnan(v):
                this[market].append(v)
        r["minutes"].add(g.minutes, g.season, decay, usage.carryover)
    return out


def league(games: Sequence[PlayerGame], seasons: tuple[int, int]) -> dict[str, float]:
    """Per market: the rate per minute ('rate|m') and a team's total per game ('team|m')."""
    out: dict[str, float] = {}
    team_totals: dict[tuple[int, int], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for market in STATS:
        vals = [
            (g.values[market], g.minutes)
            for g in games
            if seasons[0] <= g.season <= seasons[1] and not math.isnan(g.values[market])
        ]
        if vals:
            out[f"rate|{market}"] = sum(v for v, _ in vals) / sum(m for _, m in vals)
    for g in games:
        if seasons[0] <= g.season <= seasons[1]:
            for market, v in g.values.items():
                if not math.isnan(v):
                    team_totals[(g.game_id, g.team_id)][market] += v
    for market in STATS:
        per = [t[market] for t in team_totals.values() if market in t and t[market] > 0]
        if per:
            out[f"team|{market}"] = float(np.mean(per))
    return out


class Defenses:
    """What each team allowed per game, walk-forward: ``factor(team, market, before)``."""

    def __init__(
        self, games: Sequence[PlayerGame], params: Defense, league: Mapping[str, float]
    ) -> None:
        self.params = params
        self.league = league
        totals: dict[tuple[int, int], dict[str, float]] = defaultdict(lambda: defaultdict(float))
        meta: dict[tuple[int, int], tuple[datetime, int]] = {}
        for g in games:
            key = (g.game_id, g.opponent_id)  # allowed by the opponent
            for market, v in g.values.items():
                if not math.isnan(v):
                    totals[key][market] += v
            meta[key] = (g.tipoff, g.season)
        self.by_team: dict[int, list[tuple[datetime, int, dict[str, float]]]] = defaultdict(list)
        for key, vals in totals.items():
            tip, season = meta[key]
            team = key[1]
            self.by_team[team].append((tip, season, dict(vals)))
        for team_rows in self.by_team.values():
            team_rows.sort(key=lambda t: t[0])

    def factor(self, team: int, market: str, before: datetime) -> float:
        base = self.league.get(f"team|{market}")
        if not base:
            return 1.0
        decay = 0.5 ** (1 / self.params.half_life)
        r = _Running()
        for tip, season, vals in self.by_team.get(team, []):
            if tip >= before:
                break
            if market in vals:
                r.add(vals[market], season, decay, self.params.carryover)
        m = r.mean()
        if m is None:
            return 1.0
        n = min(r.count, 82)
        shrunk = (n * m + self.params.shrink * base) / (n + self.params.shrink)
        return float(np.clip(shrunk / base, *FACTOR_RANGE))

    def walk(self) -> dict[tuple[int, int], dict[str, float]]:
        """(tip-off, defending team) -> factor per market, each from earlier games only."""
        decay = 0.5 ** (1 / self.params.half_life)
        out: dict[tuple[int, int], dict[str, float]] = {}
        for team, rows in self.by_team.items():
            run: dict[str, _Running] = defaultdict(_Running)
            for tip, season, vals in rows:
                f = {}
                for market in STATS:
                    base = self.league.get(f"team|{market}")
                    r = run[market]
                    m = r.mean()
                    if not base or m is None:
                        f[market] = 1.0
                        continue
                    n = min(r.count, 82)
                    shrunk = (n * m + self.params.shrink * base) / (n + self.params.shrink)
                    f[market] = float(np.clip(shrunk / base, *FACTOR_RANGE))
                for market, v in vals.items():
                    run[market].add(v, season, decay, self.params.carryover)
                out[(_key(tip), team)] = f
        return out


def _key(tip: datetime) -> int:
    return int(tip.timestamp())


def shrunk(mean: float, games: int, prior: float, k: float) -> float:
    if k <= 0:
        return mean
    n = min(games, N_CAP)
    return (n * mean + k * prior) / (n + k)


@dataclass(frozen=True)
class Dist:
    """Negative binomial with variance ``phi`` x mean (n = mean / (phi - 1), p = 1 / phi)."""

    phi: float

    def _np(self, mean: Any) -> tuple[Any, float]:
        phi = max(self.phi, 1.0001)
        return np.maximum(mean, 1e-6) / (phi - 1), 1 / phi

    def p_over(self, mean: float, line: float) -> float:
        n, p = self._np(mean)
        return float(stats.nbinom.sf(math.floor(line), n, p))

    def log_lik(self, mean: float, value: float) -> float:
        n, p = self._np(mean)
        return float(stats.nbinom.logpmf(round(value), n, p))


# ------------------------------------------------------------------ fitting


def fit_dist(mu: np.ndarray, y: np.ndarray) -> Dist:
    yr = np.round(y)

    def nll(x: np.ndarray) -> float:
        n, p = Dist(1 + math.exp(float(x[0])))._np(mu)
        return -float(np.sum(stats.nbinom.logpmf(yr, n, p)))

    res = optimize.minimize(nll, x0=np.array([0.0]), method="Nelder-Mead")
    return Dist(float(1 + math.exp(res.x[0])))


def _mean_ll(dist: Dist, mu: np.ndarray, y: np.ndarray) -> np.ndarray:
    n, p = dist._np(mu)
    ll = stats.nbinom.logpmf(np.round(y), n, p)
    return np.asarray(np.maximum(ll, pm.LOG_LIK_FLOOR), dtype=float)


@dataclass
class StatFit:
    market: str
    shrink: float
    beta: float
    dist: Dist


@dataclass
class NbaBacktest:
    usage: Usage
    defense: Defense
    league: dict[str, float]
    fits: dict[str, StatFit]
    validate: list[pm.StatReport] = field(default_factory=list)
    """Against a season-to-date average with the same distribution."""
    validate_matchup: list[pm.StatReport] = field(default_factory=list)
    """Against itself without the opponent (beta 0, refitted): z > 0 = it helps."""


@dataclass
class _Arrays:
    m: np.ndarray
    n: np.ndarray
    prior: np.ndarray
    f: np.ndarray
    y: np.ndarray
    base: np.ndarray
    """Season-to-date average (NaN where there isn't one)."""


def _arrays(
    rows: Sequence[Row],
    factors: Mapping[tuple[int, int], Mapping[str, float]],
    lg: Mapping[str, float],
    market: str,
    seasons: tuple[int, int],
) -> _Arrays:
    m, n, prior, f, y, base = [], [], [], [], [], []
    rate = lg.get(f"rate|{market}", 0.0)
    for r in rows:
        g = r.game
        if not seasons[0] <= g.season <= seasons[1] or market not in r.means:
            continue
        v = g.values[market]
        mean, count = r.means[market]
        if math.isnan(v) or mean < MIN_MEAN[market]:
            continue
        m.append(mean)
        n.append(count)
        prior.append(rate * r.minutes)
        f.append(factors.get((_key(g.tipoff), g.opponent_id), {}).get(market, 1.0))
        y.append(v)
        base.append(r.season_avg.get(market, math.nan))
    return _Arrays(*(np.array(a, dtype=float) for a in (m, n, prior, f, y, base)))


def _mu(a: _Arrays, k: float, beta: float) -> np.ndarray:
    nn = np.minimum(a.n, N_CAP)
    usage = a.m if k <= 0 else (nn * a.m + k * a.prior) / (nn + k)
    return np.maximum(usage * a.f**beta, 1e-6)


def _fit_stat(a: _Arrays, market: str, betas: Sequence[float]) -> StatFit:
    best: tuple[float, StatFit] | None = None
    for k in GRID_SHRINK:
        for beta in betas:
            mu = _mu(a, k, beta)
            dist = fit_dist(mu, a.y)
            ll = float(np.mean(_mean_ll(dist, mu, a.y)))
            if best is None or ll > best[0]:
                best = (ll, StatFit(market, k, beta, dist))
    assert best is not None
    return best[1]


def _report(market: str, a: np.ndarray, b: np.ndarray) -> pm.StatReport:
    d = a - b
    z = float(d.mean() / (d.std(ddof=1) / math.sqrt(len(d)))) if len(d) > 1 else 0.0
    return pm.StatReport(market, len(d), float(np.mean(a)), float(np.mean(b)), z)


def backtest(games: Sequence[PlayerGame], log: Callable[[str], None] | None = None) -> NbaBacktest:
    """Usage settings by TRAIN log-likelihood (no opponent, no shrink, summed over
    stats); then the defense settings, and per stat the shrink and beta, also on
    TRAIN; VALIDATE scores the frozen choices."""
    say = log or (lambda _s: None)
    lg = league(games, TRAIN)
    best_u: tuple[float, Usage, list[Row]] | None = None
    for h in GRID_HALF_LIFE:
        for c in GRID_CARRYOVER:
            usage = Usage(h, c)
            rows = usage_rows(games, usage)
            total = 0.0
            for market in STATS:
                a = _arrays(rows, {}, lg, market, TRAIN)
                if len(a.y) == 0:
                    continue
                dist = fit_dist(a.m, a.y)
                total += float(np.mean(_mean_ll(dist, a.m, a.y)))
            say(f"usage {usage}: {total:.4f}")
            if best_u is None or total > best_u[0]:
                best_u = (total, usage, rows)
    assert best_u is not None
    _, usage, rows = best_u

    best_d: tuple[float, Defense, dict[tuple[int, int], dict[str, float]], dict[str, StatFit]] | (
        None
    ) = None
    for hd in GRID_DEFENSE_HALF_LIFE:
        for sd in GRID_DEFENSE_SHRINK:
            defense = Defense(hd, sd)
            factors = Defenses(games, defense, lg).walk()
            fits, total = {}, 0.0
            for market in STATS:
                a = _arrays(rows, factors, lg, market, TRAIN)
                if len(a.y) == 0:
                    continue
                fit = _fit_stat(a, market, GRID_BETA)
                fits[market] = fit
                total += float(np.mean(_mean_ll(fit.dist, _mu(a, fit.shrink, fit.beta), a.y)))
            say(f"defense {defense}: {total:.4f}")
            if best_d is None or total > best_d[0]:
                best_d = (total, defense, factors, fits)
    assert best_d is not None
    _, defense, factors, fits = best_d
    result = NbaBacktest(usage, defense, lg, fits)
    for market, fit in fits.items():
        train = _arrays(rows, factors, lg, market, TRAIN)
        flat = _fit_stat(train, market, (0.0,))
        a = _arrays(rows, factors, lg, market, VALIDATE)
        if len(a.y) < 2:
            continue
        model = _mean_ll(fit.dist, _mu(a, fit.shrink, fit.beta), a.y)
        no_opp = _mean_ll(flat.dist, _mu(a, flat.shrink, 0.0), a.y)
        result.validate_matchup.append(_report(market, model, no_opp))
        has = ~np.isnan(a.base)
        base_mu = np.maximum(a.base[has], MIN_MEAN[market])
        result.validate.append(_report(market, model[has], _mean_ll(fit.dist, base_mu, a.y[has])))
    return result


def freeze(result: NbaBacktest) -> dict[str, Any]:
    return {
        "artifact_version": ARTIFACT_VERSION,
        "kind": KIND,
        "market": "PLAYER_PROP",
        "sport": "NBA",
        "splits": {"train": list(TRAIN), "validate": list(VALIDATE)},
        "min_games": MIN_GAMES,
        "usage": asdict(result.usage),
        "defense": asdict(result.defense),
        "league": result.league,
        "stats": {
            m: {"shrink": f.shrink, "beta": f.beta, "dist": asdict(f.dist)}
            for m, f in result.fits.items()
        },
        "min_mean": MIN_MEAN,
        "validation_vs_season_average": [asdict(r) for r in result.validate],
        "validation_vs_no_opponent": [asdict(r) for r in result.validate_matchup],
    }


@dataclass(frozen=True)
class NbaView:
    mean: float
    usage_mean: float
    defense_factor: float
    p_over: float
    games: int


class FrozenNbaPropModel:
    """The frozen model; only state (the players' games) moves forward."""

    def __init__(self, artifact: Mapping[str, Any], games: Sequence[PlayerGame]) -> None:
        if artifact.get("artifact_version") != ARTIFACT_VERSION or artifact.get("kind") != KIND:
            raise ValueError("not an NBA prop model artifact of a known version")
        self.usage = Usage(**artifact["usage"])
        self.defense = Defense(**artifact["defense"])
        self.league = {k: float(v) for k, v in artifact["league"].items()}
        self.stats = {
            m: (float(s["shrink"]), float(s["beta"]), Dist(**s["dist"]))
            for m, s in artifact["stats"].items()
        }
        self.dists = {m: s[2] for m, s in self.stats.items()}
        self.min_mean = dict(artifact["min_mean"])
        self.by_player: dict[str, list[PlayerGame]] = defaultdict(list)
        for g in games:
            self.by_player[g.player_id].append(g)
        self.defenses = Defenses(games, self.defense, self.league)

    def view(
        self,
        player_id: str,
        market: str,
        line: float,
        before: datetime,
        season: int,
        opponent_id: int,
    ) -> NbaView | None:
        fit = self.stats.get(market)
        if fit is None:
            return None
        k, beta, dist = fit
        decay = 0.5 ** (1 / self.usage.half_life)
        r, mins = _Running(), _Running()
        for g in self.by_player.get(player_id, []):
            if g.tipoff >= before:
                break
            r.add(g.values[market], g.season, decay, self.usage.carryover)
            mins.add(g.minutes, g.season, decay, self.usage.carryover)
        m, minutes = r.mean(), mins.mean()
        if m is None or minutes is None or r.count < MIN_GAMES or m < self.min_mean[market]:
            return None
        usage = shrunk(m, r.count, self.league.get(f"rate|{market}", 0.0) * minutes, k)
        f = self.defenses.factor(opponent_id, market, before) if opponent_id else 1.0
        mean = usage * f**beta
        return NbaView(mean, usage, f, dist.p_over(mean, line), r.count)
