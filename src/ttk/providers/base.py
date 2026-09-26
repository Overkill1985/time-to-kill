"""Provider interfaces and the normalized records they return.

Business logic only ever sees these types. Each provider adapter converts its
own payload into them at the boundary and stamps provenance (provider,
source_identifier, source_timestamp). Adapters may be backed by a REST API, a
file, or (in development) data captured through an MCP server - the app never
depends on Claude or MCP at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ttk.domain import Market, Selection, Sport


@dataclass(frozen=True)
class NormalizedGame:
    provider: str
    source_identifier: str
    sport: Sport
    home_team: str
    away_team: str
    commence_time: datetime
    source_timestamp: datetime | None


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


class ProviderError(RuntimeError):
    """A provider call failed. Messages must never contain credentials."""
