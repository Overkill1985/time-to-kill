"""Reanalysis weather at past college football kickoffs, for research only.

Which stadium a game was played in comes from CollegeFootballData (``/venues``
for coordinates and the dome flag, ``/games`` per season for each game's venue;
CFBD game ids are ESPN event ids). The weather at the kickoff hour comes from
Open-Meteo's archive (ERA5 reanalysis: a modelled value for the location, not a
stadium reading). Only finished games with a reported total are imported.

Resumable: a game already in ``game_weather`` is skipped, so the import can run
over several days. Each place counts as one Open-Meteo request; ``budget`` caps
a run and requests are paced to stay under the hourly limit.
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import Game, GameSourceId, GameWeather, ReportedLine
from ttk.domain import Sport
from ttk.providers.base import ProviderError
from ttk.providers.cfbd import Venue, parse_game_venues, parse_venues
from ttk.providers.open_meteo import kickoff_hour, parse_hour

PROVIDER = "open-meteo-archive"
PER_REQUEST = 25
PER_HOUR = 4000
"""Places per hour (Open-Meteo's free limit is 5,000)."""


class CfbdSource(Protocol):
    calls_remaining: int | None

    def get(self, path: str, **params: int | str) -> list[dict[str, Any]]: ...


class Archive(Protocol):
    name: str

    def day(self, points: list[tuple[float, float]], day: date) -> list[dict[str, Any]]: ...


@dataclass
class ImportReport:
    requested: int = 0
    stored: int = 0
    indoor: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    remaining: int = 0
    """Outdoor games still without weather after this run."""
    stopped: str | None = None
    cfbd_calls_remaining: int | None = None


@dataclass(frozen=True)
class _Todo:
    game_id: int
    event_id: str
    kickoff: datetime


def _todo(session: Session, first: int, last: int) -> list[_Todo]:
    done = select(GameWeather.game_id).where(GameWeather.provider == PROVIDER)
    lined = select(ReportedLine.game_id).where(ReportedLine.total.is_not(None))
    rows = session.execute(
        select(Game.id, GameSourceId.source_identifier, Game.commence_time)
        .join(GameSourceId, GameSourceId.game_id == Game.id)
        .where(
            Game.sport == Sport.CFB,
            Game.season.between(first, last),
            Game.home_score.is_not(None),
            GameSourceId.provider == "espn",
            Game.id.in_(lined),
            Game.id.not_in(done),
        )
        .order_by(Game.commence_time)
    ).all()
    return [_Todo(gid, str(event), kick) for gid, event, kick in rows]


def _row(t: _Todo, venue: Venue, **weather: Any) -> GameWeather:
    return GameWeather(
        game_id=t.game_id,
        provider=PROVIDER,
        venue_source="cfbd",
        venue_id=str(venue.venue_id),
        venue_name=venue.name[:120],
        indoor=venue.dome,
        latitude=venue.latitude,
        longitude=venue.longitude,
        valid_at=kickoff_hour(t.kickoff),
        **weather,
    )


def import_cfb_weather(
    session_factory: sessionmaker[Session],
    cfbd: CfbdSource,
    archive: Archive,
    *,
    first: int,
    last: int,
    budget: int,
    per_request: int = PER_REQUEST,
    per_hour: int = PER_HOUR,
    sleep: Callable[[float], None] = time.sleep,
) -> ImportReport:
    report = ImportReport()
    venues = parse_venues(cfbd.get("/venues"))
    game_venue: dict[str, int] = {}
    for season in range(first, last + 1):
        game_venue.update(parse_game_venues(cfbd.get("/games", year=season, seasonType="both")))
    report.cfbd_calls_remaining = cfbd.calls_remaining

    with session_factory() as session:
        by_day: dict[date, list[tuple[_Todo, Venue]]] = defaultdict(list)
        for t in _todo(session, first, last):
            venue = venues.get(game_venue.get(t.event_id, -1))
            if venue is None:
                report.skipped["no_venue"] += 1
            elif venue.dome:
                session.add(_row(t, venue))
                report.indoor += 1
            elif venue.latitude is None or venue.longitude is None:
                report.skipped["venue_without_coordinates"] += 1
            else:
                by_day[kickoff_hour(t.kickoff).date()].append((t, venue))
        session.commit()

        pending = sum(len(v) for v in by_day.values())
        for day in sorted(by_day):
            games = by_day[day]
            for i in range(0, len(games), per_request):
                chunk = games[i : i + per_request]
                if report.requested + len(chunk) > budget:
                    report.stopped = f"budget of {budget} places reached"
                    break
                points = [(v.latitude or 0.0, v.longitude or 0.0) for _, v in chunk]
                try:
                    payloads = archive.day(points, day)
                except ProviderError as exc:
                    report.stopped = str(exc)
                    break
                report.requested += len(chunk)
                for (t, venue), payload in zip(chunk, payloads, strict=True):
                    try:
                        f = parse_hour(payload, kickoff_hour(t.kickoff))
                    except ProviderError:
                        report.skipped["no_archive_hour"] += 1
                        continue
                    session.add(
                        _row(
                            t,
                            venue,
                            wind_mph=f.wind_mph,
                            gust_mph=f.gust_mph,
                            temperature_f=f.temperature_f,
                            precipitation_mm=f.precipitation_mm,
                        )
                    )
                    report.stored += 1
                session.commit()
                sleep(len(chunk) * 3600 / per_hour)
            if report.stopped:
                break
        report.remaining = pending - report.stored - report.skipped["no_archive_hour"]
    return report
