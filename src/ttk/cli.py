"""Command line: ``ttk migrate``, ``ttk ingest-schedule --sport NFL``,
``ttk ingest-odds --sport NFL``, ``ttk serve``."""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from ttk.config import get_settings
from ttk.domain import Sport

ROOT = Path(__file__).resolve().parents[2]


def migrate(database_url: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ttk")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="Apply database migrations")
    ingest = sub.add_parser("ingest-odds", help="Fetch and store odds snapshots")
    ingest.add_argument("--sport", type=Sport, choices=list(Sport), required=True)
    schedule = sub.add_parser(
        "ingest-schedule", help="Fetch games, status and scores from ESPN (no API key)"
    )
    schedule.add_argument("--sport", type=Sport, choices=list(Sport), required=True)
    schedule.add_argument(
        "--from", dest="start", type=date.fromisoformat, help="YYYY-MM-DD (default: 2 days ago)"
    )
    schedule.add_argument(
        "--to", dest="end", type=date.fromisoformat, help="YYYY-MM-DD (default: 7 days ahead)"
    )
    serve = sub.add_parser("serve", help="Run the API on loopback")
    serve.add_argument("--port", type=int, default=8800)
    args = parser.parse_args(argv)
    settings = get_settings()

    if args.command == "migrate":
        migrate(settings.database_url)
        print("Database is at the latest migration.")
        return 0

    if args.command == "ingest-odds":
        from ttk.db.session import make_engine, make_session_factory
        from ttk.providers.the_odds_api import TheOddsApiProvider
        from ttk.services.odds_ingest import run_odds_ingestion

        if settings.odds_api_key is None:
            print("TTK_ODDS_API_KEY is not set; no odds provider is configured.", file=sys.stderr)
            return 2
        provider = TheOddsApiProvider(settings.odds_api_key.get_secret_value())
        factory = make_session_factory(make_engine(settings.database_url))
        run = run_odds_ingestion(factory, provider, args.sport)
        print(
            f"{run.status}: {run.records_written} snapshots, skipped={run.skipped}, "
            f"credits remaining={provider.requests_remaining}"
            + (f", error={run.error}" if run.error else "")
        )
        return 0 if run.status == "SUCCESS" else 1

    if args.command == "ingest-schedule":
        from ttk.db.session import make_engine, make_session_factory
        from ttk.providers.espn import EspnScheduleProvider
        from ttk.services.schedule_ingest import run_schedule_ingestion

        today = date.today()
        start = args.start or today - timedelta(days=2)
        end = args.end or today + timedelta(days=7)
        factory = make_session_factory(make_engine(settings.database_url))
        run = run_schedule_ingestion(factory, EspnScheduleProvider(), args.sport, start, end)
        print(
            f"{run.status}: {run.records_written} games {start}..{end}, "
            f"skipped={run.skipped}, stats={run.stats}"
            + (f", error={run.error}" if run.error else "")
        )
        return 0 if run.status == "SUCCESS" else 1

    if args.command == "serve":
        import uvicorn

        uvicorn.run("ttk.api.app:create_app", factory=True, host="127.0.0.1", port=args.port)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
