"""ESPN site API scoreboard adapter: schedules, status and final scores.

Unofficial, unauthenticated public API. Behaviors verified 2026-09-26:
- ``dates`` must be a single YYYYMMDD for the NFL (a range returns HTTP 400),
  so every sport is fetched one day at a time.
- College scoreboards default to a featured subset; ``groups`` selects the
  division (CFB 80 = FBS, 81 = FCS; men's basketball 50 = Division I) and
  ``limit`` must be raised to get every game. **limit above 500 is silently
  ignored and ESPN falls back to 25 events**, truncating the day without error;
  a full page is therefore flagged as possibly truncated. FBS-vs-FCS games appear in both
  CFB groups and are de-duplicated by event id.
- Scheduled games report a score of "0"; scores are only read once a game is
  in progress or over.
- Do not send a custom User-Agent; the old app found this host rejects them.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

import httpx

from ttk.domain import GameStatus, Sport
from ttk.providers.base import (
    NormalizedGame,
    ProviderError,
    ScheduleFetch,
    TeamRef,
)
from ttk.providers.http_retry import get_with_retries
from ttk.providers.odds_api_format import parse_timestamp

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports"
PAGE_LIMIT = 500  # ESPN's maximum; see module docstring

LEAGUES: dict[Sport, tuple[str, tuple[str | None, ...]]] = {
    Sport.NFL: ("football/nfl", (None,)),
    Sport.NBA: ("basketball/nba", (None,)),
    Sport.CFB: ("football/college-football", ("80", "81")),
    Sport.NCAAB: ("basketball/mens-college-basketball", ("50",)),
}

# ESPN season.type: 1 preseason, 2 regular, 3 postseason, 5 NBA play-in (postseason).
_SEASON_TYPES = {1: "PRE", 2: "REG", 3: "POST", 5: "POST"}

_STATUS_BY_NAME = {
    "STATUS_POSTPONED": GameStatus.POSTPONED,
    "STATUS_CANCELED": GameStatus.CANCELED,
    "STATUS_CANCELLED": GameStatus.CANCELED,
    "STATUS_SUSPENDED": GameStatus.SUSPENDED,
}


def _status(status_type: Mapping[str, Any]) -> GameStatus:
    if status_type.get("name") in _STATUS_BY_NAME:
        return _STATUS_BY_NAME[status_type["name"]]
    match status_type.get("state"):
        case "pre":
            return GameStatus.SCHEDULED
        case "in":
            return GameStatus.IN_PROGRESS
        case "post" if status_type.get("completed"):
            return GameStatus.FINAL
    return GameStatus.UNKNOWN


def _season_type(event: Mapping[str, Any]) -> str | None:
    raw = (event.get("season") or {}).get("type")
    return _SEASON_TYPES.get(raw) if isinstance(raw, int) else None


def _team(competitor: Mapping[str, Any]) -> TeamRef:
    team = competitor["team"]
    variants = (team.get("location"), team.get("shortDisplayName"))
    name = team["displayName"]
    return TeamRef(
        name=name,
        espn_id=str(team["id"]),
        other_names=tuple(dict.fromkeys(v for v in variants if v and v != name)),
    )


def _score(competitor: Mapping[str, Any]) -> int | None:
    raw = competitor.get("score")
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_event(event: Mapping[str, Any], sport: Sport) -> NormalizedGame | None:
    """One scoreboard event -> NormalizedGame, or None if it isn't a two-team game."""
    competitions = event.get("competitions") or []
    if not competitions:
        return None
    comp = competitions[0]
    sides = {c.get("homeAway"): c for c in comp.get("competitors", [])}
    commence = parse_timestamp(event.get("date"))
    if set(sides) != {"home", "away"} or commence is None:
        return None
    status = _status(comp.get("status", {}).get("type", {}))
    scored = status in (GameStatus.IN_PROGRESS, GameStatus.FINAL)
    event_id = str(event["id"])
    return NormalizedGame(
        provider=EspnScheduleProvider.name,
        source_identifier=event_id,
        sport=sport,
        home=_team(sides["home"]),
        away=_team(sides["away"]),
        commence_time=commence,
        source_timestamp=None,
        espn_event_id=event_id,
        status=status,
        home_score=_score(sides["home"]) if scored else None,
        away_score=_score(sides["away"]) if scored else None,
        neutral_site=bool(comp.get("neutralSite")),
        season=(event.get("season") or {}).get("year"),
        season_type=_season_type(event),
        week=(event.get("week") or {}).get("number"),
    )


class EspnScheduleProvider:
    name = "espn"

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        delay: float = 0.0,
        retries: int = 3,
        backoff: float = 5.0,
    ) -> None:
        self._client = client or httpx.Client(base_url=BASE_URL, timeout=30.0)
        self._delay = delay
        """Seconds to wait after each request (pacing for bulk history imports)."""
        self._retries = retries
        self._backoff = backoff

    def _scoreboard(self, path: str, day: date, group: str | None) -> list[dict[str, Any]]:
        params = {"dates": day.strftime("%Y%m%d"), "limit": str(PAGE_LIMIT)}
        if group:
            params["groups"] = group
        # Transient 5xx (ESPN returned a 502 mid-import on 2026-09-27) are retried.
        response = get_with_retries(
            self._client,
            f"/{path}/scoreboard",
            name="espn",
            retries=self._retries,
            backoff=self._backoff,
            params=params,
        )
        if response.status_code != 200:
            raise ProviderError(f"espn: HTTP {response.status_code} for {path} {day}")
        events: list[dict[str, Any]] = response.json().get("events", [])
        if self._delay:
            time.sleep(self._delay)
        return events

    def fetch_games(self, sport: Sport, start: date, end: date) -> ScheduleFetch:
        if end < start:
            raise ValueError("end date is before start date")
        path, groups = LEAGUES[sport]
        games: dict[str, NormalizedGame] = {}
        skipped: Counter[str] = Counter()
        day = start
        while day <= end:
            for group in groups:
                events = self._scoreboard(path, day, group)
                if len(events) >= PAGE_LIMIT:
                    skipped["possibly_truncated_day"] += 1
                for event in events:
                    game = parse_event(event, sport)
                    if game is None:
                        skipped["not_a_two_team_game"] += 1
                    elif game.source_identifier in games:
                        skipped["duplicate_across_groups"] += 1
                    else:
                        games[game.source_identifier] = game
            day += timedelta(days=1)
        return ScheduleFetch(list(games.values()), dict(skipped))
