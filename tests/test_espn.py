import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from ttk.domain import GameStatus, Sport
from ttk.providers.base import ProviderError
from ttk.providers.espn import BASE_URL, EspnScheduleProvider, parse_event

# Real ESPN scoreboard events captured 2026-09-26, trimmed to the fields we read.
FIXTURES: dict[str, list[dict[str, Any]]] = json.loads(
    (Path(__file__).parent / "fixtures" / "espn_scoreboards_2026.json").read_text("utf-8")
)


def test_final_nfl_game() -> None:
    game = parse_event(FIXTURES["nfl_final"][0], Sport.NFL)
    assert game is not None
    assert (game.away.name, game.home.name) == ("Carolina Panthers", "Atlanta Falcons")
    assert (game.home_score, game.away_score) == (3, 34)
    assert game.status is GameStatus.FINAL
    assert game.commence_time == datetime(2026, 9, 20, 17, 0, tzinfo=UTC)
    assert game.espn_event_id == game.source_identifier == "401872933"
    assert game.season == 2026
    assert game.home.espn_id is not None


def test_scheduled_game_has_no_score() -> None:
    # ESPN reports "0" before kickoff; that is not a score.
    game = parse_event(FIXTURES["nfl_scheduled"][0], Sport.NFL)
    assert game is not None
    assert game.status is GameStatus.SCHEDULED
    assert (game.home_score, game.away_score) == (None, None)


def test_in_progress_college_game_has_live_score_and_name_variants() -> None:
    game = parse_event(FIXTURES["cfb_live_and_scheduled"][0], Sport.CFB)
    assert game is not None
    assert game.status is GameStatus.IN_PROGRESS
    assert (game.home_score, game.away_score) == (10, 13)
    assert game.home.name == "Tennessee Volunteers"
    assert "Tennessee" in game.home.other_names


def test_neutral_site() -> None:
    neutral, campus = (parse_event(e, Sport.NCAAB) for e in FIXTURES["ncaab_neutral_final"])
    assert neutral is not None and neutral.neutral_site is True
    assert campus is not None and campus.neutral_site is False


@pytest.mark.parametrize(
    ("status_type", "expected"),
    [
        ({"name": "STATUS_POSTPONED", "state": "pre", "completed": False}, GameStatus.POSTPONED),
        ({"name": "STATUS_CANCELED", "state": "post", "completed": False}, GameStatus.CANCELED),
        ({"name": "STATUS_HALFTIME", "state": "in", "completed": False}, GameStatus.IN_PROGRESS),
        ({"name": "STATUS_WEIRD", "state": "post", "completed": False}, GameStatus.UNKNOWN),
    ],
)
def test_status_mapping(status_type: dict[str, Any], expected: GameStatus) -> None:
    event = json.loads(json.dumps(FIXTURES["nfl_final"][0]))
    event["competitions"][0]["status"]["type"] = status_type
    game = parse_event(event, Sport.NFL)
    assert game is not None and game.status is expected
    if expected is not GameStatus.IN_PROGRESS:
        assert game.home_score is None


def test_non_two_team_event_rejected() -> None:
    event = json.loads(json.dumps(FIXTURES["nfl_final"][0]))
    event["competitions"][0]["competitors"].pop()
    assert parse_event(event, Sport.NFL) is None


def _serve(routes: dict[tuple[str, str, str | None], list[dict[str, Any]]]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        key = (request.url.path, request.url.params["dates"], request.url.params.get("groups"))
        assert request.url.params["limit"] == "500"  # >500 silently truncates to 25
        # No custom User-Agent: ESPN rejects them (found in nfl-parlay-advisor).
        assert request.headers["user-agent"].startswith("python-httpx/")
        return httpx.Response(200, json={"events": routes.get(key, [])})

    return httpx.MockTransport(handle)


def test_fetch_iterates_days_and_groups_and_dedupes() -> None:
    path = "/apis/site/v2/sports/football/college-football/scoreboard"
    live, scheduled = FIXTURES["cfb_live_and_scheduled"]
    routes: dict[tuple[str, str, str | None], list[dict[str, Any]]] = {
        (path, "20260926", "80"): [live, scheduled],
        (path, "20260926", "81"): [scheduled],  # FBS-vs-FCS game listed in both groups
        (path, "20260927", "80"): [],
    }
    client = httpx.Client(base_url=BASE_URL, transport=_serve(routes))
    fetch = EspnScheduleProvider(client=client).fetch_games(
        Sport.CFB, date(2026, 9, 26), date(2026, 9, 27)
    )
    assert sorted(g.source_identifier for g in fetch.games) == ["401856704", "401858237"]
    assert fetch.skipped == {"duplicate_across_groups": 1}


def test_http_error_raises_provider_error() -> None:
    client = httpx.Client(
        base_url=BASE_URL, transport=httpx.MockTransport(lambda r: httpx.Response(400))
    )
    with pytest.raises(ProviderError, match="HTTP 400"):
        EspnScheduleProvider(client=client).fetch_games(
            Sport.NFL, date(2026, 9, 27), date(2026, 9, 27)
        )


def test_rejects_inverted_range() -> None:
    with pytest.raises(ValueError):
        EspnScheduleProvider().fetch_games(Sport.NFL, date(2026, 9, 28), date(2026, 9, 27))


def test_full_page_is_flagged_as_possibly_truncated() -> None:
    path = "/apis/site/v2/sports/basketball/nba/scoreboard"
    template = FIXTURES["nba_final"][0]
    page = [{**template, "id": str(n)} for n in range(500)]
    client = httpx.Client(base_url=BASE_URL, transport=_serve({(path, "20260415", None): page}))
    fetch = EspnScheduleProvider(client=client).fetch_games(
        Sport.NBA, date(2026, 4, 15), date(2026, 4, 15)
    )
    assert len(fetch.games) == 500
    assert fetch.skipped == {"possibly_truncated_day": 1}
