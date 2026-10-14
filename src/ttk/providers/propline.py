"""PropLine REST adapter (https://prop-line.com/docs): bulk game odds, and
per-event player props (``/events/{id}/markets`` and ``/events/{id}/odds``, 1
request each).

- Base ``https://api.prop-line.com``; ``GET /v1/sports/{sport_key}/odds?markets=...``.
- The key is sent in the ``X-API-Key`` header (never the URL, so it cannot leak
  into logs or tracebacks via a request URL).
- One request returns every book for a sport. Free tier: 1,000 requests/day,
  burst 10, 5/s sustained; ``X-Daily-Remaining`` reports the daily balance;
  429/503 carry ``Retry-After``.
- The payload is Odds-API-shaped with PropLine extras (suspended markets, team
  totals, DFS multipliers, ESPN ids); ``odds_api_format`` handles all of them.
  Verified against a real payload captured 2026-09-26 (tests/fixtures).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from ttk.domain import Sport
from ttk.providers.base import OddsFetch, ProviderError
from ttk.providers.odds_api_format import normalize_events

BASE_URL = "https://api.prop-line.com"

SPORT_KEYS = {
    Sport.NFL: "football_nfl",
    Sport.NBA: "basketball_nba",
    Sport.CFB: "football_ncaaf",
    Sport.NCAAB: "basketball_ncaab",
}


class RateLimited(ProviderError):
    def __init__(self, message: str, retry_after: float | None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PropLineProvider:
    name = "propline"

    def __init__(
        self,
        api_key: str,
        *,
        markets: str = "h2h,spreads,totals",
        client: httpx.Client | None = None,
        retry_wait: float = 5.0,
    ) -> None:
        if not api_key:
            raise ValueError("PropLine needs an API key (TTK_PROPLINE_API_KEY)")
        self._markets = markets
        # 60 s: a Saturday college football poll (~85,000 quotes) timed out at 30 s
        # about once a day (2026-10-07 to 10-14).
        self._client = client or httpx.Client(base_url=BASE_URL, timeout=60.0)
        self._retry_wait = retry_wait
        self._headers = {"X-API-Key": api_key}
        self.daily_remaining: int | None = None

    def _get(self, path: str, params: dict[str, str] | None, what: str) -> Any:
        """One request (retried once on a timeout, a dropped connection or a 5xx):
        records the daily balance, raises on rate limits and errors."""
        for attempt in (1, 2):
            try:
                response = self._client.get(path, params=params, headers=self._headers)
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise ProviderError(
                        f"{self.name}: request failed ({type(exc).__name__})"
                    ) from None
            else:
                if response.status_code not in (500, 502, 504) or attempt == 2:
                    break
            time.sleep(self._retry_wait)
        remaining = response.headers.get("X-Daily-Remaining")
        if remaining is not None and remaining.isdigit():
            self.daily_remaining = int(remaining)
        if response.status_code in (429, 503):
            retry = response.headers.get("Retry-After")
            raise RateLimited(
                f"{self.name}: HTTP {response.status_code} (retry after {retry or '?'} s)",
                float(retry) if retry and retry.replace(".", "", 1).isdigit() else None,
            )
        if response.status_code != 200:
            raise ProviderError(f"{self.name}: HTTP {response.status_code} for {what}")
        return response.json()

    def fetch_odds(self, sport: Sport) -> OddsFetch:
        payload: list[dict[str, Any]] = self._get(
            f"/v1/sports/{SPORT_KEYS[sport]}/odds", {"markets": self._markets}, str(sport)
        )
        return normalize_events(payload, provider=self.name, sport=sport)

    def event_markets(self, sport: Sport, event_id: str) -> list[str]:
        """Market keys listed for one event (1 request)."""
        payload = self._get(
            f"/v1/sports/{SPORT_KEYS[sport]}/events/{event_id}/markets", None, f"event {event_id}"
        )
        items = payload.get("markets", payload) if isinstance(payload, dict) else payload
        return sorted({m["key"] if isinstance(m, dict) else str(m) for m in items})

    def event_props(self, sport: Sport, event_id: str, markets: list[str]) -> list[PropQuote]:
        """Every book's quotes for these markets on one event (1 request)."""
        payload = self._get(
            f"/v1/sports/{SPORT_KEYS[sport]}/events/{event_id}/odds",
            {"markets": ",".join(markets)},
            f"event {event_id}",
        )
        return parse_props(payload)


@dataclass(frozen=True)
class PropQuote:
    book: str
    market: str
    player: str
    """The outcome's ``description``: a player name (or a team, for team props)."""
    selection: str
    """Over / Under / Yes / No, or the player's name for one-way markets."""
    point: float | None
    american_odds: float
    dfs_odds_type: str | None
    payout_multiplier: float | None
    last_change_at: str | None


def parse_props(payload: dict[str, Any]) -> list[PropQuote]:
    out = []
    for book in payload.get("bookmakers", []):
        for market in book.get("markets", []):
            for o in market.get("outcomes", []):
                price = o.get("price")
                player = o.get("description") or o.get("name")
                if price is None or not player:
                    continue
                out.append(
                    PropQuote(
                        book=book.get("key", ""),
                        market=market.get("key", ""),
                        player=player,
                        selection=o.get("name", ""),
                        point=o.get("point"),
                        american_odds=float(price),
                        dfs_odds_type=o.get("dfs_odds_type"),
                        payout_multiplier=o.get("payout_multiplier"),
                        last_change_at=o.get("last_change_at"),
                    )
                )
    return out
