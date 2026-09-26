import json
import random
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import GameStarter, QbGameStat, ReportedLine, TeamGameStat
from ttk.models.elo import EloGame, EloParams
from ttk.models.nfl_features import (
    DROPBACKS_PER_GAME,
    FeatureParams,
    QbGame,
    TeamGameEpa,
    compute_features,
)
from ttk.providers.nflverse import HistoricalGame, NflverseProvider, parse_row
from ttk.providers.nflverse_pbp import SeasonAggregates, aggregate_plays
from ttk.research.nfl_elo import FeatureInputs, Splits, backtest
from ttk.services.history_import import run_nfl_history_import
from ttk.services.pbp_import import store_season

T0 = datetime(2020, 9, 10, 17, tzinfo=UTC)


# --------------------------------------------------------------------------- aggregation


def play(**kw: str) -> dict[str, str]:
    base = {
        "game_id": "2025_01_ARI_NO",
        "posteam": "ARI",
        "play_type": "pass",
        "epa": "0.5",
        "success": "1",
        "qb_dropback": "1",
        "rush": "0",
        "two_point_attempt": "0",
        "id": "00-QB1",
        "passer_player_id": "00-QB1",
        "name": "A.Passer",
        "qb_epa": "0.5",
    }
    return {**base, **kw}


def test_aggregate_plays_filters_and_attribution() -> None:
    rows = [
        play(),  # pass: dropback by QB1
        play(
            play_type="run", qb_dropback="1", rush="1", passer_player_id="", epa="1.0", qb_epa="1.0"
        ),  # scramble: a dropback, attributed via `id`
        play(
            play_type="run", qb_dropback="0", rush="1", id="00-RB", epa="-0.2", success="0"
        ),  # designed run
        play(play_type="no_play", epa="-1.0"),  # penalty: not an offensive play
        play(two_point_attempt="1"),  # excluded
        play(epa="NA"),  # excluded
        play(
            posteam="NO",
            id="00-QB2",
            passer_player_id="00-QB2",
            epa="-0.3",
            qb_epa="-0.3",
            success="0",
        ),
    ]
    agg = aggregate_plays(rows, 2025)
    teams = {t.team_code: t for t in agg.teams}
    ari = teams["ARI"]
    assert (ari.plays, ari.dropbacks, ari.rushes) == (3, 2, 1)
    assert ari.epa_total == pytest.approx(1.3)
    assert ari.dropback_epa_total == pytest.approx(1.5)
    assert ari.successes == 2
    qbs = {q.player_id: q for q in agg.qbs}
    assert qbs["00-QB1"].dropbacks == 2 and qbs["00-QB1"].qb_epa_total == pytest.approx(1.5)
    assert qbs["00-QB2"].team_code == "NO"
    assert agg.skipped == {"two_point_attempt": 1, "missing_epa_or_team": 1}


# --------------------------------------------------------------------------- features


def g(gid: int, home: int, away: int, day: int, season: int = 2020) -> EloGame:
    return EloGame(gid, season, T0 + timedelta(days=day), home, away, False, 20, 17)


def stats(gid: int, team: int, epa_per_play: float, plays: int = 60) -> TeamGameEpa:
    return TeamGameEpa(gid, team, plays, epa_per_play * plays, 35)


def test_features_use_only_earlier_games() -> None:
    games = [g(1, 1, 2, 0), g(2, 1, 2, 7), g(3, 1, 2, 14)]
    base = [stats(1, 1, 0.2), stats(1, 2, -0.1), stats(2, 1, 0.2), stats(2, 2, -0.1)]
    f = compute_features(games, base, [], {})
    assert f[1].epa_net_diff_pts == 0.0 and f[1].home_team_games == 0
    assert f[2].epa_net_diff_pts > 0  # team 1 outplayed team 2 in game 1
    # Changing game 2's own stats cannot change game 2's features, only game 3's.
    changed = [s if s.game_id != 2 else replace(s, epa_total=-50.0) for s in base]
    f2 = compute_features(games, changed, [], {})
    assert f2[2] == f[2]
    assert f2[3].epa_net_diff_pts < f[3].epa_net_diff_pts


def test_backup_qb_start_lowers_team() -> None:
    games = [g(i, 1, 2, 7 * i) for i in range(1, 6)]
    team_stats = [s for i in range(1, 6) for s in (stats(i, 1, 0.1), stats(i, 2, 0.1))]
    # Team 1's starter QB1 is excellent; team 2's QB2 is average.
    qbs = [QbGame(i, 1, "QB1", 35, 0.3 * 35) for i in range(1, 5)]
    qbs += [QbGame(i, 2, "QB2", 35, 0.0) for i in range(1, 5)]
    starters = {(i, 1): "QB1" for i in range(1, 5)} | {(i, 2): "QB2" for i in range(1, 6)}
    starters[(5, 1)] = "BACKUP"  # never seen: rated at the prior (-0.10 EPA/dropback)
    f = compute_features(games, team_stats, qbs, starters, FeatureParams())
    assert f[4].qb_change_diff_pts == pytest.approx(0.0, abs=0.5)  # usual starters
    assert f[5].qb_change_diff_pts < -3.0  # backup for the good team
    assert f[5].home_qb_rating == pytest.approx(-0.10)
    # scale: roughly (backup - QB1 level) x dropbacks
    assert f[5].qb_change_diff_pts > (-0.10 - 0.3) * DROPBACKS_PER_GAME


def test_missing_starter_means_no_adjustment() -> None:
    games = [g(1, 1, 2, 0), g(2, 1, 2, 7)]
    f = compute_features(games, [], [QbGame(1, 1, "QB1", 30, 9.0)], {})
    assert f[2].qb_change_diff_pts == 0.0 and f[2].home_qb_rating is None


def test_new_season_shrinks_history() -> None:
    games = [g(1, 1, 2, 0, 2020), g(2, 1, 2, 7, 2020), g(3, 1, 2, 400, 2021)]
    st = [stats(1, 1, 0.3), stats(1, 2, -0.3), stats(2, 1, 0.3), stats(2, 2, -0.3)]
    keep = compute_features(games, st, [], {}, FeatureParams(season_carryover=1.0))
    shrink = compute_features(games, st, [], {}, FeatureParams(season_carryover=0.2))
    assert 0 < shrink[3].epa_net_diff_pts < keep[3].epa_net_diff_pts


# --------------------------------------------------------------------------- import


FIXTURES = Path(__file__).parent / "fixtures"
ROWS: dict[str, dict[str, str]] = json.loads(
    (FIXTURES / "nflverse_games_sample.json").read_text("utf-8")
)


class FakeNflverse(NflverseProvider):
    def __init__(self, games: list[HistoricalGame]) -> None:
        self.games = games

    def fetch_history(
        self, *, first_season: int = 1999, last_season: int | None = None
    ) -> tuple[list[HistoricalGame], dict[str, int]]:
        return self.games, {}


def test_history_import_stores_starters(session_factory: sessionmaker[Session]) -> None:
    run_nfl_history_import(
        session_factory, FakeNflverse([parse_row(ROWS["reg_2025"])]), first_season=2025
    )
    with session_factory() as s:
        starters = {row.player_name for row in s.scalars(select(GameStarter))}
    assert starters == {ROWS["reg_2025"]["home_qb_name"], ROWS["reg_2025"]["away_qb_name"]}


def test_pbp_store_is_idempotent_and_skips_unknown_games(
    session_factory: sessionmaker[Session],
) -> None:
    run_nfl_history_import(
        session_factory, FakeNflverse([parse_row(ROWS["reg_2025"])]), first_season=2025
    )
    rows = [
        play(game_id="2025_01_TB_ATL", posteam="TB"),
        play(game_id="2025_01_TB_ATL", posteam="ATL", id="00-QB2", passer_player_id="00-QB2"),
        play(game_id="2099_01_XX_YY", posteam="TB"),  # not imported: skipped, counted
    ]
    agg: SeasonAggregates = aggregate_plays(rows, 2025)
    for _ in range(2):
        counts: Counter[str] = Counter()
        with session_factory() as s:
            store_season(s, agg, counts)
            s.commit()
    assert counts["unresolved_team_game"] == 1
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(TeamGameStat)) == 2
        assert s.scalar(select(func.count()).select_from(QbGameStat)) == 2


# --------------------------------------------------------------------------- backtest


def test_backtest_includes_feature_models() -> None:
    rng = random.Random(5)
    strength = {t: (t - 4.5) * 2.0 for t in range(1, 9)}
    games, lines, team_stats, gid = [], {}, [], 0
    for season in range(2000, 2006):
        for week in range(16):
            teams = list(strength)
            rng.shuffle(teams)
            for home, away in zip(teams[::2], teams[1::2], strict=True):
                gid += 1
                true_margin = strength[home] - strength[away] + 2.0
                margin = round(rng.gauss(true_margin, 13.0))
                day = 365 * (season - 2000) + 7 * week
                games.append(
                    EloGame(
                        gid, season, T0 + timedelta(days=day), home, away, False, 20 + margin, 20
                    )
                )
                lines[gid] = ReportedLine(
                    game_id=gid,
                    provider="nflverse",
                    home_spread=-round(true_margin * 2) / 2,
                    home_spread_odds=-110,
                    away_spread_odds=-110,
                )
                for team in (home, away):
                    team_stats.append(stats(gid, team, strength[team] * 0.02 + rng.gauss(0, 0.1)))
    inputs = FeatureInputs(team_stats, [], {})
    splits = Splits(
        burn_in=(2000, 2000), train=(2001, 2003), validate=(2004, 2004), test=(2005, 2005)
    )
    report = backtest(games, lines, splits=splits, params=EloParams(), inputs=inputs)
    names = [c.name for c in report.validate.spread_candidates]
    assert names[-2:] == ["features_key_numbers", "market_anchored_features"]
    assert set(report.validate.margin_rmse) == {"elo", "features"}
    assert report.models.features is not None
    assert report.models.features.coefs["epa_net_diff_pts"] > 0  # EPA carries signal here
    assert "game_features" not in report.to_dict()["models"]  # type: ignore[operator]
