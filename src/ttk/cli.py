"""Command line: ``ttk migrate``, ``ttk ingest-schedule --sport NFL``,
``ttk ingest-odds --sport NFL``, ``ttk serve``."""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from ttk.config import get_settings
from ttk.domain import Sport
from ttk.models.metrics import Score
from ttk.research.nfl_elo import SplitReport

ROOT = Path(__file__).resolve().parents[2]


def migrate(database_url: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")


def _print_split(name: str, r: SplitReport) -> None:
    def fmt(s: Score) -> str:
        return f"n={s.n:>5}  brier={s.brier:.4f}  logloss={s.log_loss:.4f}"

    print(f"\n== {name} {r.seasons[0]}-{r.seasons[1]} ==")
    print(f"  Moneyline  Elo (all games)     {fmt(r.moneyline_elo)}")
    print(f"             Home-win baseline   {fmt(r.moneyline_home_baseline)}")
    print(f"             Elo (priced games)  {fmt(r.moneyline_elo_on_market_games)}")
    print(f"             Market no-vig       {fmt(r.moneyline_market)}")
    print("  Spread, games with real prices on both sides (pushes excluded from scoring):")
    print(f"             Market no-vig       {fmt(r.spread_market)}")
    for c in r.spread_candidates:
        v = c.vs_market
        print(
            f"             {c.name:<19} {fmt(c.spread)}  "
            f"vs market {v.mean_log_loss_diff:+.4f} (SE {v.standard_error:.4f}, z {v.z:+.1f})"
        )
    pushes = ", ".join(
        f"{c.name} {c.push_rate_predicted:.2%}"
        for c in r.spread_candidates
        if c.push_rate_predicted is not None
    )
    print(f"  Pushes     actual {r.push_rate_actual:.2%}; predicted: {pushes}")
    print("  ATS at reported prices, betting the side with the larger model edge:")
    for c in r.spread_candidates:
        print(f"    {c.name}")
        for b in c.betting:
            roi = "   n/a" if b.roi is None else f"{b.roi:+.1%}"
            print(
                f"      edge >= {b.min_edge:.0%}: bets={b.bets:>5}  "
                f"W-L-P={b.wins}-{b.losses}-{b.pushes}  units={b.units:+.1f}  ROI={roi}"
            )


def _backtest_nfl_elo(args: argparse.Namespace, database_url: str) -> int:
    import json

    from sqlalchemy import select

    from ttk.db.models import ModelVersion
    from ttk.db.session import make_engine, make_session_factory
    from ttk.research.nfl_elo import DEFAULT_SPLITS, backtest, load_nfl_games

    factory = make_session_factory(make_engine(database_url))
    with factory() as session:
        games, lines = load_nfl_games(session)
    if not games:
        print("No NFL games in the database; run `ttk import-nfl-history` first.", file=sys.stderr)
        return 2
    report = backtest(games, lines, include_test=args.final_test)
    t, p = report.tuned, report.tuned.params
    hfa = (
        f"home_field={p.home_field}"
        if t.home_field_mode == "constant"
        else f"home_field={t.home_field_per_point} Elo per point of prior-3-season home margin"
    )
    print(
        f"Tuned on TRAIN: K={p.k} {hfa} regression={p.season_regression:.2f} "
        f"mov={p.margin_of_victory} (train logloss {t.train_log_loss:.4f})"
    )
    m, a = report.models.normal, report.models.anchored
    print(
        f"Margin model (TRAIN): margin = {m.intercept:.2f} + {m.slope:.4f} x elo_diff, "
        f"sigma {m.sigma:.2f}"
    )
    w = report.models.key_number.weights
    print(
        "Key-number weights (TRAIN): "
        + ", ".join(f"{k}:{w[k]:.2f}" for k in (0, 1, 3, 4, 6, 7, 10, 14))
    )
    print(
        f"Market-anchored (TRAIN, n={a.n_train}): logit p = {a.intercept:+.3f} "
        f"+ {a.market_coef:.3f} x logit(market) + {a.disagreement_coef:+.4f} x disagreement_pts"
    )
    _print_split("TRAIN", report.train)
    _print_split("VALIDATE", report.validate)
    if report.test is not None:
        _print_split("TEST (sealed until now)", report.test)
    else:
        print("\nTEST seasons are sealed. Score them once, with --final-test, after freezing.")

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report.to_dict(), indent=1, default=str), "utf-8")
        print(f"\nReport written to {args.report}")

    if args.register:
        s = DEFAULT_SPLITS
        algorithms = {
            "elo_normal": "elo + normal margin",
            "elo_key_numbers": "elo + key-number margin",
            "market_anchored": "logistic(market no-vig, elo disagreement)",
        }
        with factory() as session:
            for c in report.validate.spread_candidates:
                name = f"nfl-spread-{c.name.replace('_', '-')}"
                exists = session.scalar(
                    select(ModelVersion).where(
                        ModelVersion.name == name, ModelVersion.version == "0.1.0"
                    )
                )
                if exists is not None:
                    print(f"{name} 0.1.0 is already registered; bump the version.")
                    continue
                session.add(
                    ModelVersion(
                        name=name,
                        version="0.1.0",
                        sport="NFL",
                        market="SPREAD",
                        algorithm=algorithms[c.name],
                        features=["elo_diff", "home_field", "neutral_site"]
                        + (["market_no_vig_same_snapshot"] if c.name == "market_anchored" else []),
                        training_window=f"{s.train[0]}-{s.train[1]}",
                        validation_window=f"{s.validate[0]}-{s.validate[1]}",
                        calibration_method="fitted on TRAIN",
                        brier_score=c.spread.brier,
                        log_loss=c.spread.log_loss,
                        status="DEVELOPMENT",
                    )
                )
                print(f"Registered {name} 0.1.0 as DEVELOPMENT (validation metrics).")
            session.commit()
    return 0


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
    history = sub.add_parser(
        "import-nfl-history", help="Import NFL games, results and reported lines (nflverse)"
    )
    history.add_argument("--from-season", type=int, default=1999)
    history.add_argument("--to-season", type=int)
    backtest = sub.add_parser("backtest-nfl-elo", help="Tune and backtest the NFL Elo baseline")
    backtest.add_argument(
        "--final-test",
        action="store_true",
        help="Also score the sealed TEST seasons. Do this once, after the model is frozen.",
    )
    backtest.add_argument(
        "--register", action="store_true", help="Record the result in the model registry"
    )
    backtest.add_argument("--report", type=Path, help="Write the full report as JSON here")
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

    if args.command == "import-nfl-history":
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

    if args.command == "backtest-nfl-elo":
        return _backtest_nfl_elo(args, settings.database_url)

    if args.command == "serve":
        import uvicorn

        uvicorn.run("ttk.api.app:create_app", factory=True, host="127.0.0.1", port=args.port)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
