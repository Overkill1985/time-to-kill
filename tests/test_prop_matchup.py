from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from test_player_props import KICK, seed
from ttk.db.models import PropPrediction
from ttk.domain import Sport
from ttk.research import prop_matchup as mu
from ttk.research import prop_model as pm
from ttk.services.forward_test import register
from ttk.services.prop_forward import prop_versions, snapshot_pulls

T0 = datetime(2025, 9, 7, tzinfo=UTC)
RUSH = "player_rush_yds"


def rb_game(i: int, opponent: int, yards: float, player: str = "rb1") -> pm.PlayerGame:
    values = dict.fromkeys(pm.STATS, 0.0)
    values[RUSH] = yards
    return pm.PlayerGame(player, i, 2025, T0 + timedelta(weeks=i), values, 1, opponent, "RB")


def test_defense_factor_is_shrunk_and_uses_only_earlier_games() -> None:
    # Defense 9 allowed 150 rushing yards to RBs in each of its 3 games; league 100.
    games = [rb_game(i, 9, 150.0, player=f"rb{i}") for i in range(3)]
    d = mu.Defenses(games, mu.DefenseParams(half_life=1e9, shrink=2.0), {f"RB|{RUSH}": 100.0})
    after = T0 + timedelta(weeks=10)
    assert d.factor(9, "RB", RUSH, after, 2025) == pytest.approx((3 * 150 + 2 * 100) / 5 / 100)
    assert d.factor(9, "RB", RUSH, T0, 2025) == pytest.approx(1.0)  # nothing earlier
    assert d.factor(8, "RB", RUSH, after, 2025) == pytest.approx(1.0)  # unknown defense
    assert mu.defense_group("player_pass_yds", "RB") == "QB"
    assert mu.defense_group("player_receptions", "FB") == "RB"


def artifact(beta: float) -> dict[str, object]:
    return mu.freeze(
        mu.MatchupBacktest(
            pm.Params(1e9, 0.25),
            mu.DefenseParams(1e9, 2.0),
            dict.fromkeys(pm.STATS, beta),
            {
                m: (
                    pm.Dist("nbinom", k=8.0) if m in pm.COUNTS else pm.Dist("gamma", a=4.0, p0=0.01)
                )
                for m in pm.STATS
            },
            {f"RB|{RUSH}": 100.0},
        )
    )


def test_frozen_matchup_model_scales_usage_by_the_defense() -> None:
    # Our back averages 60 against other defenses; defense 9 has allowed 150 a game.
    games = [rb_game(i, 5, 60.0) for i in range(4)] + [
        rb_game(10 + i, 9, 150.0, player=f"other{i}") for i in range(3)
    ]
    before = T0 + timedelta(weeks=20)
    flat = mu.FrozenMatchupModel(artifact(0.0), games).view("rb1", RUSH, 59.5, before, 2025, 9)
    full = mu.FrozenMatchupModel(artifact(1.0), games).view("rb1", RUSH, 59.5, before, 2025, 9)
    assert flat is not None and full is not None
    assert flat.mean == pytest.approx(60.0) and full.defense_factor == pytest.approx(1.3)
    assert full.mean == pytest.approx(78.0) and full.p_over > flat.p_over


def test_v03_shrinks_a_short_history_toward_the_position_average() -> None:
    # Three games at 60 against defense 5; RBs average 100: with 2 pseudo-games, 76.
    games = [rb_game(i, 5, 60.0) for i in range(3)]
    a = artifact(0.0)
    a["shrink"], a["priors"] = {RUSH: 2.0}, {f"RB|{RUSH}": 100.0}
    view = mu.FrozenMatchupModel(a, games).view("rb1", RUSH, 59.5, T0 + timedelta(weeks=5), 2025, 9)
    assert view is not None and view.mean == pytest.approx(76.0)
    rebuilt = mu.from_artifact(a)
    assert rebuilt.shrink == {RUSH: 2.0} and mu.freeze(rebuilt)["beta"] == a["beta"]


def test_v04_scales_passing_to_a_full_game_starter() -> None:
    # A quarterback alternating finished starts (30 attempts, 300 yards) with games he
    # left early (10, 100): his history averages 200, his finished starts 300.
    games = []
    for i in range(20):
        full = i % 2 == 0
        values = dict.fromkeys(pm.STATS, 0.0)
        values["player_pass_attempts"] = 30.0 if full else 10.0
        values["player_pass_yds"] = 300.0 if full else 100.0
        values["player_pass_completions"] = 20.0 if full else 6.0
        games.append(pm.PlayerGame("qb1", i, 2025, T0 + timedelta(weeks=i), values, 1, 5, "QB"))
    base = mu.FrozenMatchupModel(artifact(0.0), games)
    factors = mu.starter_factors(games, base, (2025, 2025))
    assert factors["player_pass_yds"] > 1.3  # full starts beat the all-games average
    v04 = mu.FrozenMatchupModel(mu.starter_artifact(artifact(0.0), factors), games)
    before = T0 + timedelta(weeks=30)
    old = base.view("qb1", "player_pass_yds", 249.5, before, 2025, 9)
    new = v04.view("qb1", "player_pass_yds", 249.5, before, 2025, 9)
    assert old is not None and new is not None
    assert new.mean == pytest.approx(old.mean * factors["player_pass_yds"])
    assert new.p_over > old.p_over
    rush_old = base.view("qb1", RUSH, 0.5, before, 2025, 9)
    rush_new = v04.view("qb1", RUSH, 0.5, before, 2025, 9)
    assert rush_old == rush_new  # only the passing stats are scaled


def test_nfl_anchored_model_keeps_a_share_of_the_lean_on_receiving_props_only() -> None:
    from ttk.research import nba_props as nb

    games = []
    for i in range(6):
        g = rb_game(i, 5, 60.0)
        g.values["player_receptions"] = 4.0
        games.append(g)
    a = {
        "artifact_version": mu.ARTIFACT_VERSION,
        "kind": mu.ANCHORED_KIND,
        "base": artifact(0.0),
        "markets": list(mu.ANCHORED_MARKETS),
        "weight": 0.5,
    }
    model = mu.FrozenAnchoredMatchupModel(a, games)
    before = T0 + timedelta(weeks=10)
    assert set(model.dists) == set(mu.ANCHORED_MARKETS)  # only receiving props recorded
    base = model.base.view("rb1", "player_receptions", 3.5, before, 2025, 9)
    view = model.view("rb1", "player_receptions", 3.5, before, 2025, 9, 0.45)
    assert base is not None and view is not None
    assert view.p_over == pytest.approx(nb.anchored(0.45, base.p_over, 0.5))
    assert view.mean == pytest.approx(base.mean)
    assert model.view("rb1", "player_receptions", 3.5, before, 2025, 9, None) is None
    assert model.view("rb1", RUSH, 59.5, before, 2025, 9, 0.5) is None  # not its market


def test_matchup_model_is_forward_tested_and_shown_on_the_props_api(
    database_url: str, session_factory: sessionmaker[Session]
) -> None:
    from fastapi.testclient import TestClient

    from ttk.api.app import create_app
    from ttk.config import Settings

    seed(session_factory)
    with session_factory() as s:
        register(
            s,
            name="nfl-prop-matchup",
            version="0.2.0",
            sport=Sport.NFL,
            algorithm="test",
            features=list(pm.STATS),
            artifact=artifact(0.5),
            training_window="2023-2024",
            validation_window="2025-2025",
            log_loss=None,
            brier=None,
        )
        s.commit()
        assert [v.name for v in prop_versions(s)] == ["nfl-prop-matchup"]
        snapshot_pulls(s, now=KICK - timedelta(minutes=30))
        s.commit()
        p = s.scalars(select(PropPrediction)).one()
        assert (p.player, p.market, p.line) == ("Jalen Hurts", "player_pass_yds", 245.5)
    body = (
        TestClient(create_app(Settings(database_url=database_url)), base_url="http://localhost")
        .get("/api/props", params={"date": "2026-10-18"})
        .json()
    )
    assert body["model"] == "nfl-prop-matchup 0.2.0"
    row = body["props"][0]
    assert row["model_over"] == pytest.approx(p.p_over) and row["model_mean"] > 260
