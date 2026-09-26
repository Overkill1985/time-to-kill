import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ttk.domain import Market, Selection, Sport
from ttk.providers.odds_api_format import normalize_events

FIXTURES = Path(__file__).parent / "fixtures"


def test_real_propline_payload() -> None:
    """Captured from the PropLine MCP on 2026-09-26 (Pinnacle, first two spread lines)."""
    events = json.loads((FIXTURES / "propline_nfl_spreads_2026-09-26.json").read_text("utf-8"))
    fetch = normalize_events(events, provider="propline", sport=Sport.NFL)

    assert len(fetch.games) == 1
    game = fetch.games[0]
    assert (game.away.name, game.home.name) == ("Cincinnati Bengals", "Pittsburgh Steelers")
    # PropLine carries ESPN team ids; they are kept for cross-provider linking.
    assert (game.away.espn_id, game.home.espn_id) == ("4", "23")
    assert game.commence_time == datetime(2026, 9, 27, 17, 0, tzinfo=UTC)

    main = [q for q in fetch.quotes if q.line in (3.5, -3.5)]
    assert {(q.selection, q.line, q.american_odds) for q in main} == {
        (Selection.HOME, 3.5, -115.0),
        (Selection.AWAY, -3.5, 102.0),
    }
    assert all(q.market is Market.SPREAD and q.sportsbook_key == "pinnacle" for q in fetch.quotes)
    # The alternate line under the same key is kept, not merged into the main line.
    assert {q.line for q in fetch.quotes} == {3.5, -3.5, 1.0, -1.0}
    assert all(q.source_timestamp is not None for q in fetch.quotes)


def _event(bookmakers: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "e1",
        "home_team": "Home FC",
        "away_team": "Away FC",
        "commence_time": "2026-10-01T00:00:00Z",
        "bookmakers": bookmakers,
    }


def _market(key: str, outcomes: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"key": key, "last_update": "2026-09-30T12:00:00Z", "outcomes": outcomes, **extra}


def test_markets_and_filters() -> None:
    ml = _market("h2h", [{"name": "Home FC", "price": -150}, {"name": "Away FC", "price": 130}])
    tot = _market(
        "totals",
        [
            {"name": "Over", "price": -110, "point": 45.5},
            {"name": "Under", "price": -110, "point": 45.5},
        ],
    )
    events = [
        _event(
            [
                {"key": "dk", "title": "DraftKings", "markets": [ml, tot]},
                {"key": "prizepicks", "title": "PrizePicks", "markets": [ml]},
                {
                    "key": "fd",
                    "title": "FanDuel",
                    "markets": [
                        _market("h2h", ml["outcomes"], suspended_at="2026-09-30T13:00:00Z"),
                        _market("totals", tot["outcomes"], team="Home FC"),
                        _market("totals", tot["outcomes"], period="h1"),
                        _market("player_points", []),
                        _market(
                            "h2h",
                            [
                                {"name": "Home FC", "price": -150, "payout_multiplier": 1.5},
                                {"name": "Mystery", "price": 110},
                            ],
                        ),
                    ],
                },
            ]
        )
    ]
    fetch = normalize_events(events, provider="test", sport=Sport.NBA)

    assert {(q.market, q.selection, q.line) for q in fetch.quotes} == {
        (Market.MONEYLINE, Selection.HOME, None),
        (Market.MONEYLINE, Selection.AWAY, None),
        (Market.TOTAL, Selection.OVER, 45.5),
        (Market.TOTAL, Selection.UNDER, 45.5),
    }
    assert {q.sportsbook_key for q in fetch.quotes} == {"dk"}
    assert fetch.skipped == {
        "excluded_book": 1,
        "suspended_market": 1,
        "team_total_not_supported": 1,
        "period_market": 1,
        "unsupported_market:player_points": 1,
        "boosted_or_discounted_price": 1,
        "unparseable_outcome": 1,
    }


def test_outrights_skipped() -> None:
    fetch = normalize_events(
        [{**_event([]), "is_outright": True}], provider="test", sport=Sport.NFL
    )
    assert fetch.games == [] and fetch.skipped == {"outright_event": 1}


def test_null_collections_are_empty() -> None:
    # PropLine's event list sends "bookmakers": null for events without odds.
    event = {**_event([]), "bookmakers": None}
    fetch = normalize_events([event], provider="test", sport=Sport.CFB)
    assert len(fetch.games) == 1 and fetch.quotes == []
    book = {"key": "dk", "title": "DK", "markets": None}
    market_null = {"key": "dk", "title": "DK", "markets": [_market("h2h", None)]}  # type: ignore[arg-type]
    assert (
        normalize_events([_event([book, market_null])], provider="t", sport=Sport.CFB).quotes == []
    )


def test_side_field_wins_over_abbreviated_names() -> None:
    # DraftKings via PropLine: "PIT Steelers", not the event's "Pittsburgh Steelers".
    event = {
        **_event(
            [
                {
                    "key": "draftkings",
                    "title": "DraftKings",
                    "markets": [
                        _market(
                            "spreads",
                            [
                                {"name": "HOM FC", "price": -112, "point": -3.5, "side": "home"},
                                {"name": "AWY FC", "price": -108, "point": 3.5, "side": "away"},
                            ],
                        ),
                    ],
                },
            ]
        ),
    }
    fetch = normalize_events([event], provider="propline", sport=Sport.NFL)
    assert {(q.selection, q.line) for q in fetch.quotes} == {
        (Selection.HOME, -3.5),
        (Selection.AWAY, 3.5),
    }
    assert fetch.skipped == {}


@pytest.mark.parametrize(
    "description",
    [
        "Team Total Points - J'ville St",
        "Alternate Total Points (line 30.5) - J'ville St",
        "Alternate Total Points (line 0.5) - C Arkansas",
    ],
)
def test_untagged_team_totals_are_skipped(description: str) -> None:
    # Real FanDuel CFB descriptions (PropLine, 2026-09-26) that arrived with team=null.
    over_under = [
        {"name": "Over", "price": -111, "point": 30.5},
        {"name": "Under", "price": -115, "point": 30.5},
    ]
    game_total = _market("totals", over_under, description="Alternate Total Points (line 52.5)")
    team_total = _market("totals", over_under, description=description, team=None)
    event = _event([{"key": "fanduel", "title": "FanDuel", "markets": [game_total, team_total]}])
    fetch = normalize_events([event], provider="propline", sport=Sport.CFB)
    assert len(fetch.quotes) == 2  # only the real game total survives
    assert fetch.skipped == {"team_total_untagged": 1}
