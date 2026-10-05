from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import IngestionRun
from ttk.domain import Sport
from ttk.services.alerts import check_health, run_alerts

NOW = datetime(2026, 10, 4, 22, 0, tzinfo=UTC)
LOCAL = datetime(2026, 10, 4, 18, 0)


def add_run(
    sf: sessionmaker[Session], started: datetime, status: str = "SUCCESS", kind: str = "odds"
) -> None:
    with sf() as s:
        s.add(
            IngestionRun(
                provider="propline",
                kind=kind,
                sport=Sport.NFL,
                started_at=started,
                finished_at=None if status == "RUNNING" else started,
                status=status,
            )
        )
        s.commit()


def logs(tmp_path: Path, *, forward_last: str, quota: int = 700) -> None:
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs" / "collector.log").write_text(
        f"2026-10-04 17:55:00 quota remaining: {quota}\n", "utf-8"
    )
    (tmp_path / "logs" / "forward.log").write_text(
        f"{forward_last} forward snapshots: 0 written\n", "utf-8"
    )


def keys(sf: sessionmaker[Session], tmp_path: Path) -> set[str]:
    with sf() as s:
        return {a.key for a in check_health(s, now=NOW, data_dir=tmp_path, now_local=LOCAL)}


def test_healthy_system_has_no_alerts(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    add_run(session_factory, NOW - timedelta(minutes=10))
    logs(tmp_path, forward_last="2026-10-04 17:50:00")
    assert keys(session_factory, tmp_path) == set()


def test_each_check(session_factory: sessionmaker[Session], tmp_path: Path) -> None:
    add_run(session_factory, NOW - timedelta(hours=3))  # last success 3 h ago
    for minutes in (30, 20, 10):
        add_run(session_factory, NOW - timedelta(minutes=minutes), status="FAILED")
    add_run(session_factory, NOW - timedelta(hours=9), status="RUNNING", kind="odds-history")
    logs(tmp_path, forward_last="2026-10-04 15:30", quota=40)  # 2.5 h silent
    found = keys(session_factory, tmp_path)
    assert {"odds-stale", "odds-failing", "quota-low", "forward-silent"} <= found
    assert any(k.startswith("running-") for k in found)


def test_alerts_once_renotify_and_resolve(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    add_run(session_factory, NOW - timedelta(hours=3))
    logs(tmp_path, forward_last="2026-10-04 17:50:00")
    shown: list[str] = []

    def notify(title: str, text: str) -> None:
        shown.append(text)

    def run(at: datetime) -> list[str]:
        with session_factory() as s:
            return run_alerts(s, data_dir=tmp_path, now=at, now_local=LOCAL, notify=notify)

    first = run(NOW)
    assert [line[:5] for line in first] == ["ALERT"] and "odds poll" in first[0]
    assert run(NOW + timedelta(minutes=15)) == []  # same problem: no repeat
    assert len(run(NOW + timedelta(hours=7))) == 1  # still open after 6 h: reminded
    add_run(session_factory, NOW + timedelta(hours=7, minutes=5))  # the collector is back
    resolved = run(NOW + timedelta(hours=7, minutes=10))
    assert resolved and resolved[0].startswith("RESOLVED odds-stale")
    assert len(shown) == 3
    log = (tmp_path / "logs" / "alerts.log").read_text("utf-8")
    assert log.count("ALERT") == 2 and log.count("RESOLVED") == 1


def test_a_broken_notifier_never_breaks_a_pass(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    add_run(session_factory, NOW - timedelta(hours=3))
    logs(tmp_path, forward_last="2026-10-04 17:50:00")

    def broken(title: str, text: str) -> None:
        raise OSError("no notification service")

    with session_factory() as s:
        sent = run_alerts(s, data_dir=tmp_path, now=NOW, now_local=LOCAL, notify=broken)
    assert sent  # still logged and returned
