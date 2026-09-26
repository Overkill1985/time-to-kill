"""Normalizer for the Odds-API-style event payload.

The Odds API v4 and PropLine both return a list of events shaped like
``{id, sport_key, home_team, away_team, commence_time, bookmakers: [{key, title,
last_update, markets: [{key, last_update, outcomes: [{name, price, point}]}]}]}``.
PropLine adds optional fields that this normalizer honors when present:

- ``markets[].suspended_at``: book pulled the market; its prices are not bettable.
- ``markets[].team``: set on a TEAM total (same ``totals`` key as the game total).
- ``outcomes[].payout_multiplier``: DFS pick'em boosts; only 1.0 / null are real prices.
- ``markets[].period``: non-null for quarter/half markets, which are out of scope.
- Several lines per market key (alternates share the ``spreads`` key).
- ``espn_event_id`` and ``home_team_id``/``away_team_id`` like ``espn.ncaaf:2309``:
  ESPN ids used to link games and teams to the ESPN schedule without name matching.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from ttk.domain import Market, Selection, Sport
from ttk.providers.base import NormalizedGame, NormalizedOddsQuote, OddsFetch, TeamRef

MARKET_KEYS = {"h2h": Market.MONEYLINE, "spreads": Market.SPREAD, "totals": Market.TOTAL}

# DFS pick'em and similar books whose "odds" are not sportsbook prices.
DEFAULT_EXCLUDED_BOOKS = frozenset({"underdog", "prizepicks", "sleeper", "dabble", "betr", "rebet"})


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _espn_team_id(value: object) -> str | None:
    """PropLine's 'espn.ncaaf:2309' -> '2309'. Anything else -> None."""
    if isinstance(value, str) and value.startswith("espn.") and ":" in value:
        return value.rsplit(":", 1)[1] or None
    return None


def _selection(outcome_name: str, market: Market, event: Mapping[str, Any]) -> Selection | None:
    if market in (Market.TOTAL, Market.TEAM_TOTAL):
        return {"over": Selection.OVER, "under": Selection.UNDER}.get(outcome_name.lower())
    if outcome_name == event["home_team"]:
        return Selection.HOME
    if outcome_name == event["away_team"]:
        return Selection.AWAY
    if outcome_name.lower() == "draw":
        return Selection.DRAW
    return None


def normalize_events(
    events: Iterable[Mapping[str, Any]],
    *,
    provider: str,
    sport: Sport,
    excluded_books: frozenset[str] = DEFAULT_EXCLUDED_BOOKS,
) -> OddsFetch:
    games: list[NormalizedGame] = []
    quotes: list[NormalizedOddsQuote] = []
    skipped: Counter[str] = Counter()

    for event in events:
        if event.get("is_outright"):
            skipped["outright_event"] += 1
            continue
        event_id = str(event["id"])
        commence = parse_timestamp(event["commence_time"])
        if commence is None:
            skipped["missing_commence_time"] += 1
            continue
        espn_event = event.get("espn_event_id")
        games.append(
            NormalizedGame(
                provider=provider,
                source_identifier=event_id,
                sport=sport,
                home=TeamRef(event["home_team"], _espn_team_id(event.get("home_team_id"))),
                away=TeamRef(event["away_team"], _espn_team_id(event.get("away_team_id"))),
                commence_time=commence,
                source_timestamp=parse_timestamp(event.get("last_update")),
                espn_event_id=str(espn_event) if espn_event else None,
            )
        )
        for book in event.get("bookmakers") or []:
            if book["key"] in excluded_books:
                skipped["excluded_book"] += 1
                continue
            for mkt in book.get("markets") or []:
                market = MARKET_KEYS.get(mkt["key"])
                if market is None:
                    skipped[f"unsupported_market:{mkt['key']}"] += 1
                    continue
                if mkt.get("period"):
                    skipped["period_market"] += 1
                    continue
                if mkt.get("suspended_at"):
                    skipped["suspended_market"] += 1
                    continue
                if market is Market.TOTAL and mkt.get("team"):
                    # A team total rides the totals key; never mix it into the game total.
                    # Storing it needs the team on the quote, which is not modeled yet.
                    skipped["team_total_not_supported"] += 1
                    continue
                ts = parse_timestamp(mkt.get("last_update") or book.get("last_update"))
                for outcome in mkt.get("outcomes") or []:
                    multiplier = outcome.get("payout_multiplier")
                    if multiplier is not None and multiplier != 1.0:
                        skipped["boosted_or_discounted_price"] += 1
                        continue
                    selection = _selection(outcome["name"], market, event)
                    price = outcome.get("price")
                    if selection is None or price is None or -100 < price < 100:
                        skipped["unparseable_outcome"] += 1
                        continue
                    point = outcome.get("point")
                    quotes.append(
                        NormalizedOddsQuote(
                            provider=provider,
                            game_source_identifier=event_id,
                            sportsbook_key=book["key"],
                            sportsbook_name=book.get("title", book["key"]),
                            market=market,
                            selection=selection,
                            line=None if market is Market.MONEYLINE else point,
                            american_odds=float(price),
                            source_timestamp=parse_timestamp(outcome.get("last_seen_at")) or ts,
                        )
                    )
    return OddsFetch(games, quotes, dict(skipped))
