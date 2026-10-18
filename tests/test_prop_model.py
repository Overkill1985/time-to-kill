from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session, sessionmaker

from test_player_props import KICK, seed
from ttk.db.models import Game, NflPlayerGameStat, PropPrediction
from ttk.domain import GameStatus, Sport
from ttk.research import prop_model as pm
from ttk.services.forward_test import register
from ttk.services.prop_forward import prop_report, prop_versions, scored, snapshot_pulls


def test_distributions_give_sensible_over_chances() -> None:
    nb = pm.Dist("nbinom", k=8.0)
    assert 0.5 < nb.p_over(5.0, 4.5) < 0.6  # a mean of 5 receptions, line 4.5
    assert nb.p_over(5.0, 2.5) > nb.p_over(5.0, 4.5) > nb.p_over(5.0, 6.5)
    g = pm.Dist("gamma", a=2.0, p0=0.3, zero_slope=0.05)
    assert g.p_over(60.0, 59.5) < 0.5  # skewed: the median sits below the mean
    assert g.p_over(60.0, -0.5) == 1.0
    assert g.p_over(60.0, 20.5) > g.p_over(60.0, 80.5)
    assert g.zero(10.0) > g.zero(60.0)  # zeros get rarer as usage rises


def test_weighted_mean_uses_earlier_games_and_discounts_last_season() -> None:
    t0 = datetime(2025, 9, 7, tzinfo=UTC)
    games = [
        pm.PlayerGame(
            "p", i, 2025 if i < 2 else 2026, t0 + timedelta(weeks=i), {"player_receptions": v}
        )
        for i, v in enumerate([10.0, 10.0, 2.0, 2.0])
    ]
    means = pm.Means(games, pm.Params(half_life=1e9, carryover=0.25))  # no decay
    m = means.mean(games[:3], 2026, "player_receptions")
    assert m is not None and m[1] == 3
    # weights: this season's game 1.0, last season's 0.25 each -> (2 + 2.5 + 2.5) / 1.5
    assert m[0] == pytest.approx((2.0 + 0.25 * 10 + 0.25 * 10) / 1.5)
    assert means.mean(games[:2], 2026, "player_receptions") is None  # under MIN_GAMES


ARTIFACT = pm.freeze(
    pm.PropBacktest(
        pm.Params(4.0, 0.25),
        {
            m: (pm.Dist("nbinom", k=8.0) if m in pm.COUNTS else pm.Dist("gamma", a=4.0, p0=0.01))
            for m in pm.STATS
        },
    )
)


def register_model(s: Session) -> None:
    register(
        s,
        name="nfl-prop-usage",
        version="0.1.0",
        sport=Sport.NFL,
        algorithm="test",
        features=list(pm.STATS),
        artifact=ARTIFACT,
        training_window="2023-2024",
        validation_window="2025-2025",
        log_loss=None,
        brier=None,
    )


def test_forward_test_records_before_kickoff_and_scores_after(
    session_factory: sessionmaker[Session],
) -> None:
    game_id = seed(session_factory)  # Hurts: 12 games, 200..310 passing yards; 245.5 line
    with session_factory() as s:
        register_model(s)
        s.commit()
        assert [v.name for v in prop_versions(s)] == ["nfl-prop-usage"]
        assert snapshot_pulls(s, now=KICK + timedelta(minutes=1)) == []  # started: nothing
        lines = snapshot_pulls(s, now=KICK - timedelta(minutes=30))
        s.commit()
        assert lines == [f"prop model nfl-prop-usage 0.1.0: game {game_id} 1 h: 1 props recorded"]
        p = s.scalars(select(PropPrediction)).one()
        assert (p.player, p.market, p.line, p.books) == ("Jalen Hurts", "player_pass_yds", 245.5, 3)
        assert p.mean > 260 and p.p_over > 0.5  # his recent games run 270-310
        assert p.market_over == pytest.approx(0.5, abs=0.01)
        assert snapshot_pulls(s, now=KICK - timedelta(minutes=29)) == []  # once per pull
        with pytest.raises(DatabaseError, match="append-only"):
            p.p_over = 0.1
            s.flush()
        s.rollback()
    with session_factory() as s:  # final, Hurts throws 270: over
        game = s.get_one(Game, game_id)
        game.status, game.home_score, game.away_score = GameStatus.FINAL, 27, 20
        assert scored(s, prop_versions(s)[0].id) == []  # no stats yet
        s.add(
            NflPlayerGameStat(
                game_id=game_id,
                team_id=game.home_team_id,
                player_id="00-0036389",
                player_name="Jalen Hurts",
                position="QB",
                completions=20,
                attempts=30,
                passing_yards=270,
                passing_tds=2,
                interceptions=0,
                carries=4,
                rushing_yards=15,
                rushing_tds=0,
                targets=0,
                receptions=0,
                receiving_yards=0,
                receiving_tds=0,
            )
        )
        s.commit()
        report = prop_report(s, prop_versions(s)[0])
    assert (report.recorded, report.decided) == (1, 1)
    overall = report.scores[0]
    assert overall.decided == 1 and overall.z is None  # z needs 100 decided props
    assert overall.paired_diff is not None and overall.paired_diff < 0  # the model leaned over
