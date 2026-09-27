from datetime import UTC, datetime, timedelta

import pytest

from ttk.models.elo import EloGame
from ttk.research.nba_lineups import (
    PRIOR_GAMES,
    REPLACEMENT_GAME_SCORE,
    BoxRow,
    availability,
    hollinger_game_score,
)

T0 = datetime(2024, 1, 1, tzinfo=UTC)


def test_hollinger_game_score() -> None:
    # Jarrett Allen, CLE at IND 2024-02-10: 10 pts, 4-6 FG, 2-3 FT, 2 OREB, 5 DREB,
    # 0 AST, 1 STL, 2 BLK, 1 TO, 4 PF.
    gmsc = hollinger_game_score(
        points=10, fgm=4, fga=6, ftm=2, fta=3, oreb=2, dreb=5, ast=0, stl=1, blk=2, tov=1, pf=4
    )
    assert gmsc == pytest.approx(10 + 1.6 - 4.2 - 0.4 + 1.4 + 1.5 + 1 + 0 + 1.4 - 1.6 - 1)


def game(gid: int, day: int, home: int = 1, away: int = 2, season: int = 2024) -> EloGame:
    return EloGame(gid, season, T0 + timedelta(days=day), home, away, False, 100, 90)


def rows(team: int, played: dict[str, float], absent: tuple[str, ...] = ()) -> list[BoxRow]:
    out = [BoxRow(p, team, True, gs) for p, gs in played.items()]
    # Healthy scratches are listed as not played; injured players are simply absent.
    out += [BoxRow(p, team, False, 0.0) for p in absent]
    return out


def test_missing_players_are_valued_from_earlier_games_only() -> None:
    full1, full2 = {"star": 30.0, "bench": 5.0}, {"c": 10.0, "d": 10.0}
    games = [game(i, i) for i in range(1, 8)]
    box = {i: rows(1, full1) + rows(2, full2) for i in range(1, 7)}
    # Game 7: the star sits (listed DNP); nothing about game 7 may inform game 7.
    box[7] = rows(1, {"bench": 5.0}, absent=("star",)) + rows(2, full2)
    feats = availability(games, box)
    assert feats[1].home_missing == 0.0 and feats[1].missing_prev_diff == 0.0
    assert feats[6].missing_diff == 0.0  # everyone played
    f7 = feats[7]
    assert f7.away_missing == 0.0 and f7.home_missing > 0.0
    # star's value from games 1-6 (six 30s, recency-weighted, shrunk toward 5)
    d = 0.5 ** (1 / 20)
    w = sum(d**k for k in range(6))
    value = (30.0 * w + REPLACEMENT_GAME_SCORE * PRIOR_GAMES) / (w + PRIOR_GAMES)
    rot = 1 - 0.5 ** (6 / 5)  # six straight games from 0 with half-life 5
    assert f7.home_missing == pytest.approx(rot * value)
    assert f7.missing_diff == pytest.approx(f7.home_missing)
    # Game 8: the previous game's absence is known before the opener.
    games.append(game(8, 8))
    box[8] = rows(1, full1) + rows(2, full2)
    assert availability(games, box)[8].home_missing_prev == pytest.approx(f7.home_missing)


def test_trades_are_not_absences_and_missing_box_scores_teach_nothing() -> None:
    games = [game(1, 1), game(2, 2), game(3, 3, home=3, away=2), game(4, 4), game(5, 5)]
    box = {
        1: rows(1, {"a": 20.0, "b": 10.0}) + rows(2, {"c": 10.0}),
        2: rows(1, {"a": 20.0, "b": 10.0}) + rows(2, {"c": 10.0}),
        # "b" now plays for team 3: he leaves team 1's rotation.
        3: rows(3, {"b": 10.0}) + rows(2, {"c": 10.0}),
        # Game 4 has no box score: no features, and nothing learned from it.
        5: rows(1, {"a": 20.0}) + rows(2, {"c": 10.0}),
    }
    feats = availability(games, box)
    assert 4 not in feats
    assert feats[5].home_missing == 0.0  # "b" is team 3's now, not missing for team 1


def test_rotation_weights_shrink_at_a_new_season() -> None:
    games = [game(1, 1), game(2, 2), game(3, 300, season=2025)]
    box = {
        1: rows(1, {"a": 20.0}) + rows(2, {"c": 10.0}),
        2: rows(1, {"a": 20.0}) + rows(2, {"c": 10.0}),
        3: rows(1, {}, absent=("a",)) + rows(2, {"c": 10.0}),
    }
    same_season = {**box}
    feats = availability(games, box)
    games_same = [game(1, 1), game(2, 2), game(3, 3)]
    feats_same = availability(games_same, same_season)
    assert feats[3].home_missing == pytest.approx(0.5 * feats_same[3].home_missing)
