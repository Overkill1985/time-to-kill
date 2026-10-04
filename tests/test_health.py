from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import IngestionRun
from ttk.domain import Sport
from ttk.services.health import last_quota, odds_gaps, summary, weekly_path

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def test_odds_gaps_finds_long_stretches_only() -> None:
    polls = [T0 + timedelta(minutes=15 * i) for i in range(4)] + [T0 + timedelta(hours=6)]
    gaps = odds_gaps(polls)
    assert gaps == [(T0 + timedelta(minutes=45), T0 + timedelta(hours=6))]


def test_last_quota_reads_the_collector_log(tmp_path: Path) -> None:
    log = tmp_path / "collector.log"
    log.write_text(
        "2026-10-04 10:00:00 quota remaining: 900\n"
        "2026-10-04 10:15:00 NFL: SUCCESS 12 changes\n"
        "2026-10-04 10:15:01 quota remaining: 897\n",
        encoding="utf-8",
    )
    assert last_quota(log) == ("2026-10-04 10:15:01", 897)
    assert last_quota(tmp_path / "missing.log") is None


def test_weekly_path_is_keyed_by_monday(tmp_path: Path) -> None:
    sunday = datetime(2026, 10, 4, 20, 0)
    assert weekly_path(tmp_path, sunday).name == "2026-09-28.md"
    assert weekly_path(tmp_path, datetime(2026, 10, 5, 9, 0)).name == "2026-10-05.md"


def test_summary_flags_outages_and_dead_runs(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    now = T0
    with session_factory() as s:

        def run(started: datetime, status: str = "SUCCESS", kind: str = "odds") -> None:
            s.add(
                IngestionRun(
                    provider="propline",
                    kind=kind,
                    sport=Sport.NFL,
                    started_at=started,
                    finished_at=started,
                    status=status,
                )
            )

        run(now - timedelta(days=8))  # before the window: anchors the outage
        run(now - timedelta(days=6))  # after a 2-day gap that began before the window
        run(now - timedelta(minutes=10))
        run(now - timedelta(days=3), status="RUNNING", kind="odds-history")  # died mid-run
        s.commit()
        text = summary(s, now=now, db_path=tmp_path / "x.db", data_dir=tmp_path)
    assert "2 gap(s) over 45 minutes" in text  # the 2-day outage, and the 6-day quiet stretch
    assert "Runs left RUNNING" in text and "odds-history" in text
    assert "DEVELOPMENT" in text
