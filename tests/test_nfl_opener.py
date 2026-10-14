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


def test_home_lean_averages_the_latest_finished_non_neutral_gaps() -> None:
    from datetime import UTC, datetime, timedelta

    from ttk.db.models import ReportedLine
    from ttk.models.elo import EloGame, EloPrediction
    from ttk.services.nfl_spread_predictor import home_lean

    start = datetime(2025, 9, 7, tzinfo=UTC)
    games, preds, lines = {}, [], {}
    # (expected margin, home line, played, neutral)
    specs = [
        (3.0, -1.0, True, False),  # gap +2 (oldest: dropped by last=2)
        (5.0, -4.0, True, False),  # gap +1
        (0.0, 1.0, True, True),  # neutral: skipped
        (2.0, -1.5, True, False),  # gap +0.5
        (9.0, -1.0, False, False),  # not played: skipped
    ]
    for i, (mu, line, played, neutral) in enumerate(specs):
        games[i] = EloGame(
            i, 2025, start + timedelta(days=i), 1, 2, neutral, 20 if played else None, 17
        )
        preds.append(EloPrediction(i, 2025, 1500, 1500, mu, 0.5))
        lines[i] = ReportedLine(game_id=i, provider="nflverse", home_spread=line)
    lean = home_lean(preds, games, lines, lambda p: p.elo_diff, last=2)
    assert lean == pytest.approx((1.0 + 0.5) / 2)
    assert home_lean([], games, lines, lambda p: p.elo_diff) == 0.0
