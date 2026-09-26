"""Command line: ``ttk migrate``, ``ttk ingest-schedule --sport NFL``,
``ttk ingest-odds --sport NFL``, ``ttk serve``."""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from ttk.config import Settings, get_settings
from ttk.domain import Sport
from ttk.models.metrics import Score
from ttk.providers.base import OddsProvider
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
    rmse = ", ".join(f"{name} {value:.2f}" for name, value in r.margin_rmse.items())
    print(f"  Margin RMSE (points, n={r.margin_n}): {rmse}")
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
    from ttk.research.nfl_elo import (
        DEFAULT_SPLITS,
        backtest,
        load_feature_inputs,
        load_nfl_games,
    )

    factory = make_session_factory(make_engine(database_url))
    with factory() as session:
        games, lines = load_nfl_games(session)
        inputs = load_feature_inputs(session)
    if not games:
        print("No NFL games in the database; run `ttk import-nfl-history` first.", file=sys.stderr)
        return 2
    if not inputs.available:
        print("No play-by-play aggregates; run `ttk import-nfl-pbp` for EPA/QB models.")
    report = backtest(games, lines, include_test=args.final_test, inputs=inputs)
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
    f = report.models.features
    if f is not None:
        fp = f.params
        print(
            f"Feature model (TRAIN): margin = {f.intercept:.2f} "
            + " ".join(f"{c:+.4f} x {n}" for n, c in f.coefs.items())
            + f", sigma {f.key.base.sigma:.2f} (team half-life {fp.team_half_life_games:g} games, "
            f"season carryover {fp.season_carryover:g}, "
            f"QB prior {fp.qb_prior_dropbacks:g} dropbacks)"
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
            "features_key_numbers": "linear margin(elo, epa, qb change) + key numbers",
            "market_anchored_features": "logistic(market no-vig, feature-model disagreement)",
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
                        + (
                            ["epa_net_diff_pts", "qb_change_diff_pts"]
                            if "features" in c.name
                            else []
                        )
                        + (
                            ["market_no_vig_same_snapshot"]
                            if c.name.startswith("market_anchored")
                            else []
                        ),
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


def _collect_odds(args: argparse.Namespace, settings: Settings) -> int:
    import time

    from ttk.db.session import make_engine, make_session_factory
    from ttk.providers.propline import RateLimited
    from ttk.services.collector import collect_once

    provider = _odds_provider(settings, args.provider)
    if provider is None:
        return 2
    sports = [Sport(s.strip().upper()) for s in args.sports.split(",") if s.strip()]
    factory = make_session_factory(make_engine(settings.database_url))
    remaining: int | None = None
    while True:
        started = time.monotonic()
        try:
            result = collect_once(factory, provider, sports, remaining=remaining)
        except RateLimited as exc:
            wait = exc.retry_after or 60.0
            print(f"Rate limited; waiting {wait:.0f} s", file=sys.stderr)
            time.sleep(wait)
            continue
        remaining = result.remaining
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        for run in result.runs:
            print(
                f"{stamp} {run.sport}: {run.status} {run.records_written} snapshots"
                + (f", error={run.error}" if run.error else "")
            )
        for sport, why in result.skipped.items():
            print(f"{stamp} {sport}: skipped ({why})")
        print(f"{stamp} quota remaining: {remaining}")
        if args.loop_minutes is None:
            return 0 if all(r.status == "SUCCESS" for r in result.runs) else 1
        time.sleep(max(args.loop_minutes * 60 - (time.monotonic() - started), 0))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ttk")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="Apply database migrations")
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
    backtest = sub.add_parser("backtest-nfl-elo", help="Tune and backtest the NFL spread models")
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

    if args.command == "collect-odds":
        return _collect_odds(args, settings)

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

    if args.command == "import-nfl-pbp":
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

    if args.command == "backtest-nfl-elo":
        return _backtest_nfl_elo(args, settings.database_url)

    if args.command == "serve":
        import uvicorn

        uvicorn.run("ttk.api.app:create_app", factory=True, host="127.0.0.1", port=args.port)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
