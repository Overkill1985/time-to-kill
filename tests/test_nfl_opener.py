import pytest

from ttk.research.nfl_opener import OpenerGame, model_moves


def game(gid: int, open_line: float, close_line: float, margin: float) -> OpenerGame:
    return OpenerGame(gid, 2023, open_line, close_line, {"m": margin}, None)


def test_points_follow_the_model_side_and_the_slope_tracks_disagreement() -> None:
    games = [
        game(1, -3.0, -4.5, 6.0),  # likes home by 3: the line moved 1.5 toward home
        game(2, -3.0, -2.0, 0.0),  # likes away by 3: the line moved 1 toward away
        game(3, -7.0, -7.0, 7.5),  # agrees within 0.5: no move
        game(4, 3.0, 3.5, -5.0),  # likes away by 2: moved 0.5 toward away
    ]
    m = model_moves("m", games)
    assert m.games == 4
    by_edge = {r.min_disagreement: r for r in m.rows}
    # All four: +1.5, +1.0, 0.0, +0.5.
    assert by_edge[0.0].bets == 4 and by_edge[0.0].mean_points == pytest.approx(0.75)
    assert (by_edge[0.0].moved_toward, by_edge[0.0].unchanged) == (3, 1)
    assert by_edge[3.0].bets == 2 and by_edge[3.0].mean_points == pytest.approx(1.25)
    assert m.slope > 0
