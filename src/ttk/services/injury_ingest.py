"""Poll a provider's injury list and store it as a change log in injury_reports.

A row is appended only when a player's report changes (status, type, comment,
expected return or the provider's update time) or when the player leaves the list
(a ``cleared`` row). ``observed_at`` is when we saw it, so ``injuries_at(t)``
answers "what was known at t" without leakage. Rows are append-only (trigger).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import IngestionRun, InjuryReport, Team
from ttk.domain import Sport
from ttk.providers.espn_injuries import EspnInjuries, InjuryEntry
from ttk.services.runs import RunResult, audited_run


@dataclass(frozen=True)
class PlayerInjury:
    player_id: str
    player_name: str
    team_id: int | None
    status: str
    injury_type: str | None
    comment: str | None
    return_date: str | None
    updated_at: datetime | None
    observed_at: datetime


def _key(row: InjuryReport) -> tuple[object, ...]:
    return (
        row.team_id,
        row.status,
        row.injury_type,
        row.comment,
        row.return_date,
        row.source_timestamp,
    )


def injuries_at(
    session: Session, sport: Sport, at: datetime, *, provider: str = "espn"
) -> dict[str, PlayerInjury]:
    """Each player's latest report observed at or before ``at``; cleared players
    are omitted."""
    rows = session.scalars(
        select(InjuryReport)
        .where(
            InjuryReport.provider == provider,
            InjuryReport.sport == sport,
            InjuryReport.observed_at <= at,
        )
        .order_by(InjuryReport.observed_at, InjuryReport.id)
    ).all()
    latest: dict[str, InjuryReport] = {}
    for row in rows:
        if row.player_source_identifier:
            latest[row.player_source_identifier] = row
    return {
        pid: PlayerInjury(
            pid,
            row.player_name,
            row.team_id,
            row.status,
            row.injury_type,
            row.comment,
            row.return_date,
            row.source_timestamp,
            row.observed_at,
        )
        for pid, row in latest.items()
        if not row.cleared
    }


def _latest_rows(session: Session, sport: Sport, provider: str) -> dict[str, InjuryReport]:
    latest: dict[str, InjuryReport] = {}
    for row in session.scalars(
        select(InjuryReport)
        .where(InjuryReport.provider == provider, InjuryReport.sport == sport)
        .order_by(InjuryReport.observed_at, InjuryReport.id)
    ):
        if row.player_source_identifier:
            latest[row.player_source_identifier] = row
    return latest


def store_injuries(
    session: Session,
    sport: Sport,
    entries: list[InjuryEntry],
    observed_at: datetime,
    *,
    provider: str = "espn",
) -> Counter[str]:
    stats: Counter[str] = Counter({"entries": len(entries)})
    teams = {
        espn_id: team_id
        for team_id, espn_id in session.execute(
            select(Team.id, Team.espn_id).where(Team.sport == sport, Team.espn_id.is_not(None))
        )
    }
    latest = _latest_rows(session, sport, provider)
    active = {pid for pid, row in latest.items() if not row.cleared}
    if not entries and active:
        stats["empty_feed_ignored"] += 1  # a glitch, not every player healing at once
        return stats
    seen: set[str] = set()
    for e in entries:
        if e.player_id in seen:
            stats["duplicate_player"] += 1
            continue
        seen.add(e.player_id)
        team_id = teams.get(e.team_espn_id)
        if team_id is None:
            stats["unmatched_team"] += 1
        row = InjuryReport(
            sport=sport,
            team_id=team_id,
            player_name=e.player_name,
            player_source_identifier=e.player_id,
            status=e.status,
            injury_type=e.injury_type,
            comment=e.comment,
            return_date=e.return_date,
            provider=provider,
            source_timestamp=e.updated_at,
            observed_at=observed_at,
            cleared=False,
        )
        previous = latest.get(e.player_id)
        if previous is not None and not previous.cleared and _key(previous) == _key(row):
            stats["unchanged"] += 1
            continue
        session.add(row)
        stats["new" if previous is None or previous.cleared else "changed"] += 1
    for pid in sorted(active - seen):
        gone = latest[pid]
        session.add(
            InjuryReport(
                sport=sport,
                team_id=gone.team_id,
                player_name=gone.player_name,
                player_source_identifier=pid,
                status=gone.status,
                provider=provider,
                observed_at=observed_at,
                cleared=True,
            )
        )
        stats["cleared"] += 1
    return stats


def ingest_injuries(
    session_factory: sessionmaker[Session],
    sport: Sport,
    *,
    source: EspnInjuries,
    now: datetime,
) -> IngestionRun:
    def work(session: Session, run: IngestionRun) -> RunResult:
        stats = store_injuries(session, sport, source.fetch(sport), now)
        written = stats["new"] + stats["changed"] + stats["cleared"]
        return RunResult(written, {}, stats)

    return audited_run(session_factory, provider="espn", kind="injuries", sport=sport, work=work)
