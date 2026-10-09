"""Forecast weather for NFL games under forward test (research/nfl_weather).

A forecast is taken once per (game, horizon), when the forward runner first
snapshots that game at that horizon, and stored append-only in
``weather_forecasts`` with the time it was taken. Only outdoor stadiums with
known coordinates are forecast; where a game is played and its roof come from
nflverse ``games.csv`` (re-read at most every ``VENUE_REFRESH``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ttk.db.models import Game, GameSourceId, WeatherForecast, utcnow
from ttk.domain import Sport
from ttk.providers.base import ProviderError
from ttk.providers.open_meteo import HourlyForecast
from ttk.research.nfl_weather import Venue, outdoor_stadium, venues

VENUE_REFRESH = timedelta(hours=6)


class Forecaster(Protocol):
    name: str

    def forecast(self, latitude: float, longitude: float, at: datetime) -> HourlyForecast: ...


@dataclass(frozen=True)
class Forecast:
    """A stored forecast's values (kept apart from the ORM row, which expires on commit)."""

    stadium_id: str
    fetched_at: datetime
    wind_mph: float
    gust_mph: float | None
    temperature_f: float | None
    precipitation_mm: float | None


def _values(row: WeatherForecast) -> Forecast:
    return Forecast(
        row.stadium_id,
        row.fetched_at,
        row.wind_mph,
        row.gust_mph,
        row.temperature_f,
        row.precipitation_mm,
    )


class WeatherState:
    """Bound to the snapshot pass's session by ``refresh``; ``forecast`` returns
    the stored forecast for (game, horizon), taking it first if there is none."""

    def __init__(
        self, load_rows: Callable[[], list[dict[str, str]]], forecaster: Forecaster
    ) -> None:
        self._load_rows = load_rows
        self._forecaster = forecaster
        self._venues: Mapping[str, Venue] = {}
        self._venues_at: datetime | None = None
        self._session: Session | None = None
        self._stored: dict[tuple[int, int], Forecast] = {}
        self.errors: list[str] = []

    def refresh(self, session: Session) -> None:
        self._session = session
        now = utcnow()
        if self._venues_at is None or now - self._venues_at > VENUE_REFRESH:
            try:
                self._venues = venues(self._load_rows())
                self._venues_at = now
            except ProviderError as exc:
                # Keep the last venues; with none, no game is forecast this pass.
                self.errors.append(str(exc))
        self._stored = {
            (row.game_id, row.horizon_hours): _values(row)
            for row in session.scalars(select(WeatherForecast))
        }

    def forecast(self, game_id: int, horizon: int, at: datetime) -> Forecast | None:
        if (game_id, horizon) in self._stored:
            return self._stored[(game_id, horizon)]
        session = self._session
        if session is None:
            return None
        game = session.get_one(Game, game_id)
        if game.sport != Sport.NFL:
            return None
        nflverse_id = session.scalar(
            select(GameSourceId.source_identifier).where(
                GameSourceId.game_id == game_id, GameSourceId.provider == "nflverse"
            )
        )
        found = outdoor_stadium(self._venues.get(nflverse_id or ""))
        if found is None:
            return None
        stadium_id, stadium = found
        try:
            f = self._forecaster.forecast(stadium.latitude, stadium.longitude, game.commence_time)
        except ProviderError as exc:
            self.errors.append(str(exc))
            return None
        row = WeatherForecast(
            game_id=game_id,
            horizon_hours=horizon,
            provider=self._forecaster.name,
            stadium_id=stadium_id,
            latitude=stadium.latitude,
            longitude=stadium.longitude,
            valid_at=f.valid_at,
            fetched_at=at,
            wind_mph=f.wind_mph,
            gust_mph=f.gust_mph,
            temperature_f=f.temperature_f,
            precipitation_mm=f.precipitation_mm,
        )
        session.add(row)
        session.flush()
        self._stored[(game_id, horizon)] = values = _values(row)
        return values
