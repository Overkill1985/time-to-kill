from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import ReportedLine
from ttk.domain import GameStatus, Sport
from ttk.providers.base import NormalizedGame, TeamRef
from ttk.services.identity import resolve_game
from ttk.services.repair import drop_malformed_espn_lines


def game(session: Session, event: str, home: str, away: str) -> int:
    g = NormalizedGame(
        "espn",
        event,
        Sport.NBA,
        TeamRef(home, espn_id=home),
        TeamRef(away, espn_id=away),
        datetime(2023, 1, int(event[-1]), tzinfo=UTC),
        None,
        espn_event_id=event,
        status=GameStatus.FINAL,
        home_score=100,
        away_score=90,
        season=2023,
        season_type="REG",
    )
    return resolve_game(session, g, Counter()).game.id


def test_drop_malformed_espn_lines(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        bad, good = game(session, "1", "1", "2"), game(session, "2", "3", "4")
        session.add_all(
            [
                # a price stored as the spread, and a total equal to its over price
                ReportedLine(game_id=bad, provider="espn:espn-bet", home_spread=-110.0),
                ReportedLine(
                    game_id=bad, provider="espn:mgm", home_spread=-4.5, total=-115, over_odds=-115
                ),
                ReportedLine(game_id=bad, provider="espn-open:mgm", home_spread=-4.0),
                ReportedLine(game_id=bad, provider="nflverse", home_spread=-4.5),
                ReportedLine(
                    game_id=good, provider="espn:mgm", home_spread=2.5, total=221.5, over_odds=-110
                ),
            ]
        )
        session.commit()
        found = drop_malformed_espn_lines(session)
        assert [(m.sport, m.season, m.games, m.rows) for m in found] == [("NBA", 2023, 1, 3)]
        assert len(session.scalars(select(ReportedLine)).all()) == 5  # dry run
        drop_malformed_espn_lines(session, apply=True)
        session.commit()
        left = session.execute(select(ReportedLine.game_id, ReportedLine.provider)).all()
        # every ESPN line of the bad game goes (to be re-fetched); others stay
        assert sorted(left) == [(bad, "nflverse"), (good, "espn:mgm")]
        assert drop_malformed_espn_lines(session) == []
