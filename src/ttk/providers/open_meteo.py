"""Open-Meteo hourly weather forecasts (https://open-meteo.com). Free for
non-commercial use, no key. ``/v1/forecast`` with ``hourly=`` variables, mph and
Fahrenheit units and ``timezone=UTC`` returns parallel lists under ``hourly``
(``time`` as ``YYYY-MM-DDTHH:MM``). Verified 2026-10-09.

Wind is ``wind_speed_10m``: the sustained wind 10 m above ground, the closest
match to the game-time wind nflverse records (research/nfl_weather).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ttk.providers.base import ProviderError
from ttk.providers.http_retry import get_with_retries

URL = "https://api.open-meteo.com/v1/forecast"
HOURLY = ("wind_speed_10m", "wind_gusts_10m", "temperature_2m", "precipitation")


@dataclass(frozen=True)
class HourlyForecast:
    valid_at: datetime
    wind_mph: float
    gust_mph: float | None
    temperature_f: float | None
    precipitation_mm: float | None


def kickoff_hour(at: datetime) -> datetime:
    """The forecast hour nearest ``at`` (UTC)."""
    at = at.astimezone(UTC)
    hour = at.replace(minute=0, second=0, microsecond=0)
    return hour + timedelta(hours=1) if at - hour >= timedelta(minutes=30) else hour


def parse_hour(payload: dict[str, Any], hour: datetime) -> HourlyForecast:
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    key = hour.strftime("%Y-%m-%dT%H:%M")
    if key not in times:
        raise ProviderError(f"open-meteo: no forecast for {key} UTC")
    i = times.index(key)

    def value(name: str) -> float | None:
        values = hourly.get(name) or []
        v = values[i] if i < len(values) else None
        return float(v) if v is not None else None

    wind = value("wind_speed_10m")
    if wind is None:
        raise ProviderError(f"open-meteo: no wind for {key} UTC")
    return HourlyForecast(
        hour, wind, value("wind_gusts_10m"), value("temperature_2m"), value("precipitation")
    )


class OpenMeteo:
    name = "open-meteo"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=30.0)

    def forecast(self, latitude: float, longitude: float, at: datetime) -> HourlyForecast:
        hour = kickoff_hour(at)
        stamp = hour.strftime("%Y-%m-%dT%H:%M")
        response = get_with_retries(
            self._client,
            URL,
            name="open-meteo",
            params={
                "latitude": str(latitude),
                "longitude": str(longitude),
                "hourly": ",".join(HOURLY),
                "wind_speed_unit": "mph",
                "temperature_unit": "fahrenheit",
                "timezone": "UTC",
                "start_hour": stamp,
                "end_hour": stamp,
            },
        )
        if response.status_code != 200:
            raise ProviderError(f"open-meteo: HTTP {response.status_code}")
        return parse_hour(response.json(), hour)
