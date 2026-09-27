"""NBA spread research: the validated NFL pipeline applied to ESPN history, plus
rest features and a test against OPENING lines.

Data: ``ttk import-espn-history --sport NBA`` - games/results and per-book
closing lines (``espn:<book>``), opening lines from 2023-24 (``espn-open:<book>``).
Per game the representative line is the median home spread across books at the
most common number, priced by the first book (in BOOK_PRIORITY order) quoting it.

Protocol (docs/MODEL-GOVERNANCE.md): ESPN season years (2025-26 = 2026).
Burn-in 2018, TRAIN 2019-2022, VALIDATE 2023-2024, TEST 2025-2026 sealed.

Rest features come from the schedule (known in advance, so pregame-safe):
days since each team's previous game (capped), and back-to-back flags.

The opening-line test: every candidate is evaluated as if betting at the
opener, scored against results AND against the closing line (CLV). A model
that beats openers but not closes is exactly the "early-line" edge the
closing-line-only NFL benchmark could not see.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ttk import betting_math as bm
from ttk.db.models import Game, PlayerGameStat, ReportedLine
from ttk.domain import GameStatus, Sport
from ttk.models.anchored import MarketAnchoredModel, fit_market_anchored
from ttk.models.elo import EloGame, EloPrediction
from ttk.models.margin import KeyNumberMarginModel, MarginModel, key_number_weights_for_means
from ttk.research.nba_lineups import Availability, BoxRow, availability, hollinger_game_score
from ttk.research.nfl_elo import (
    BacktestReport,
    BettingResult,
    CandidateResult,
    CoverFn,
    PricedSpread,
    Splits,
    _evaluate_candidate,
    _in,
    _margin,
    _no_vig,
    backtest,
    priced_spreads,
)

NBA_SPLITS = Splits(
    burn_in=(2018, 2018), train=(2019, 2022), validate=(2023, 2024), test=(2025, 2026)
)
NBA_HOME_FIELD_GRID = (40.0, 60.0, 80.0, 100.0, 120.0)
NBA_K_GRID = (5.0, 7.5, 10.0, 15.0, 20.0, 25.0)  # 10 was the TRAIN optimum at the old edge
BOOK_PRIORITY = (
    "consensus",
    "draftkings",
    "espn-bet",
    "caesars-sportsbook",
    "mgm",
    "fanduel",
    "westgate",
    "unibet",
    "caesars",
    "betfairsportsbook",
)
REST_CAP = 4


# --------------------------------------------------------------------------- data


def _representative(rows: Sequence[ReportedLine]) -> ReportedLine | None:
    """One line per game: the most common home spread across books, priced by the
    highest-priority book quoting that number with prices on both sides."""
    priced = [r for r in rows if r.home_spread is not None]
    if not priced:
        return None
    number = Counter(r.home_spread for r in priced).most_common(1)[0][0]
    at_number = [r for r in priced if r.home_spread == number]
    rank = {book: i for i, book in enumerate(BOOK_PRIORITY)}

    def order(r: ReportedLine) -> tuple[int, int]:
        book = r.provider.split(":", 1)[1]
        both = _no_vig(r.home_spread_odds, r.away_spread_odds) is not None
        return (0 if both else 1, rank.get(book, len(rank)))

    best = min(at_number, key=order)
    totals = [r.total for r in rows if r.total is not None]
    return ReportedLine(
        game_id=best.game_id,
        provider=best.provider,
        home_spread=number,
        home_spread_odds=best.home_spread_odds,
        away_spread_odds=best.away_spread_odds,
        total=statistics.median(totals) if totals else None,
        over_odds=best.over_odds,
        under_odds=best.under_odds,
        home_moneyline=best.home_moneyline,
        away_moneyline=best.away_moneyline,
    )


@dataclass(frozen=True)
class NbaData:
    games: list[EloGame]
    closes: dict[int, ReportedLine]
    opens: dict[int, ReportedLine]
    rest: dict[int, tuple[float, float, bool, bool]]
    """game_id -> (home rest days, away rest days, home back-to-back, away back-to-back)."""
    lineups: dict[int, Availability] = field(default_factory=dict)
    """game_id -> walk-forward player availability (games with box scores)."""


def load_nba(session: Session) -> NbaData:
    rows = session.scalars(
        select(Game).where(
            Game.sport == Sport.NBA,
            Game.season.is_not(None),
            Game.status.in_([GameStatus.FINAL, GameStatus.SCHEDULED]),
            # Preseason and All-Star games are not competitive team results.
            Game.season_type.in_(["REG", "POST"]),
        )
    ).all()
    games = [
        EloGame(
            g.id,
            int(g.season or 0),
            g.commence_time,
            g.home_team_id,
            g.away_team_id,
            g.neutral_site,
            g.home_score if g.status == GameStatus.FINAL else None,
            g.away_score if g.status == GameStatus.FINAL else None,
        )
        for g in rows
    ]
    by_game: dict[str, dict[int, list[ReportedLine]]] = {
        "espn": defaultdict(list),
        "espn-open": defaultdict(list),
    }
    for line in session.scalars(select(ReportedLine).where(ReportedLine.provider.like("espn%"))):
        kind = line.provider.split(":", 1)[0]
        if kind in by_game:
            by_game[kind][line.game_id].append(line)
    closes = {
        gid: r
        for gid, rows_ in by_game["espn"].items()
        if (r := _representative(rows_)) is not None
    }
    opens = {
        gid: r
        for gid, rows_ in by_game["espn-open"].items()
        if (r := _representative(rows_)) is not None
    }
    box: dict[int, list[BoxRow]] = defaultdict(list)
    for stat in session.scalars(
        select(PlayerGameStat).where(PlayerGameStat.game_id.in_([g.game_id for g in games]))
    ):
        box[stat.game_id].append(
            BoxRow(
                stat.player_id,
                stat.team_id,
                stat.played,
                hollinger_game_score(
                    points=stat.points,
                    fgm=stat.fgm,
                    fga=stat.fga,
                    ftm=stat.ftm,
                    fta=stat.fta,
                    oreb=stat.oreb,
                    dreb=stat.dreb,
                    ast=stat.ast,
                    stl=stat.stl,
                    blk=stat.blk,
                    tov=stat.tov,
                    pf=stat.pf,
                ),
            )
        )
    return NbaData(games, closes, opens, rest_features(games), availability(games, box))


def rest_features(games: Sequence[EloGame]) -> dict[int, tuple[float, float, bool, bool]]:
    """Days since each team's previous game (calendar days, capped), and whether
    it played the day before. Uses only the schedule, which is known in advance."""
    last: dict[int, datetime] = {}
    out: dict[int, tuple[float, float, bool, bool]] = {}
    for g in sorted(games, key=lambda g: (g.commence_time, g.game_id)):
        rests = []
        for team in (g.home_id, g.away_id):
            prev = last.get(team)
            days = (g.commence_time.date() - prev.date()).days if prev else REST_CAP
            rests.append(float(min(max(days, 0), REST_CAP)))
            last[team] = g.commence_time
        out[g.game_id] = (rests[0], rests[1], rests[0] <= 1, rests[1] <= 1)
    return out


# --------------------------------------------------------------------------- margin models


REST_FEATURES = ("elo_diff", "rest_diff", "home_b2b", "away_b2b")
FEATURE_SETS: dict[str, tuple[str, ...]] = {
    "rest": REST_FEATURES,
    # Known at tip-off only: scored against the close, never used at the opener.
    "lineup_tip": (*REST_FEATURES, "missing_diff"),
    # Known before the opener (the team's previous game).
    "lineup_prev": (*REST_FEATURES, "missing_prev_diff"),
}
AT_TIP_ONLY = frozenset({"lineup_tip"})
LINEUP_FEATURES = frozenset({"missing_diff", "missing_prev_diff"})


def feature_values(p: EloPrediction, data: NbaData) -> dict[str, float]:
    """Every feature for one game. Lineup features are 0 (neutral) for a game
    without a box score; fitting uses only games that have one."""
    home_rest, away_rest, home_b2b, away_b2b = data.rest[p.game_id]
    lineup = data.lineups.get(p.game_id)
    return {
        "elo_diff": p.elo_diff,
        "rest_diff": home_rest - away_rest,
        "home_b2b": float(home_b2b),
        "away_b2b": float(away_b2b),
        "missing_diff": lineup.missing_diff if lineup else 0.0,
        "missing_prev_diff": lineup.missing_prev_diff if lineup else 0.0,
    }


@dataclass(frozen=True)
class FeatureMarginModel:
    """Linear expected home margin on named features, with key-number cover and
    push probabilities around it."""

    names: tuple[str, ...]
    intercept: float
    coefs: dict[str, float]
    key: KeyNumberMarginModel
    n_train: int

    def expected_margin(self, values: dict[str, float]) -> float:
        return self.intercept + sum(self.coefs[n] * values[n] for n in self.names)


def fit_feature_model(
    predictions: Sequence[EloPrediction],
    data: NbaData,
    train: tuple[int, int],
    names: tuple[str, ...],
) -> FeatureMarginModel:
    games = {g.game_id: g for g in data.games}
    needs_lineup = bool(LINEUP_FEATURES & set(names))
    rows = [
        p
        for p in predictions
        if _in(p.season, train)
        and games[p.game_id].played
        and (not needs_lineup or p.game_id in data.lineups)
    ]
    x = np.column_stack(
        [
            np.ones(len(rows)),
            np.asarray([[feature_values(p, data)[n] for n in names] for p in rows]),
        ]
    )
    y = np.asarray([_margin(games[p.game_id]) for p in rows], dtype=float)
    beta = np.linalg.lstsq(x, y, rcond=None)[0]
    means = x @ beta
    sigma = float(np.std(y - means, ddof=x.shape[1]))
    weights = key_number_weights_for_means([float(m) for m in means], sigma, [int(v) for v in y])
    return FeatureMarginModel(
        names,
        float(beta[0]),
        {n: float(b) for n, b in zip(names, beta[1:], strict=True)},
        KeyNumberMarginModel(MarginModel(0.0, 0.0, sigma), weights),
        len(rows),
    )


# --------------------------------------------------------------------------- report


@dataclass
class OpenerResult:
    name: str
    bets: list[BettingResult]
    price_clv_n: int
    avg_price_clv: float | None
    """Bet price x closing no-vig - 1, for bets whose closing line stayed on the
    opening number (only then is the closing price comparable)."""
    moved_n: int
    avg_points_gained: float | None
    """For bets where the line moved: points held versus the close (never priced)."""


@dataclass
class NbaReport:
    base: BacktestReport
    models: dict[str, FeatureMarginModel]
    anchored: dict[str, MarketAnchoredModel]
    validate_features: list[CandidateResult]
    """Feature-model candidates on VALIDATE, scored against the close."""
    opener_games: int
    openers: list[OpenerResult] = field(default_factory=list)
    margin_rmse: dict[str, float] = field(default_factory=dict)
    lineup_coverage: dict[str, int] = field(default_factory=dict)
    """Games with walk-forward lineup features, per split."""

    @property
    def rest(self) -> FeatureMarginModel:
        return self.models["rest"]


def _feature_candidates(
    name: str, model: FeatureMarginModel, anchored: MarketAnchoredModel, data: NbaData
) -> list[tuple[str, CoverFn]]:
    def key_fn(r: PricedSpread) -> tuple[float, float | None]:
        mu = model.expected_margin(feature_values(r.prediction, data))
        probs = model.key.spread_at_mean(r.home_line, mu)
        return probs.home_cover_excluding_push, probs.push

    def anchored_fn(r: PricedSpread) -> tuple[float, float | None]:
        mu = model.expected_margin(feature_values(r.prediction, data))
        return anchored.home_cover_probability(r.market_home_cover, mu + r.home_line), None

    return [(f"{name}_key_numbers", key_fn), (f"market_anchored_{name}", anchored_fn)]


def _no_vig_home(line: ReportedLine) -> float | None:
    return _no_vig(line.home_spread_odds, line.away_spread_odds)


def opener_test(
    name: str,
    cover: CoverFn,
    rows: Sequence[PricedSpread],
    closes: dict[int, ReportedLine],
) -> OpenerResult:
    """Bet at the opener (rows are priced at opening lines); score vs results and
    measure CLV against the closing line at the same number."""
    result = _evaluate_candidate(name, rows, cover)
    price_clvs: list[float] = []
    points: list[float] = []
    for r in rows:
        p_home, _ = cover(r)
        edge = p_home - r.market_home_cover
        if abs(edge) < 0.02:  # the same 2-point threshold as the betting rows
            continue
        close = closes.get(r.game.game_id)
        if close is None or close.home_spread is None:
            continue
        home_side = edge > 0
        opener_line = r.home_line if home_side else -r.home_line
        close_line = close.home_spread if home_side else -close.home_spread
        if close_line != opener_line:
            points.append(opener_line - close_line)  # + = we hold more points
            continue
        p_close = _no_vig_home(close)
        if p_close is None:
            continue
        p_side = p_close if home_side else 1 - p_close
        price = r.home_odds if home_side else r.away_odds
        price_clvs.append(bm.closing_line_value(bm.american_to_decimal(price), p_side))
    return OpenerResult(
        name,
        result.betting,
        len(price_clvs),
        sum(price_clvs) / len(price_clvs) if price_clvs else None,
        len(points),
        sum(points) / len(points) if points else None,
    )


def nba_backtest(data: NbaData, *, include_test: bool = False) -> NbaReport:
    splits = NBA_SPLITS
    base = backtest(
        data.games,
        data.closes,
        splits=splits,
        include_test=include_test,
        home_field_grid=NBA_HOME_FIELD_GRID,
        k_grid=NBA_K_GRID,
    )
    predictions = base.tuned.run(data.games)
    games = {g.game_id: g for g in data.games}
    train_rows = [
        r
        for r in priced_spreads(predictions, games, data.closes, splits.train)
        if r.cover_margin != 0
    ]
    models: dict[str, FeatureMarginModel] = {}
    anchored: dict[str, MarketAnchoredModel] = {}
    candidates: dict[str, list[tuple[str, CoverFn]]] = {}
    for name, names in FEATURE_SETS.items():
        if LINEUP_FEATURES & set(names) and not data.lineups:
            continue  # no box scores imported
        model = fit_feature_model(predictions, data, splits.train, names)
        models[name] = model
        anchored[name] = fit_market_anchored(
            [r.market_home_cover for r in train_rows],
            [
                model.expected_margin(feature_values(r.prediction, data)) + r.home_line
                for r in train_rows
            ],
            [int(r.cover_margin > 0) for r in train_rows],
        )
        candidates[name] = _feature_candidates(name, model, anchored[name], data)
    validate_rows = priced_spreads(predictions, games, data.closes, splits.validate)
    validate_features = [
        _evaluate_candidate(label, validate_rows, fn)
        for pairs in candidates.values()
        for label, fn in pairs
    ]

    # Opening-line test on VALIDATE seasons (openers exist from 2023-24). Features
    # known only at tip-off are excluded: using them at the opener would be leakage.
    opener_rows = priced_spreads(predictions, games, data.opens, splits.validate)
    base_fns = dict(base.models.candidates())
    openers = [
        opener_test(label, fn, opener_rows, data.closes)
        for label, fn in [
            ("market_anchored", base_fns["market_anchored"]),
            ("elo_key_numbers", base_fns["elo_key_numbers"]),
            *[
                pair
                for name, pairs in candidates.items()
                if name not in AT_TIP_ONLY
                for pair in pairs
            ],
        ]
    ]

    errors: dict[str, list[float]] = defaultdict(list)
    for p in predictions:
        g = games[p.game_id]
        if _in(p.season, splits.validate) and g.played:
            errors["elo"].append(base.models.normal.expected_margin(p.elo_diff) - _margin(g))
            values = feature_values(p, data)
            for name, model in models.items():
                errors[name].append(model.expected_margin(values) - _margin(g))
    line_errors = [
        (-data.closes[p.game_id].home_spread) - _margin(games[p.game_id])  # type: ignore[operator]
        for p in predictions
        if _in(p.season, splits.validate)
        and games[p.game_id].played
        and p.game_id in data.closes
        and data.closes[p.game_id].home_spread is not None
    ]
    rmse = {k: float(np.sqrt(np.mean(np.square(v)))) for k, v in errors.items() if v}
    if line_errors:
        rmse["market_close"] = float(np.sqrt(np.mean(np.square(line_errors))))
    coverage = {
        label: sum(
            1
            for g in data.games
            if _in(g.season, window) and g.played and g.game_id in data.lineups
        )
        for label, window in (("train", splits.train), ("validate", splits.validate))
    }
    return NbaReport(
        base,
        models,
        anchored,
        validate_features,
        len(opener_rows),
        openers,
        rmse,
        coverage,
    )
