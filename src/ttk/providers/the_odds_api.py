"""The Odds API v4 adapter (https://the-odds-api.com/liveapi/guides/v4/).

Costs credits: one per region per market per call. The key travels as a query
parameter (the API's design), so errors are re-raised without the URL.
"""

from __future__ import annotations

from typing import Any

import httpx

from ttk.domain import Sport
from ttk.providers.base import OddsFetch, ProviderError
from ttk.providers.odds_api_format import normalize_events

BASE_URL = "https://api.the-odds-api.com/v4"

SPORT_KEYS = {
    Sport.NFL: "americanfootball_nfl",
    Sport.NBA: "basketball_nba",
    Sport.CFB: "americanfootball_ncaaf",
    Sport.NCAAB: "basketball_ncaab",
}


class TheOddsApiProvider:
    name = "the-odds-api"

    def __init__(
        self,
        api_key: str,
        *,
        regions: str = "us",
        markets: str = "h2h,spreads,totals",
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("The Odds API needs an API key (TTK_ODDS_API_KEY)")
        self._api_key = api_key
        self._regions = regions
        self._markets = markets
        self._client = client or httpx.Client(base_url=BASE_URL, timeout=20.0)
        self.requests_remaining: int | None = None

    def fetch_odds(self, sport: Sport) -> OddsFetch:
        try:
            response = self._client.get(
                f"/sports/{SPORT_KEYS[sport]}/odds",
                params={
                    "apiKey": self._api_key,
                    "regions": self._regions,
                    "markets": self._markets,
                    "oddsFormat": "american",
                    "dateFormat": "iso",
                },
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name}: request failed ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise ProviderError(f"{self.name}: HTTP {response.status_code} for {sport}")
        remaining = response.headers.get("x-requests-remaining")
        if remaining is not None and remaining.replace(".", "", 1).isdigit():
            self.requests_remaining = int(float(remaining))
        payload: list[dict[str, Any]] = response.json()
        return normalize_events(payload, provider=self.name, sport=sport)
