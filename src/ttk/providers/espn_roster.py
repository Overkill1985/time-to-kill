"""ESPN team rosters (site API): who is on a team now, for checking that a
player prop is filed under a game the player's team actually plays in.

``/apis/site/v2/sports/{path}/teams/{espn_team_id}/roster`` lists ``athletes``
flat (NBA) or grouped by position with ``items`` (NFL). Unofficial and free.
"""

from __future__ import annotations

from typing import Any

import httpx

from ttk.domain import Sport
from ttk.providers.base import ProviderError
from ttk.providers.http_retry import get_with_retries

BASE = "https://site.api.espn.com/apis/site/v2/sports"
PATHS = {Sport.NFL: "football/nfl", Sport.NBA: "basketball/nba"}


def parse_roster(payload: dict[str, Any]) -> list[str]:
    """Player display names, flat or grouped."""
    names: list[str] = []
    for entry in payload.get("athletes", []):
        items = entry.get("items") if isinstance(entry, dict) else None
        for athlete in items if items is not None else [entry]:
            name = athlete.get("displayName") if isinstance(athlete, dict) else None
            if name:
                names.append(name)
    return names


class EspnRosters:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=30.0)

    def fetch(self, sport: Sport, espn_team_id: str) -> list[str]:
        if sport not in PATHS:
            raise ProviderError(f"espn rosters: no roster path for {sport}")
        response = get_with_retries(
            self._client, f"{BASE}/{PATHS[sport]}/teams/{espn_team_id}/roster", name="espn roster"
        )
        if response.status_code != 200:
            raise ProviderError(f"espn roster: HTTP {response.status_code} for team {espn_team_id}")
        return parse_roster(response.json())
