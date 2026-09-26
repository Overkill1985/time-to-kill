"""Provider interfaces and the normalized records they return.

Business logic only ever sees these types. Each provider adapter converts its
own payload into them at the boundary and stamps provenance (provider,
source_identifier, source_timestamp). Adapters may be backed by a REST API, a
file, or (in development) data captured through an MCP server - the app never
depends on Claude or MCP at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from ttk.domain import GameStatus, Market, Selection, Sport


@dataclass(frozen=True)
class TeamRef:
    name: str
    """The provider's name for the team."""
    espn_id: str | None = None
    """ESPN team id when the provider knows it (ESPN always; PropLine sometimes)."""
    other_names: tuple[str, ...] = ()
    """Extra name variants the provider supplies (ESPN: location, short name)."""


@dataclass(frozen=True)
class NormalizedGame:
    provider: str
    source_identifier: str
    sport: Sport
    home: TeamRef
    away: TeamRef
    commence_time: datetime
    source_timestamp: datetime | None
    espn_event_id: str | None = None
    """Cross-provider link: ESPN's event id, if this provider reports it."""
    # Results fields: set only by schedule/results providers.
    status: GameStatus | None = None
    home_score: int | None = None
    away_score: int | None = None
    neutral_site: bool | None = None
    season: int | None = None
    season_type: str | None = None
    """PRE, REG or POST."""
    week: int | None = None


@dataclass(frozen=True)
class NormalizedOddsQuote:
    provider: str
    game_source_identifier: str
    sportsbook_key: str
    sportsbook_name: str
    market: Market
    selection: Selection
    line: float | None
    """Spread from the selection's perspective (home -3.5), total points, or None for moneyline."""
    american_odds: float
    source_timestamp: datetime | None
    """When the provider says this price was last seen/updated."""


@dataclass(frozen=True)
class OddsFetch:
    games: list[NormalizedGame]
    quotes: list[NormalizedOddsQuote]
    skipped: dict[str, int]
    """Counts of payload items dropped during normalization, by reason. Reported, never hidden."""


class OddsProvider(Protocol):
    name: str

    def fetch_odds(self, sport: Sport) -> OddsFetch: ...


@dataclass(frozen=True)
class ScheduleFetch:
    games: list[NormalizedGame]
    skipped: dict[str, int]


class ScheduleProvider(Protocol):
    """Games, status and final scores. The schedule authority for the app."""

    name: str

    def fetch_games(self, sport: Sport, start: date, end: date) -> ScheduleFetch:
        """Every game from start to end inclusive (dates in US Eastern, as leagues schedule)."""
        ...


class ProviderError(RuntimeError):
    """A provider call failed. Messages must never contain credentials."""
