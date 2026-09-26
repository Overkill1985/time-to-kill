"""nflverse play-by-play -> per-game team and quarterback EPA aggregates.

Source: ``nflverse/nflverse-data`` release ``pbp``, one ``play_by_play_{season}.csv.gz``
per season (1999+; ~12-19 MB each). Streamed and aggregated in memory; raw
plays are never stored. Verified 2026-09-26 on 1999 and 2025:
- ``game_id`` matches nfldata ``games.csv`` (e.g. 2025_01_ARI_NO).
- Offensive plays: ``play_type`` pass or run with an ``epa`` value, excluding
  two-point attempts (nflfastR convention; penalties recorded as ``no_play``
  are excluded).
- Dropbacks: ``qb_dropback == 1``. The quarterback is ``id`` (set on scrambles,
  where ``passer_player_id`` is empty), falling back to ``passer_player_id``.
- Player ids are NFL GSIS ids, matching ``games.csv`` ``home_qb_id``.
"""

from __future__ import annotations

import csv
import gzip
import io
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import httpx

from ttk.providers.base import ProviderError

PBP_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.csv.gz"
)


@dataclass
class TeamGameAggregate:
    game_source_id: str
    team_code: str
    plays: int = 0
    epa_total: float = 0.0
    successes: int = 0
    dropbacks: int = 0
    dropback_epa_total: float = 0.0
    rushes: int = 0
    rush_epa_total: float = 0.0


@dataclass
class QbGameAggregate:
    game_source_id: str
    team_code: str
    player_id: str
    player_name: str | None
    dropbacks: int = 0
    qb_epa_total: float = 0.0


@dataclass
class SeasonAggregates:
    season: int
    teams: list[TeamGameAggregate]
    qbs: list[QbGameAggregate]
    skipped: dict[str, int]


def aggregate_plays(rows: Iterable[Mapping[str, str]], season: int) -> SeasonAggregates:
    teams: dict[tuple[str, str], TeamGameAggregate] = {}
    qbs: dict[tuple[str, str], QbGameAggregate] = {}
    skipped: Counter[str] = Counter()
    for row in rows:
        if row.get("play_type") not in ("pass", "run"):
            continue
        if row.get("two_point_attempt") == "1":
            skipped["two_point_attempt"] += 1
            continue
        epa_raw, team = row.get("epa", ""), row.get("posteam", "")
        if epa_raw in ("", "NA") or not team:
            skipped["missing_epa_or_team"] += 1
            continue
        epa = float(epa_raw)
        game = row["game_id"]
        agg = teams.get((game, team))
        if agg is None:
            agg = teams[(game, team)] = TeamGameAggregate(game, team)
        agg.plays += 1
        agg.epa_total += epa
        agg.successes += row.get("success") == "1"
        if row.get("qb_dropback") == "1":
            agg.dropbacks += 1
            agg.dropback_epa_total += epa
            qb_id = row.get("id") or row.get("passer_player_id") or ""
            if not qb_id or qb_id == "NA":
                skipped["dropback_without_qb_id"] += 1
                continue
            qb_epa_raw = row.get("qb_epa", "")
            qb = qbs.get((game, qb_id))
            if qb is None:
                name = row.get("name") or row.get("passer_player_name") or None
                qb = qbs[(game, qb_id)] = QbGameAggregate(game, team, qb_id, name)
            qb.dropbacks += 1
            qb.qb_epa_total += float(qb_epa_raw) if qb_epa_raw not in ("", "NA") else epa
        elif row.get("rush") == "1":
            agg.rushes += 1
            agg.rush_epa_total += epa
    return SeasonAggregates(season, list(teams.values()), list(qbs.values()), dict(skipped))


class NflversePbpProvider:
    name = "nflverse"

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=300.0, follow_redirects=True)

    def fetch_season(self, season: int) -> SeasonAggregates:
        try:
            response = self._client.get(PBP_URL.format(season=season))
        except httpx.HTTPError as exc:
            raise ProviderError(f"nflverse pbp: request failed ({type(exc).__name__})") from None
        if response.status_code == 404:
            raise ProviderError(f"nflverse pbp: no file for season {season}")
        if response.status_code != 200:
            raise ProviderError(f"nflverse pbp: HTTP {response.status_code} for {season}")
        with gzip.GzipFile(fileobj=io.BytesIO(response.content)) as raw:
            rows = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8"))
            return aggregate_plays(rows, season)
