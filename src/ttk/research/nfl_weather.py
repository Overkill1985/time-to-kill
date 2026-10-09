"""NFL totals and wind: does the market's total under-react to wind?

Measured 2026-10-09 (docs/MODELS.md, "Rest, travel and weather"): in outdoor
games the final total falls short of the line by about 0.11 points per mph of
the wind **recorded at the game** (nflverse ``games.csv`` ``wind``), fitted on
2002-2017 and holding on 2018-2021. At bet time only a forecast exists, so the
model is forward-tested on Open-Meteo's forecast wind taken at each horizon.

Model: predicted total = market line + ``wind_coef`` x (wind - ``wind_ref``),
outdoor stadiums only (nflverse ``roof == "outdoors"``; domes, retractable roofs
and games nflverse has not classified get no prediction). ``wind_ref`` is the
mean recorded wind of the outdoor training games, so the adjustment averages
zero there: the model leans under in wind and over in calm, never toward one
side in general. P(over) is a normal around the prediction (sigma from the
training residuals), with continuity at whole-number lines.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from ttk.research.moneyline import no_vig
from ttk.research.totals import Calibration, p_over, push_probability

ARTIFACT_VERSION = 1
TRAIN = (2002, 2017)
VALIDATE = (2018, 2021)
OUTDOORS = "outdoors"


@dataclass(frozen=True)
class Stadium:
    name: str
    wikipedia: str
    """The English Wikipedia page the coordinates were read from (2026-10-09)."""
    latitude: float
    longitude: float


# Outdoor stadiums (nflverse roof "outdoors") hosting 2026 games, by nflverse
# stadium id. Coordinates: each page's primary coordinates on English Wikipedia
# (API prop=coordinates), read 2026-10-09. A stadium missing here gets no
# forecast, so no prediction.
STADIUMS: dict[str, Stadium] = {
    "BAL00": Stadium("M&T Bank Stadium", "M&T Bank Stadium", 39.2781, -76.6228),
    "BOS00": Stadium("Gillette Stadium", "Gillette Stadium", 42.0910, -71.2640),
    "BUF00": Stadium("Highmark Stadium", "Highmark Stadium", 42.7731, -78.7922),
    "CAR00": Stadium("Bank of America Stadium", "Bank of America Stadium", 35.2258, -80.8528),
    "CHI98": Stadium("Soldier Field", "Soldier Field", 41.8623, -87.6167),
    "CIN00": Stadium("Paycor Stadium", "Paycor Stadium", 39.0950, -84.5160),
    "CLE00": Stadium("Huntington Bank Field", "Huntington Bank Field", 41.5061, -81.6994),
    "DEN00": Stadium("Empower Field at Mile High", "Empower Field at Mile High", 39.7439, -105.02),
    "GNB00": Stadium("Lambeau Field", "Lambeau Field", 44.5014, -88.0622),
    "JAX00": Stadium("EverBank Stadium", "EverBank Stadium", 30.3239, -81.6375),
    "KAN00": Stadium("GEHA Field at Arrowhead Stadium", "Arrowhead Stadium", 39.0489, -94.4839),
    "MIA00": Stadium("Hard Rock Stadium", "Hard Rock Stadium", 25.9581, -80.2389),
    "NAS00": Stadium("Nissan Stadium", "Nissan Stadium (Nashville)", 36.1664, -86.7714),
    "NYC01": Stadium("MetLife Stadium", "MetLife Stadium", 40.8136, -74.0744),
    "PHI00": Stadium("Lincoln Financial Field", "Lincoln Financial Field", 39.9008, -75.1675),
    "PIT00": Stadium("Acrisure Stadium", "Acrisure Stadium", 40.4467, -80.0158),
    "SEA00": Stadium("Lumen Field", "Lumen Field", 47.5952, -122.3316),
    "SFO01": Stadium("Levi's Stadium", "Levi's Stadium", 37.4030, -121.9700),
    "TAM00": Stadium("Raymond James Stadium", "Raymond James Stadium", 27.9758, -82.5033),
    "WAS00": Stadium("Northwest Stadium", "Northwest Stadium", 38.9078, -76.8644),
    "LON00": Stadium("Wembley Stadium", "Wembley Stadium", 51.5556, -0.2794),
    "LON02": Stadium("Tottenham Hotspur Stadium", "Tottenham Hotspur Stadium", 51.6044, -0.0664),
    "MEX00": Stadium("Estadio Banorte", "Estadio Azteca", 19.3030, -99.1505),
}


@dataclass(frozen=True)
class Venue:
    stadium_id: str
    stadium: str
    roof: str


def venues(rows: Iterable[Mapping[str, str]]) -> dict[str, Venue]:
    """nflverse game id -> where it is played and the roof nflverse lists."""
    return {
        r["game_id"]: Venue(r.get("stadium_id") or "", r.get("stadium") or "", r.get("roof") or "")
        for r in rows
        if r.get("game_id")
    }


_BY_NAME = {s.name: key for key, s in STADIUMS.items()}


def outdoor_stadium(venue: Venue | None) -> tuple[str, Stadium] | None:
    """(stadium id, stadium) to forecast for, or None when the game gets no
    prediction. Matched by the stadium's **name**: nflverse has filed a London
    game under the home team's stadium id with the London stadium's name
    (2026_05_PHI_JAX: JAX00, "Tottenham Hotspur Stadium")."""
    if venue is None or venue.roof != OUTDOORS or venue.stadium not in _BY_NAME:
        return None
    key = _BY_NAME[venue.stadium]
    return key, STADIUMS[key]


@dataclass(frozen=True)
class WindRow:
    season: int
    line: float
    total: float
    wind: float
    over_odds: float | None
    under_odds: float | None


def _num(value: str | None) -> float | None:
    if value in (None, "", "NA"):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def wind_rows(rows: Iterable[Mapping[str, str]]) -> list[WindRow]:
    """Finished outdoor regular-season games with a total line and recorded wind."""
    out = []
    for r in rows:
        if r.get("game_type") != "REG" or r.get("roof") != OUTDOORS:
            continue
        line, total, wind = _num(r.get("total_line")), _num(r.get("total")), _num(r.get("wind"))
        if line is None or total is None or wind is None:
            continue
        out.append(
            WindRow(
                int(r["season"]),
                line,
                total,
                wind,
                _num(r.get("over_odds")),
                _num(r.get("under_odds")),
            )
        )
    return out


@dataclass(frozen=True)
class WindModel:
    wind_coef: float
    wind_ref: float
    sigma: float

    def adjustment(self, wind: float) -> float:
        return self.wind_coef * (wind - self.wind_ref)

    def _cal(self, wind: float) -> Calibration:
        # Calibration.points(line) = line + adjustment
        return Calibration(self.adjustment(wind), 1.0, self.sigma)

    def p_over(self, line: float, wind: float) -> float:
        return p_over(self._cal(wind), line, line)

    def push(self, line: float, wind: float) -> float:
        return push_probability(self._cal(wind), line, line)


def fit(rows: Sequence[WindRow]) -> WindModel:
    wind = np.array([r.wind for r in rows])
    miss = np.array([r.total - r.line for r in rows])
    coef, _ = np.polyfit(wind, miss, 1)
    ref = float(wind.mean())
    resid = miss - coef * (wind - ref)
    return WindModel(float(coef), ref, float(resid.std(ddof=2)))


@dataclass(frozen=True)
class WindValidation:
    games: int
    rmse_line: float
    rmse_model: float
    rmse_z: float
    """Paired z of the squared-error improvement (positive = the model is better)."""
    priced: int
    """Games with real over/under prices and no push (the log-loss sample)."""
    log_loss_market: float | None
    log_loss_model: float | None
    log_loss_z: float | None
    """Paired z of model minus market log loss (negative = the model is better)."""


def _paired_z(d: np.ndarray) -> float:
    sd = float(d.std(ddof=1))
    return float(d.mean() / (sd / math.sqrt(len(d)))) if sd > 0 else 0.0


def validate(model: WindModel, rows: Sequence[WindRow]) -> WindValidation:
    miss = np.array([r.total - r.line for r in rows])
    adj = np.array([model.adjustment(r.wind) for r in rows])
    line_sq, model_sq = miss**2, (miss - adj) ** 2
    market_ll, model_ll = [], []
    for r in rows:
        market = no_vig(r.over_odds, r.under_odds)
        if market is None or r.total == r.line:
            continue
        over = r.total > r.line
        p = model.p_over(r.line, r.wind)
        market_ll.append(-math.log(market if over else 1 - market))
        model_ll.append(-math.log(p if over else 1 - p))
    d = np.array(model_ll) - np.array(market_ll)
    return WindValidation(
        len(rows),
        float(math.sqrt(line_sq.mean())),
        float(math.sqrt(model_sq.mean())),
        _paired_z(line_sq - model_sq),
        len(d),
        float(np.mean(market_ll)) if market_ll else None,
        float(np.mean(model_ll)) if model_ll else None,
        _paired_z(d) if len(d) > 1 else None,
    )


@dataclass(frozen=True)
class WindBacktest:
    model: WindModel
    train_games: int
    validation: WindValidation


def backtest(rows: Iterable[Mapping[str, str]]) -> WindBacktest:
    all_rows = wind_rows(rows)
    train = [r for r in all_rows if TRAIN[0] <= r.season <= TRAIN[1]]
    valid = [r for r in all_rows if VALIDATE[0] <= r.season <= VALIDATE[1]]
    model = fit(train)
    return WindBacktest(model, len(train), validate(model, valid))


def freeze(result: WindBacktest) -> dict[str, Any]:
    return {
        "artifact_version": ARTIFACT_VERSION,
        "market": "TOTAL",
        "sport": "NFL",
        "kind": "nfl_wind",
        "splits": {"train": list(TRAIN), "validate": list(VALIDATE)},
        "roof": OUTDOORS,
        "model": asdict(result.model),
        "train_games": result.train_games,
        "validation": asdict(result.validation),
        "wind_source_train": "nflverse games.csv wind (recorded at the game)",
        "wind_source_live": "Open-Meteo wind_speed_10m at the kickoff hour, per horizon",
    }


def thaw(artifact: Mapping[str, Any]) -> WindModel:
    if artifact.get("artifact_version") != ARTIFACT_VERSION or artifact.get("kind") != "nfl_wind":
        raise ValueError("not an NFL wind artifact of a known version")
    return WindModel(**artifact["model"])
