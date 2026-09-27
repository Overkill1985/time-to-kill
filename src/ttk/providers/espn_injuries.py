"""ESPN's current injury list per sport (live only; there is no history endpoint).

``GET site.api.espn.com/apis/site/v2/sports/{path}/injuries`` -> ``injuries``: one
entry per team (``id`` = ESPN team id) with that team's ``injuries``. Verified
2026-09-27: NBA ~70 entries, NFL ~800 (~9 MB), college football a handful,
men's college basketball none.

- ``status``: Out, Day-To-Day (NBA); Out, Doubtful, Questionable, Injured Reserve,
  Active (NFL; "Active" = cleared to play). Stored as reported.
- ``date`` is when ESPN last updated the entry; ``details.returnDate`` is its
  expected return. The athlete id appears only in the profile link (``/id/<n>/``).
- History comes from polling: services/injury_ingest.py stores changes with our
  ``observed_at``, so a model can ask what was known at any moment.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from ttk.domain import Sport
from ttk.providers.base import ProviderError
from ttk.providers.http_retry import get_with_retries

INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/{path}/injuries"
INJURY_PATHS = {
    Sport.NFL: "football/nfl",
    Sport.NBA: "basketball/nba",
    Sport.CFB: "football/college-football",
    Sport.NCAAB: "basketball/mens-college-basketball",
}
_ATHLETE_ID = re.compile(r"/id/(\d+)")


@dataclass(frozen=True)
class InjuryEntry:
    team_espn_id: str
    player_id: str
    player_name: str
    status: str
    injury_type: str | None
    comment: str | None
    return_date: str | None
    updated_at: datetime | None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _athlete_id(athlete: Mapping[str, Any]) -> str | None:
    for link in athlete.get("links") or []:
        match = _ATHLETE_ID.search(str(link.get("href") or ""))
        if match:
            return match.group(1)
    return None


def parse_injuries(payload: Mapping[str, Any]) -> list[InjuryEntry]:
    entries: list[InjuryEntry] = []
    for team in payload.get("injuries") or []:
        team_id = str(team.get("id") or "")
        for item in team.get("injuries") or []:
            athlete = item.get("athlete") or {}
            player_id = _athlete_id(athlete)
            status = item.get("status")
            if not team_id or not player_id or not status:
                continue  # never guess who a report is about
            details = item.get("details") or {}
            return_date = details.get("returnDate")
            entries.append(
                InjuryEntry(
                    team_espn_id=team_id,
                    player_id=player_id,
                    player_name=str(athlete.get("displayName") or ""),
                    status=str(status),
                    injury_type=details.get("type"),
                    comment=item.get("shortComment") or None,
                    return_date=str(return_date)[:10] if return_date else None,
                    updated_at=_parse_time(item.get("date")),
                )
            )
    return entries


class EspnInjuries:
    name = "espn"

    def __init__(
        self, *, client: httpx.Client | None = None, retries: int = 2, backoff: float = 5.0
    ) -> None:
        self._client = client or httpx.Client(timeout=60.0)
        self._retries = retries
        self._backoff = backoff

    def fetch(self, sport: Sport) -> list[InjuryEntry]:
        response = get_with_retries(
            self._client,
            INJURIES_URL.format(path=INJURY_PATHS[sport]),
            name="espn-injuries",
            retries=self._retries,
            backoff=self._backoff,
        )
        if response.status_code != 200:
            raise ProviderError(f"espn-injuries: HTTP {response.status_code} for {sport}")
        return parse_injuries(response.json())
