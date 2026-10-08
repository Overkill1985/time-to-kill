"""Data in: odds, schedules, history imports, repairs, and the collector loop."""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

from ttk.cli._common import (
    Handler,
    _alerts,
    _bet_alerts,
    _odds_provider,
    _remaining,
)
from ttk.config import Settings
from ttk.domain import Sport


def _repair_espn_lines(args: argparse.Namespace, settings: Settings) -> int:
    from ttk.db.session import make_engine, make_session_factory
    from ttk.providers.espn_odds import EspnCoreOdds
    from ttk.services.espn_history_import import import_season_odds
    from ttk.services.repair import drop_malformed_espn_lines

    factory = make_session_factory(make_engine(settings.database_url))
    with factory() as session:
        found = drop_malformed_espn_lines(session, apply=args.apply)
        if args.apply:
            session.commit()
    for m in found:
        print(f"{m.sport} {m.season}: {m.games} games, {m.rows} ESPN line rows to re-fetch")
    print(
        f"{sum(m.games for m in found)} games with malformed lines"
        + ("" if args.apply else " (dry run; --apply to drop and re-fetch)")
    )
    if not args.apply:
        return 0
    odds = EspnCoreOdds()
    for m in found:
        if m.season is None:
            continue
        run = import_season_odds(factory, Sport(m.sport), m.season, odds=odds, delay=args.delay)
        print(f"{m.sport} {m.season}: {run.status} {run.records_written} rows, stats={run.stats}")
    return 0


def _repair_merged_games(args: argparse.Namespace, settings: Settings) -> int:
    from ttk.db.session import make_engine, make_session_factory
    from ttk.providers.espn import EspnScheduleProvider
    from ttk.providers.espn_odds import EspnCoreOdds
    from ttk.services.espn_history_import import import_season_odds
    from ttk.services.repair import split_merged_espn_games
    from ttk.services.schedule_ingest import run_schedule_ingestion

    factory = make_session_factory(make_engine(settings.database_url))
    with factory() as session:
        merged = split_merged_espn_games(session, apply=args.apply)
        if args.apply:
            session.commit()
    for m in merged:
        print(
            f"game {m.game_id} {m.sport} {m.season}: keep ESPN {m.kept_event}, "
            f"unlink {', '.join(m.removed_events)}; drop {m.reported_lines} ESPN lines, "
            f"{m.player_rows} box-score rows; re-ingest {m.window[0]}..{m.window[1]}"
        )
    print(f"{len(merged)} merged games" + ("" if args.apply else " (dry run; --apply to repair)"))
    if not args.apply or not merged:
        return 0
    schedule = EspnScheduleProvider(delay=args.delay)
    for m in merged:
        run = run_schedule_ingestion(factory, schedule, Sport(m.sport), *m.window)
        if run.status != "SUCCESS":
            print(f"game {m.game_id}: schedule re-ingest failed: {run.error}", file=sys.stderr)
            return 1
    odds = EspnCoreOdds()
    for sport, season in sorted({(m.sport, m.season) for m in merged if m.season is not None}):
        run = import_season_odds(factory, Sport(sport), season, odds=odds, delay=args.delay)
        print(f"{sport} {season} lines: {run.status} {run.records_written} rows, stats={run.stats}")
    return 0


def _collect_odds(args: argparse.Namespace, settings: Settings) -> int:
    import time
    import traceback

    from ttk.db.session import make_engine, make_session_factory
    from ttk.providers.espn import EspnScheduleProvider
    from ttk.providers.espn_boxscore import EspnBoxscores
    from ttk.providers.espn_injuries import EspnInjuries
    from ttk.providers.espn_roster import EspnRosters
    from ttk.services.bets import settle_bets
    from ttk.services.collector import (
        collect_once,
        refresh_boxscores,
        refresh_injuries,
        refresh_schedules,
        refresh_team_boxes,
    )
    from ttk.services.parlay_lab import settle_parlays
    from ttk.services.props import PropsProvider, collect_props

    log_file = None
    if args.log is not None:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        log_file = args.log.open("a", encoding="utf-8")

    def emit(message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
        print(line)  # no-op under pythonw, where there is no console
        if log_file is not None:
            log_file.write(line + "\n")
            log_file.flush()

    provider = _odds_provider(settings, args.provider)
    if provider is None:
        emit("No odds provider key configured; stopping.")
        return 2
    sports = [Sport(s.strip().upper()) for s in args.sports.split(",") if s.strip()]
    factory = make_session_factory(make_engine(settings.database_url))
    schedule_provider = EspnScheduleProvider()
    injuries = EspnInjuries()
    boxscores = EspnBoxscores()
    rosters = EspnRosters()
    remaining: int | None = None
    emit(
        f"collector started: {provider.name}, sports={[str(s) for s in sports]}, "
        f"every {args.loop_minutes or 0:g} min"
    )

    def one_pass() -> list[str]:
        """One collection pass; returns the statuses of the odds polls."""
        nonlocal remaining
        # Provider errors (including 429/503 rate limits) are recorded on the run as
        # FAILED inside each step; that sport waits for the next pass.
        for run in refresh_schedules(factory, schedule_provider, sports):
            emit(
                f"{run.sport} schedule: {run.status} {run.records_written} games, "
                f"stats={run.stats}" + (f", error={run.error}" if run.error else "")
            )
        for run in refresh_injuries(factory, injuries):
            emit(
                f"{run.sport} injuries: {run.status} {run.records_written} changes, "
                f"stats={run.stats}" + (f", error={run.error}" if run.error else "")
            )
        for run in refresh_boxscores(factory, boxscores):
            emit(
                f"{run.sport} box scores: {run.status} {run.records_written} player rows, "
                f"stats={run.stats}" + (f", error={run.error}" if run.error else "")
            )
        for run in refresh_team_boxes(factory, boxscores):
            emit(
                f"{run.sport} team box totals: {run.status} {run.records_written} rows, "
                f"stats={run.stats}" + (f", error={run.error}" if run.error else "")
            )
        with factory() as session:
            for bet in settle_bets(session):
                emit(
                    f"settled bet {bet.id}: {bet.result} {bet.description} "
                    f"P/L {bet.profit_loss or 0:+.2f}"
                )
            for parlay in settle_parlays(session):
                emit(
                    f"settled parlay {parlay.id}: {parlay.result} "
                    f"P/L {parlay.profit_loss or 0:+.2f}"
                )
            session.commit()
        result = collect_once(factory, provider, sports, remaining=remaining)
        remaining = result.remaining
        for run in result.runs:
            emit(
                f"{run.sport}: {run.status} {run.records_written} changes, "
                f"stats={run.stats}" + (f", error={run.error}" if run.error else "")
            )
        for sport, why in result.skipped.items():
            emit(f"{sport}: skipped ({why})")
        if hasattr(provider, "event_props"):
            # Player-prop snapshots at 24 h / 1 h before kickoff (services/props).
            for line in collect_props(factory, cast(PropsProvider, provider), rosters.fetch):
                emit(line)
            remaining = getattr(provider, "daily_remaining", remaining)
        emit(f"quota remaining: {remaining}")
        _alerts(settings, factory, emit)
        _bet_alerts(settings, factory, emit)
        return [r.status for r in result.runs]

    try:
        while True:
            started = time.monotonic()
            try:
                statuses = one_pass()
            except Exception:
                if args.loop_minutes is None:
                    raise
                # One bad pass (e.g. the database locked by a bulk import) must not end
                # collection: log it and retry next pass. It once sat dead for 15 hours.
                emit("pass failed; the collector keeps running:\n" + traceback.format_exc())
                statuses = ["FAILED"]
            if args.loop_minutes is None:
                return 0 if all(st == "SUCCESS" for st in statuses) else 1
            time.sleep(max(args.loop_minutes * 60 - (time.monotonic() - started), 0))
    except KeyboardInterrupt:
        emit("collector stopped")
        return 0
    except Exception:
        emit("collector crashed:\n" + traceback.format_exc())
        raise
    finally:
        if log_file is not None:
            log_file.close()


def _ingest_odds(args: argparse.Namespace, settings: Settings) -> int:
    from ttk.db.session import make_engine, make_session_factory
    from ttk.services.odds_ingest import run_odds_ingestion

    provider = _odds_provider(settings, args.provider)
    if provider is None:
        return 2
    factory = make_session_factory(make_engine(settings.database_url))
    run = run_odds_ingestion(factory, provider, args.sport)
    print(
        f"{run.status}: {run.records_written} snapshots, skipped={run.skipped}, "
        f"stats={run.stats}, quota remaining={_remaining(provider)}"
        + (f", error={run.error}" if run.error else "")
    )
    return 0 if run.status == "SUCCESS" else 1


def _ingest_schedule(args: argparse.Namespace, settings: Settings) -> int:
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
        f"skipped={run.skipped}, stats={run.stats}" + (f", error={run.error}" if run.error else "")
    )
    return 0 if run.status == "SUCCESS" else 1


def _import_nfl_history(args: argparse.Namespace, settings: Settings) -> int:
    from ttk.db.session import make_engine, make_session_factory
    from ttk.providers.nflverse import NflverseProvider
    from ttk.services.history_import import run_nfl_history_import

    factory = make_session_factory(make_engine(settings.database_url))
    run = run_nfl_history_import(
        factory, NflverseProvider(), first_season=args.from_season, last_season=args.to_season
    )
    print(
        f"{run.status}: {run.records_written} games, skipped={run.skipped}, stats={run.stats}"
        + (f", error={run.error}" if run.error else "")
    )
    return 0 if run.status == "SUCCESS" else 1


def _import_nfl_pbp(args: argparse.Namespace, settings: Settings) -> int:
    from ttk.db.session import make_engine, make_session_factory
    from ttk.providers.nflverse_pbp import NflversePbpProvider
    from ttk.services.pbp_import import run_pbp_import

    factory = make_session_factory(make_engine(settings.database_url))
    failed = 0
    for run in run_pbp_import(
        factory, NflversePbpProvider(), range(args.from_season, args.to_season + 1)
    ):
        season = (run.stats or {}).get("season", "?")
        print(
            f"{season}: {run.status} {run.records_written} team-games, "
            f"skipped={run.skipped}, stats={run.stats}"
            + (f", error={run.error}" if run.error else "")
        )
        failed += run.status != "SUCCESS"
    return 1 if failed else 0


def _import_cfbd(args: argparse.Namespace, settings: Settings) -> int:
    from ttk.db.session import make_engine, make_session_factory
    from ttk.providers.cfbd import CfbdClient
    from ttk.services.cfbd_import import import_cfbd, import_cfbd_games

    if settings.cfbd_api_key is None:
        print("Set TTK_CFBD_API_KEY in .env (free at collegefootballdata.com).", file=sys.stderr)
        return 2
    factory = make_session_factory(make_engine(settings.database_url))
    client = CfbdClient(settings.cfbd_api_key.get_secret_value())
    importer = import_cfbd if args.command == "import-cfbd" else import_cfbd_games
    runs = importer(factory, client, (args.from_season, args.to_season))
    for run in runs:
        print(
            f"{run.stats.get('season') if run.stats else '?'}: {run.status} "
            f"{run.records_written} rows, stats={run.stats}"
            + (f", error={run.error}" if run.error else "")
        )
    print(f"CFBD calls left this month: {client.calls_remaining}")
    return 0 if all(r.status == "SUCCESS" for r in runs) else 1


def _import_team_boxes(args: argparse.Namespace, settings: Settings) -> int:
    from ttk.db.session import make_engine, make_session_factory
    from ttk.providers.espn_boxscore import EspnBoxscores
    from ttk.services.boxscore_import import import_team_boxes

    factory = make_session_factory(make_engine(settings.database_url))
    run = import_team_boxes(
        factory,
        args.sport,
        (args.from_season, args.to_season),
        boxscores=EspnBoxscores(),
        delay=args.delay,
    )
    print(f"team boxes {run.status}: {run.records_written} rows, stats={run.stats}")
    if run.error:
        print(run.error, file=sys.stderr)
    return 0 if run.status == "SUCCESS" else 1


def _import_boxscores(args: argparse.Namespace, settings: Settings) -> int:
    from ttk.db.session import make_engine, make_session_factory
    from ttk.providers.espn_boxscore import EspnBoxscores
    from ttk.services.boxscore_import import import_boxscores

    factory = make_session_factory(make_engine(settings.database_url))
    run = import_boxscores(
        factory,
        args.sport,
        (args.from_season, args.to_season),
        boxscores=EspnBoxscores(),
        delay=args.delay,
    )
    print(f"box scores {run.status}: {run.records_written} player rows, stats={run.stats}")
    if run.error:
        print(run.error, file=sys.stderr)
    return 0 if run.status == "SUCCESS" else 1


def _import_espn_history(args: argparse.Namespace, settings: Settings) -> int:
    from ttk.db.session import make_engine, make_session_factory
    from ttk.providers.base import ProviderError
    from ttk.services.espn_history_import import import_season

    factory = make_session_factory(make_engine(settings.database_url))
    for season in range(args.from_season, args.to_season + 1):
        try:
            games, odds_run = import_season(factory, args.sport, season, delay=args.delay)
        except ProviderError as exc:
            print(f"{season}: schedule failed: {exc}", file=sys.stderr)
            return 1
        print(
            f"{season}: {games} games; odds {odds_run.status} "
            f"{odds_run.records_written} lines, stats={odds_run.stats}"
            + (f", error={odds_run.error}" if odds_run.error else ""),
            flush=True,
        )
    return 0


def register(sub: Any) -> None:
    ingest = sub.add_parser("ingest-odds", help="Fetch and store one odds snapshot")
    ingest.add_argument("--sport", type=Sport, choices=list(Sport), required=True)
    ingest.add_argument("--provider", choices=["propline", "the-odds-api"])
    collect = sub.add_parser(
        "collect-odds",
        help="Poll odds for sports with upcoming games (builds timestamped line history)",
    )
    collect.add_argument(
        "--sports", default="NFL,CFB,NBA,NCAAB", help="Comma-separated (default: all four)"
    )
    collect.add_argument("--provider", choices=["propline", "the-odds-api"])
    collect.add_argument(
        "--loop-minutes",
        type=float,
        help="Keep polling every N minutes until stopped (default: one pass)",
    )
    collect.add_argument("--log", type=Path, help="Also append output to this file")
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
    history = sub.add_parser(
        "import-nfl-history", help="Import NFL games, results and reported lines (nflverse)"
    )
    history.add_argument("--from-season", type=int, default=1999)
    history.add_argument("--to-season", type=int)
    pbp = sub.add_parser(
        "import-nfl-pbp",
        help="Import nflverse play-by-play EPA aggregates (~15 MB download per season)",
    )
    pbp.add_argument("--from-season", type=int, default=1999)
    pbp.add_argument("--to-season", type=int, default=date.today().year)
    espn_history = sub.add_parser(
        "import-espn-history",
        help="Import games, results and sportsbook lines (open/close) from ESPN, per season",
    )
    espn_history.add_argument("--sport", type=Sport, choices=list(Sport), required=True)
    espn_history.add_argument(
        "--from-season",
        type=int,
        required=True,
        help="ESPN season year = the year a season ends (2025-26 = 2026)",
    )
    espn_history.add_argument("--to-season", type=int, required=True)
    espn_history.add_argument(
        "--delay", type=float, default=0.25, help="Seconds between requests (be polite to ESPN)"
    )
    team_boxes = sub.add_parser(
        "import-team-boxes",
        help="Import team box-score totals (for possession-based efficiency) from ESPN",
    )
    team_boxes.add_argument(
        "--sport", type=Sport, choices=[Sport.NCAAB, Sport.NBA], default=Sport.NCAAB
    )
    team_boxes.add_argument("--from-season", type=int, required=True, help="ESPN season year")
    team_boxes.add_argument("--to-season", type=int, required=True)
    team_boxes.add_argument("--delay", type=float, default=0.25, help="Seconds between requests")
    cfbd = sub.add_parser(
        "import-cfbd",
        help="Import college football preseason facts from CollegeFootballData (TTK_CFBD_API_KEY)",
    )
    cfbd.add_argument("--from-season", type=int, required=True, help="Season year (start year)")
    cfbd.add_argument("--to-season", type=int, required=True)
    cfbd_games = sub.add_parser(
        "import-cfbd-games",
        help="Import college football per-game team efficiency (PPA) from CollegeFootballData",
    )
    cfbd_games.add_argument("--from-season", type=int, required=True)
    cfbd_games.add_argument("--to-season", type=int, required=True)
    box = sub.add_parser(
        "import-boxscores",
        help="Import player box scores (who played, minutes, stats) for final games, from ESPN",
    )
    box.add_argument("--sport", type=Sport, choices=[Sport.NBA, Sport.NCAAB], default=Sport.NBA)
    box.add_argument("--from-season", type=int, required=True, help="ESPN season year")
    box.add_argument("--to-season", type=int, required=True)
    box.add_argument("--delay", type=float, default=0.25, help="Seconds between requests")
    fix_lines = sub.add_parser(
        "repair-espn-lines",
        help="Re-fetch ESPN lines that stored a price as a line (dry run unless --apply)",
    )
    fix_lines.add_argument("--apply", action="store_true")
    fix_lines.add_argument("--delay", type=float, default=0.25)
    repair = sub.add_parser(
        "repair-merged-games",
        help="Split games that wrongly joined two ESPN events (dry run unless --apply)",
    )
    repair.add_argument("--apply", action="store_true")
    repair.add_argument("--delay", type=float, default=0.25)


HANDLERS: dict[str, Handler] = {
    "ingest-odds": _ingest_odds,
    "collect-odds": _collect_odds,
    "ingest-schedule": _ingest_schedule,
    "import-nfl-history": _import_nfl_history,
    "import-nfl-pbp": _import_nfl_pbp,
    "import-espn-history": _import_espn_history,
    "import-team-boxes": _import_team_boxes,
    "import-boxscores": _import_boxscores,
    "import-cfbd": _import_cfbd,
    "import-cfbd-games": _import_cfbd,
    "repair-espn-lines": _repair_espn_lines,
    "repair-merged-games": _repair_merged_games,
}
