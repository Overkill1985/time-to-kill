"""A plain-text health summary: storage, collection, quotas and forward tests.

``ttk summary`` prints it; the forward-test runner writes one a week to
data/reports/weekly/<date>.md. It is meant to catch, without anyone looking,
the kind of problems that have happened: a collector down for hours, storage
growing faster than planned, a crawl that died and left a run "RUNNING".
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ttk.db.models import ForwardPrediction, Game, IngestionRun, InjuryReport, OddsSnapshot
from ttk.domain import GameStatus
from ttk.services.forward_test import forward_dashboard

GAP_ALERT = timedelta(minutes=45)
STALE_RUNNING = timedelta(hours=6)
_QUOTA = re.compile(r"^(\S+ \S+) quota remaining: (\d+)")


def _size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def _mb(n: float) -> str:
    return f"{n / 1_048_576:,.0f} MB"


def odds_gaps(
    times: list[datetime], *, limit: timedelta = GAP_ALERT
) -> list[tuple[datetime, datetime]]:
    """Stretches longer than ``limit`` between consecutive odds polls."""
    ordered = sorted(times)
    return [(a, b) for a, b in zip(ordered, ordered[1:], strict=False) if b - a > limit]


def last_quota(log: Path) -> tuple[str, int] | None:
    """The collector's last reported PropLine quota (time, requests left)."""
    if not log.exists():
        return None
    found = None
    with log.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _QUOTA.match(line)
            if m:
                found = (m.group(1), int(m.group(2)))
    return found


def summary(
    session: Session, *, now: datetime, db_path: Path, data_dir: Path, days: int = 7
) -> str:
    since = now - timedelta(days=days)
    lines = [f"# Time-to-Kill health summary, {now:%Y-%m-%d %H:%M} UTC (last {days} days)", ""]

    # ---- storage
    backups = sum(_size(p) for p in (data_dir / "backups").glob("*.db"))
    db, wal = _size(db_path), _size(db_path.with_name(db_path.name + "-wal"))
    per_day = session.execute(
        select(func.date(OddsSnapshot.observed_at), func.count())
        .where(OddsSnapshot.observed_at >= since)
        .group_by(func.date(OddsSnapshot.observed_at))
        .order_by(func.date(OddsSnapshot.observed_at))
    ).all()
    lines += [
        "## Storage",
        f"- Database {_mb(db)} (+ {_mb(wal)} write-ahead log); backups {_mb(backups)}.",
        "- Odds changes per day: "
        + (", ".join(f"{d} {n:,}" for d, n in per_day) if per_day else "none"),
        "",
    ]

    # ---- collection
    runs = session.execute(
        select(IngestionRun.provider, IngestionRun.kind, IngestionRun.status, func.count())
        .where(IngestionRun.started_at >= since)
        .group_by(IngestionRun.provider, IngestionRun.kind, IngestionRun.status)
    ).all()
    failed = [(p, k, n) for p, k, s, n in runs if s == "FAILED"]
    polls = sorted(
        set(
            session.scalars(
                select(IngestionRun.started_at).where(
                    IngestionRun.kind == "odds",
                    IngestionRun.status == "SUCCESS",
                    IngestionRun.started_at >= since,
                )
            ).all()
        )
    )
    # Anchor on the last poll before the window, so an outage that began before it
    # (and ended inside it) still counts; and on now, for one still going on.
    before = session.scalar(
        select(func.max(IngestionRun.started_at)).where(
            IngestionRun.kind == "odds",
            IngestionRun.status == "SUCCESS",
            IngestionRun.started_at < since,
        )
    )
    gaps = odds_gaps([*([before] if before else []), *polls, now])
    stale = session.execute(
        select(IngestionRun.provider, IngestionRun.kind, IngestionRun.started_at).where(
            IngestionRun.status == "RUNNING",
            IngestionRun.started_at < now - STALE_RUNNING,
            IngestionRun.started_at >= since,
        )
    ).all()
    quota = last_quota(data_dir / "logs" / "collector.log")
    lines += ["## Collection"]
    lines.append(
        f"- Odds poll runs (one per sport per pass): {len(polls)}, "
        f"about {len(polls) / days:.0f} a day."
    )
    if gaps:
        longest = max(gaps, key=lambda g: g[1] - g[0])
        lines.append(
            f"- **{len(gaps)} gap(s) over {GAP_ALERT.seconds // 60} minutes**; longest "
            f"{longest[0]:%m-%d %H:%M} to {longest[1]:%m-%d %H:%M} UTC "
            f"({(longest[1] - longest[0]).total_seconds() / 3600:.1f} h)."
        )
    else:
        lines.append(f"- No gaps over {GAP_ALERT.seconds // 60} minutes between odds polls.")
    lines.append(
        "- Failed runs: "
        + (", ".join(f"{p}/{k} x{n}" for p, k, n in failed) if failed else "none")
        + "."
    )
    if stale:
        lines.append(
            "- **Runs left RUNNING (a process died mid-run):** "
            + ", ".join(f"{p}/{k} from {t:%m-%d %H:%M}" for p, k, t in stale)
            + "."
        )
    if quota:
        lines.append(f"- PropLine requests left today: {quota[1]} (as of {quota[0]} local).")
    lines.append("")

    # ---- forward tests
    dash = forward_dashboard(session)
    lines += ["## Forward tests"]
    for m in dash["models"]:
        lines.append(
            f"- {m['model']}: {m['snapshots']} snapshots, {m['finished']} on finished games."
        )
    for s in dash["scores"]:
        if not s["decided"]:
            continue
        z = f"z {s['z']:+.1f}" if s["z"] is not None else f"z needs {s['min_decided_for_z']} games"
        lines.append(
            f"  - {s['model']} at {s['horizon_hours']} h: {s['decided']} decided, log loss "
            f"{s['model_log_loss']:.4f} vs market {s['market_log_loss']:.4f} ({z})."
        )
    upcoming = session.scalar(
        select(func.count(func.distinct(ForwardPrediction.game_id)))
        .join(Game, Game.id == ForwardPrediction.game_id)
        .where(Game.status != GameStatus.FINAL)
    )
    lines += [f"- Games snapshotted but not yet final: {upcoming or 0}.", ""]

    # ---- injuries
    changes = Counter(
        dict(
            session.execute(
                select(InjuryReport.sport, func.count())
                .where(InjuryReport.observed_at >= since)
                .group_by(InjuryReport.sport)
            ).all()
        )
    )
    lines += [
        "## Injury reports",
        "- Changes recorded: "
        + (", ".join(f"{k} {v}" for k, v in sorted(changes.items())) if changes else "none")
        + ".",
        "",
        dash["note"],
    ]
    return "\n".join(lines) + "\n"


def weekly_path(data_dir: Path, now_local: datetime) -> Path:
    """data/reports/weekly/<Monday of this week>.md"""
    monday = (now_local - timedelta(days=now_local.weekday())).date()
    return data_dir / "reports" / "weekly" / f"{monday}.md"
