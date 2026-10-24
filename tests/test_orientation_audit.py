from datetime import timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session, sessionmaker

from test_forward import KICK, model, odds
from ttk.db.models import ForwardExclusion, Game, ReportedLine
from ttk.domain import GameStatus
from ttk.services.forward_test import scored_rows, snapshot
from ttk.services.orientation_audit import exclude_flipped, is_mirrored


def test_is_mirrored() -> None:
    assert is_mirrored(-11.5, 11.0) and is_mirrored(7.0, -7.5)
    assert not is_mirrored(-11.5, -11.0)  # same team favored, the line moved
    assert not is_mirrored(-1.5, 1.0)  # near pick'em: not judged


def _final(sf: sessionmaker[Session], game_id: int, espn_home_spread: float) -> None:
    with sf() as s:
        g = s.get_one(Game, game_id)
        g.status, g.home_score, g.away_score = GameStatus.FINAL, 24, 20
        s.add(
            ReportedLine(game_id=game_id, provider="espn:draftkings", home_spread=espn_home_spread)
        )
        s.commit()


@pytest.mark.parametrize(("espn", "excluded"), [(3.5, True), (-3.0, False)])
def test_records_on_a_reversed_line_are_excluded_from_scoring(
    session_factory: sessionmaker[Session], espn: float, excluded: bool
) -> None:
    # Our record: home -3.5. ESPN's close +3.5 (the mirror) or -3.0 (agrees).
    now = KICK - timedelta(hours=20)
    game_id = odds(session_factory, now - timedelta(minutes=5))
    with session_factory() as s:
        snapshot(s, [model(s)], now=now)
        s.commit()
    _final(session_factory, game_id, espn)
    with session_factory() as s:
        assert len(scored_rows(s)) == 1
        dry = exclude_flipped(s)
        assert len(dry) == int(excluded) and not s.scalars(select(ForwardExclusion)).all()
        exclude_flipped(s, apply=True)
        s.commit()
        assert len(scored_rows(s)) == (0 if excluded else 1)
        assert exclude_flipped(s) == []  # nothing left to exclude
        if excluded:
            with pytest.raises(DatabaseError):  # append-only
                s.execute(delete(ForwardExclusion))
                s.flush()
