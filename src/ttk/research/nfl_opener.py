"""NFL early lines: does the model's disagreement with the opener predict where
the line closes? (2023-2024, ESPN's per-book opening and closing lines.)

Betting before the market moves is where an edge usually lives (docs/MODELS.md,
"What could actually beat the market"). For every game with an ESPN opener and
close, the model's expected home margin (fitted on TRAIN, walk-forward state) is
compared with the opener's; a bet on the side it prefers, taken at the opener,
gains ``open - close`` points on the home side (the opposite for away).

Starting quarterbacks are known at kickoff, not when the opener is posted, so
the opener-time model drops the QB-change term; the version with it is reported
only as a reference.

These seasons were scored once for the frozen NFL model (2026-10-05). This asks
a different question (line movement, not results) and selects no model.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from ttk.db.models import Game, ReportedLine
from ttk.domain import Sport
from ttk.models.elo import EloPrediction
from ttk.providers.espn_odds import is_market
from ttk.research.espn_models import _representative
from ttk.research.nfl_elo import FittedModels, backtest, load_feature_inputs, load_nfl_games

SEASONS = (2023, 2024)
EDGES = (0.0, 1.0, 2.0, 3.0)


@dataclass(frozen=True)
class OpenerGame:
    game_id: int
    season: int
    open_line: float
    """Home spread, bookmaker style (-3.5 = home favored by 3.5)."""
    close_line: float
    margins: dict[str, float]
    """Model -> expected home margin."""
    result: float | None


@dataclass(frozen=True)
class EdgeRow:
    min_disagreement: float
    bets: int
    mean_points: float | None
    se_points: float | None
    moved_toward: int
    moved_away: int
    unchanged: int


@dataclass(frozen=True)
class ModelMoves:
    model: str
    games: int
    slope: float
    """Line movement toward home (open - close) per point of disagreement."""
    slope_se: float
    rows: list[EdgeRow]


def _openers_and_closes(
    session: Session, first: int, last: int
) -> dict[int, tuple[ReportedLine, ReportedLine]]:
    by: dict[str, dict[int, list[ReportedLine]]] = {
        "espn": defaultdict(list),
        "espn-open": defaultdict(list),
    }
    rows = session.scalars(
        select(ReportedLine)
        .join(Game, Game.id == ReportedLine.game_id)
        .where(
            Game.sport == Sport.NFL,
            Game.season.between(first, last),
            ReportedLine.provider.like("espn%"),
        )
    )
    for r in rows:
        kind, _, book = r.provider.partition(":")
        if kind in by and is_market(book):
            by[kind][r.game_id].append(r)
    out = {}
    for gid, opens in by["espn-open"].items():
        o, c = _representative(opens), _representative(by["espn"].get(gid, []))
        if o is not None and c is not None and o.home_spread is not None:
            out[gid] = (o, c)
    return out


def opener_games(
    session: Session, seasons: tuple[int, int] = SEASONS
) -> tuple[list[OpenerGame], FittedModels]:
    games, lines = load_nfl_games(session)
    inputs = load_feature_inputs(session)
    report = backtest(games, lines, inputs=inputs)  # fitted on TRAIN; nothing refitted
    models = report.models
    predictions = {p.game_id: p for p in report.tuned.run(games)}
    by_id = {g.game_id: g for g in games}

    def elo(p: EloPrediction) -> float | None:
        return models.normal.expected_margin(p.elo_diff)

    def features_no_qb(p: EloPrediction) -> float | None:
        f = models.game_features.get(p.game_id)
        if models.features is None or f is None:
            return None
        return models.features.expected_margin(p, replace(f, qb_change_diff_pts=0.0))

    margin_fns: dict[str, Callable[[EloPrediction], float | None]] = {
        "elo": elo,
        "epa (no QB change)": features_no_qb,
        "epa + QB change (kickoff starters; reference only)": models.feature_margin,
    }
    out = []
    for gid, (o, c) in _openers_and_closes(session, *seasons).items():
        p = predictions.get(gid)
        if p is None or o.home_spread is None or c.home_spread is None:
            continue
        margins = {name: m for name, fn in margin_fns.items() if (m := fn(p)) is not None}
        g = by_id[gid]
        result = (
            float(g.home_score - g.away_score)
            if g.home_score is not None and g.away_score is not None
            else None
        )
        out.append(OpenerGame(gid, p.season, o.home_spread, c.home_spread, margins, result))
    return out, models


def _se(xs: Sequence[float]) -> float | None:
    return statistics.stdev(xs) / math.sqrt(len(xs)) if len(xs) > 1 else None


def model_moves(model: str, games: Sequence[OpenerGame]) -> ModelMoves:
    rows = [g for g in games if model in g.margins]
    # Disagreement: the model's expected home margin minus the opener's (-open_line).
    d = [g.margins[model] + g.open_line for g in rows]
    move = [g.open_line - g.close_line for g in rows]  # + = the line moved toward home
    n = len(rows)
    mean_d, mean_m = sum(d) / n, sum(move) / n
    sxx = sum((x - mean_d) ** 2 for x in d)
    slope = sum((x - mean_d) * (y - mean_m) for x, y in zip(d, move, strict=True)) / sxx
    resid = [y - mean_m - slope * (x - mean_d) for x, y in zip(d, move, strict=True)]
    se = math.sqrt(sum(e * e for e in resid) / (n - 2) / sxx)
    edge_rows = []
    for k in EDGES:
        gains = [
            (1.0 if x > 0 else -1.0) * y
            for x, y in zip(d, move, strict=True)
            if abs(x) >= k and x != 0
        ]
        edge_rows.append(
            EdgeRow(
                k,
                len(gains),
                statistics.fmean(gains) if gains else None,
                _se(gains),
                sum(1 for x in gains if x > 0),
                sum(1 for x in gains if x < 0),
                sum(1 for x in gains if x == 0),
            )
        )
    return ModelMoves(model, n, slope, se, edge_rows)


def opener_report(session: Session) -> list[ModelMoves]:
    games, _ = opener_games(session)
    names = sorted({m for g in games for m in g.margins}, key=lambda m: (len(m), m))
    return [model_moves(name, games) for name in names]
