"""Performance Lab: how a forward-tested model's results depend on how picky it
is, whether its probabilities are calibrated, and whether either drifts.

Built only on forward snapshots of finished games (services/forward_test), the
one data source no model was tuned on. For one model at one horizon:

- threshold lab: betting the model's side (the side it favors against the
  market) only when its probability of that side is at least 50%, 52%, ... 60%,
  at the best bettable price at the snapshot: record, ROI with its standard
  error, the break-even hit rate at the prices taken, and closing-line value;
- calibration: predicted against observed home-cover rates in probability
  bins, for the model and for the market;
- weekly trend: log loss against the market and closing-line value per week
  (Monday, Eastern), with running totals, to show drift.

Nothing here picks a threshold: a threshold chosen on these rows and then judged
on the same rows would be overfitting. Rows under ``MIN_SAMPLE`` are flagged.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from ttk import betting_math as bm
from ttk.domain import Sport
from ttk.services.forward_test import ScoredRow, scored_rows

EASTERN = ZoneInfo("America/New_York")
PROBABILITY_THRESHOLDS = (0.50, 0.52, 0.54, 0.56, 0.58, 0.60)
BIN_EDGES = (0.0, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 1.0)
MIN_SAMPLE = 30


@dataclass(frozen=True)
class ThresholdRow:
    min_probability: float
    bets: int
    wins: int
    losses: int
    pushes: int
    units: float
    roi: float | None
    roi_se: float | None
    hit_rate: float | None
    """Wins / (wins + losses)."""
    break_even: float | None
    """Mean break-even hit rate at the prices taken."""
    avg_clv: float | None
    clv_n: int
    enough: bool


@dataclass(frozen=True)
class CalibrationBin:
    low: float
    high: float
    n: int
    mean_predicted: float | None
    observed: float | None


@dataclass(frozen=True)
class WeekRow:
    week: date
    """Monday (Eastern) of the week of kickoff."""
    games: int
    decided: int
    model_log_loss: float | None
    market_log_loss: float | None
    diff: float | None
    cumulative_diff: float | None
    """Running mean of the paired difference through this week."""
    avg_clv: float | None
    clv_n: int
    cumulative_clv: float | None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _ll(p: float, y: int) -> float:
    p = min(max(p, 1e-9), 1 - 1e-9)
    return -math.log(p if y else 1 - p)


def threshold_lab(
    rows: list[ScoredRow], thresholds: tuple[float, ...] = PROBABILITY_THRESHOLDS
) -> list[ThresholdRow]:
    out = []
    for t in thresholds:
        picked = [r for r in rows if r.side_odds is not None and r.side_probability >= t]
        returns, break_evens, clvs = [], [], []
        wins = losses = pushes = 0
        for r in picked:
            assert r.side_odds is not None
            decimal = bm.american_to_decimal(r.side_odds)
            break_evens.append(1 / decimal)
            if r.price_clv is not None:
                clvs.append(r.price_clv)
            if r.won is None:
                pushes += 1
                returns.append(0.0)
            elif r.won:
                wins += 1
                returns.append(decimal - 1)
            else:
                losses += 1
                returns.append(-1.0)
        n = len(returns)
        roi = _mean(returns)
        se = None
        if roi is not None and n > 1:
            se = math.sqrt(sum((x - roi) ** 2 for x in returns) / (n - 1) / n)
        out.append(
            ThresholdRow(
                min_probability=t,
                bets=n,
                wins=wins,
                losses=losses,
                pushes=pushes,
                units=sum(returns),
                roi=roi,
                roi_se=se,
                hit_rate=wins / (wins + losses) if wins + losses else None,
                break_even=_mean(break_evens),
                avg_clv=_mean(clvs),
                clv_n=len(clvs),
                enough=n >= MIN_SAMPLE,
            )
        )
    return out


def calibration(rows: list[ScoredRow], *, market: bool = False) -> list[CalibrationBin]:
    """Predicted vs observed home cover in bins; pushes are left out, as the
    probabilities are P(home covers | no push)."""
    bins: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for r in rows:
        if r.cover_margin == 0:
            continue
        p = r.market_home_cover if market else r.model_home_cover
        i = min(bisect_right(BIN_EDGES, p) - 1, len(BIN_EDGES) - 2)
        bins[i].append((p, int(r.cover_margin > 0)))
    out = []
    for i in range(len(BIN_EDGES) - 1):
        items = bins.get(i, [])
        out.append(
            CalibrationBin(
                BIN_EDGES[i],
                BIN_EDGES[i + 1],
                len(items),
                _mean([p for p, _ in items]),
                _mean([float(y) for _, y in items]),
            )
        )
    return out


def weekly_trend(rows: list[ScoredRow]) -> list[WeekRow]:
    by_week: dict[date, list[ScoredRow]] = defaultdict(list)
    for r in rows:
        day = r.commence_time.astimezone(EASTERN).date()
        by_week[day - timedelta(days=day.weekday())].append(r)
    out = []
    all_diffs: list[float] = []
    all_clv: list[float] = []
    for week in sorted(by_week):
        items = by_week[week]
        model_ll, market_ll, diffs = [], [], []
        for r in items:
            if r.cover_margin == 0:
                continue
            y = int(r.cover_margin > 0)
            a, b = _ll(r.model_home_cover, y), _ll(r.market_home_cover, y)
            model_ll.append(a)
            market_ll.append(b)
            diffs.append(a - b)
        clvs = [r.price_clv for r in items if r.price_clv is not None]
        all_diffs += diffs
        all_clv += clvs
        out.append(
            WeekRow(
                week=week,
                games=len(items),
                decided=len(diffs),
                model_log_loss=_mean(model_ll),
                market_log_loss=_mean(market_ll),
                diff=_mean(diffs),
                cumulative_diff=_mean(all_diffs),
                avg_clv=_mean(clvs),
                clv_n=len(clvs),
                cumulative_clv=_mean(all_clv),
            )
        )
    return out


def lab_choices(session: Session, sport: Sport | None = None) -> list[dict[str, Any]]:
    """(model, horizon, finished snapshots), most data first."""
    counts: dict[tuple[str, int], int] = defaultdict(int)
    for r in scored_rows(session, sport):
        counts[(r.model, r.horizon_hours)] += 1
    return [
        {"model": m, "horizon_hours": h, "games": n}
        for (m, h), n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def performance_lab(session: Session, model: str, horizon_hours: int) -> dict[str, Any]:
    rows = [r for r in scored_rows(session, model=model) if r.horizon_hours == horizon_hours]
    return {
        "model": model,
        "horizon_hours": horizon_hours,
        "games": len(rows),
        "min_sample": MIN_SAMPLE,
        "thresholds": [asdict(t) for t in threshold_lab(rows)],
        "calibration": {
            "model": [asdict(b) for b in calibration(rows)],
            "market": [asdict(b) for b in calibration(rows, market=True)],
        },
        "weeks": [asdict(w) for w in weekly_trend(rows)],
        "note": "Forward-test records of a DEVELOPMENT model, never bets. Thresholds are "
        "shown, not chosen: picking the best one from these rows would overfit them. "
        f"Fewer than {MIN_SAMPLE} bets is flagged as too few to read.",
    }
