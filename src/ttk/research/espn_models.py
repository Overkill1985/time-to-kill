"""Spread research on ESPN history (NBA, CFB): the validated NFL pipeline plus
per-sport features and a test against OPENING lines.

Data: ``ttk import-espn-history --sport X`` - games/results and per-book closing
lines (``espn:<book>``), and the books' own opening lines where ESPN has them
(``espn-open:<book>``). Per game the representative line is the most common home
spread across books, priced by the first book (in BOOK_PRIORITY order) quoting it
with real prices on both sides.

Each sport is a ``SportConfig`` (research/nba_model.py, research/cfb_model.py):
splits (docs/MODEL-GOVERNANCE.md), tuning grids, rest cap and feature sets.

Rest features come from the schedule (known in advance, so pregame-safe): days
since each team's previous game (capped), and back-to-back flags.

The opening-line test: every candidate is evaluated as if betting at the
opener, scored against results AND against the closing line (CLV). A model
that beats openers but not closes is exactly the "early-line" edge the
closing-line-only NFL benchmark could not see.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ttk import betting_math as bm
from ttk.db.models import (
    Game,
    InjuryReport,
    PlayerGameStat,
    ReportedLine,
    TeamGameBox,
    TeamGameStat,
    TeamSeasonFeature,
)
from ttk.domain import GameStatus, Sport
from ttk.models.anchored import MarketAnchoredModel, fit_market_anchored
from ttk.models.elo import EloGame, EloPrediction
from ttk.models.margin import KeyNumberMarginModel, MarginModel, key_number_weights_for_means
from ttk.models.nfl_features import FeatureParams, TeamGameEpa, compute_features
from ttk.providers.espn_odds import is_market
from ttk.research.cfb_preseason import PRESEASON_FEATURES, SeasonFact, preseason_features
from ttk.research.nba_injuries import (
    HORIZONS_HOURS,
    InjuryTimeline,
    Report,
    SitRates,
    injury_features,
)
from ttk.research.nba_lineups import Availability, BoxRow, availability, hollinger_game_score
from ttk.research.nfl_elo import (
    HOME_FIELD_PER_POINT_GRID,
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


@dataclass(frozen=True)
class SportConfig:
    sport: Sport
    splits: Splits
    home_field_grid: tuple[float, ...]
    k_grid: tuple[float, ...]
    feature_sets: dict[str, tuple[str, ...]]
    """name -> features for a linear margin model (see ``feature_values``)."""
    rest_cap: int
    """Rest days are capped here (and a team's first game gets the cap)."""
    regression_targets: tuple[str, ...] = ("mean",)
    home_field_per_point_grid: tuple[float, ...] = HOME_FIELD_PER_POINT_GRID
    injuries: bool = False
    """Injury features from our own injury-report history (research/nba_injuries.py)."""
    team_boxes: bool = False
    """Possession-based efficiency from team_game_boxes: ``eff_diff`` adjusted,
    ``eff_raw_diff`` not (points per possession, offense minus defense)."""
    team_epa: bool = False
    """Walk-forward team efficiency from team_game_stats (models/nfl_features.py):
    ``epa_diff`` opponent-adjusted, ``epa_raw_diff`` not."""
    at_tip_only: frozenset[str] = frozenset()
    """Feature sets known only at tip-off: scored against the close, never the opener."""


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
class SportData:
    config: SportConfig
    games: list[EloGame]
    closes: dict[int, ReportedLine]
    opens: dict[int, ReportedLine]
    rest: dict[int, tuple[float, float, bool, bool]]
    """game_id -> (home rest days, away rest days, home back-to-back, away back-to-back)."""
    lineups: dict[int, Availability] = field(default_factory=dict)
    """game_id -> walk-forward player availability (games with box scores)."""
    preseason: dict[int, dict[str, float]] = field(default_factory=dict)
    """game_id -> preseason features known at kickoff (college football)."""
    efficiency: dict[int, dict[str, float]] = field(default_factory=dict)
    """game_id -> team efficiency from earlier games (``epa_diff``, ``epa_raw_diff``)."""
    injuries: dict[int, dict[str, float]] = field(default_factory=dict)
    """game_id -> injury features at each horizon (games after tracking began)."""
    sit_rates: SitRates | None = None
    """Sit rates per (horizon, status) learned from finished games."""

    def line_check(self) -> tuple[int, int]:
        """(checked, disagreeing): closing lines of 2+ points whose favourite differs
        from the moneyline favourite. A few are real (stale or asymmetric quotes);
        many mean corrupted lines - it is how two ESPN data bugs were caught."""
        checked = disagree = 0
        in_scope = {g.game_id for g in self.games}
        for game_id, line in self.closes.items():
            hs, hml, aml = line.home_spread, line.home_moneyline, line.away_moneyline
            if game_id not in in_scope or hs is None or abs(hs) < 2 or hml is None or aml is None:
                continue
            if hml == aml:
                continue
            checked += 1
            disagree += (hs < 0) != (hml < aml)
        return checked, disagree


def load_sport(session: Session, config: SportConfig) -> SportData:
    rows = session.scalars(
        select(Game).where(
            Game.sport == config.sport,
            Game.season.is_not(None),
            Game.status.in_([GameStatus.FINAL, GameStatus.SCHEDULED]),
            # Preseason and exhibition (All-Star) games are not competitive results.
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
        kind, _, book = line.provider.partition(":")
        if kind in by_game and is_market(book):  # rows stored before a book was excluded
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
    # A join, not IN (game ids): college basketball has ~70k games, past SQLite's
    # 32,766-parameter limit.
    in_scope = {g.game_id for g in games}
    for stat in session.scalars(
        select(PlayerGameStat)
        .join(Game, Game.id == PlayerGameStat.game_id)
        .where(Game.sport == config.sport)
    ):
        if stat.game_id not in in_scope:
            continue
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
    snapshots: dict[tuple[int, int], dict[str, tuple[float, float]]] = {}
    facts = [
        SeasonFact(f.season, f.team_id, f.name, f.value, f.known_at)
        for f in session.scalars(
            select(TeamSeasonFeature).where(TeamSeasonFeature.sport == config.sport)
        )
    ]
    data = SportData(
        config,
        games,
        closes,
        opens,
        rest_features(games, cap=config.rest_cap),
        availability(games, box, snapshots=snapshots),
        preseason_features(games, facts) if facts else {},
        {
            **(_efficiency(session, config, games) if config.team_epa else {}),
            **(_possession_efficiency(session, config, games) if config.team_boxes else {}),
        },
    )
    if config.injuries:
        timeline = InjuryTimeline.build(
            [
                Report(r.player_source_identifier, r.status, r.cleared, r.observed_at, r.team_id)
                for r in session.scalars(
                    select(InjuryReport).where(
                        InjuryReport.sport == config.sport,
                        InjuryReport.provider == "espn",
                        InjuryReport.player_source_identifier.is_not(None),
                    )
                )
                if r.player_source_identifier
            ]
        )
        played = {gid: {row.player_id for row in rows if row.played} for gid, rows in box.items()}
        features, rates = injury_features(games, snapshots, timeline, played)
        data = replace(data, injuries=features, sit_rates=rates)
    return data


def _efficiency(
    session: Session, config: SportConfig, games: list[EloGame]
) -> dict[int, dict[str, float]]:
    stats = [
        TeamGameEpa(s.game_id, s.team_id, s.plays, s.epa_total, s.dropbacks)
        for s in session.scalars(
            select(TeamGameStat)
            .join(Game, Game.id == TeamGameStat.game_id)
            .where(Game.sport == config.sport)
        )
    ]
    if not stats:
        return {}
    adjusted = compute_features(games, stats, [], {}, FeatureParams(opponent_adjust=True))
    raw = compute_features(games, stats, [], {}, FeatureParams())
    return {
        gid: {"epa_diff": f.epa_net_diff_pts, "epa_raw_diff": raw[gid].epa_net_diff_pts}
        for gid, f in adjusted.items()
    }


def _possession_efficiency(
    session: Session, config: SportConfig, games: list[EloGame]
) -> dict[int, dict[str, float]]:
    """Points per possession from team box totals (team_game_boxes), through the same
    walk-forward machinery as EPA: a game's possessions are the mean of both teams'
    estimates; ``eff_diff`` is opponent-adjusted, ``eff_raw_diff`` is not."""
    by_game: dict[int, list[TeamGameBox]] = defaultdict(list)
    for b in session.scalars(
        select(TeamGameBox)
        .join(Game, Game.id == TeamGameBox.game_id)
        .where(Game.sport == config.sport)
    ):
        by_game[b.game_id].append(b)
    stats = []
    for game_id, boxes in by_game.items():
        if len(boxes) != 2:
            continue
        possessions = sum(b.fga - b.oreb + b.tov + 0.475 * b.fta for b in boxes) / 2
        if possessions <= 0:
            continue
        for b in boxes:
            points = 2 * b.fgm + b.fg3m + b.ftm
            stats.append(TeamGameEpa(game_id, b.team_id, round(possessions), float(points), 0))
    if not stats:
        return {}
    adjusted = compute_features(games, stats, [], {}, FeatureParams(opponent_adjust=True))
    raw = compute_features(games, stats, [], {}, FeatureParams())
    return {
        gid: {"eff_diff": f.epa_net_diff_pts, "eff_raw_diff": raw[gid].epa_net_diff_pts}
        for gid, f in adjusted.items()
    }


def rest_features(
    games: Sequence[EloGame], *, cap: int
) -> dict[int, tuple[float, float, bool, bool]]:
    """Days since each team's previous game (calendar days, capped), and whether
    it played the day before. Uses only the schedule, which is known in advance."""
    last: dict[int, datetime] = {}
    out: dict[int, tuple[float, float, bool, bool]] = {}
    for g in sorted(games, key=lambda g: (g.commence_time, g.game_id)):
        rests = []
        for team in (g.home_id, g.away_id):
            prev = last.get(team)
            days = (g.commence_time.date() - prev.date()).days if prev else cap
            rests.append(float(min(max(days, 0), cap)))
            last[team] = g.commence_time
        out[g.game_id] = (rests[0], rests[1], rests[0] <= 1, rests[1] <= 1)
    return out


# --------------------------------------------------------------------------- margin models


LINEUP_FEATURES = frozenset({"missing_diff", "missing_prev_diff"})


def feature_values(p: EloPrediction, data: SportData) -> dict[str, float]:
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
        **{name: 0.0 for name in PRESEASON_FEATURES},
        **data.preseason.get(p.game_id, {}),
        "epa_diff": 0.0,
        "epa_raw_diff": 0.0,
        "eff_diff": 0.0,
        "eff_raw_diff": 0.0,
        **data.efficiency.get(p.game_id, {}),
        **{f"injury_missing_diff_{h}h": 0.0 for h in HORIZONS_HOURS},
        **data.injuries.get(p.game_id, {}),
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
    data: SportData,
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
class SportReport:
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
    name: str, model: FeatureMarginModel, anchored: MarketAnchoredModel, data: SportData
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


def sport_backtest(data: SportData, *, include_test: bool = False) -> SportReport:
    config = data.config
    splits = config.splits
    base = backtest(
        data.games,
        data.closes,
        splits=splits,
        include_test=include_test,
        home_field_grid=config.home_field_grid,
        k_grid=config.k_grid,
        regression_targets=config.regression_targets,
        home_field_per_point_grid=config.home_field_per_point_grid,
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
    for name, names in config.feature_sets.items():
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
                if name not in config.at_tip_only
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
    return SportReport(
        base,
        models,
        anchored,
        validate_features,
        len(opener_rows),
        openers,
        rmse,
        coverage,
    )
