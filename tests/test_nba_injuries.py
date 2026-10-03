from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from ttk.models.elo import EloGame
from ttk.providers.espn_boxscore import EspnBoxscores
from ttk.research.nba_injuries import (
    STATUS_PRIORS,
    InjuryTimeline,
    Report,
    injury_features,
)
from ttk.services.collector import refresh_boxscores

T0 = datetime(2026, 10, 20, 23, 0, tzinfo=UTC)


def test_timeline_reports_what_was_known_then() -> None:
    reports = [
        Report("p1", "Day-To-Day", False, T0 - timedelta(days=3), team_id=1),
        Report("p1", "Out", False, T0 - timedelta(days=1), team_id=1),
        Report("p1", "Out", True, T0 + timedelta(days=2), team_id=1),  # cleared later
        Report("p2", "Out", False, T0 - timedelta(days=1), team_id=9),  # moved teams
    ]
    t = InjuryTimeline.build(reports)
    assert t.status_at("p1", T0 - timedelta(days=4)) is None  # not listed yet
    assert t.status_at("p1", T0 - timedelta(days=2)) == "Day-To-Day"
    assert t.status_at("p1", T0) == "Out"
    assert t.status_at("p1", T0 + timedelta(days=3)) is None  # off the list
    assert t.status_at("p2", T0, team_id=1) is None  # listed with another team
    assert t.status_at("p2", T0, team_id=9) == "Out"
    assert t.first_observed == T0 - timedelta(days=3)


def game(gid: int, day: int) -> EloGame:
    return EloGame(gid, 2027, T0 + timedelta(days=day), 1, 2, False, 110, 100)


def test_injury_features_use_learned_sit_rates() -> None:
    # Team 1's star (weight 1.0, value 20) is Day-To-Day before both games.
    snapshots = {
        (gid, team): ({"star": (1.0, 20.0)} if team == 1 else {"other": (1.0, 10.0)})
        for gid in (1, 2, 3)
        for team in (1, 2)
    }
    reports = [Report("star", "Day-To-Day", False, T0 - timedelta(days=5), team_id=1)]
    played = {1: {"other"}, 2: {"other"}}  # he sat both finished games
    feats, rates = injury_features(
        [game(1, 0), game(2, 2), game(3, 4)],
        snapshots,
        InjuryTimeline.build(reports),
        played,
        horizons=(24,),
    )
    prior_sat, prior_played = STATUS_PRIORS["Day-To-Day"]
    q0 = prior_sat / (prior_sat + prior_played)
    q1 = (1 + prior_sat) / (1 + prior_sat + prior_played)
    q2 = (2 + prior_sat) / (2 + prior_sat + prior_played)
    assert feats[1]["injury_missing_diff_24h"] == pytest.approx(q0 * 20.0)  # prior only
    assert feats[2]["injury_missing_diff_24h"] == pytest.approx(q1 * 20.0)  # learned from game 1
    assert feats[3]["injury_missing_diff_24h"] == pytest.approx(q2 * 20.0)  # game 3 unplayed
    assert rates.observed(24, "Day-To-Day") == (2, 2)


def test_no_features_before_tracking_began() -> None:
    reports = [Report("star", "Out", False, T0 + timedelta(days=1), team_id=1)]
    snapshots = {(1, 1): {"star": (1.0, 20.0)}, (1, 2): {}}
    feats, _ = injury_features(
        [game(1, 0)], snapshots, InjuryTimeline.build(reports), {}, horizons=(24, 1)
    )
    # 24 h and 1 h before tip are both before the first report: unknown, not healthy.
    assert feats == {}


def test_collector_imports_box_scores_on_its_cadence(
    session_factory: sessionmaker[Session],
) -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={})

    source = EspnBoxscores(client=httpx.Client(transport=httpx.MockTransport(handle)))
    now = datetime(2026, 11, 1, tzinfo=UTC)
    first = refresh_boxscores(session_factory, source, now=now)
    assert [(r.sport, r.status) for r in first] == [("NBA", "SUCCESS")]
    from sqlalchemy import func, select

    from ttk.db.models import IngestionRun

    with session_factory() as session:
        last = session.scalar(select(func.max(IngestionRun.finished_at)))
    assert last is not None
    assert refresh_boxscores(session_factory, source, now=last + timedelta(hours=1)) == []
    assert len(refresh_boxscores(session_factory, source, now=last + timedelta(hours=7))) == 1
