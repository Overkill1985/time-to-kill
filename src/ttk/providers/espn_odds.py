"""ESPN "core" API historical odds: per-game lines from real sportsbooks, with
opening and closing prices where ESPN recorded them.

``GET sports.core.api.espn.com/v2/sports/{sport}/leagues/{league}/events/{id}/
competitions/{id}/odds`` -> ``items``, one per provider. Verified 2026-09-26 on
NBA games 2017-18 through 2025-26:

- Every sampled game had at least one sportsbook with spread, spread prices,
  moneylines and a total. ``homeTeamOdds``/``awayTeamOdds`` carry the prices;
  ``open``/``close`` sub-objects (pointSpread, spread, moneyLine; over/under,
  total at item level) appear from 2023-24 on.
- Not every provider is a market: projection sites (accuscore, numberfire,
  teamrankings) and in-game "Live Odds" feeds are excluded.
- ``homeTeamOdds.<open|close>.pointSpread`` is the home team's bookmaker line
  ("+5.5"); without open/close, the item's ``spread`` is used, whose sign is
  verified against the moneyline favorite at import (see ``home_line_sign``).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from ttk.domain import Sport
from ttk.providers.base import ProviderError

CORE_URL = (
    "https://sports.core.api.espn.com/v2/sports/{path}/events/{event}/competitions/{event}/odds"
)
CORE_PATHS = {
    Sport.NFL: "football/leagues/nfl",
    Sport.NBA: "basketball/leagues/nba",
    Sport.CFB: "football/leagues/college-football",
    Sport.NCAAB: "basketball/leagues/mens-college-basketball",
}
NOT_A_MARKET = ("accuscore", "numberfire", "teamrankings", "live odds")


@dataclass(frozen=True)
class BookLine:
    """One sportsbook's line for a game at one moment (open or close)."""

    book: str
    moment: str
    """'open' or 'close' ('close' = ESPN's final/current value for older seasons)."""
    home_spread: float | None
    home_spread_odds: float | None
    away_spread_odds: float | None
    total: float | None
    over_odds: float | None
    under_odds: float | None
    home_moneyline: float | None
    away_moneyline: float | None


def _num(value: Any) -> float | None:
    if value in (None, "", "OFF", "EVEN"):
        return 100.0 if value == "EVEN" else None
    try:
        return float(str(value).replace("+", ""))
    except ValueError:
        return None


def _price(value: Any) -> float | None:
    """An American price, or None. ESPN sends 0 where a book offered no price."""
    number = _num(value)
    return None if number is None or -100 < number < 100 else number


def _american(node: Mapping[str, Any] | None, key: str) -> float | None:
    part = (node or {}).get(key) or {}
    return _num(part.get("american") if isinstance(part, Mapping) else None)


def slug(name: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")[:30]


def parse_item(item: Mapping[str, Any], *, home_line_sign: float = 1.0) -> list[BookLine]:
    """``home_line_sign`` maps the item-level ``spread`` to the home line (used only
    when no open/close pointSpread exists)."""
    name = (item.get("provider") or {}).get("name", "")
    if not name or any(k in name.lower() for k in NOT_A_MARKET):
        return []
    home = item.get("homeTeamOdds") or {}
    away = item.get("awayTeamOdds") or {}
    lines = []
    for moment in ("open", "close"):
        h, a = home.get(moment) or {}, away.get(moment) or {}
        if not h.get("pointSpread"):
            continue
        lines.append(
            BookLine(
                book=slug(name),
                moment=moment,
                home_spread=_american(h, "pointSpread"),
                home_spread_odds=_price(_american(h, "spread")),
                away_spread_odds=_price(_american(a, "spread")),
                total=_num(((item.get(moment) or {}).get("total") or {}).get("american")),
                over_odds=_price(_american(item.get(moment), "over")),
                under_odds=_price(_american(item.get(moment), "under")),
                home_moneyline=_price(_american(h, "moneyLine")),
                away_moneyline=_price(_american(a, "moneyLine")),
            )
        )
    if not any(line.moment == "close" for line in lines) and item.get("spread") is not None:
        spread = _num(item.get("spread"))
        lines.append(
            BookLine(
                book=slug(name),
                moment="close",
                home_spread=None if spread is None else home_line_sign * spread,
                home_spread_odds=_price(home.get("spreadOdds")),
                away_spread_odds=_price(away.get("spreadOdds")),
                total=_num(item.get("overUnder")),
                over_odds=_price(item.get("overOdds")),
                under_odds=_price(item.get("underOdds")),
                home_moneyline=_price(home.get("moneyLine")),
                away_moneyline=_price(away.get("moneyLine")),
            )
        )
    return lines


class EspnCoreOdds:
    name = "espn-core"

    def __init__(
        self, *, client: httpx.Client | None = None, retries: int = 3, backoff: float = 5.0
    ) -> None:
        self._client = client or httpx.Client(timeout=30.0)
        self._retries = retries
        self._backoff = backoff

    def _get(self, url: str) -> httpx.Response:
        """GET with retries on transport errors and 5xx (ESPN returns occasional 503s)."""
        for attempt in range(self._retries + 1):
            try:
                response = self._client.get(url)
            except httpx.HTTPError as exc:
                if attempt == self._retries:
                    raise ProviderError(
                        f"espn-core: request failed ({type(exc).__name__})"
                    ) from None
            else:
                if response.status_code < 500 or attempt == self._retries:
                    return response
            time.sleep(self._backoff * (attempt + 1))
        raise AssertionError("unreachable")

    def fetch(self, sport: Sport, event_id: str) -> list[dict[str, Any]]:
        response = self._get(CORE_URL.format(path=CORE_PATHS[sport], event=event_id))
        if response.status_code == 404:
            return []
        if response.status_code != 200:
            raise ProviderError(f"espn-core: HTTP {response.status_code} for {event_id}")
        items: list[dict[str, Any]] = response.json().get("items", [])
        return items
