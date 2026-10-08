"""Shared helpers for the command modules."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ttk.config import Settings
from ttk.providers.base import OddsProvider

ROOT = Path(__file__).resolve().parents[3]
Handler = Callable[[argparse.Namespace, Settings], int]


def migrate(database_url: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")


def _db_paths(settings: Settings) -> tuple[Path, Path]:
    """(database file, data directory) for a SQLite URL."""
    db_path = Path(settings.database_url.removeprefix("sqlite:///"))
    return db_path, db_path.parent


def _fmt_pct(value: float | None, n: int | None = None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:+.1f}%" + (f" (n={n})" if n is not None else "")


def _odds_provider(settings: Settings, requested: str | None) -> OddsProvider | None:
    name = settings.odds_provider_name(requested)
    if name == "propline" and settings.propline_api_key is not None:
        from ttk.providers.propline import PropLineProvider

        return PropLineProvider(settings.propline_api_key.get_secret_value())
    if name == "the-odds-api" and settings.odds_api_key is not None:
        from ttk.providers.the_odds_api import TheOddsApiProvider

        return TheOddsApiProvider(settings.odds_api_key.get_secret_value())
    wanted = {"propline": "TTK_PROPLINE_API_KEY", "the-odds-api": "TTK_ODDS_API_KEY"}
    print(
        f"No odds provider key configured ({wanted.get(name or '', 'TTK_PROPLINE_API_KEY')}).",
        file=sys.stderr,
    )
    return None


def _remaining(provider: OddsProvider) -> object:
    return getattr(provider, "daily_remaining", getattr(provider, "requests_remaining", None))


def _alerts(settings: Settings, factory: Any, emit: Callable[[str], None]) -> None:
    """Health alerts on every pass of the long-running loops. Never raises."""
    from ttk.services.alerts import run_alerts, windows_notify

    try:
        _, data_dir = _db_paths(settings)
        with factory() as session:
            sent = run_alerts(
                session,
                data_dir=data_dir,
                notify=windows_notify if settings.alerts_notify else None,
            )
        for line in sent:
            emit(line)
    except Exception as exc:  # alerting must not take the collector down
        emit(f"alert check failed: {type(exc).__name__}: {exc}")


def _bet_alerts(settings: Settings, factory: Any, emit: Callable[[str], None]) -> None:
    """Bet, settlement and steam alerts on every collector pass. Never raises."""
    from ttk.services.alerts import windows_notify
    from ttk.services.bet_alerts import run_bet_alerts

    try:
        _, data_dir = _db_paths(settings)
        with factory() as session:
            sent = run_bet_alerts(
                session,
                data_dir=data_dir,
                steam_sports=settings.steam_sports(),
                notify=windows_notify if settings.alerts_notify else None,
            )
        for line in sent:
            emit(line)
    except Exception as exc:  # alerting must not take the collector down
        emit(f"bet alert check failed: {type(exc).__name__}: {exc}")


def _weekly_summary(settings: Settings, factory: Any, emit: Callable[[str], None]) -> None:
    """Write this week's health summary once, after 8 AM local on Monday (or the
    first pass after that, if the computer was off)."""
    from ttk.db.models import utcnow
    from ttk.services.health import summary, weekly_path

    db_path, data_dir = _db_paths(settings)
    now_local = datetime.now()
    path = weekly_path(data_dir, now_local)
    monday_8am = datetime.combine(
        (now_local - timedelta(days=now_local.weekday())).date(), datetime.min.time()
    ) + timedelta(hours=8)
    if path.exists() or now_local < monday_8am:
        return
    with factory() as session:
        text = summary(session, now=utcnow(), db_path=db_path, data_dir=data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    emit(f"weekly summary written: {path}")
