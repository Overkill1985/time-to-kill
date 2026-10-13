"""College football totals and wind, measured as for the NFL (research/nfl_weather).

Wind is Open-Meteo's archive (ERA5) value at the kickoff hour (``game_weather``,
services/weather_history), not a stadium reading. The line is the representative
ESPN closing total the college models are judged against (research/espn_models).
Fitted on the CFB TRAIN seasons, checked on VALIDATE; outdoor games only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ttk.db.models import GameWeather
from ttk.research.cfb_model import CFB
from ttk.research.espn_models import load_sport
from ttk.research.nfl_weather import WindModel, WindRow, WindValidation, fit, validate
from ttk.services.weather_history import PROVIDER

THRESHOLDS = (10, 15, 20)


@dataclass(frozen=True)
class UnderRecord:
    threshold: int
    wins: int
    losses: int
    pushes: int
    roi: float | None


def under_record(rows: Sequence[WindRow], threshold: int) -> UnderRecord:
    """Betting the under at the reported price in games with wind >= threshold."""
    w = lo = pu = 0
    units = 0.0
    for r in rows:
        if r.wind < threshold or r.under_odds is None:
            continue
        if r.total < r.line:
            w += 1
            units += r.under_odds / 100 if r.under_odds > 0 else 100 / -r.under_odds
        elif r.total > r.line:
            lo += 1
            units -= 1
        else:
            pu += 1
    n = w + lo + pu
    return UnderRecord(threshold, w, lo, pu, units / n if n else None)


@dataclass(frozen=True)
class BlockReport:
    seasons: tuple[int, int]
    games: int
    mean_wind: float
    slope: float
    """The block's own least-squares slope, points of total miss per mph."""
    slope_se: float
    unders: list[UnderRecord]


@dataclass(frozen=True)
class CfbWindReport:
    indoor: int
    train: BlockReport
    validate: BlockReport
    model: WindModel
    """Fitted on TRAIN."""
    validation: WindValidation


def cfb_wind_rows(session: Session) -> tuple[list[WindRow], int]:
    """Outdoor games with archive wind and a closing total; and the indoor count."""
    data = load_sport(session, CFB)
    games = {g.game_id: g for g in data.games}
    rows, indoor = [], 0
    for w in session.scalars(select(GameWeather).where(GameWeather.provider == PROVIDER)):
        close, g = data.closes.get(w.game_id), games.get(w.game_id)
        if close is None or g is None or close.total is None or g.home_score is None:
            continue
        if w.indoor or w.wind_mph is None:
            indoor += 1
            continue
        assert g.away_score is not None
        rows.append(
            WindRow(
                g.season,
                close.total,
                float(g.home_score + g.away_score),
                w.wind_mph,
                close.over_odds,
                close.under_odds,
            )
        )
    return rows, indoor


def _block(rows: Sequence[WindRow], seasons: tuple[int, int]) -> BlockReport:
    m = fit(rows)
    n = len(rows)
    mean = sum(r.wind for r in rows) / n
    sxx = sum((r.wind - mean) ** 2 for r in rows)
    resid = [r.total - r.line - m.adjustment(r.wind) for r in rows]
    centered = sum(resid) / n
    se = (sum((e - centered) ** 2 for e in resid) / (n - 2) / sxx) ** 0.5
    return BlockReport(
        seasons, n, mean, m.wind_coef, se, [under_record(rows, t) for t in THRESHOLDS]
    )


def cfb_wind_report(session: Session) -> CfbWindReport:
    rows, indoor = cfb_wind_rows(session)
    splits = CFB.splits
    train = [r for r in rows if splits.train[0] <= r.season <= splits.train[1]]
    valid = [r for r in rows if splits.validate[0] <= r.season <= splits.validate[1]]
    model = fit(train)
    return CfbWindReport(
        indoor,
        _block(train, splits.train),
        _block(valid, splits.validate),
        model,
        validate(model, valid),
    )
