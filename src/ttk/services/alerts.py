"""Near-real-time health alerts: a Windows notification and a log line, once per
problem (re-sent every ``RENOTIFY`` while it lasts, and once more when it clears).

Both long-running processes - the odds collector and the forward-test runner -
call ``run_alerts`` on every pass, so each watches the other. A shared state file
(data/alerts/state.json) keeps them from alerting twice for the same problem.

Checks: no successful odds poll for 45 minutes (the collector is down or
failing); repeated failed odds polls; runs left RUNNING (a process died); the
PropLine quota running low; the forward-test runner silent for an hour.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ttk.db.models import IngestionRun, utcnow
from ttk.services.health import STALE_RUNNING, last_quota

ODDS_STALE = timedelta(minutes=45)
FAILED_POLLS = 3
QUOTA_LOW = 100
FORWARD_SILENT = timedelta(hours=1)
RENOTIFY = timedelta(hours=6)
_LOG_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?) ")


@dataclass(frozen=True)
class Alert:
    key: str
    """Stable identity of the problem (dedupe across passes and processes)."""
    message: str


def _last_log_time(log: Path) -> datetime | None:
    """The newest timestamp at the start of a line (local time, as the runners write)."""
    if not log.exists():
        return None
    newest = None
    with log.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _LOG_TIME.match(line)
            if m:
                fmt = "%Y-%m-%d %H:%M:%S" if len(m.group(1)) > 16 else "%Y-%m-%d %H:%M"
                newest = datetime.strptime(m.group(1), fmt)
    return newest


def check_health(
    session: Session, *, now: datetime, data_dir: Path, now_local: datetime | None = None
) -> list[Alert]:
    alerts: list[Alert] = []
    last_ok = session.scalar(
        select(func.max(IngestionRun.finished_at)).where(
            IngestionRun.kind == "odds", IngestionRun.status == "SUCCESS"
        )
    )
    if last_ok is not None and now - last_ok > ODDS_STALE:
        hours = (now - last_ok).total_seconds() / 3600
        alerts.append(
            Alert(
                "odds-stale",
                f"No successful odds poll for {hours:.1f} h: is the collector running?",
            )
        )
    recent = session.scalars(
        select(IngestionRun.status)
        .where(IngestionRun.kind == "odds")
        .order_by(IngestionRun.started_at.desc())
        .limit(FAILED_POLLS)
    ).all()
    if len(recent) == FAILED_POLLS and all(s == "FAILED" for s in recent):
        alerts.append(Alert("odds-failing", f"The last {FAILED_POLLS} odds polls failed."))
    for run_id, provider, kind, started in session.execute(
        select(
            IngestionRun.id, IngestionRun.provider, IngestionRun.kind, IngestionRun.started_at
        ).where(IngestionRun.status == "RUNNING", IngestionRun.started_at < now - STALE_RUNNING)
    ):
        alerts.append(
            Alert(
                f"running-{run_id}",
                f"{provider}/{kind} has been RUNNING since {started:%m-%d %H:%M} UTC "
                "(the process probably died).",
            )
        )
    quota = last_quota(data_dir / "logs" / "collector.log")
    if quota is not None and quota[1] < QUOTA_LOW:
        alerts.append(Alert("quota-low", f"PropLine requests left today: {quota[1]}."))
    forward_log = data_dir / "logs" / "forward.log"
    last_forward = _last_log_time(forward_log)
    local = now_local or datetime.now()
    if last_forward is not None and local - last_forward > FORWARD_SILENT:
        hours = (local - last_forward).total_seconds() / 3600
        alerts.append(
            Alert("forward-silent", f"The forward-test runner has been silent for {hours:.1f} h.")
        )
    return alerts


def windows_notify(title: str, message: str) -> None:
    """A Windows toast. The text reaches PowerShell through environment
    variables, never as part of the script, so nothing in it can run."""
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
        " ContentType = WindowsRuntime] > $null;"
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$n = $t.GetElementsByTagName('text');"
        "[void]$n.Item(0).AppendChild($t.CreateTextNode($env:TTK_ALERT_TITLE));"
        "[void]$n.Item(1).AppendChild($t.CreateTextNode($env:TTK_ALERT_TEXT));"
        "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app)"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($t))"
    )
    env = {**os.environ, "TTK_ALERT_TITLE": title[:120], "TTK_ALERT_TEXT": message[:500]}
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env=env,
        timeout=30,
        check=False,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def run_alerts(
    session: Session,
    *,
    data_dir: Path,
    now: datetime | None = None,
    now_local: datetime | None = None,
    notify: Callable[[str, str], None] | None = windows_notify,
) -> list[str]:
    """Check, then notify only what is new, still open after RENOTIFY, or resolved.
    Returns the lines sent (also appended to data/logs/alerts.log)."""
    now = now or utcnow()
    state_path = data_dir / "alerts" / "state.json"
    try:
        state: dict[str, dict[str, str]] = json.loads(state_path.read_text("utf-8"))
    except (OSError, ValueError):
        state = {}
    current = {
        a.key: a for a in check_health(session, now=now, data_dir=data_dir, now_local=now_local)
    }
    sent: list[str] = []
    for key, alert in current.items():
        seen = state.get(key)
        due = seen is None or now - datetime.fromisoformat(seen["notified"]) > RENOTIFY
        if due:
            sent.append(f"ALERT {alert.message}")
            state[key] = {
                "since": seen["since"] if seen else now.isoformat(),
                "notified": now.isoformat(),
            }
    for key in [k for k in state if k not in current]:
        sent.append(f"RESOLVED {key} (open since {state[key]['since'][:16]} UTC)")
        del state[key]
    if sent:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=1), "utf-8")
        log = data_dir / "logs" / "alerts.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            for line in sent:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
        if notify is not None:
            for line in sent:
                with contextlib.suppress(Exception):  # a notification must never break a pass
                    notify("Time-to-Kill", line)
    return sent
