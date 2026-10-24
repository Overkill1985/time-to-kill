from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import Game, ReportedLine, Team
from ttk.domain import GameStatus, Sport
from ttk.services.daily_report import (
    Record,
    build_report,
    load_report,
    report_days,
    save_report,
)

DAY = date(2026, 10, 19)  # a Monday, Eastern
NOW = datetime(2026, 10, 20, 13, tzinfo=UTC)  # 9:00 ET the next morning


def seed(sf: sessionmaker[Session]) -> None:
    """Bears vs Lions, three NFL games with nflverse closing totals of 44.5: 50 (over),
    40 (under), and on DAY 47 (over)."""
    with sf() as s:
        chi = Team(sport="NFL", name="Chicago Bears", abbreviation="CHI")
        det = Team(sport="NFL", name="Detroit Lions", abbreviation="DET")
        s.add_all([chi, det])
        s.flush()
        kicks = [
            datetime(2026, 10, 5, 17, tzinfo=UTC),
            datetime(2026, 10, 12, 17, tzinfo=UTC),
            datetime(2026, 10, 19, 17, tzinfo=UTC),
        ]
        for kick, (home, away) in zip(kicks, ((30, 20), (24, 16), (27, 20)), strict=True):
            g = Game(
                sport=Sport.NFL,
                season=2026,
                season_type="REG",
                home_team_id=chi.id,
                away_team_id=det.id,
                commence_time=kick,
                status=GameStatus.FINAL,
                home_score=home,
                away_score=away,
            )
            s.add(g)
            s.flush()
            s.add(ReportedLine(game_id=g.id, provider="nflverse", total=44.5, home_spread=-3.0))
        s.commit()


def test_report_counts_overs_against_the_closing_total(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    seed(session_factory)
    with session_factory() as s:
        report = build_report(s, now=NOW, cache=tmp_path / "closes.json")
    assert report.day == DAY.isoformat()
    (nfl,) = report.totals
    assert (nfl.sport, nfl.games, nfl.yesterday_games) == ("NFL", 3, 1)
    assert nfl.over_rate == 2 / 3 and nfl.yesterday_over_rate == 1.0
    bears = next(t for t in nfl.teams if t.team == "Chicago Bears")
    assert (bears.overs, bears.unders, bears.last10) == (2, 1, "OUO")
    assert "NFL 100% of 1" in report.headline

    save_report(report, tmp_path)
    assert report_days(tmp_path) == [DAY.isoformat()]
    loaded = load_report(tmp_path)
    assert loaded is not None and loaded["totals"][0]["games"] == 3
    assert load_report(tmp_path, DAY - timedelta(days=1)) is None


def test_record_hit_rate_ignores_pushes() -> None:
    r = Record()
    for won in (True, True, False, None):
        r.add(won)
    assert (r.wins, r.losses, r.pushes) == (2, 1, 1) and r.hit_rate == 2 / 3


def test_daily_report_api(database_url: str, session_factory: sessionmaker[Session]) -> None:
    from fastapi.testclient import TestClient

    from ttk.api.app import create_app
    from ttk.config import Settings

    client = TestClient(
        create_app(Settings(database_url=database_url)), base_url="http://localhost"
    )
    assert client.get("/api/daily-report").status_code == 404
    seed(session_factory)
    data_dir = Path(database_url.removeprefix("sqlite:///")).parent
    with session_factory() as s:
        save_report(build_report(s, now=NOW), data_dir)
    body = client.get("/api/daily-report").json()
    assert body["days"] == [DAY.isoformat()] and body["report"]["day"] == DAY.isoformat()
