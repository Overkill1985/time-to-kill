from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from test_alerts import NOW, add_run
from ttk.db.models import IngestionRun
from ttk.domain import Sport
from ttk.services.runs import RunResult, audited_run, close_abandoned


def test_an_unexpected_error_fails_the_run_instead_of_leaving_it_running(
    session_factory: sessionmaker[Session],
) -> None:
    def work(session: Session, run: IngestionRun) -> RunResult:
        raise RuntimeError("database is locked")

    with pytest.raises(RuntimeError):
        audited_run(session_factory, provider="propline", kind="odds", sport=Sport.NFL, work=work)
    with session_factory() as s:
        run = s.scalars(select(IngestionRun)).one()
        assert run.status == "FAILED" and run.finished_at is not None
        assert run.error == "RuntimeError: database is locked"


def test_close_abandoned_closes_only_old_running_runs(
    session_factory: sessionmaker[Session],
) -> None:
    add_run(session_factory, NOW - timedelta(hours=9), status="RUNNING")
    add_run(session_factory, NOW - timedelta(minutes=5), status="RUNNING")  # in progress
    add_run(session_factory, NOW - timedelta(hours=9))
    with session_factory() as s:
        closed = close_abandoned(s, NOW)
        s.commit()
        assert len(closed) == 1
        statuses = sorted(s.scalars(select(IngestionRun.status)))
    assert statuses == ["ABANDONED", "RUNNING", "SUCCESS"]
