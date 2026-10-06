"""Totals (over/under) from pace and efficiency, college basketball first.

Walk-forward, from team box totals (team_game_boxes) of earlier games only:
each team's pace (possessions per game), offensive and defensive points per
possession, exponentially decayed (``half_life`` games, ``carryover`` at a new
season) and shrunk toward the league average by pseudo-counts. With
``opponent_adjust``, a game is credited relative to the opponent's rating going
in (a fast opponent inflates pace; a soft defense inflates offense).

Prediction for a game: possessions = league + (home pace - league) + (away pace
- league); each side's points per possession = league + (its offense - league)
+ (the opponent's defense - league); total = possessions x both sides' points
per possession. A linear calibration (TRAIN) maps it to points.

Validation, as for spreads (VALIDATE seasons only):
- margin of error of the predicted total against the closing total's;
- over/under: a standalone P(over | no push) from a normal around the
  prediction (sigma from TRAIN residuals), and a market-anchored one
  (logistic on the market's no-vig and the prediction's disagreement with
  the line, fitted on TRAIN), each in paired log loss against the market.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ttk.db.models import Game, ReportedLine, TeamGameBox
from ttk.models.anchored import MarketAnchoredModel, fit_market_anchored
from ttk.models.elo import EloGame
from ttk.research.moneyline import no_vig


@dataclass(frozen=True)
class TeamGame:
    game_id: int
    team_id: int
    possessions: float
    points: float


def load_team_games(session: Session, sport: str) -> list[TeamGame]:
    """Both teams' points and the game's possessions (the mean of both teams'
    estimates, as for the efficiency spread feature)."""
    by_game: dict[int, list[TeamGameBox]] = defaultdict(list)
    for b in session.scalars(
        select(TeamGameBox).join(Game, Game.id == TeamGameBox.game_id).where(Game.sport == sport)
    ):
        by_game[b.game_id].append(b)
    out = []
    for game_id, boxes in by_game.items():
        if len(boxes) != 2:
            continue
        possessions = sum(b.fga - b.oreb + b.tov + 0.475 * b.fta for b in boxes) / 2
        if possessions <= 0:
            continue
        for b in boxes:
            out.append(TeamGame(game_id, b.team_id, possessions, float(2 * b.fgm + b.fg3m + b.ftm)))
    return out


@dataclass(frozen=True)
class TotalsParams:
    half_life: float = 8.0
    carryover: float = 0.5
    prior_possessions: float = 280.0
    """Pseudo-possessions at the league's points per possession."""
    prior_games: float = 4.0
    """Pseudo-games at the league's pace."""
    opponent_adjust: bool = True


@dataclass(frozen=True)
class TotalPrediction:
    game_id: int
    possessions: float
    total: float
    home_games: int
    away_games: int


@dataclass
class _Sum:
    total: float = 0.0
    weight: float = 0.0

    def scale(self, f: float) -> None:
        self.total *= f
        self.weight *= f

    def mean(self, prior_weight: float, prior_mean: float) -> float:
        return (self.total + prior_weight * prior_mean) / (self.weight + prior_weight)


@dataclass
class _Team:
    offense: _Sum = field(default_factory=_Sum)  # points / possession
    defense: _Sum = field(default_factory=_Sum)  # points allowed / possession
    pace: _Sum = field(default_factory=_Sum)  # possessions / game
    season: int | None = None
    games: int = 0


def walk_forward(
    games: Sequence[EloGame], team_games: Iterable[TeamGame], params: TotalsParams
) -> dict[int, TotalPrediction]:
    stats: dict[int, dict[int, TeamGame]] = defaultdict(dict)
    for tg in team_games:
        stats[tg.game_id][tg.team_id] = tg
    teams: dict[int, _Team] = {}
    league_points, league_pace = _Sum(), _Sum()
    league_season: int | None = None
    decay = math.pow(0.5, 1.0 / params.half_life)

    def team(tid: int, season: int) -> _Team:
        t = teams.setdefault(tid, _Team())
        if t.season is not None and t.season != season:
            for s in (t.offense, t.defense, t.pace):
                s.scale(params.carryover)
        t.season = season
        return t

    out: dict[int, TotalPrediction] = {}
    for g in sorted(games, key=lambda g: (g.commence_time, g.game_id)):
        if league_season is not None and league_season != g.season:
            league_points.scale(params.carryover)
            league_pace.scale(params.carryover)
        league_season = g.season
        lp = league_points.mean(1.0, 1.0)
        lpace = league_pace.mean(1.0, 68.0)
        home, away = team(g.home_id, g.season), team(g.away_id, g.season)
        (h_off, h_def, h_pace), (a_off, a_def, a_pace) = (
            (
                t.offense.mean(params.prior_possessions, lp),
                t.defense.mean(params.prior_possessions, lp),
                t.pace.mean(params.prior_games, lpace),
            )
            for t in (home, away)
        )
        possessions = lpace + (h_pace - lpace) + (a_pace - lpace)
        home_ppp = lp + (h_off - lp) + (a_def - lp)
        away_ppp = lp + (a_off - lp) + (h_def - lp)
        out[g.game_id] = TotalPrediction(
            g.game_id, possessions, possessions * (home_ppp + away_ppp), home.games, away.games
        )

        game = stats.get(g.game_id, {})
        own_h, own_a = game.get(g.home_id), game.get(g.away_id)
        if own_h is None or own_a is None:
            continue
        going_in = {g.home_id: (h_off, h_def, h_pace), g.away_id: (a_off, a_def, a_pace)}
        for oid, t, own, opp in (
            (g.away_id, home, own_h, own_a),
            (g.home_id, away, own_a, own_h),
        ):
            o_off, o_def, o_pace = going_in[oid]
            off_adj = (o_def - lp) if params.opponent_adjust else 0.0
            def_adj = (o_off - lp) if params.opponent_adjust else 0.0
            pace_adj = (o_pace - lpace) if params.opponent_adjust else 0.0
            for s in (t.offense, t.defense, t.pace):
                s.scale(decay)
            t.offense.total += own.points - off_adj * own.possessions
            t.offense.weight += own.possessions
            t.defense.total += opp.points - def_adj * own.possessions
            t.defense.weight += own.possessions
            t.pace.total += own.possessions - pace_adj
            t.pace.weight += 1.0
            t.games += 1
        for own in (own_h, own_a):
            league_points.total += own.points
            league_points.weight += own.possessions
        league_pace.total += own_h.possessions
        league_pace.weight += 1.0
    return out


# --------------------------------------------------------------------------- scoring


@dataclass(frozen=True)
class TotalRow:
    game_id: int
    season: int
    predicted: float
    actual: float
    line: float | None
    market_over: float | None
    """Closing no-vig P(over)."""
    over_odds: float | None
    under_odds: float | None
    min_games: int


def rows_for(
    games: Sequence[EloGame],
    predictions: dict[int, TotalPrediction],
    closes: dict[int, ReportedLine],
    window: tuple[int, int],
    *,
    min_games: int = 5,
) -> list[TotalRow]:
    """Played games in ``window`` where both teams had ``min_games`` of history
    (early-season ratings are mostly prior)."""
    out = []
    for g in games:
        p = predictions.get(g.game_id)
        if p is None or not g.played or not (window[0] <= g.season <= window[1]):
            continue
        if min(p.home_games, p.away_games) < min_games:
            continue
        line = closes.get(g.game_id)
        assert g.home_score is not None and g.away_score is not None
        out.append(
            TotalRow(
                g.game_id,
                g.season,
                p.total,
                float(g.home_score + g.away_score),
                line.total if line else None,
                no_vig(line.over_odds, line.under_odds) if line else None,
                line.over_odds if line else None,
                line.under_odds if line else None,
                min(p.home_games, p.away_games),
            )
        )
    return out


@dataclass(frozen=True)
class Calibration:
    intercept: float
    slope: float
    sigma: float
    """Residual standard deviation of the calibrated prediction (TRAIN)."""

    def points(self, predicted: float) -> float:
        return self.intercept + self.slope * predicted


def fit_calibration(rows: Sequence[TotalRow]) -> Calibration:
    x = np.array([r.predicted for r in rows])
    y = np.array([r.actual for r in rows])
    slope, intercept = np.polyfit(x, y, 1)
    resid = y - (intercept + slope * x)
    return Calibration(float(intercept), float(slope), float(resid.std(ddof=2)))


def _phi(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def p_over(cal: Calibration, predicted: float, line: float) -> float:
    """P(over | no push) for a whole-number score: continuity at the line."""
    mu = cal.points(predicted)
    if line == int(line):
        over = 1 - _phi((line + 0.5 - mu) / cal.sigma)
        under = _phi((line - 0.5 - mu) / cal.sigma)
        return over / (over + under)
    return 1 - _phi((line - mu) / cal.sigma)


def fit_anchored(rows: Sequence[TotalRow], cal: Calibration) -> MarketAnchoredModel:
    priced = [r for r in rows if r.line is not None and r.market_over is not None]
    decided = [r for r in priced if r.actual != r.line]
    return fit_market_anchored(
        [r.market_over for r in decided],  # type: ignore[misc]
        [cal.points(r.predicted) - r.line for r in decided],  # type: ignore[operator]
        [int(r.actual > r.line) for r in decided],  # type: ignore[operator]
    )


@dataclass(frozen=True)
class TotalsReport:
    games: int
    rmse: float
    market_rmse: float
    priced: int
    standalone_vs_market: tuple[float, float]
    """(mean paired log-loss difference, standard error); negative beats the market."""
    anchored_vs_market: tuple[float, float]
    anchored_bets: dict[float, tuple[int, float]]
    """min edge -> (bets, ROI) betting the anchored side at the closing price."""


def _ll(p: float, y: int) -> float:
    p = min(max(p, 1e-9), 1 - 1e-9)
    return -math.log(p if y else 1 - p)


def _paired(d: list[float]) -> tuple[float, float]:
    n = len(d)
    if n < 2:
        return (sum(d) / n if n else 0.0), 0.0
    mean = sum(d) / n
    return mean, math.sqrt(sum((x - mean) ** 2 for x in d) / (n - 1) / n)


def evaluate(
    rows: Sequence[TotalRow], cal: Calibration, anchored: MarketAnchoredModel
) -> TotalsReport:
    errors = [cal.points(r.predicted) - r.actual for r in rows]
    market_errors = [r.line - r.actual for r in rows if r.line is not None]
    d_stand, d_anch = [], []
    bets: dict[float, list[float]] = {e: [] for e in (0.0, 0.02, 0.04, 0.06)}
    priced = 0
    from ttk import betting_math as bm

    for r in rows:
        if r.line is None or r.market_over is None or r.actual == r.line:
            continue
        priced += 1
        y = int(r.actual > r.line)
        stand = p_over(cal, r.predicted, r.line)
        anch = anchored.home_cover_probability(r.market_over, cal.points(r.predicted) - r.line)
        d_stand.append(_ll(stand, y) - _ll(r.market_over, y))
        d_anch.append(_ll(anch, y) - _ll(r.market_over, y))
        edge = anch - r.market_over
        over = edge >= 0
        price = r.over_odds if over else r.under_odds
        if price is None:
            continue
        won = (y == 1) == over
        for e, units in bets.items():
            if abs(edge) >= e:
                units.append(bm.american_to_decimal(price) - 1 if won else -1.0)
    return TotalsReport(
        games=len(rows),
        rmse=math.sqrt(sum(e * e for e in errors) / len(errors)) if errors else math.nan,
        market_rmse=math.sqrt(sum(e * e for e in market_errors) / len(market_errors))
        if market_errors
        else math.nan,
        priced=priced,
        standalone_vs_market=_paired(d_stand),
        anchored_vs_market=_paired(d_anch),
        anchored_bets={e: (len(u), sum(u) / len(u) if u else math.nan) for e, u in bets.items()},
    )


HALF_LIVES = (6.0, 10.0, 16.0, 25.0)
CARRYOVERS = (0.3, 0.5, 0.7)
OPPONENT_ADJUST = (True, False)


@dataclass(frozen=True)
class TotalsBacktest:
    params: TotalsParams
    calibration: Calibration
    anchored: MarketAnchoredModel
    train: TotalsReport
    validate: TotalsReport
    grid: list[tuple[TotalsParams, float]]
    """Every candidate with its TRAIN error of the calibrated total."""


def backtest(
    games: Sequence[EloGame],
    team_games: Sequence[TeamGame],
    closes: dict[int, ReportedLine],
    train: tuple[int, int],
    validate: tuple[int, int],
) -> TotalsBacktest:
    """Pick the parameters by TRAIN error, fit the calibration and the anchored
    model on TRAIN, score TRAIN and VALIDATE. Test seasons are never read."""
    import itertools

    best = None
    grid = []
    for hl, co, adj in itertools.product(HALF_LIVES, CARRYOVERS, OPPONENT_ADJUST):
        params = TotalsParams(half_life=hl, carryover=co, opponent_adjust=adj)
        preds = walk_forward(games, team_games, params)
        rows = rows_for(games, preds, closes, train)
        cal = fit_calibration(rows)
        rmse = math.sqrt(sum((cal.points(r.predicted) - r.actual) ** 2 for r in rows) / len(rows))
        grid.append((params, rmse))
        if best is None or rmse < best[0]:
            best = (rmse, params, preds, cal, rows)
    assert best is not None
    _, params, preds, cal, train_rows = best
    anchored = fit_anchored(train_rows, cal)
    return TotalsBacktest(
        params,
        cal,
        anchored,
        evaluate(train_rows, cal, anchored),
        evaluate(rows_for(games, preds, closes, validate), cal, anchored),
        grid,
    )


# --------------------------------------------------------------------------- frozen

TOTALS_ARTIFACT_VERSION = 1
MIN_GAMES = 5
"""Both teams need this many games of history, as in validation (rows_for)."""


def freeze_totals(
    result: TotalsBacktest, sport: str, splits: dict[str, list[int]]
) -> dict[str, object]:
    """The backtest's fitted parameters as plain JSON (TRAIN only)."""
    p, c, a = result.params, result.calibration, result.anchored
    return {
        "artifact_version": TOTALS_ARTIFACT_VERSION,
        "market": "TOTAL",
        "sport": sport,
        "splits": splits,
        "min_games": MIN_GAMES,
        "params": {
            "half_life": p.half_life,
            "carryover": p.carryover,
            "prior_possessions": p.prior_possessions,
            "prior_games": p.prior_games,
            "opponent_adjust": p.opponent_adjust,
        },
        "calibration": {"intercept": c.intercept, "slope": c.slope, "sigma": c.sigma},
        "anchored": {
            "intercept": a.intercept,
            "market_coef": a.market_coef,
            "disagreement_coef": a.disagreement_coef,
            "n_train": a.n_train,
        },
    }


@dataclass(frozen=True)
class TotalView:
    predicted: float
    """Calibrated predicted total (points)."""
    over_standalone: float
    over_anchored: float
    """P(over | no push) at the line."""
    push: float
    features: dict[str, float]


def push_probability(cal: Calibration, predicted: float, line: float) -> float:
    if line != int(line):
        return 0.0
    mu = cal.points(predicted)
    return _phi((line + 0.5 - mu) / cal.sigma) - _phi((line - 0.5 - mu) / cal.sigma)


class FrozenTotals:
    """Rebuilds the predictor from an artifact; only state (team ratings from
    finished games' box totals) moves forward."""

    def __init__(
        self, artifact: dict[str, Any], games: Sequence[EloGame], team_games: Sequence[TeamGame]
    ) -> None:
        if artifact.get("artifact_version") != TOTALS_ARTIFACT_VERSION:
            raise ValueError("unknown totals artifact version")
        self.artifact = artifact
        self.params = TotalsParams(**artifact["params"])
        self.calibration = Calibration(**artifact["calibration"])
        self.anchored = MarketAnchoredModel(**artifact["anchored"])
        self.min_games = int(artifact["min_games"])
        self.predictions = walk_forward(games, team_games, self.params)

    def view(self, game_id: int, line: float, market_over: float) -> TotalView | None:
        p = self.predictions.get(game_id)
        if p is None or min(p.home_games, p.away_games) < self.min_games:
            return None
        points = self.calibration.points(p.total)
        return TotalView(
            points,
            p_over(self.calibration, p.total, line),
            self.anchored.home_cover_probability(market_over, points - line),
            push_probability(self.calibration, p.total, line),
            {
                "possessions": p.possessions,
                "raw_total": p.total,
                "home_games": float(p.home_games),
                "away_games": float(p.away_games),
            },
        )


def verify_totals(
    frozen: FrozenTotals,
    result: TotalsBacktest,
    games: Sequence[EloGame],
    closes: dict[int, ReportedLine],
    validate: tuple[int, int],
) -> int:
    """The thawed model must reproduce the backtest's VALIDATE report exactly.
    Returns the number of games checked."""
    rows = rows_for(games, frozen.predictions, closes, validate, min_games=frozen.min_games)
    report = evaluate(rows, frozen.calibration, frozen.anchored)
    if repr(report) != repr(result.validate):  # repr: an empty bucket's ROI is nan
        raise AssertionError("frozen totals model differs from the backtest on VALIDATE")
    return report.games
