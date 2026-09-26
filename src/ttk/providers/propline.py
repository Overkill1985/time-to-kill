"""PropLine REST adapter (https://prop-line.com/docs), bulk game odds.

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
    ) -> None:
        if not api_key:
            raise ValueError("PropLine needs an API key (TTK_PROPLINE_API_KEY)")
        self._markets = markets
        self._client = client or httpx.Client(base_url=BASE_URL, timeout=30.0)
        self._headers = {"X-API-Key": api_key}
        self.daily_remaining: int | None = None

    def fetch_odds(self, sport: Sport) -> OddsFetch:
        try:
            response = self._client.get(
                f"/v1/sports/{SPORT_KEYS[sport]}/odds",
                params={"markets": self._markets},
                headers=self._headers,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name}: request failed ({type(exc).__name__})") from None
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
            raise ProviderError(f"{self.name}: HTTP {response.status_code} for {sport}")
        payload: list[dict[str, Any]] = response.json()
        return normalize_events(payload, provider=self.name, sport=sport)
