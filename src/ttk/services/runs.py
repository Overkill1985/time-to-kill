"""Audited ingestion runs: every fetch leaves an ingestion_runs row, success or not."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import IngestionRun, utcnow
from ttk.domain import Sport
from ttk.providers.base import ProviderError


@dataclass
class RunResult:
    records_written: int
    skipped: dict[str, int]
    stats: Counter[str] = field(default_factory=Counter)


def audited_run(
    session_factory: sessionmaker[Session],
    *,
    provider: str,
    kind: str,
    sport: Sport,
    work: Callable[[Session, IngestionRun], RunResult],
) -> IngestionRun:
    """Run ``work`` in one transaction. A ProviderError is recorded on the run
    (and nothing else is written), not raised. Any other error (e.g. the database
    locked past the busy timeout) is recorded on the run as FAILED too, then raised:
    it used to leave the run RUNNING for good."""
    with session_factory() as session:
        run = IngestionRun(provider=provider, kind=kind, sport=sport)
        session.add(run)
        session.commit()
        try:
            result = work(session, run)
            run.records_written = result.records_written
            run.skipped = result.skipped
            run.stats = dict(result.stats)
            run.status = "SUCCESS"
        except ProviderError as exc:
            session.rollback()
            run.status = "FAILED"
            run.error = str(exc)
        except Exception as exc:
            session.rollback()
            _record_failure(session, run, f"{type(exc).__name__}: {exc}")
            raise
        run.finished_at = utcnow()
        session.add(run)
        session.commit()
        return run


def _record_failure(session: Session, run: IngestionRun, error: str) -> None:
    """Best effort: the database may still be locked; then the collector's start
    closes the run as ABANDONED (``close_abandoned``)."""
    try:
        run.status, run.error, run.finished_at = "FAILED", error[:2000], utcnow()
        session.add(run)
        session.commit()
    except Exception:
        session.rollback()


ABANDONED_AFTER = timedelta(hours=6)


def close_abandoned(session: Session, now: datetime | None = None) -> list[IngestionRun]:
    """Close runs left RUNNING longer than ``ABANDONED_AFTER``: their process ended
    (killed, or a failure that could not be recorded). Call it where only one
    process writes runs, at the collector's start."""
    now = now or utcnow()
    stale = list(
        session.scalars(
            select(IngestionRun).where(
                IngestionRun.status == "RUNNING", IngestionRun.started_at < now - ABANDONED_AFTER
            )
        )
    )
    for run in stale:
        run.status, run.finished_at = "ABANDONED", now
        run.error = run.error or "the process ended before the run finished"
    return stale
