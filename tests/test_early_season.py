from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import Game, Team
from ttk.domain import GameStatus, Market, Selection, Sport
from ttk.services.early_season import bucket_of, games_played_before, split
from ttk.services.forward_test import ScoredRow

T0 = datetime(2026, 11, 3, 23, tzinfo=UTC)


def test_buckets() -> None:
    assert [bucket_of(n) for n in (0, 4, 5, 14, 15, 30)] == [
        "0-4 games",
        "0-4 games",
        "5-14 games",
        "5-14 games",
        "15+ games",
        "15+ games",
    ]


def test_games_played_counts_the_less_experienced_team_this_season_only(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as s:
        a, b, c = (Team(sport="NCAAB", name=n) for n in ("A", "B", "C"))
        s.add_all([a, b, c])
        s.flush()

        def game(home: Team, away: Team, when: datetime, season: int, final: bool) -> Game:
            g = Game(
                sport=Sport.NCAAB,
                season=season,
                home_team_id=home.id,
                away_team_id=away.id,
                commence_time=when,
                status=GameStatus.FINAL if final else GameStatus.SCHEDULED,
                home_score=70 if final else None,
                away_score=60 if final else None,
            )
            s.add(g)
            s.flush()
            return g

        game(a, b, T0 - timedelta(days=200), 2026, True)  # last season: not counted
        for i in range(6):
            game(a, c, T0 - timedelta(days=10 - i), 2027, True)  # A has 6, C has 6
        game(b, c, T0 - timedelta(days=1), 2027, True)  # B has 1, C has 7
        target = game(a, b, T0, 2027, False)
        s.commit()
        assert games_played_before(s, [target.id]) == {target.id: 1}


def test_split_scores_each_bucket() -> None:
    def row(game_id: int, model_p: float, home_covers: bool) -> ScoredRow:
        return ScoredRow(
            "m 1",
            1,
            game_id,
            "NCAAB",
            Market.SPREAD,
            T0,
            -3.0,
            model_p,
            0.5,
            1.0 if home_covers else -1.0,
            Selection.HOME,
            -110,
            None,
            0.5,
        )

    rows = [row(1, 0.6, True), row(2, 0.6, False), row(3, 0.6, True)]
    out = split(rows, {1: 2, 2: 3, 3: 20})
    assert [(b.bucket, b.decided, b.wins, b.losses) for b in out] == [
        ("0-4 games", 2, 1, 1),
        ("15+ games", 1, 1, 0),
    ]
    assert out[0].points_vs_close == 0.5 and out[0].points_n == 2
