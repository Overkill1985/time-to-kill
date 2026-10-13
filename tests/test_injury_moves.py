from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import Game, GameSourceId, IngestionRun, InjuryReport, Team
from ttk.domain import Market, Selection, Sport
from ttk.providers.base import NormalizedGame, NormalizedOddsQuote, OddsFetch, TeamRef
from ttk.providers.nflverse import SnapRow, parse_snaps
from ttk.services.injury_moves import injury_moves, snap_shares, summarize
from ttk.services.odds_ingest import store_odds

KICK = datetime(2026, 10, 11, 17, 0, tzinfo=UTC)


def odds(s: Session, at: datetime, home_line: float) -> None:
    game = NormalizedGame(
        "espn",
        "e1",
        Sport.NFL,
        TeamRef("Philadelphia Eagles"),
        TeamRef("Dallas Cowboys"),
        KICK,
        None,
    )

    def q(sel: Selection, line: float, book: str) -> NormalizedOddsQuote:
        return NormalizedOddsQuote("fake", "e1", book, book, Market.SPREAD, sel, line, -110, None)

    quotes = [q(Selection.HOME, home_line, b) for b in ("draftkings", "fanduel")] + [
        q(Selection.AWAY, -home_line, b) for b in ("draftkings", "fanduel")
    ]
    run = IngestionRun(provider="fake", kind="odds", sport=Sport.NFL)
    s.add(run)
    s.flush()
    store_odds(s, OddsFetch([game], quotes, {}), run=run, observed_at=at, stats=Counter())


def test_parse_snaps_takes_the_larger_share() -> None:
    rows = parse_snaps(
        [
            {
                "season": "2026",
                "week": "1",
                "game_type": "REG",
                "team": "PHI",
                "player": "A",
                "position": "LB",
                "offense_pct": "0",
                "defense_pct": "0.9",
            },
            {"season": "2026", "week": "x"},  # malformed: skipped
        ]
    )
    assert rows == [SnapRow(2026, 1, "REG", "PHI", "A", "LB", 0.9)]


def test_a_bet_against_bad_news_gains_the_later_move(
    session_factory: sessionmaker[Session],
) -> None:
    news = KICK - timedelta(days=2)
    with session_factory() as s:
        odds(s, news - timedelta(hours=1), -3.5)  # the line when the news is seen
        odds(s, KICK - timedelta(minutes=30), -2.0)  # moved toward Dallas by kickoff
        gid = s.scalar(select(GameSourceId.game_id).where(GameSourceId.source_identifier == "e1"))
        game = s.get_one(Game, gid)
        game.week = 6
        home = s.get_one(Team, game.home_team_id)
        home.espn_id = "21"  # PHI's ESPN id (nflverse TEAMS)
        base = news - timedelta(days=3)
        s.add_all(
            [
                # The first poll is a state, not news.
                InjuryReport(
                    sport=Sport.NFL,
                    team_id=home.id,
                    player_name="Jalen Hurts",
                    status="Questionable",
                    provider="espn",
                    observed_at=base,
                ),
                InjuryReport(
                    sport=Sport.NFL,
                    team_id=home.id,
                    player_name="Jalen Hurts",
                    status="Out",
                    provider="espn",
                    observed_at=news,
                ),
                # A backup: under the snap-share floor.
                InjuryReport(
                    sport=Sport.NFL,
                    team_id=home.id,
                    player_name="Tanner McKee",
                    status="Out",
                    provider="espn",
                    observed_at=news,
                ),
            ]
        )
        s.commit()
        shares = snap_shares(
            [SnapRow(2026, w, "REG", "PHI", "Jalen Hurts", "QB", 1.0) for w in (1, 2)]
            + [SnapRow(2026, 1, "REG", "PHI", "Tanner McKee", "QB", 0.05)]
        )
        moves = injury_moves(s, shares)
    assert len(moves) == 1
    m = moves[0]
    assert (m.player, m.previous, m.status, m.home) == ("Jalen Hurts", "Questionable", "Out", True)
    assert m.weight == pytest.approx(0.66)
    assert (m.line_at, m.line_close) == (-3.5, -2.0)
    assert m.gain == pytest.approx(1.5)  # a bet on Dallas +3.5 beat the +2 close
    summary = summarize("all", moves)
    assert (summary.events, summary.positive, summary.mean_gain) == (1, 1, 1.5)
