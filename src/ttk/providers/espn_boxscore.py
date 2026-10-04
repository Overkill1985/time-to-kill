"""ESPN game summaries: player box scores (who started, who played, minutes, stats).

``GET site.api.espn.com/apis/site/v2/sports/{path}/summary?event={id}``. Verified
2026-09-27 on NBA games 2017-18 through 2025-26:

- ``boxscore.players`` has one entry per team (``team.id`` is the ESPN team id);
  ``statistics[0].names`` labels each athlete's ``stats`` (MIN, PTS, FG "made-att",
  3PT, FT, REB, AST, TO, STL, BLK, OREB, DREB, PF, +/-). Minutes are whole numbers.
- Healthy scratches are listed with ``didNotPlay`` and a ``reason`` ("COACH'S
  DECISION") and empty stats. **Injured and inactive players are not listed.**
- The summary's ``injuries`` block is the player's *current* status (a 2017 game
  shows 2026 dates), so it is never read here: it would leak the future.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from ttk.domain import Sport
from ttk.providers.base import ProviderError
from ttk.providers.http_retry import get_with_retries

SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/{path}/summary?event={event}"
SUMMARY_PATHS = {Sport.NBA: "basketball/nba", Sport.NCAAB: "basketball/mens-college-basketball"}


@dataclass(frozen=True)
class PlayerLine:
    team_espn_id: str
    player_id: str
    player_name: str | None
    starter: bool
    played: bool
    dnp_reason: str | None
    minutes: float | None = None
    points: int | None = None
    fgm: int | None = None
    fga: int | None = None
    ftm: int | None = None
    fta: int | None = None
    oreb: int | None = None
    dreb: int | None = None
    ast: int | None = None
    stl: int | None = None
    blk: int | None = None
    tov: int | None = None
    pf: int | None = None
    plus_minus: int | None = None


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.replace("+", ""))
    except ValueError:
        return None


def _made_attempted(value: str | None) -> tuple[int | None, int | None]:
    if not value or "-" not in value.lstrip("-"):
        return None, None
    made, _, attempted = value.partition("-")
    return _int(made), _int(attempted)


def parse_boxscore(summary: Mapping[str, Any]) -> list[PlayerLine]:
    lines: list[PlayerLine] = []
    for team in (summary.get("boxscore") or {}).get("players") or []:
        team_id = str((team.get("team") or {}).get("id") or "")
        groups = team.get("statistics") or []
        if not team_id or not groups:
            continue
        names = [str(n).upper() for n in groups[0].get("names") or []]
        for entry in groups[0].get("athletes") or []:
            athlete = entry.get("athlete") or {}
            player_id = str(athlete.get("id") or "")
            if not player_id:
                continue
            stats = dict(zip(names, entry.get("stats") or [], strict=False))
            minutes = _int(stats.get("MIN"))
            played = not entry.get("didNotPlay") and bool(minutes)
            fgm, fga = _made_attempted(stats.get("FG"))
            ftm, fta = _made_attempted(stats.get("FT"))
            lines.append(
                PlayerLine(
                    team_espn_id=team_id,
                    player_id=player_id,
                    player_name=athlete.get("displayName"),
                    starter=bool(entry.get("starter")),
                    played=played,
                    dnp_reason=(entry.get("reason") or None) if not played else None,
                    minutes=float(minutes) if minutes is not None else None,
                    points=_int(stats.get("PTS")),
                    fgm=fgm,
                    fga=fga,
                    ftm=ftm,
                    fta=fta,
                    oreb=_int(stats.get("OREB")),
                    dreb=_int(stats.get("DREB")),
                    ast=_int(stats.get("AST")),
                    stl=_int(stats.get("STL")),
                    blk=_int(stats.get("BLK")),
                    tov=_int(stats.get("TO")),
                    pf=_int(stats.get("PF")),
                    plus_minus=_int(stats.get("+/-")),
                )
            )
    return lines


class EspnBoxscores:
    name = "espn-summary"

    def __init__(
        self, *, client: httpx.Client | None = None, retries: int = 3, backoff: float = 5.0
    ) -> None:
        self._client = client or httpx.Client(timeout=30.0)
        self._retries = retries
        self._backoff = backoff

    def fetch(self, sport: Sport, event_id: str) -> list[PlayerLine]:
        summary = self.fetch_summary(sport, event_id)
        return parse_boxscore(summary) if summary else []

    def fetch_summary(self, sport: Sport, event_id: str) -> dict[str, Any]:
        """The raw summary, or {} for an unknown event."""
        response = get_with_retries(
            self._client,
            SUMMARY_URL.format(path=SUMMARY_PATHS[sport], event=event_id),
            name=self.name,
            retries=self._retries,
            backoff=self._backoff,
        )
        if response.status_code == 404:
            return {}
        if response.status_code != 200:
            raise ProviderError(f"{self.name}: HTTP {response.status_code} for {event_id}")
        body: dict[str, Any] = response.json()
        return body


@dataclass(frozen=True)
class TeamBox:
    """One team's totals in one game (``boxscore.teams[].statistics``)."""

    team_espn_id: str
    fgm: int
    fga: int
    fg3m: int
    fg3a: int
    ftm: int
    fta: int
    oreb: int
    dreb: int
    tov: int

    @property
    def points(self) -> int:
        return 2 * self.fgm + self.fg3m + self.ftm

    @property
    def possessions(self) -> float:
        """The standard estimate: FGA - OREB + TO + 0.475 x FTA."""
        return self.fga - self.oreb + self.tov + 0.475 * self.fta


def parse_team_totals(summary: Mapping[str, Any]) -> list[TeamBox]:
    """Both teams' totals, or [] if either is missing a needed stat (never guessed)."""
    out = []
    for team in (summary.get("boxscore") or {}).get("teams") or []:
        team_id = str((team.get("team") or {}).get("id") or "")
        stats = {s.get("name"): s.get("displayValue") for s in team.get("statistics") or []}
        fgm, fga = _made_attempted(stats.get("fieldGoalsMade-fieldGoalsAttempted"))
        fg3m, fg3a = _made_attempted(
            stats.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted")
        )
        ftm, fta = _made_attempted(stats.get("freeThrowsMade-freeThrowsAttempted"))
        oreb, dreb = _int(stats.get("offensiveRebounds")), _int(stats.get("defensiveRebounds"))
        tov = _int(stats.get("totalTurnovers") or stats.get("turnovers"))
        values = (fgm, fga, fg3m, fg3a, ftm, fta, oreb, dreb, tov)
        if not team_id or any(v is None for v in values):
            return []
        out.append(TeamBox(team_id, *(int(v) for v in values if v is not None)))
    return out if len(out) == 2 else []
