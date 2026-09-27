"""ESPN "core" API historical odds: per-game lines from real sportsbooks, with
opening and closing prices where ESPN recorded them.

``GET sports.core.api.espn.com/v2/sports/{sport}/leagues/{league}/events/{id}/
competitions/{id}/odds`` -> ``items``, one per provider. Verified 2026-09-26 on
NBA games 2017-18 through 2025-26:

- Every sampled game had at least one sportsbook with spread, spread prices,
  moneylines and a total. ``homeTeamOdds``/``awayTeamOdds`` carry the prices;
  ``open``/``close`` sub-objects (pointSpread, spread, moneyLine; over/under,
  total at item level) appear from 2023-24 on.
- Not every provider is a market: projection and picks sites (accuscore,
  numberfire, teamrankings, betegy, fantasy911), in-game "Live Odds" feeds and
  the "Opening" pseudo-provider (college football 2015) are excluded.
- ``homeTeamOdds.<open|close>.pointSpread`` is the home team's bookmaker line
  ("+5.5"); without open/close, the item's ``spread`` is used, whose sign is
  verified against the moneyline favorite at import (see ``home_line_sign``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from ttk.domain import Sport
from ttk.providers.base import ProviderError
from ttk.providers.http_retry import get_with_retries

CORE_URL = (
    "https://sports.core.api.espn.com/v2/sports/{path}/events/{event}/competitions/{event}/odds"
)
CORE_PATHS = {
    Sport.NFL: "football/leagues/nfl",
    Sport.NBA: "basketball/leagues/nba",
    Sport.CFB: "football/leagues/college-football",
    Sport.NCAAB: "basketball/leagues/mens-college-basketball",
}
NOT_A_MARKET = (
    # projection / picks sites, not sportsbooks
    "accuscore",
    "numberfire",
    "teamrankings",
    "betegy",
    "fantasy911",
    # in-game odds; and ESPN's "Opening" pseudo-provider (an opener, never a close)
    "live odds",
    "opening",
)


def is_market(provider_name: str) -> bool:
    """False for projection sites, live feeds and the "Opening" pseudo-provider.
    Accepts a display name or a stored slug ("espn:betegy")."""
    name = provider_name.lower()
    return bool(name) and not any(k in name for k in NOT_A_MARKET)


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


def _valid(line: BookLine) -> bool:
    """False when a field holds a price instead of a line. Seen 2026-09-27 on
    retro-filled "ESPN BET" items (NBA 2022-23, CFB 2022-23): ``close.pointSpread``
    was the spread price (-110) and ``close.total`` the over price (-115), while
    ``current`` held the real line. No spread reaches 100 points, and a total can't
    be negative or equal its own price."""
    if line.home_spread is not None and abs(line.home_spread) >= 100:
        return False
    return line.total is None or (
        line.total > 0 and line.total not in (line.over_odds, line.under_odds)
    )


def _moment(item: Mapping[str, Any], name: str, moment: str, block: str) -> BookLine | None:
    home, away = item.get("homeTeamOdds") or {}, item.get("awayTeamOdds") or {}
    h, a = home.get(block) or {}, away.get(block) or {}
    if not h.get("pointSpread"):
        return None
    return BookLine(
        book=slug(name),
        moment=moment,
        home_spread=_american(h, "pointSpread"),
        home_spread_odds=_price(_american(h, "spread")),
        away_spread_odds=_price(_american(a, "spread")),
        total=_num(((item.get(block) or {}).get("total") or {}).get("american")),
        over_odds=_price(_american(item.get(block), "over")),
        under_odds=_price(_american(item.get(block), "under")),
        home_moneyline=_price(_american(h, "moneyLine")),
        away_moneyline=_price(_american(a, "moneyLine")),
    )


def parse_item(item: Mapping[str, Any], *, home_line_sign: float = 1.0) -> list[BookLine]:
    """``home_line_sign`` maps the item-level ``spread`` to the home line (used only
    when no open/close pointSpread exists).

    A malformed ``close`` falls back to ``current`` (a finished game's final line);
    a malformed ``open`` is dropped - there is nothing to recover it from. Invalid
    lines are never stored."""
    name = (item.get("provider") or {}).get("name", "")
    if not is_market(name):
        return []
    home = item.get("homeTeamOdds") or {}
    away = item.get("awayTeamOdds") or {}
    lines = []
    opener = _moment(item, name, "open", "open")
    if opener is not None and _valid(opener):
        lines.append(opener)
    close = _moment(item, name, "close", "close")
    if close is not None and not _valid(close):
        close = _moment(item, name, "close", "current")
    if close is not None and _valid(close):
        lines.append(close)
    if not any(line.moment == "close" for line in lines) and item.get("spread") is not None:
        spread = _num(item.get("spread"))
        fallback = BookLine(
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
        if _valid(fallback):
            lines.append(fallback)
    return lines


class EspnCoreOdds:
    name = "espn-core"

    def __init__(
        self, *, client: httpx.Client | None = None, retries: int = 3, backoff: float = 5.0
    ) -> None:
        self._client = client or httpx.Client(timeout=30.0)
        self._retries = retries
        self._backoff = backoff

    def fetch(self, sport: Sport, event_id: str) -> list[dict[str, Any]]:
        response = get_with_retries(
            self._client,
            CORE_URL.format(path=CORE_PATHS[sport], event=event_id),
            name=self.name,
            retries=self._retries,
            backoff=self._backoff,
        )
        if response.status_code == 404:
            return []
        if response.status_code != 200:
            raise ProviderError(f"espn-core: HTTP {response.status_code} for {event_id}")
        items: list[dict[str, Any]] = response.json().get("items", [])
        return items
