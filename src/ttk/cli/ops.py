"""Operations: migrations, the API server, alerts, health, props coverage, team links."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from ttk.cli._common import (
    Handler,
    _db_paths,
    migrate,
)
from ttk.config import Settings


def _teams(args: argparse.Namespace, settings: Settings) -> int:
    from ttk.db.session import make_engine, make_session_factory
    from ttk.services.team_review import LinkError, link_team, unmatched_teams

    factory = make_session_factory(make_engine(settings.database_url))
    with factory() as session:
        if args.command == "teams-unmatched":
            teams = unmatched_teams(session)
            for t in teams:
                print(
                    f"\n{t.sport} {t.name} (team id {t.team_id}); aliases: {', '.join(t.aliases)}"
                )
                for g in t.games:
                    target = f" -> ESPN game {g.espn_counterpart}" if g.espn_counterpart else ""
                    print(
                        f"    game {g.game_id} {g.commence_time[:16]} {g.status} vs {g.opponent}, "
                        f"{g.odds_rows} odds rows{target}"
                    )
                print(
                    "    closest ESPN teams: "
                    + "; ".join(f"{name} (id {tid}, {sim:.2f})" for tid, name, sim in t.suggestions)
                )
            if not teams:
                print("No unmatched teams: every provider name links to an ESPN team.")
            else:
                print(
                    "\nLink one with `ttk link-team --team-id ID --espn-team-id ID` (a dry run; "
                    "add --apply). Suggestions are never applied on their own."
                )
            return 0
        try:
            plan = link_team(session, args.team_id, args.espn_team_id, apply=args.apply)
        except LinkError as exc:
            print(f"Not linked: {exc}", file=sys.stderr)
            return 2
        if args.apply:
            session.commit()
    print(f"{'Linked' if plan.applied else 'Would link'} {plan.unmatched} -> {plan.espn_team}:")
    for step in plan.steps:
        print(f"  {step}")
    if not plan.applied:
        print("Dry run: nothing changed. Add --apply to make these changes.")
    return 0


def _migrate(args: argparse.Namespace, settings: Settings) -> int:
    migrate(settings.database_url)
    print("Database is at the latest migration.")
    return 0


def _alerts_check(args: argparse.Namespace, settings: Settings) -> int:
    from ttk.db.models import utcnow
    from ttk.db.session import make_engine, make_session_factory
    from ttk.services.alerts import check_health, windows_notify
    from ttk.services.bet_alerts import bet_moves, settlements, steam

    _, data_dir = _db_paths(settings)
    factory = make_session_factory(make_engine(settings.database_url))
    with factory() as session:
        found = check_health(session, now=utcnow(), data_dir=data_dir)
    for alert in found:
        print(f"ALERT [{alert.key}] {alert.message}")
    if not found:
        print("No alerts: collection and forward tests look healthy.")
    with factory() as session:
        now = utcnow()
        events = [
            *bet_moves(session, now=now),
            *settlements(session, now=now),
            *steam(session, now=now, sports=settings.steam_sports()),
        ]
    for alert in events:
        print(f"EVENT [{alert.key}] {alert.message}")
    print(f"{len(events)} bet/steam event(s) now (the collector sends each once).")
    if args.test_notify:
        windows_notify("Time-to-Kill", "Test notification: alerts will look like this.")
        print("Test notification sent.")
    return 0


def _summary(args: argparse.Namespace, settings: Settings) -> int:
    from ttk.db.models import utcnow
    from ttk.db.session import make_engine, make_session_factory
    from ttk.services.health import summary as health_summary

    db_path, data_dir = _db_paths(settings)
    factory = make_session_factory(make_engine(settings.database_url))
    with factory() as session:
        text = health_summary(
            session, now=utcnow(), db_path=db_path, data_dir=data_dir, days=args.days
        )
    print(text, end="")
    if args.write is not None:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(text, encoding="utf-8")
    return 0


def _props_report(args: argparse.Namespace, settings: Settings) -> int:
    from ttk.db.session import make_engine, make_session_factory
    from ttk.services.props import coverage

    factory = make_session_factory(make_engine(settings.database_url))
    with factory() as session:
        rows = coverage(session)
    for c in rows:
        print(
            f"\n{c.sport}, {c.horizon} h before kickoff: {c.pulls} pulls, "
            f"{c.with_quotes} with sportsbook props, {c.quotes} quotes kept; dropped "
            f"{c.dropped_not_a_sportsbook} pick'em/exchange, {c.dropped_off_roster} off-roster"
        )
        for book, (n, q) in c.books.items():
            print(f"    {book:<16} in {n} of {c.pulls} pulls, {q} quotes")
        top = ", ".join(f"{m} {n}" for m, n in list(c.markets.items())[:8])
        print(f"    markets: {top}")
        if c.off_roster_names:
            names = ", ".join(f"{n} ({k})" for n, k in list(c.off_roster_names.items())[:12])
            print(f"    most-quoted off-roster names: {names}")
    if not rows:
        print("No prop pulls yet: the collector takes them 24 h and 1 h before NFL/NBA games.")
    return 0


def _serve(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    uvicorn.run("ttk.api.app:create_app", factory=True, host="127.0.0.1", port=args.port)
    return 0


def register(sub: Any) -> None:
    sub.add_parser("migrate", help="Apply database migrations")
    alerts_p = sub.add_parser("alerts", help="Run the health alert checks once and print them")
    alerts_p.add_argument(
        "--test-notify", action="store_true", help="Also send a test Windows notification"
    )
    summ = sub.add_parser(
        "summary", help="Health summary: storage, collection gaps, quotas, forward tests"
    )
    summ.add_argument("--days", type=int, default=7)
    summ.add_argument("--write", type=Path, help="Also write it to this file")
    sub.add_parser("props-report", help="Player-prop snapshot coverage: books, markets and drops")
    sub.add_parser(
        "teams-unmatched",
        help="List provider team names not linked to an ESPN team, with their games",
    )
    link = sub.add_parser(
        "link-team", help="Link an unmatched team to its ESPN team (a dry run unless --apply)"
    )
    link.add_argument("--team-id", type=int, required=True, help="The unmatched team")
    link.add_argument("--espn-team-id", type=int, required=True, help="Its ESPN team (teams.id)")
    link.add_argument("--apply", action="store_true", help="Make the changes (default: show them)")
    serve = sub.add_parser("serve", help="Run the API on loopback")
    serve.add_argument("--port", type=int, default=8800)


HANDLERS: dict[str, Handler] = {
    "migrate": _migrate,
    "serve": _serve,
    "alerts": _alerts_check,
    "summary": _summary,
    "props-report": _props_report,
    "teams-unmatched": _teams,
    "link-team": _teams,
}
