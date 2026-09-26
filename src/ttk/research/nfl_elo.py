"""NFL spread models: Elo baseline and its successors, tuned and compared walk-forward.

Protocol (docs/MODEL-GOVERNANCE.md):
- Elo runs once over all seasons in time order; each prediction uses only games
  already played, so it is a walk-forward backtest by construction.
- 1999-2001 are burn-in (ratings settling) and never scored.
- Everything fitted - Elo hyperparameters, season home-field scale, margin
  models, key-number weights, the market-anchored regression - is fitted on
  TRAIN seasons only. VALIDATE judges. TEST is sealed until explicitly requested.
- Season home field (rolling mode) uses only seasons before the one predicted.
- Spread candidates are compared on the same games: those with a reported line
  AND real prices on both sides (2006+), so the no-vig market is always real.
- Reported nflverse lines have undocumented timing. The market-anchored model
  uses a line as an input only when the simulated bet is placed at that same
  line, never an earlier price, and never as a closing line.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ttk import betting_math as bm
from ttk.db.models import Game, ReportedLine
from ttk.domain import GameStatus, Sport
from ttk.models.anchored import MarketAnchoredModel, fit_market_anchored
from ttk.models.elo import EloGame, EloParams, EloPrediction, run_elo
from ttk.models.margin import (
    KeyNumberMarginModel,
    MarginDistribution,
    MarginModel,
    fit_key_number_weights,
    fit_margin_model,
)
from ttk.models.metrics import CalibrationBin, Score, calibration_table, score


@dataclass(frozen=True)
class Splits:
    burn_in: tuple[int, int] = (1999, 2001)
    train: tuple[int, int] = (2002, 2017)
    validate: tuple[int, int] = (2018, 2021)
    test: tuple[int, int] = (2022, 2025)


DEFAULT_SPLITS = Splits()

K_GRID = (10.0, 15.0, 20.0, 25.0, 30.0)
HOME_FIELD_GRID = (0.0, 20.0, 35.0, 50.0, 65.0)
HOME_FIELD_PER_POINT_GRID = (10.0, 15.0, 20.0, 25.0, 30.0, 35.0)
REGRESSION_GRID = (0.2, 1 / 3, 0.5)
MOV_GRID = (True, False)
EDGE_THRESHOLDS = (0.0, 0.02, 0.04, 0.06)
HOME_FIELD_LOOKBACK = 3


# --------------------------------------------------------------------------- data


def load_nfl_games(session: Session) -> tuple[list[EloGame], dict[int, ReportedLine]]:
    rows = session.scalars(
        select(Game).where(
            Game.sport == Sport.NFL,
            Game.season.is_not(None),
            Game.status.in_([GameStatus.FINAL, GameStatus.SCHEDULED, GameStatus.IN_PROGRESS]),
        )
    ).all()
    games = [
        EloGame(
            game_id=g.id,
            season=int(g.season or 0),
            commence_time=g.commence_time,
            home_id=g.home_team_id,
            away_id=g.away_team_id,
            neutral_site=g.neutral_site,
            home_score=g.home_score if g.status == GameStatus.FINAL else None,
            away_score=g.away_score if g.status == GameStatus.FINAL else None,
        )
        for g in rows
    ]
    lines = {
        line.game_id: line
        for line in session.scalars(select(ReportedLine).where(ReportedLine.provider == "nflverse"))
    }
    return games, lines


def _in(season: int, window: tuple[int, int]) -> bool:
    return window[0] <= season <= window[1]


def _margin(g: EloGame) -> int:
    assert g.home_score is not None and g.away_score is not None
    return g.home_score - g.away_score


def prior_home_field(
    games: Sequence[EloGame], lookback: int = HOME_FIELD_LOOKBACK
) -> dict[int, float]:
    """Season -> mean home margin (points) over the previous ``lookback`` seasons of
    played, non-neutral games. Uses no game from the season it is for."""
    by_season: dict[int, list[int]] = defaultdict(list)
    for g in games:
        if g.played and not g.neutral_site:
            by_season[g.season].append(_margin(g))
    seasons = sorted({g.season for g in games})
    result: dict[int, float] = {}
    for season in seasons:
        prior = [m for s in range(season - lookback, season) for m in by_season.get(s, [])]
        if prior:
            result[season] = sum(prior) / len(prior)
    return result


# --------------------------------------------------------------------------- tuning


@dataclass(frozen=True)
class TunedElo:
    params: EloParams
    home_field_mode: str
    """'constant' (params.home_field) or 'rolling' (scale x prior seasons' home margin)."""
    home_field_per_point: float | None
    train_log_loss: float
    home_field_by_season: dict[int, float] | None = None

    def run(self, games: Sequence[EloGame]) -> list[EloPrediction]:
        return run_elo(games, self.params, home_field_by_season=self.home_field_by_season)


def _win_log_loss(
    predictions: Sequence[EloPrediction], games: dict[int, EloGame], window: tuple[int, int]
) -> float:
    probs, outcomes = [], []
    for p in predictions:
        g = games[p.game_id]
        if _in(p.season, window) and g.played and _margin(g) != 0:
            probs.append(p.home_win_probability)
            outcomes.append(int(_margin(g) > 0))
    return score(probs, outcomes).log_loss


def tune(games: Sequence[EloGame], splits: Splits) -> TunedElo:
    """Grid search minimizing TRAIN moneyline log loss over two home-field modes.
    Never looks past TRAIN."""
    by_id = {g.game_id: g for g in games}
    prior_hfa = prior_home_field(games)
    best: TunedElo | None = None

    def consider(candidate: TunedElo) -> None:
        nonlocal best
        if best is None or candidate.train_log_loss < best.train_log_loss:
            best = candidate

    for k, reg, mov in itertools.product(K_GRID, REGRESSION_GRID, MOV_GRID):
        for hfa in HOME_FIELD_GRID:
            params = EloParams(k=k, home_field=hfa, season_regression=reg, margin_of_victory=mov)
            loss = _win_log_loss(run_elo(games, params), by_id, splits.train)
            consider(TunedElo(params, "constant", None, loss))
        for per_point in HOME_FIELD_PER_POINT_GRID:
            params = EloParams(k=k, home_field=0.0, season_regression=reg, margin_of_victory=mov)
            by_season = {s: per_point * m for s, m in prior_hfa.items()}
            loss = _win_log_loss(
                run_elo(games, params, home_field_by_season=by_season), by_id, splits.train
            )
            consider(TunedElo(params, "rolling", per_point, loss, by_season))
    assert best is not None
    return best


# --------------------------------------------------------------------------- evaluation


@dataclass
class BettingResult:
    min_edge: float
    bets: int = 0
    wins: int = 0
    losses: int = 0
    pushes: int = 0
    units: float = 0.0

    @property
    def roi(self) -> float | None:
        return self.units / self.bets if self.bets else None


@dataclass
class CandidateResult:
    name: str
    spread: Score
    push_rate_predicted: float | None
    vs_market: PairedComparison
    """Per-game log loss minus the market's on the same games (negative = better)."""
    betting: list[BettingResult] = field(default_factory=list)


@dataclass(frozen=True)
class PairedComparison:
    mean_log_loss_diff: float
    standard_error: float

    @property
    def z(self) -> float:
        return self.mean_log_loss_diff / self.standard_error if self.standard_error else 0.0


def paired_log_loss(
    model: Sequence[float], market: Sequence[float], outcomes: Sequence[int]
) -> PairedComparison:
    diffs = [
        score([p], [y]).log_loss - score([q], [y]).log_loss
        for p, q, y in zip(model, market, outcomes, strict=True)
    ]
    n = len(diffs)
    if n < 2:
        return PairedComparison(0.0, 0.0)
    mean = sum(diffs) / n
    variance = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    return PairedComparison(mean, (variance / n) ** 0.5)


@dataclass
class SplitReport:
    seasons: tuple[int, int]
    moneyline_elo: Score
    moneyline_elo_on_market_games: Score
    moneyline_market: Score
    moneyline_home_baseline: Score
    spread_market: Score
    push_rate_actual: float
    spread_candidates: list[CandidateResult]
    calibration: list[CalibrationBin] = field(default_factory=list)


def _no_vig(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return bm.no_vig_probabilities(
        [bm.american_to_decimal(a), bm.american_to_decimal(b)]
    ).probabilities[0]


@dataclass(frozen=True)
class PricedSpread:
    """A played game with a reported spread and real prices on both sides."""

    game: EloGame
    prediction: EloPrediction
    home_line: float
    home_odds: float
    away_odds: float
    market_home_cover: float

    @property
    def cover_margin(self) -> float:
        return _margin(self.game) + self.home_line


def priced_spreads(
    predictions: Iterable[EloPrediction],
    games: dict[int, EloGame],
    lines: dict[int, ReportedLine],
    window: tuple[int, int],
) -> list[PricedSpread]:
    rows = []
    for p in predictions:
        g = games[p.game_id]
        line = lines.get(g.game_id)
        if not (_in(p.season, window) and g.played and line is not None):
            continue
        if line.home_spread is None or line.home_spread_odds is None:
            continue
        if line.away_spread_odds is None:
            continue
        market = _no_vig(line.home_spread_odds, line.away_spread_odds)
        assert market is not None
        rows.append(
            PricedSpread(
                g, p, line.home_spread, line.home_spread_odds, line.away_spread_odds, market
            )
        )
    return rows


# P(home covers | no push), plus P(push) when the candidate models it.
CoverFn = Callable[[PricedSpread], tuple[float, float | None]]


def _evaluate_candidate(name: str, rows: Sequence[PricedSpread], cover: CoverFn) -> CandidateResult:
    probs, outcomes, market = [], [], []
    predicted_push, push_modeled = 0.0, True
    bets = [BettingResult(t) for t in EDGE_THRESHOLDS]
    for r in rows:
        p_home, p_push = cover(r)
        if p_push is None:
            push_modeled = False
        else:
            predicted_push += p_push
        if r.cover_margin != 0:
            probs.append(p_home)
            outcomes.append(int(r.cover_margin > 0))
            market.append(r.market_home_cover)
        edge_home = p_home - r.market_home_cover
        home_side = edge_home >= 0
        edge = abs(edge_home)
        price = r.home_odds if home_side else r.away_odds
        won = r.cover_margin > 0 if home_side else r.cover_margin < 0
        for b in bets:
            if edge == 0 or edge < b.min_edge:
                continue
            b.bets += 1
            if r.cover_margin == 0:
                b.pushes += 1
            elif won:
                b.wins += 1
                b.units += bm.american_to_decimal(price) - 1.0
            else:
                b.losses += 1
                b.units -= 1.0
    return CandidateResult(
        name,
        score(probs, outcomes),
        predicted_push / len(rows) if rows and push_modeled else None,
        paired_log_loss(probs, market, outcomes),
        bets,
    )


@dataclass(frozen=True)
class FittedModels:
    normal: MarginModel
    key_number: KeyNumberMarginModel
    anchored: MarketAnchoredModel

    def candidates(self) -> list[tuple[str, CoverFn]]:
        def margin_fn(model: MarginDistribution) -> CoverFn:
            def fn(r: PricedSpread) -> tuple[float, float | None]:
                probs = model.spread(r.home_line, r.prediction.elo_diff)
                return probs.home_cover_excluding_push, probs.push

            return fn

        def anchored_fn(r: PricedSpread) -> tuple[float, float | None]:
            disagreement = self.key_number.expected_margin(r.prediction.elo_diff) + r.home_line
            return (
                self.anchored.home_cover_probability(r.market_home_cover, disagreement),
                None,
            )

        return [
            ("elo_normal", margin_fn(self.normal)),
            ("elo_key_numbers", margin_fn(self.key_number)),
            ("market_anchored", anchored_fn),
        ]


def evaluate(
    predictions: Sequence[EloPrediction],
    games: dict[int, EloGame],
    lines: dict[int, ReportedLine],
    models: FittedModels,
    window: tuple[int, int],
    home_win_rate: float,
) -> SplitReport:
    ml_p, ml_y, mm_elo, mm_mkt, mm_y = [], [], [], [], []
    for p in predictions:
        g = games[p.game_id]
        if not (_in(p.season, window) and g.played) or _margin(g) == 0:
            continue
        y = int(_margin(g) > 0)
        ml_p.append(p.home_win_probability)
        ml_y.append(y)
        line = lines.get(g.game_id)
        market = _no_vig(line.home_moneyline, line.away_moneyline) if line else None
        if market is not None:
            mm_elo.append(p.home_win_probability)
            mm_mkt.append(market)
            mm_y.append(y)

    rows = priced_spreads(predictions, games, lines, window)
    decided = [r for r in rows if r.cover_margin != 0]
    return SplitReport(
        seasons=window,
        moneyline_elo=score(ml_p, ml_y),
        moneyline_elo_on_market_games=score(mm_elo, mm_y),
        moneyline_market=score(mm_mkt, mm_y),
        moneyline_home_baseline=score([home_win_rate] * len(ml_y), ml_y),
        spread_market=score(
            [r.market_home_cover for r in decided], [int(r.cover_margin > 0) for r in decided]
        ),
        push_rate_actual=(len(rows) - len(decided)) / len(rows) if rows else 0.0,
        spread_candidates=[_evaluate_candidate(name, rows, fn) for name, fn in models.candidates()],
        calibration=calibration_table(ml_p, ml_y),
    )


@dataclass
class BacktestReport:
    tuned: TunedElo
    models: FittedModels
    train: SplitReport
    validate: SplitReport
    test: SplitReport | None

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["tuned"].pop("home_field_by_season", None)
        data["models"]["key_number"].pop("_cache", None)
        return data


def fit_models(
    predictions: Sequence[EloPrediction],
    games: dict[int, EloGame],
    lines: dict[int, ReportedLine],
    train: tuple[int, int],
) -> FittedModels:
    """Margin models and the market-anchored regression, fitted on TRAIN only."""
    pairs = [
        (p.elo_diff, _margin(games[p.game_id]))
        for p in predictions
        if _in(p.season, train) and games[p.game_id].played
    ]
    diffs, margins = [d for d, _ in pairs], [m for _, m in pairs]
    normal = fit_margin_model(diffs, margins)
    key_number = fit_key_number_weights(normal, diffs, margins)
    decided = [r for r in priced_spreads(predictions, games, lines, train) if r.cover_margin != 0]
    anchored = fit_market_anchored(
        [r.market_home_cover for r in decided],
        [key_number.expected_margin(r.prediction.elo_diff) + r.home_line for r in decided],
        [int(r.cover_margin > 0) for r in decided],
    )
    return FittedModels(normal, key_number, anchored)


def backtest(
    games: Sequence[EloGame],
    lines: dict[int, ReportedLine],
    *,
    splits: Splits = DEFAULT_SPLITS,
    params: EloParams | None = None,
    include_test: bool = False,
) -> BacktestReport:
    by_id = {g.game_id: g for g in games}
    if params is None:
        tuned = tune(games, splits)
    else:
        loss = _win_log_loss(run_elo(games, params), by_id, splits.train)
        tuned = TunedElo(params, "constant", None, loss)
    predictions = tuned.run(games)
    models = fit_models(predictions, by_id, lines, splits.train)

    train_decided = [
        _margin(by_id[p.game_id])
        for p in predictions
        if _in(p.season, splits.train) and by_id[p.game_id].played
    ]
    nonzero = [m for m in train_decided if m != 0]
    home_win_rate = sum(m > 0 for m in nonzero) / len(nonzero)

    def run(window: tuple[int, int]) -> SplitReport:
        return evaluate(predictions, by_id, lines, models, window, home_win_rate)

    return BacktestReport(
        tuned=tuned,
        models=models,
        train=run(splits.train),
        validate=run(splits.validate),
        test=run(splits.test) if include_test else None,
    )
