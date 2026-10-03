import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import Team, TeamSeasonFeature
from ttk.domain import Sport
from ttk.models.elo import EloGame
from ttk.providers.base import ProviderError
from ttk.providers.cfbd import (
    CfbdClient,
    Transfer,
    parse_coaches,
    parse_portal,
    parse_preseason_poll,
    parse_recruiting,
    parse_returning,
    parse_talent,
    poll_release,
    preseason_cutoff,
)
from ttk.research.cfb_preseason import SeasonFact, preseason_features
from ttk.services.cfbd_import import portal_facts, store_facts

# Real CollegeFootballData responses for 2023 (trimmed), captured 2026-10-03.
FIX = json.loads((Path(__file__).parent / "fixtures" / "cfbd_2023.json").read_text("utf-8"))


def test_parse_annual_composites() -> None:
    talent = {f.team: f for f in parse_talent(FIX["talent_2023"], 2023)}
    assert talent["Alabama"].value == pytest.approx(1015.43)
    assert talent["Ohio"].known_at == preseason_cutoff(2023)
    returning = parse_returning(FIX["returning_2023"], 2023)
    assert {f.name for f in returning} == {
        "returning_ppa_pct",
        "returning_passing_ppa_pct",
        "returning_usage",
    }
    assert all(0 <= f.value <= 1.5 for f in returning if f.name.endswith("pct"))
    recruiting = parse_recruiting(FIX["recruiting_2023"], 2023)
    assert all(f.known_at == datetime(2023, 2, 15, tzinfo=UTC) for f in recruiting)


def test_new_head_coach_is_dated_by_the_hire() -> None:
    coaches = {f.team: f for f in parse_coaches(FIX["coaches_2023"], 2023)}
    # Deion Sanders, hired by Colorado 2022-12-03: new for 2023, known from that date.
    assert coaches["Colorado"].value == 1.0
    assert coaches["Colorado"].known_at == datetime(2022, 12, 3, tzinfo=UTC)
    assert coaches["Alabama"].value == 0.0 and coaches["Alabama"].known_at is None
    assert coaches["Hawai'i"].value == 0.0  # Timmy Chang, hired January 2022


def test_preseason_poll_is_regular_week_1() -> None:
    poll = {f.team: f for f in parse_preseason_poll(FIX["rankings_2023"], 2023)}
    assert len(poll) == 25
    assert poll["Georgia"].value > poll["Michigan"].value > poll["Alabama"].value
    assert poll["Georgia"].known_at == poll_release(2023)


def test_portal_counts_only_transfers_dated_before_the_season() -> None:
    entries = parse_portal(FIX["portal_2023"])
    assert entries and all(e.season == 2023 for e in entries)
    late = Transfer(2023, "Alabama", "Georgia", 0.9, 4, datetime(2023, 9, 1, tzinfo=UTC))
    undated = Transfer(2023, "Alabama", "Georgia", 0.9, 4, None)
    early = Transfer(2023, "Alabama", "Georgia", 0.9, 4, datetime(2023, 1, 5, tzinfo=UTC))
    facts = {(f.team, f.name): f.value for f in portal_facts([late, undated, early], 2023)}
    assert facts == {
        ("Georgia", "portal_in_count"): 1.0,
        ("Georgia", "portal_in_rating"): 0.9,
        ("Alabama", "portal_out_count"): 1.0,
        ("Alabama", "portal_out_rating"): 0.9,
    }


def test_store_facts_maps_by_espn_id_and_counts_unmatched(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        session.add_all(
            [
                Team(sport=Sport.CFB, name="Alabama Crimson Tide", espn_id="333"),
                Team(sport=Sport.CFB, name="Colorado Buffaloes", espn_id="38"),
            ]
        )
        session.flush()
        facts = parse_talent(FIX["talent_2023"], 2023) + parse_coaches(FIX["coaches_2023"], 2023)
        stats = store_facts(session, 2023, facts, {"Alabama": 333, "Colorado": 38})
        session.commit()
        stored = {
            (name, value)
            for name, value in session.execute(
                select(Team.name, TeamSeasonFeature.name).join(
                    TeamSeasonFeature, TeamSeasonFeature.team_id == Team.id
                )
            )
        }
        assert ("Colorado Buffaloes", "new_head_coach") in stored
        assert ("Alabama Crimson Tide", "talent") in stored
        assert stats["unmatched_school"] == 10  # the other schools: counted, not guessed
        again = store_facts(session, 2023, facts, {"Alabama": 333, "Colorado": 38})
        session.commit()
        assert again["talent"] == 2  # re-import replaces the season's rows
        assert len(session.scalars(select(TeamSeasonFeature)).all()) == 4


T0 = datetime(2023, 9, 2, 17, 0, tzinfo=UTC)


def game(gid: int, week: int, home: int = 1, away: int = 2) -> EloGame:
    return EloGame(gid, 2023, T0 + timedelta(weeks=week), home, away, False, 30, 20)


def fact(team: int, name: str, value: float, known: datetime | None) -> SeasonFact:
    return SeasonFact(2023, team, name, value, known)


def test_preseason_features_known_at_kickoff_and_fading() -> None:
    cutoff = preseason_cutoff(2023)
    facts = [
        fact(1, "talent", 900.0, cutoff),
        fact(2, "talent", 700.0, cutoff),
        fact(3, "talent", 800.0, cutoff),
        fact(1, "new_head_coach", 1.0, datetime(2022, 12, 3, tzinfo=UTC)),
        # announced after the first game: unknown for it, known for later games
        fact(2, "preseason_ap_points", 1000.0, T0 + timedelta(days=1)),
        fact(1, "returning_ppa_pct", 0.5, None),  # undated: never used
    ]
    feats = preseason_features([game(1, 0), game(2, 1), game(3, 4)], facts)
    talent_z = (900 - 800) / ((((900 - 800) ** 2 + (700 - 800) ** 2) / 3) ** 0.5)
    assert feats[1]["talent_diff"] == pytest.approx(talent_z * 2)  # all-season, no fade
    assert feats[3]["talent_diff"] == pytest.approx(talent_z * 2)
    assert feats[1]["new_coach_diff"] == pytest.approx(1.0)  # first game: full weight
    assert feats[2]["new_coach_diff"] == pytest.approx(0.5 ** (1 / 4))  # one game in
    assert feats[3]["new_coach_diff"] == pytest.approx(0.5 ** (2 / 4))
    assert feats[1]["ap_diff"] == 0.0  # not yet public
    assert feats[2]["ap_diff"] == pytest.approx(-1.0 * 0.5 ** (1 / 4))
    assert all(f["returning_diff"] == 0.0 for f in feats.values())


def test_recruiting_is_the_mean_of_four_classes() -> None:
    facts = [
        SeasonFact(y, t, "recruiting_points", pts, datetime(y, 2, 15, tzinfo=UTC))
        for t, pts in ((1, 300.0), (2, 200.0), (3, 100.0))
        for y in range(2020, 2024)
    ] + [SeasonFact(2019, 1, "recruiting_points", 10_000.0, datetime(2019, 2, 15, tzinfo=UTC))]
    feats = preseason_features([game(1, 0)], facts)
    # 2019 is five classes back and doesn't count; teams 1 vs 2 differ by 100 points.
    sd = ((100**2 + 0 + 100**2) / 3) ** 0.5
    assert feats[1]["recruiting_diff"] == pytest.approx(100 / sd)


def test_client_retries_throttling_and_reads_the_quota() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["authorization"])
        if len(calls) == 1:
            return httpx.Response(429)
        return httpx.Response(
            200, json=FIX["talent_2023"], headers={"x-calllimit-remaining": "900"}
        )

    client = httpx.Client(base_url="https://x.test", transport=httpx.MockTransport(handle))
    cfbd = CfbdClient("k", client=client, backoff=0, delay=0)
    assert len(cfbd.get("/talent", year=2023)) == 7
    assert calls == ["Bearer k", "Bearer k"] and cfbd.calls_remaining == 900
    denied = CfbdClient(
        "bad",
        client=httpx.Client(
            base_url="https://x.test", transport=httpx.MockTransport(lambda r: httpx.Response(401))
        ),
        backoff=0,
        delay=0,
    )
    with pytest.raises(ProviderError, match="unauthorized"):
        denied.get("/talent", year=2023)


def test_parse_advanced_game_stats() -> None:
    from ttk.providers.cfbd import parse_advanced

    rows = {r.team: r for r in parse_advanced(FIX["advanced_2023_one_game"])}
    jsu = rows["Jacksonville State"]
    # Real game 401520145 (= ESPN's event id): Jacksonville State vs UTEP, 2023 week 1.
    assert (jsu.event_id, jsu.plays) == ("401520145", 64)
    assert jsu.total_ppa == pytest.approx(0.6249, abs=1e-4)
    assert (jsu.pass_plays, jsu.rush_plays) == (21, 43)  # derived: totalPPA / ppa
    assert jsu.pass_plays + jsu.rush_plays == jsu.plays
    assert rows["UTEP"].plays == 70


def test_opponent_adjustment_discounts_soft_schedules() -> None:
    """Teams 1 and 2 post identical offensive EPA, but team 1 did it against team 3,
    whose defense had been allowing a lot; adjusted, team 1 rates lower."""
    from ttk.models.nfl_features import FeatureParams, TeamGameEpa, compute_features

    t0 = datetime(2023, 9, 2, tzinfo=UTC)
    g = [
        EloGame(1, 2023, t0, 4, 3, False, 50, 0),  # team 3's defense gets shredded
        EloGame(2, 2023, t0 + timedelta(days=1), 4, 3, False, 50, 0),
        EloGame(3, 2023, t0 + timedelta(days=7), 1, 3, False, 30, 20),  # 1 vs soft 3
        EloGame(4, 2023, t0 + timedelta(days=7), 2, 5, False, 30, 20),  # 2 vs neutral 5
        EloGame(5, 2023, t0 + timedelta(days=14), 1, 2, False, 0, 0),  # to be predicted
    ]
    stats = [
        TeamGameEpa(1, 4, 70, 35.0, 30),
        TeamGameEpa(1, 3, 70, 0.0, 30),
        TeamGameEpa(2, 4, 70, 35.0, 30),
        TeamGameEpa(2, 3, 70, 0.0, 30),
        TeamGameEpa(3, 1, 70, 14.0, 30),
        TeamGameEpa(3, 3, 70, 0.0, 30),
        TeamGameEpa(4, 2, 70, 14.0, 30),
        TeamGameEpa(4, 5, 70, 0.0, 30),
    ]
    raw = compute_features(g, stats, [], {}, FeatureParams())
    adj = compute_features(g, stats, [], {}, FeatureParams(opponent_adjust=True))
    assert raw[5].epa_net_diff_pts == pytest.approx(0.0, abs=1e-9)  # identical raw numbers
    assert adj[5].epa_net_diff_pts < -1.0  # team 1's came against a soft defense
