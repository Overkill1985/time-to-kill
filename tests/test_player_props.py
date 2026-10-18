from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import Game, NflPlayerGameStat, PropPull, PropQuote, Sportsbook, Team
from ttk.domain import GameStatus, Sport
from ttk.services.player_props import props_for_games, upcoming_games

KICK = datetime(2026, 10, 18, 17, tzinfo=UTC)


def seed(sf: sessionmaker[Session]) -> int:
    """Eagles vs Cowboys; Jalen Hurts' last 12 games (passing yards 200..310 by 10);
    a 1 h pull with three books on his passing-yards line, and a lone alt line."""
    with sf() as s:
        phi = Team(sport="NFL", name="Philadelphia Eagles", abbreviation="PHI", espn_id="21")
        dal = Team(sport="NFL", name="Dallas Cowboys", abbreviation="DAL", espn_id="6")
        books = [Sportsbook(key=k, name=k) for k in ("draftkings", "fanduel", "betmgm")]
        s.add_all([phi, dal, *books])
        s.flush()
        for i in range(12):  # most recent last; 4 last season, 8 this season
            season = 2025 if i < 4 else 2026
            past = Game(
                sport=Sport.NFL,
                season=season,
                home_team_id=phi.id,
                away_team_id=dal.id,
                commence_time=KICK - timedelta(weeks=12 - i),
                status=GameStatus.FINAL,
                home_score=20,
                away_score=17,
            )
            s.add(past)
            s.flush()
            s.add(
                NflPlayerGameStat(
                    game_id=past.id,
                    team_id=phi.id,
                    player_id="00-0036389",
                    player_name="Jalen Hurts",
                    position="QB",
                    completions=0,
                    attempts=0,
                    passing_yards=200 + 10 * i,
                    passing_tds=0,
                    interceptions=0,
                    carries=0,
                    rushing_yards=0,
                    rushing_tds=0,
                    targets=0,
                    receptions=0,
                    receiving_yards=0,
                    receiving_tds=0,
                )
            )
        game = Game(
            sport=Sport.NFL,
            season=2026,
            home_team_id=phi.id,
            away_team_id=dal.id,
            commence_time=KICK,
        )
        s.add(game)
        s.flush()
        pull = PropPull(
            game_id=game.id,
            provider="propline",
            event_id="e1",
            horizon_hours=1,
            pulled_at=KICK - timedelta(hours=1),
            books=3,
            quotes=7,
        )
        s.add(pull)
        s.flush()

        def q(book: Sportsbook, sel: str, point: float, odds: float) -> PropQuote:
            return PropQuote(
                pull_id=pull.id,
                sportsbook_id=book.id,
                market="player_pass_yds",
                player="Jalen Hurts (PHI)",
                team_side="HOME",
                selection=sel,
                point=point,
                american_odds=odds,
            )

        dk, fd, mgm = books
        s.add_all(
            [
                q(dk, "Over", 245.5, -110),
                q(dk, "Under", 245.5, -110),
                q(fd, "Over", 245.5, -105),
                q(fd, "Under", 245.5, -115),
                q(mgm, "Over", 245.5, -120),
                q(mgm, "Under", 245.5, +100),
                q(mgm, "Over", 260.5, +130),  # alt line, one book, one side
            ]
        )
        s.commit()
        return game.id


def test_main_line_market_and_hit_rates(session_factory: sessionmaker[Session]) -> None:
    game_id = seed(session_factory)
    with session_factory() as s:
        games = upcoming_games(s, KICK - timedelta(days=1), KICK + timedelta(days=1))
        assert [g.id for g in games] == [game_id]
        rows = props_for_games(s, games)
    assert len(rows) == 1
    r = rows[0]
    assert (r.player, r.team, r.market, r.line, r.books) == (
        "Jalen Hurts",  # the books' "Jalen Hurts (PHI)", team code dropped
        "PHI",
        "player_pass_yds",
        245.5,
        3,
    )
    assert r.over_probability == pytest.approx(0.5, abs=0.01)  # median of three books
    assert r.best_over is not None and (r.best_over.american, r.best_over.book) == (-105, "fanduel")
    assert r.best_under is not None and (r.best_under.american, r.best_under.book) == (
        100,
        "betmgm",
    )
    # This season: 240..310 -> 7 over 245.5 of 8. Last 10 (310 down to 220): 7 over, 3 under.
    assert r.season is not None and (r.season.games, r.season.over, r.season.under) == (8, 7, 1)
    assert r.last10 is not None and (r.last10.games, r.last10.over, r.last10.under) == (10, 7, 3)
    assert r.recent[:3] == [310, 300, 290]  # most recent first
    assert r.last10.average == pytest.approx(265.0)
