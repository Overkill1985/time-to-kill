"""CollegeFootballData.com: college football preseason facts per team-season.

``https://api.collegefootballdata.com``, bearer-token auth (``TTK_CFBD_API_KEY``).
Endpoints and fields are from the published OpenAPI spec (api-docs.json, read
2026-10-03). Every fact carries ``known_at``, when it was public:

- ``/talent?year=Y`` (247 roster talent composite) and ``/player/returning?year=Y``
  (share of last season's production returning): preseason composites, dated
  ``PRESEASON_CUTOFF`` (August 1 of Y).
- ``/recruiting/teams?year=Y``: the class signed for season Y, dated February 15 of
  Y (after the February signing day).
- ``/coaches?year=Y``: a head coach whose hire date falls in the year before the
  season makes ``new_head_coach`` = 1, dated by the hire.
- ``/player/portal?year=Y``: transfers for season Y, each dated by its transfer.
- ``/rankings?year=Y``: regular-season week 1 is the preseason poll (verified on
  2023: LSU 5th and Florida State 8th, before they met), dated August 22.
- Talent, returning production and recruiting carry only school names; they map to
  teams through ``/teams`` (CFBD team ids are ESPN's: 133 of 133 FBS, 2023).

End-of-season ratings (SP+, final polls) are never used for the same season: they
contain its results.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from ttk.providers.base import ProviderError
from ttk.providers.http_retry import get_with_retries

BASE_URL = "https://api.collegefootballdata.com"


def preseason_cutoff(season: int) -> datetime:
    return datetime(season, 8, 1, tzinfo=UTC)


def poll_release(season: int) -> datetime:
    """The preseason AP poll comes out in mid-August (as late as August 21, in 2016);
    the earliest season openers are around August 23."""
    return datetime(season, 8, 22, tzinfo=UTC)


def signing_day(season: int) -> datetime:
    return datetime(season, 2, 15, tzinfo=UTC)


@dataclass(frozen=True)
class TeamFact:
    team: str
    """CFBD school name (resolved to our team by services/cfbd_import.py)."""
    season: int
    name: str
    value: float
    known_at: datetime | None


@dataclass(frozen=True)
class Transfer:
    season: int
    origin: str | None
    destination: str | None
    rating: float | None
    stars: int | None
    transfer_date: datetime | None


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_talent(rows: Sequence[Mapping[str, Any]], season: int) -> list[TeamFact]:
    return [
        TeamFact(str(r["team"]), season, "talent", v, preseason_cutoff(season))
        for r in rows
        if r.get("team") and (v := _float(r.get("talent"))) is not None
    ]


RETURNING_FIELDS = {
    "percentPPA": "returning_ppa_pct",
    "percentPassingPPA": "returning_passing_ppa_pct",
    "usage": "returning_usage",
}


def parse_returning(rows: Sequence[Mapping[str, Any]], season: int) -> list[TeamFact]:
    facts = []
    for r in rows:
        if not r.get("team"):
            continue
        for field, name in RETURNING_FIELDS.items():
            v = _float(r.get(field))
            if v is not None:
                facts.append(TeamFact(str(r["team"]), season, name, v, preseason_cutoff(season)))
    return facts


def parse_recruiting(rows: Sequence[Mapping[str, Any]], season: int) -> list[TeamFact]:
    return [
        TeamFact(str(r["team"]), season, "recruiting_points", v, signing_day(season))
        for r in rows
        if r.get("team") and (v := _float(r.get("points"))) is not None
    ]


def parse_coaches(rows: Sequence[Mapping[str, Any]], season: int) -> list[TeamFact]:
    """``new_head_coach`` = 1 for a team whose head coach for ``season`` was hired
    after the previous preseason cutoff (and by this one); 0 for its other coaches'
    teams. Coaches without a hire date are skipped, never guessed."""
    out: dict[str, TeamFact] = {}
    window_start, window_end = preseason_cutoff(season - 1), preseason_cutoff(season)
    for coach in rows:
        hired = _date(coach.get("hireDate"))
        for stint in coach.get("seasons") or []:
            team, year = stint.get("school"), stint.get("year")
            if not team or year != season or hired is None:
                continue
            new = window_start < hired <= window_end
            if new or str(team) not in out:
                out[str(team)] = TeamFact(
                    str(team), season, "new_head_coach", 1.0 if new else 0.0, hired if new else None
                )
    return list(out.values())


def parse_portal(rows: Sequence[Mapping[str, Any]]) -> list[Transfer]:
    return [
        Transfer(
            int(r.get("season") or 0),
            r.get("origin") or None,
            r.get("destination") or None,
            _float(r.get("rating")),
            int(r["stars"]) if isinstance(r.get("stars"), int) else None,
            _date(r.get("transferDate")),
        )
        for r in rows
    ]


def parse_preseason_poll(
    weeks: Sequence[Mapping[str, Any]], season: int, poll: str = "AP Top 25"
) -> list[TeamFact]:
    """AP points in the season's first regular-season poll (unranked teams: absent)."""
    regular = [w for w in weeks if str(w.get("seasonType", "")).lower() == "regular"]
    if not regular:
        return []
    first = min(regular, key=lambda w: int(w.get("week") or 99))
    for p in first.get("polls") or []:
        if p.get("poll") != poll:
            continue
        return [
            TeamFact(
                str(rank["school"]),
                season,
                "preseason_ap_points",
                v,
                poll_release(season),
            )
            for rank in p.get("ranks") or []
            if rank.get("school") and (v := _float(rank.get("points"))) is not None
        ]
    return []


class CfbdClient:
    name = "cfbd"

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        retries: int = 3,
        backoff: float = 5.0,
        delay: float = 1.0,
    ) -> None:
        self._client = client or httpx.Client(base_url=BASE_URL, timeout=60.0)
        self._headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        self._retries = retries
        self._backoff = backoff
        self._delay = delay
        """Seconds between calls: back-to-back calls get HTTP 429 (2026-10-03)."""
        self.calls_remaining: int | None = None
        """The monthly quota left, from ``x-calllimit-remaining`` (free tier: 1,000)."""

    def get(self, path: str, **params: int | str) -> list[dict[str, Any]]:
        self._client.headers.update(self._headers)
        for attempt in range(self._retries + 1):
            response = get_with_retries(
                self._client,
                path,
                name=self.name,
                retries=self._retries,
                backoff=self._backoff,
                params={k: str(v) for k, v in params.items()},
            )
            if response.status_code != 429 or attempt == self._retries:
                break
            time.sleep(self._backoff * 2 * (attempt + 1))  # short-window throttle
        if self._delay:
            time.sleep(self._delay)
        remaining = response.headers.get("x-calllimit-remaining")
        if remaining is not None and remaining.isdigit():
            self.calls_remaining = int(remaining)
        if response.status_code == 401:
            raise ProviderError("cfbd: unauthorized (check TTK_CFBD_API_KEY)")
        if response.status_code == 429:
            raise ProviderError(
                f"cfbd: rate limited for {path} (monthly calls left: {self.calls_remaining})"
            )
        if response.status_code != 200:
            raise ProviderError(f"cfbd: HTTP {response.status_code} for {path}")
        body = response.json()
        if not isinstance(body, list):
            raise ProviderError(f"cfbd: unexpected payload for {path}")
        return body
