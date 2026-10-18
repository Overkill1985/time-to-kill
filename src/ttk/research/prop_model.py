"""NFL player prop model, version 0.1: each player's recent usage -> P(over the line).

For a player and a stat, the expected value is an exponentially weighted mean of his
earlier games (half-life ``half_life`` games; last season's games count
``carryover`` as much), from nflverse player stats. No opponent, injuries or game
script yet: this is the baseline the next versions must beat.

Around that mean, each stat has its own distribution, fitted on TRAIN:
- counts (attempts, completions, receptions, carries, TDs): negative binomial with
  dispersion ``k`` (variance = mean + mean^2 / k);
- yards: gamma with shape ``a`` (variance = mean^2 / a); a game with zero yards is
  ``p0`` likely (receivers who aren't targeted), so P(over) = (1 - p0) x gamma tail.

Fitted on TRAIN seasons by log-likelihood of what happened, walk-forward (each
game's mean uses only earlier games); checked on VALIDATE against a season-to-date
average with the same distributions. A player needs ``MIN_GAMES`` earlier games.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
from scipy import optimize, stats
from sqlalchemy import select
from sqlalchemy.orm import Session

from ttk.db.models import Game, NflPlayerGameStat
from ttk.domain import GameStatus, Sport

ARTIFACT_VERSION = 1
TRAIN = (2023, 2024)
VALIDATE = (2025, 2025)
MIN_GAMES = 3

STATS: dict[str, Callable[[NflPlayerGameStat], float]] = {
    "player_pass_yds": lambda r: r.passing_yards,
    "player_pass_attempts": lambda r: r.attempts,
    "player_pass_completions": lambda r: r.completions,
    "player_rush_yds": lambda r: r.rushing_yards,
    "player_rush_attempts": lambda r: r.carries,
    "player_receptions": lambda r: r.receptions,
    "player_reception_yds": lambda r: r.receiving_yards,
    "player_anytime_td": lambda r: r.rushing_tds + r.receiving_tds,
}
COUNTS = {
    "player_pass_attempts",
    "player_pass_completions",
    "player_rush_attempts",
    "player_receptions",
    "player_anytime_td",
}
# Who a stat is modeled for: a stat a player barely takes part in (a QB's
# receptions) is noise. A player-game counts when his weighted mean is at least this.
MIN_MEAN = {
    "player_pass_yds": 50.0,
    "player_pass_attempts": 10.0,
    "player_pass_completions": 6.0,
    "player_rush_yds": 5.0,
    "player_rush_attempts": 2.0,
    "player_receptions": 0.8,
    "player_reception_yds": 8.0,
    "player_anytime_td": 0.05,
}
GRID_HALF_LIFE = (2.0, 3.0, 4.0, 6.0, 8.0)
LOG_LIK_FLOOR = -20.0
"""One game's log-likelihood is capped below at this when comparing models, so a
freak game (a 0-yard day for a 90-yard receiver) can't decide the comparison."""
GRID_CARRYOVER = (0.25, 0.5, 0.75)


@dataclass(frozen=True)
class PlayerGame:
    player_id: str
    game_id: int
    season: int
    kickoff: datetime
    values: dict[str, float]
    team_id: int = 0
    opponent_id: int = 0
    position: str = ""
    """nflverse position (QB, RB, WR, TE, FB, ...); the matchup model groups by it."""


def load_player_games(session: Session) -> list[PlayerGame]:
    rows = session.execute(
        select(
            NflPlayerGameStat,
            Game.season,
            Game.commence_time,
            Game.home_team_id,
            Game.away_team_id,
        )
        .join(Game, Game.id == NflPlayerGameStat.game_id)
        .where(Game.sport == Sport.NFL, Game.status == GameStatus.FINAL)
        .order_by(Game.commence_time)
    ).all()
    return [
        PlayerGame(
            r.player_id,
            r.game_id,
            int(season or 0),
            kick,
            {m: float(f(r)) for m, f in STATS.items()},
            r.team_id,
            away if r.team_id == home else home,
            r.position or "",
        )
        for r, season, kick, home, away in rows
    ]


@dataclass(frozen=True)
class Params:
    half_life: float
    carryover: float


class Means:
    """Walk-forward weighted mean per (player, stat): ``before(player, game)``."""

    def __init__(self, games: Sequence[PlayerGame], params: Params) -> None:
        self.params = params
        self.by_player: dict[str, list[PlayerGame]] = defaultdict(list)
        for g in sorted(games, key=lambda g: g.kickoff):
            self.by_player[g.player_id].append(g)

    def mean(
        self, history: Sequence[PlayerGame], season: int, market: str
    ) -> tuple[float, int] | None:
        """Weighted mean of ``history`` (oldest first) for a game in ``season``, and
        the number of games behind it; None with fewer than MIN_GAMES."""
        if len(history) < MIN_GAMES:
            return None
        decay = 0.5 ** (1 / self.params.half_life)
        total = weight = 0.0
        w = 1.0
        for g in reversed(history):
            ww = w * (1.0 if g.season == season else self.params.carryover)
            total += ww * g.values[market]
            weight += ww
            w *= decay
        return (total / weight, len(history)) if weight > 0 else None

    def walk(self) -> Iterable[tuple[PlayerGame, list[PlayerGame]]]:
        for games in self.by_player.values():
            for i, g in enumerate(games):
                yield g, games[max(0, i - 40) : i]


@dataclass(frozen=True)
class Dist:
    """One stat's distribution around a mean."""

    kind: str
    """'nbinom' or 'gamma'."""
    k: float = 0.0
    """nbinom dispersion (variance = mean + mean^2 / k)."""
    a: float = 0.0
    """gamma shape (variance = mean^2 / a), of the non-zero part."""
    p0: float = 0.0
    """gamma: chance of exactly zero (no target, no carry)."""
    zero_slope: float = 0.0
    """gamma: p0 = p0 x exp(-zero_slope x mean / scale): zeros get rarer as usage rises."""

    def p_over(self, mean: float, line: float) -> float:
        """P(value > line)."""
        mean = max(mean, 1e-6)
        if self.kind == "nbinom":
            n = self.k
            p = n / (n + mean)
            return float(stats.nbinom.sf(math.floor(line), n, p))
        p_zero = self.zero(mean)
        if line < 0:
            return 1.0
        mean_pos = mean / (1 - p_zero)
        tail = float(stats.gamma.sf(line, self.a, scale=mean_pos / self.a))
        return (1 - p_zero) * tail

    def zero(self, mean: float) -> float:
        return float(np.clip(self.p0 * math.exp(-self.zero_slope * mean), 0.0, 0.95))

    def log_lik(self, mean: float, value: float) -> float:
        mean = max(mean, 1e-6)
        if self.kind == "nbinom":
            n = self.k
            return float(stats.nbinom.logpmf(round(value), n, n / (n + mean)))
        p_zero = self.zero(mean)
        if value <= 0:
            return math.log(max(p_zero, 1e-9))
        mean_pos = mean / (1 - p_zero)
        # A yard total is a whole number: the density over the yard it falls in.
        return math.log(max(1 - p_zero, 1e-9)) + float(
            stats.gamma.logpdf(value, self.a, scale=mean_pos / self.a)
        )


def _pairs(means: Means, market: str, seasons: tuple[int, int]) -> list[tuple[float, float]]:
    out = []
    for g, history in means.walk():
        if not seasons[0] <= g.season <= seasons[1]:
            continue
        m = means.mean(history, g.season, market)
        if m is None or m[0] < MIN_MEAN[market]:
            continue
        out.append((m[0], g.values[market]))
    return out


def fit_dist(market: str, pairs: Sequence[tuple[float, float]]) -> Dist:
    mu = np.array([m for m, _ in pairs])
    y = np.array([v for _, v in pairs])
    if market in COUNTS:

        def nll_nb(log_k: np.ndarray) -> float:
            k = math.exp(float(log_k[0]))
            return -float(np.sum(stats.nbinom.logpmf(np.round(y), k, k / (k + mu))))

        res = optimize.minimize(nll_nb, x0=np.array([1.0]), method="Nelder-Mead")
        return Dist("nbinom", k=float(math.exp(res.x[0])))
    scale = float(np.mean(mu))

    def nll_gamma(x: np.ndarray) -> float:
        a = math.exp(float(x[0]))
        p0 = 1 / (1 + math.exp(-float(x[1])))
        slope = math.exp(float(x[2])) / scale
        pz = np.clip(p0 * np.exp(-slope * mu), 1e-6, 0.95)
        zero = y <= 0
        ll = np.where(zero, np.log(pz), 0.0)
        pos = ~zero
        mean_pos = mu[pos] / (1 - pz[pos])
        ll[pos] = np.log(1 - pz[pos]) + stats.gamma.logpdf(y[pos], a, scale=mean_pos / a)
        return -float(np.sum(ll))

    res = optimize.minimize(
        nll_gamma, x0=np.array([0.0, -1.0, 0.0]), method="Nelder-Mead", options={"maxiter": 2000}
    )
    return Dist(
        "gamma",
        a=float(math.exp(res.x[0])),
        p0=float(1 / (1 + math.exp(-res.x[1]))),
        zero_slope=float(math.exp(res.x[2]) / scale),
    )


@dataclass
class StatReport:
    market: str
    games: int
    log_lik: float
    """Mean log-likelihood per player-game (higher is better)."""
    baseline_log_lik: float
    """Season-to-date average with the same distribution."""
    z: float
    """Paired z of model minus baseline (positive = the model is better)."""


@dataclass
class PropBacktest:
    params: Params
    dists: dict[str, Dist]
    validate: list[StatReport] = field(default_factory=list)


def _season_average(history: Sequence[PlayerGame], season: int, market: str) -> float | None:
    this = [g.values[market] for g in history if g.season == season]
    pool = this if len(this) >= MIN_GAMES else [g.values[market] for g in history[-MIN_GAMES:]]
    return sum(pool) / len(pool) if len(pool) >= MIN_GAMES else None


def backtest(games: Sequence[PlayerGame]) -> PropBacktest:
    """Choose (half-life, carryover) by total TRAIN log-likelihood across stats, fit
    each stat's distribution on TRAIN, and score VALIDATE against the baseline."""
    best: tuple[float, Params, dict[str, Dist]] | None = None
    for h in GRID_HALF_LIFE:
        for c in GRID_CARRYOVER:
            params = Params(h, c)
            means = Means(games, params)
            total = 0.0
            dists = {}
            for market in STATS:
                pairs = _pairs(means, market, TRAIN)
                dist = fit_dist(market, pairs)
                dists[market] = dist
                total += sum(dist.log_lik(m, v) for m, v in pairs) / max(1, len(pairs))
            if best is None or total > best[0]:
                best = (total, params, dists)
    assert best is not None
    _, params, dists = best
    result = PropBacktest(params, dists)
    means = Means(games, params)
    for market in STATS:
        diffs, model_ll, base_ll = [], [], []
        for g, history in means.walk():
            if not VALIDATE[0] <= g.season <= VALIDATE[1]:
                continue
            m = means.mean(history, g.season, market)
            b = _season_average(history, g.season, market)
            if m is None or b is None or m[0] < MIN_MEAN[market]:
                continue
            v = g.values[market]
            # The baseline gets the same floor on its mean as the model's filter.
            lm = max(dists[market].log_lik(m[0], v), LOG_LIK_FLOOR)
            lb = max(dists[market].log_lik(max(b, MIN_MEAN[market]), v), LOG_LIK_FLOOR)
            model_ll.append(lm)
            base_ll.append(lb)
            diffs.append(lm - lb)
        d = np.array(diffs)
        z = float(d.mean() / (d.std(ddof=1) / math.sqrt(len(d)))) if len(d) > 1 else 0.0
        result.validate.append(
            StatReport(market, len(d), float(np.mean(model_ll)), float(np.mean(base_ll)), z)
        )
    return result


def freeze(result: PropBacktest) -> dict[str, Any]:
    return {
        "artifact_version": ARTIFACT_VERSION,
        "kind": "nfl_prop_usage",
        "market": "PLAYER_PROP",
        "sport": "NFL",
        "splits": {"train": list(TRAIN), "validate": list(VALIDATE)},
        "min_games": MIN_GAMES,
        "params": asdict(result.params),
        "dists": {m: asdict(d) for m, d in result.dists.items()},
        "min_mean": MIN_MEAN,
        "validation": [asdict(r) for r in result.validate],
    }


@dataclass(frozen=True)
class PropView:
    mean: float
    p_over: float
    games: int


class FrozenPropModel:
    """The frozen model; only state (the players' games) moves forward."""

    def __init__(self, artifact: Mapping[str, Any], games: Sequence[PlayerGame]) -> None:
        if (
            artifact.get("artifact_version") != ARTIFACT_VERSION
            or artifact.get("kind") != "nfl_prop_usage"
        ):
            raise ValueError("not an NFL prop model artifact of a known version")
        self.params = Params(**artifact["params"])
        self.dists = {m: Dist(**d) for m, d in artifact["dists"].items()}
        self.min_mean = dict(artifact["min_mean"])
        self.means = Means(games, self.params)

    def view(
        self, player_id: str, market: str, line: float, before: datetime, season: int
    ) -> PropView | None:
        dist = self.dists.get(market)
        if dist is None:
            return None
        history = [g for g in self.means.by_player.get(player_id, []) if g.kickoff < before]
        m = self.means.mean(history[-40:], season, market)
        if m is None or m[0] < self.min_mean[market]:
            return None
        return PropView(m[0], dist.p_over(m[0], line), m[1])
