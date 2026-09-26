"""Audited ingestion runs: every fetch leaves an ingestion_runs row, success or not."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

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
    (and nothing else is written), not raised."""
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
        run.finished_at = utcnow()
        session.add(run)
        session.commit()
        return run
