from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import (
    Game,
    PlayerGameStat,
    PropPrediction,
    PropPull,
    PropQuote,
    Sportsbook,
    Team,
)
from ttk.domain import GameStatus, Sport
from ttk.research import nba_props as nb
from ttk.services.forward_test import register
from ttk.services.prop_forward import prop_report, prop_versions, snapshot_pulls

T0 = datetime(2025, 11, 1, 23, tzinfo=UTC)
PTS = "player_points"


def pg(player: str, i: int, opponent: int, points: float, team: int = 1) -> nb.PlayerGame:
    values = dict.fromkeys(nb.STATS, 0.0)
    values[PTS] = points
    return nb.PlayerGame(player, i, 2026, T0 + timedelta(days=i), team, opponent, 30.0, values)


def artifact(shrink: float, beta: float) -> dict[str, object]:
    return nb.freeze(
        nb.NbaBacktest(
            nb.Usage(1e9, 1.0),
            nb.Defense(1e9, 2.0),
            {"rate|player_points": 0.5, "team|player_points": 100.0},
            {m: nb.StatFit(m, shrink, beta, nb.Dist(2.0)) for m in nb.STATS},
        )
    )


def test_usage_is_shrunk_to_role_and_scaled_by_what_the_opponent_allowed() -> None:
    # Our player: 10 games of 20 points in 30 minutes (role prior 0.5/min x 30 = 15).
    # Team 9 has allowed 150 points in each of its 3 games (league 100 a team-game).
    games = [pg("p1", i, 5, 20.0) for i in range(10)] + [
        pg(f"o{i}", 20 + i, 9, 150.0, team=2) for i in range(3)
    ]
    before = T0 + timedelta(days=40)
    flat = nb.FrozenNbaPropModel(artifact(0.0, 0.0), games).view("p1", PTS, 22.5, before, 2026, 9)
    full = nb.FrozenNbaPropModel(artifact(5.0, 1.0), games).view("p1", PTS, 22.5, before, 2026, 9)
    assert flat is not None and full is not None
    assert flat.mean == pytest.approx(20.0)
    assert full.usage_mean == pytest.approx((10 * 20 + 5 * 15) / 15)
    assert full.defense_factor == pytest.approx((3 * 150 + 2 * 100) / 5 / 100)
    assert full.mean == pytest.approx(full.usage_mean * 1.3) and full.p_over > flat.p_over
    # An unknown opponent is average; too few games is no view.
    unknown = nb.FrozenNbaPropModel(artifact(0.0, 1.0), games)
    assert unknown.view("p1", PTS, 22.5, before, 2026, 77).defense_factor == 1.0  # type: ignore[union-attr]
    assert unknown.view("p1", PTS, 22.5, T0 + timedelta(days=3), 2026, 9) is None


def test_dist_variance_is_phi_times_the_mean() -> None:
    d = nb.Dist(3.0)
    n, p = d._np(10.0)
    assert n * (1 - p) / p == pytest.approx(10.0)  # mean
    assert n * (1 - p) / p**2 == pytest.approx(30.0)  # variance


TIP = datetime(2026, 10, 20, 23, tzinfo=UTC)


def seed(sf: sessionmaker[Session]) -> None:
    """76ers @ Knicks: Brunson's last 10 games (20..29 points); a 1 h pull with three
    books on his points line and on a teammate who then doesn't play."""
    with sf() as s:
        nyk = Team(sport="NBA", name="New York Knicks", abbreviation="NY", espn_id="18")
        phi = Team(sport="NBA", name="Philadelphia 76ers", abbreviation="PHI", espn_id="20")
        books = [Sportsbook(key=k, name=k) for k in ("draftkings", "fanduel", "betmgm")]
        s.add_all([nyk, phi, *books])
        s.flush()

        def line(game: Game, player: str, name: str, points: int, played: bool = True) -> None:
            s.add(
                PlayerGameStat(
                    game_id=game.id,
                    team_id=nyk.id,
                    player_id=player,
                    player_name=name,
                    provider="espn",
                    starter=True,
                    played=played,
                    minutes=34.0 if played else None,
                    points=points,
                    oreb=1,
                    dreb=3,
                    ast=6,
                    stl=1,
                    blk=0,
                    fg3m=2,
                )
            )

        for i in range(10):
            past = Game(
                sport=Sport.NBA,
                season=2026,
                season_type="REG",
                home_team_id=nyk.id,
                away_team_id=phi.id,
                commence_time=TIP - timedelta(days=30 - i),
                status=GameStatus.FINAL,
            )
            s.add(past)
            s.flush()
            line(past, "3934672", "Jalen Brunson", 20 + i)
            line(past, "4395724", "Josh Hart", 10)
        game = Game(
            sport=Sport.NBA,
            season=2027,
            season_type="REG",
            home_team_id=nyk.id,
            away_team_id=phi.id,
            commence_time=TIP,
        )
        s.add(game)
        s.flush()
        pull = PropPull(
            game_id=game.id,
            provider="propline",
            event_id="e1",
            horizon_hours=1,
            pulled_at=TIP - timedelta(hours=1),
            books=3,
            quotes=12,
        )
        s.add(pull)
        s.flush()
        for player, point in (("Jalen Brunson (NY)", 26.5), ("Josh Hart (NY)", 9.5)):
            for b in books:
                for sel in ("Over", "Under"):
                    s.add(
                        PropQuote(
                            pull_id=pull.id,
                            sportsbook_id=b.id,
                            market=PTS,
                            player=player,
                            team_side="HOME",
                            selection=sel,
                            point=point,
                            american_odds=-110,
                        )
                    )
        s.commit()


def test_nba_model_is_forward_tested_scored_and_shown(
    database_url: str, session_factory: sessionmaker[Session]
) -> None:
    from fastapi.testclient import TestClient

    from ttk.api.app import create_app
    from ttk.config import Settings

    seed(session_factory)
    with session_factory() as s:
        register(
            s,
            name="nba-prop-usage",
            version="0.1.0",
            sport=Sport.NBA,
            algorithm="test",
            features=list(nb.STATS),
            artifact=artifact(0.0, 0.5),
            training_window="2023-2025",
            validation_window="2026-2026",
            log_loss=None,
            brier=None,
        )
        s.commit()
        assert [v.name for v in prop_versions(s)] == ["nba-prop-usage"]
        snapshot_pulls(s, now=TIP - timedelta(minutes=30))
        s.commit()
        recorded = {p.player: p for p in s.scalars(select(PropPrediction))}
        assert set(recorded) == {"Jalen Brunson", "Josh Hart"}
        assert recorded["Jalen Brunson"].line == 26.5

    body = (
        TestClient(create_app(Settings(database_url=database_url)), base_url="http://localhost")
        .get("/api/props", params={"date": "2026-10-20", "sport": "NBA"})
        .json()
    )
    assert body["model"] == "nba-prop-usage 0.1.0"
    brunson = next(r for r in body["props"] if r["player"] == "Jalen Brunson")
    assert brunson["model_over"] == pytest.approx(recorded["Jalen Brunson"].p_over)

    # Final: Brunson scores 30; Hart doesn't play (void, left out).
    with session_factory() as s:
        game = s.scalars(select(Game).where(Game.season == 2027)).one()
        game.status = GameStatus.FINAL
        nyk = s.scalars(select(Team).where(Team.name == "New York Knicks")).one()
        for pid, name, pts, played in (
            ("3934672", "Jalen Brunson", 30, True),
            ("4395724", "Josh Hart", 0, False),
        ):
            s.add(
                PlayerGameStat(
                    game_id=game.id,
                    team_id=nyk.id,
                    player_id=pid,
                    player_name=name,
                    provider="espn",
                    starter=played,
                    played=played,
                    points=pts,
                )
            )
        s.commit()
        report = prop_report(s, prop_versions(s)[0])
        assert (report.recorded, report.decided) == (2, 1)


def test_anchored_model_keeps_a_fitted_share_of_the_lean() -> None:
    import numpy as np

    rng = np.random.default_rng(7)
    rows = []
    for _ in range(4000):
        market = float(rng.uniform(0.3, 0.7))
        model_p = float(np.clip(market + rng.normal(0, 0.15), 0.02, 0.98))
        truth = nb.anchored(market, model_p, 0.25)  # the lean is worth a quarter
        rows.append((market, model_p, bool(rng.random() < truth)))
    weight, se = nb.fit_weight(rows)
    assert abs(weight - 0.25) < 3 * se and se < 0.1
    assert nb.anchored(0.5, 0.8, 0.0) == pytest.approx(0.5)  # weight 0: the market
    assert nb.anchored(0.5, 0.8, 1.0) == pytest.approx(0.8)  # weight 1: the model

    games = [pg("p1", i, 5, 20.0) for i in range(10)]
    a = {
        "artifact_version": nb.ARTIFACT_VERSION,
        "kind": nb.ANCHORED_KIND,
        "base": artifact(0.0, 0.0),
        "weight": 0.25,
    }
    model = nb.FrozenAnchoredNbaPropModel(a, games)
    before = T0 + timedelta(days=40)
    base = model.base.view("p1", PTS, 17.5, before, 2026, 5)
    view = model.view("p1", PTS, 17.5, before, 2026, 5, 0.5)
    assert base is not None and view is not None
    assert view.p_over == pytest.approx(nb.anchored(0.5, base.p_over, 0.25))
    assert model.view("p1", PTS, 17.5, before, 2026, 5, None) is None  # needs the market
