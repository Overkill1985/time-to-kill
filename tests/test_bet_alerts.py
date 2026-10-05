import json
from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from test_bets import KICK, bet, game_id, poll
from ttk.db.models import Game
from ttk.domain import GameStatus, Sport
from ttk.services.bet_alerts import bet_moves, run_bet_alerts, settlements, steam
from ttk.services.bets import record_bet, settle_bets


def test_bet_line_moves_alert_both_ways(session_factory: sessionmaker[Session]) -> None:
    poll(session_factory, KICK - timedelta(hours=6), -110, -110, line=-3.5)
    gid = game_id(session_factory)
    with session_factory() as s:
        record_bet(s, bet(gid))  # Home -3.5, three hours before kickoff
        s.commit()
    poll(session_factory, KICK - timedelta(hours=2), -110, -110, line=-2.5)
    with session_factory() as s:
        assert bet_moves(s, now=KICK - timedelta(hours=1)) == []  # a point: under 1.5
    poll(session_factory, KICK - timedelta(minutes=50), -110, -110, line=-5.5)
    with session_factory() as s:
        (alert,) = bet_moves(s, now=KICK - timedelta(minutes=30))
        assert alert.key.endswith("-for") and "2 points your way" in alert.message
        assert "now -5.5" in alert.message
        assert bet_moves(s, now=KICK + timedelta(minutes=5)) == []  # kicked off
    poll(session_factory, KICK - timedelta(minutes=20), -110, -110, line=-1.5)
    with session_factory() as s:
        (alert,) = bet_moves(s, now=KICK - timedelta(minutes=10))
    assert alert.key.endswith("-against") and "2 points against you" in alert.message


def test_settlement_events_are_sent_once(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    poll(session_factory, KICK - timedelta(hours=6), -110, -110)
    gid = game_id(session_factory)
    with session_factory() as s:
        record_bet(s, bet(gid))
        game = s.get_one(Game, gid)
        game.status, game.home_score, game.away_score = GameStatus.FINAL, 27, 20
        settle_bets(s, now=KICK + timedelta(hours=4))
        s.commit()
        now = KICK + timedelta(hours=5)
        (alert,) = settlements(s, now=now)
        assert "Bet settled: WIN Home -3.5" in alert.message and "P/L +90.91" in alert.message
        assert settlements(s, now=now + timedelta(days=2)) == []  # only recent ones

        sent: list[tuple[str, str]] = []
        lines = run_bet_alerts(
            s, data_dir=tmp_path, now=now, notify=lambda t, m: sent.append((t, m))
        )
        assert lines == [f"EVENT {alert.message}"] and len(sent) == 1
        assert run_bet_alerts(s, data_dir=tmp_path, now=now + timedelta(minutes=15)) == []
    remembered = json.loads((tmp_path / "alerts" / "events.json").read_text("utf-8"))
    assert list(remembered) == [alert.key]
    assert "Bet settled" in (tmp_path / "logs" / "alerts.log").read_text("utf-8")


def test_steam_needs_a_fast_move_across_books(session_factory: sessionmaker[Session]) -> None:
    now = KICK - timedelta(hours=3)
    poll(session_factory, now - timedelta(minutes=90), -110, -110, line=-3.5)
    with session_factory() as s:
        assert steam(s, now=now, sports=[Sport.NFL]) == []
    poll(session_factory, now - timedelta(minutes=20), -110, -110, line=-5.0)
    with session_factory() as s:
        (alert,) = steam(s, now=now, sports=[Sport.NFL])
        assert alert.key.endswith("-5")
        assert "home spread moved 1.5 points in 60 min (-3.5 -> -5, 3 books)" in alert.message
        assert steam(s, now=now, sports=[Sport.NBA]) == []
        # Two hours later the move is old news (and inside the lookback both ends agree).
        assert steam(s, now=now + timedelta(minutes=100), sports=[Sport.NFL]) == []
