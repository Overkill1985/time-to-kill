"""Command line: ``ttk migrate``, ``ttk ingest-schedule --sport NFL``,
``ttk ingest-odds --sport NFL``, ``ttk serve``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ttk import betting_math as bm
from ttk.config import Settings, get_settings
from ttk.domain import Market, Selection, Sport
from ttk.models.metrics import Score
from ttk.providers.base import OddsProvider
from ttk.research.nfl_elo import BettingResult, SplitReport

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


def _print_betting(rows: list[BettingResult]) -> None:
    for b in rows:
        roi = "   n/a" if b.roi is None else f"{b.roi:+.1%}"
        print(f"      edge >= {b.min_edge:.0%}: bets={b.bets:>5}  units={b.units:+.1f}  ROI={roi}")


def _backtest_espn(args: argparse.Namespace, database_url: str, sport: Sport) -> int:
    from ttk.db.session import make_engine, make_session_factory
    from ttk.research.cfb_model import CFB
    from ttk.research.espn_models import load_sport, sport_backtest
    from ttk.research.nba_model import NBA
    from ttk.research.ncaab_model import NCAAB

    config = {Sport.NBA: NBA, Sport.CFB: CFB, Sport.NCAAB: NCAAB}[sport]
    factory = make_session_factory(make_engine(database_url))
    with factory() as session:
        data = load_sport(session, config)
    if not data.closes:
        print(
            f"No {sport} lines; run `ttk import-espn-history --sport {sport}` first.",
            file=sys.stderr,
        )
        return 2
    report = sport_backtest(data, include_test=args.final_test)
    t, p = report.base.tuned, report.base.tuned.params
    splits = config.splits
    games_in_scope = {g.game_id for g in data.games}
    print(
        f"{sport} games {len(data.games)}, with closing lines "
        f"{len(games_in_scope & set(data.closes))}, with openers "
        f"{len(games_in_scope & set(data.opens))}"
    )
    checked, disagree = data.line_check()
    print(
        f"Line check: {disagree} of {checked} closing lines (2+ points) favour a different "
        f"team than the moneyline ({disagree / checked:.1%})"
        if checked
        else "Line check: no lines with moneylines to check"
    )
    hfa = (
        f"home_field={p.home_field}"
        if t.home_field_mode == "constant"
        else f"home_field={t.home_field_per_point} Elo per point of prior-3-season home margin"
    )
    print(
        f"Tuned on TRAIN {splits.train[0]}-{splits.train[1]}: K={p.k} {hfa} "
        f"regression={p.season_regression:.2f} toward {p.regression_target} "
        f"mov={p.margin_of_victory} "
        f"(train logloss {t.train_log_loss:.4f})"
    )
    if t.home_field_by_season:
        print(
            "  Home field by season (Elo): "
            + ", ".join(f"{s}:{v:.0f}" for s, v in sorted(t.home_field_by_season.items()))
        )
    w = report.base.models.key_number.weights
    print("Key-number weights (TRAIN): " + ", ".join(f"{k}:{w[k]:.2f}" for k in sorted(w)[:8]))
    if data.preseason:
        with_facts = {gid for gid, f in data.preseason.items() if any(f.values())}

        def covered(lo: int, hi: int) -> int:
            return sum(1 for g in data.games if g.game_id in with_facts and lo <= g.season <= hi)

        coverage = ", ".join(
            f"{label} {covered(*window)}"
            for label, window in (("train", splits.train), ("validate", splits.validate))
        )
        print(f"Games with preseason facts: {coverage}")
    if report.lineup_coverage:
        print(
            "Games with lineup features: "
            + ", ".join(f"{k} {v}" for k, v in report.lineup_coverage.items())
        )
    for name, m in report.models.items():
        print(
            f"{name} model (TRAIN, n={m.n_train}): margin = {m.intercept:.2f} "
            + " ".join(f"{c:+.4f} x {n}" for n, c in m.coefs.items())
            + (" [known at tip-off: close only]" if name in config.at_tip_only else "")
        )
    _print_split("TRAIN", report.base.train)
    _print_split("VALIDATE", report.base.validate)
    print("  Feature candidates on VALIDATE (vs the close):")
    for c in report.validate_features:
        v = c.vs_market
        print(
            f"    {c.name:<21} logloss {c.spread.log_loss:.4f}  vs market "
            f"{v.mean_log_loss_diff:+.4f} (SE {v.standard_error:.4f}, z {v.z:+.1f})"
        )
        _print_betting(c.betting)
    rmse = ", ".join(f"{k} {v:.2f}" for k, v in report.margin_rmse.items())
    print(f"  Margin RMSE on VALIDATE: {rmse}")
    print(f"\n== Betting the OPENER (VALIDATE, {report.opener_games} games with openers) ==")
    for o in report.openers:
        clv = "n/a" if o.avg_price_clv is None else f"{o.avg_price_clv:+.2%}"
        pts = "n/a" if o.avg_points_gained is None else f"{o.avg_points_gained:+.2f}"
        print(
            f"  {o.name:<21} price CLV {clv} (n={o.price_clv_n}, close on the same number); "
            f"points vs close {pts} (n={o.moved_n}, line moved)"
        )
        _print_betting(o.bets)
    if report.base.test is not None:
        _print_split("TEST (sealed until now)", report.base.test)
    else:
        print("\nTEST seasons are sealed. Score them once, with --final-test, after freezing.")
    return 0


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


def _injury_check(args: argparse.Namespace, settings: Settings) -> int:
    from ttk.db.session import make_engine, make_session_factory
    from ttk.research.espn_models import load_sport
    from ttk.research.nba_injuries import HORIZONS_HOURS, STATUS_PRIORS
    from ttk.research.nba_model import NBA

    factory = make_session_factory(make_engine(settings.database_url))
    with factory() as session:
        data = load_sport(session, NBA)
    rates = data.sit_rates
    if rates is None or not data.injuries:
        print("No NBA injury history yet (the collector records it from 2026-09-27).")
        return 0
    finished = [g for g in data.games if g.played and g.game_id in data.injuries]
    upcoming = [g for g in data.games if not g.played and g.game_id in data.injuries]
    print(
        f"Games with injury features: {len(finished)} finished, {len(upcoming)} upcoming "
        "(upcoming games use what we know now)"
    )
    statuses = sorted({status for _, status in rates.counts} | set(STATUS_PRIORS))
    for h in HORIZONS_HOURS:
        print(f"\nListed {h} h before tip-off: how often the player sat (rotation players)")
        for status in statuses:
            sat, n = rates.observed(h, status)
            if n == 0 and status not in ("Out", "Day-To-Day"):
                continue
            seen = f"{sat}/{n} sat ({sat / n:.0%})" if n else "no finished games yet"
            print(f"  {status:<12} {seen:<28} model uses q={rates.q(h, status):.2f}")
    return 0


def _db_paths(settings: Settings) -> tuple[Path, Path]:
    """(database file, data directory) for a SQLite URL."""
    db_path = Path(settings.database_url.removeprefix("sqlite:///"))
    return db_path, db_path.parent


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


def _forward(args: argparse.Namespace, settings: Settings) -> int:
    import time

    from ttk.db.models import utcnow
    from ttk.db.session import make_engine, make_session_factory
    from ttk.services.forward_models import (
        INJURY_SUBSTITUTION,
        build_forward_models,
        freeze_and_register,
        refresh_inputs,
    )
    from ttk.services.forward_test import forward_counts, forward_report, snapshot

    factory = make_session_factory(make_engine(settings.database_url))
    if args.command == "forward-freeze":
        with factory() as session:
            rows, checked, _ = freeze_and_register(
                session,
                args.sport,
                args.feature_set,
                label=args.label,
                live_substitution=INJURY_SUBSTITUTION if args.injury_substitution else None,
            )
            session.commit()
        for row in rows:
            print(
                f"registered {row.name} {row.version} "
                f"(DEVELOPMENT, validation log loss {row.log_loss or 0:.4f})"
            )
        print(f"verified: the frozen model reproduces the backtest on {checked} validation games")
        return 0
    if args.command == "forward-report":
        with factory() as session:
            for name, total, finished in forward_counts(session):
                print(f"{name}: {total} snapshots, {finished} on finished games")
            scores = forward_report(session, args.sport)
        for sc in scores:
            if not sc.decided:
                continue
            z = f"{sc.z:+.1f}" if sc.z is not None else "n/a"
            print(
                f"\n{sc.model}, {sc.horizon_hours} h before kickoff: {sc.decided} decided games\n"
                f"  log loss model {sc.model_log_loss:.4f} vs market {sc.market_log_loss:.4f} "
                f"(diff {sc.paired_diff:+.4f}, z {z})"
            )
            if sc.price_clv:
                avg = sum(sc.price_clv) / len(sc.price_clv)
                print(f"  price CLV at the same number {avg:+.2%} (n={len(sc.price_clv)})")
            if sc.points_vs_close:
                avg = sum(sc.points_vs_close) / len(sc.points_vs_close)
                print(f"  points vs the closing main line {avg:+.2f} (n={len(sc.points_vs_close)})")
            for b in sc.bets:
                roi = "n/a" if b.roi is None else f"{b.roi:+.1%}"
                print(
                    f"    edge >= {b.min_edge:.0%}: bets={b.bets} "
                    f"W-L-P={b.wins}-{b.losses}-{b.pushes} units={b.units:+.1f} ROI={roi}"
                )
        if not any(sc.decided for sc in scores):
            print("No finished games with forward snapshots yet.")
        return 0

    books = settings.bettable_book_keys()
    loop = args.command == "forward-run"
    rebuild_every = 6 * 3600
    log_file = None
    if getattr(args, "log", None) is not None:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        log_file = args.log.open("a", encoding="utf-8")

    def emit(message: str) -> None:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"
        if log_file is not None:
            log_file.write(line + "\n")
            log_file.flush()
        if sys.stdout is not None:  # None under pythonw (the scheduled task)
            print(line, flush=True)

    models, built_at = None, 0.0
    try:
        while True:
            started = time.monotonic()
            try:
                with factory() as session:
                    if models is None or time.monotonic() - built_at > rebuild_every:
                        if args.refresh_inputs:
                            key = settings.cfbd_api_key
                            for step in refresh_inputs(
                                factory,
                                now=utcnow(),
                                cfbd_api_key=key.get_secret_value() if key else None,
                            ):
                                emit(step)
                        models = build_forward_models(session)
                        built_at = time.monotonic()
                        session.commit()
                        emit("models: " + ", ".join(m.version.name for m in models))
                    stats = snapshot(session, models, bettable_books=books)
                    session.commit()
                emit(f"forward snapshots: {stats.written} written, skipped {dict(stats.skipped)}")
                if loop:
                    _weekly_summary(settings, factory, emit)
            except Exception:
                if not loop:
                    raise
                import traceback

                emit("forward pass failed; retrying next pass:\n" + traceback.format_exc())
            if not loop:
                return 0
            time.sleep(max(args.loop_minutes * 60 - (time.monotonic() - started), 0))
    finally:
        if log_file is not None:
            log_file.close()


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
    import traceback

    from ttk.db.session import make_engine, make_session_factory
    from ttk.providers.espn import EspnScheduleProvider
    from ttk.providers.espn_boxscore import EspnBoxscores
    from ttk.providers.espn_injuries import EspnInjuries
    from ttk.services.bets import settle_bets
    from ttk.services.collector import (
        collect_once,
        refresh_boxscores,
        refresh_injuries,
        refresh_schedules,
        refresh_team_boxes,
    )
    from ttk.services.parlay_lab import settle_parlays

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
        emit(f"quota remaining: {remaining}")
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


def _card(args: argparse.Namespace, settings: Settings) -> int:
    from zoneinfo import ZoneInfo

    from ttk.db.session import make_engine, make_session_factory
    from ttk.services.daily_card import build_card
    from ttk.services.nfl_spread_predictor import NflSpreadPredictor

    eastern = ZoneInfo("America/New_York")
    day = args.date or datetime.now(eastern).date()
    factory = make_session_factory(make_engine(settings.database_url))
    from ttk.services.forward_models import build_card_models

    with factory() as session:
        predictor = NflSpreadPredictor.build(session)
        card = build_card(
            session,
            day,
            settings.qualification_rules(),
            predictor=predictor,
            persist=not args.no_persist,
            bettable_books=settings.bettable_book_keys(),
            models=build_card_models(session),  # other sports' frozen models (slow)
        )

    print(f"TIME-TO-KILL  {day:%A %B %d, %Y}".upper())
    print()
    modeled = sum(s.modeled for s in card.by_sport.values())
    with_market = sum(s.with_market for s in card.by_sport.values())
    print(f"{with_market} games with a live market, {modeled} modeled")
    print(card.headline)
    if card.bettable_books is None:
        print(
            "Best price and EV use EVERY book, including exchanges and prediction markets "
            "(set TTK_BETTABLE_BOOKS)."
        )
    else:
        print("Best price and EV use: " + ", ".join(sorted(card.bettable_books)))
    for sport, s in sorted(card.by_sport.items()):
        print(
            f"  {sport:<6} games {s.games:>3}  with market {s.with_market:>3}  "
            f"modeled {s.modeled:>3}  qualified {s.qualified:>2}  lean {s.lean:>2}"
        )
    if predictor is None:
        print("\nNo NFL model inputs: run import-nfl-history and import-nfl-pbp.")
    for e in card.entries:
        kickoff = e.commence_time.astimezone(eastern)
        movement = (
            f"{e.line_opening:+g} -> {e.line_current:+g}"
            if e.line_opening is not None and e.line_current is not None
            else "n/a"
        )
        print(f"\n[{e.classification}] {e.bet}   {e.matchup}, {kickoff:%a %I:%M %p} ET")
        print(
            f"  best {bm.format_american(e.american_odds)} at {e.sportsbook}   "
            f"fair {bm.format_american(e.fair_american_odds)}   line movement {movement}"
        )
        print(
            f"  model {e.model_probability:.1%}  market {e.market_probability:.1%}  "
            f"edge {e.edge * 100:+.1f} pts  EV {e.ev_percent:+.1f}%  "
            f"push {e.push_probability:.1%}"
        )
        print(
            f"  uncertainty {e.uncertainty}  data {e.data_quality}  "
            f"odds {e.odds_age_minutes:.0f} min old  model {e.model_version}"
        )
        if args.why:
            for c in e.checks:
                mark = "PASS" if c.passed else "FAIL"
                print(f"    {mark:<4} {c.name:<22} {c.actual:<16} required {c.required}")
        elif e.notes:
            print("  why: " + "; ".join(e.notes))
    for side in card.unbettable:
        print(f"\n[NO BETTABLE PRICE] {side}")
    for u in card.unmodeled:
        print(f"\n[UNMODELED] {u.sport} {u.matchup}: {u.reason}")
    return 0


def _fmt_pct(value: float | None, n: int | None = None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:+.1f}%" + (f" (n={n})" if n is not None else "")


def _bets(args: argparse.Namespace, settings: Settings) -> int:
    from sqlalchemy import select

    from ttk.db.models import Bet
    from ttk.db.session import make_engine, make_session_factory
    from ttk.domain import BetResult
    from ttk.services.bets import BetError, NewBet, performance, record_bet, settle_bets

    factory = make_session_factory(make_engine(settings.database_url))
    with factory() as session:
        if args.bets_command == "add":
            placed_at = args.placed_at
            if placed_at is not None and placed_at.tzinfo is None:
                print("--placed-at needs a UTC offset, e.g. -04:00", file=sys.stderr)
                return 2
            try:
                bet = record_bet(
                    session,
                    NewBet(
                        args.game_id,
                        args.market,
                        args.selection,
                        args.line,
                        args.odds,
                        args.book,
                        args.stake,
                        placed_at,
                        args.notes,
                    ),
                )
            except BetError as exc:
                print(f"Not recorded: {exc}", file=sys.stderr)
                return 2
            session.commit()
            print(
                f"Recorded bet {bet.id}: {bet.description} "
                f"{bm.format_american(bet.american_odds)}, stake {bet.stake:g}"
            )
            print(
                f"  model {_fmt_pct(bet.model_probability)}  market "
                f"{_fmt_pct(bet.market_probability)}  edge {_fmt_pct(bet.edge)}  "
                f"EV {_fmt_pct(bet.expected_value)}"
            )
            if bet.model_probability is None:
                print("  (no model prediction at this line before the bet; run `ttk card`)")
            return 0

        if args.bets_command == "list":
            stmt = select(Bet).where(Bet.parlay_id.is_(None)).order_by(Bet.placed_at)
            if args.pending:
                stmt = stmt.where(Bet.result == BetResult.PENDING)
            for bet in session.scalars(stmt):
                pl = "" if bet.profit_loss is None else f"  P/L {bet.profit_loss:+.2f}"
                clv = "" if bet.clv is None else f"  CLV {bet.clv * 100:+.1f}%"
                print(
                    f"{bet.id:>4} {bet.placed_at:%Y-%m-%d %H:%M} {bet.result:<7} "
                    f"{bet.description} {bm.format_american(bet.american_odds)} "
                    f"stake {bet.stake:g}{pl}{clv}"
                )
            return 0

        if args.bets_command == "settle":
            settled = settle_bets(session)
            session.commit()
            for bet in settled:
                print(
                    f"{bet.id:>4} {bet.result:<5} {bet.description} "
                    f"P/L {bet.profit_loss or 0:+.2f}  CLV {_fmt_pct(bet.clv)}"
                )
            print(f"{len(settled)} bets settled")
            return 0

        perf = performance(
            session, sport=args.sport, market=args.market, unit_size=settings.unit_size
        )
        print(
            f"Settled {perf.bets}: {perf.wins}-{perf.losses}-{perf.pushes} "
            f"(voids {perf.voids}), pending {perf.pending} (stake {perf.pending_stake:g})"
        )
        print(
            f"  staked {perf.staked:g}  profit {perf.profit:+.2f}  ROI {_fmt_pct(perf.roi)}  "
            f"units {perf.units:+.2f} (unit {perf.unit_size:g})"
        )
        print(
            f"  hit rate {'n/a' if perf.hit_rate is None else f'{perf.hit_rate:.1%}'}  "
            f"max drawdown {perf.max_drawdown:.2f}"
        )
        print(
            f"  avg edge {_fmt_pct(perf.avg_edge, perf.edge_n)}  "
            f"avg EV {_fmt_pct(perf.avg_ev, perf.ev_n)}  "
            f"avg CLV {_fmt_pct(perf.avg_clv, perf.clv_n)}"
        )
        return 0


def _simulate(args: argparse.Namespace, settings: Settings) -> int:
    from ttk.db.session import make_engine, make_session_factory
    from ttk.models.simulation import PRESETS
    from ttk.services.nfl_spread_predictor import NflSpreadPredictor
    from ttk.services.simulation_service import SimulationError, run_simulation

    factory = make_session_factory(make_engine(settings.database_url))
    with factory() as session:
        try:
            s = run_simulation(
                session,
                args.game_id,
                NflSpreadPredictor.build(session),
                iterations=PRESETS[args.preset],
                seed=args.seed,
                bettable_books=settings.bettable_book_keys(),
            )
        except SimulationError as exc:
            print(exc, file=sys.stderr)
            return 2
    print(
        f"{s.matchup}: {s.iterations:,} simulations, seed {s.seed}, total centered on "
        f"{s.total_line:g} ({s.total_line_source})"
    )
    print(
        f"  mean score {s.home_score_mean:.1f}-{s.away_score_mean:.1f} (home-away)  "
        f"home win {s.home_win.value:.1%} +- {s.home_win.standard_error:.1%}  "
        f"tie {s.tie.value:.1%}"
    )
    print("  margin " + "  ".join(f"{k} {v:+g}" for k, v in s.margin_quantiles.items()))
    print("  total  " + "  ".join(f"{k} {v:g}" for k, v in s.total_quantiles.items()))
    print("  home spread sensitivity (line: cover / push):")
    print(
        "    " + "  ".join(f"{r.line:+g}: {r.win:.1%}/{r.push:.1%}" for r in s.spread_sensitivity)
    )
    print("  over sensitivity:")
    print("    " + "  ".join(f"{r.line:g}: {r.win:.1%}/{r.push:.1%}" for r in s.total_sensitivity))
    for side in s.spread_sides + s.total_sides:
        if side.american_odds is None:
            continue
        worst = (
            "none in range"
            if side.max_acceptable_line is None
            else f"{side.max_acceptable_line:+g}"
        )
        print(
            f"  {side.selection:<5} main {side.line:+g} at {bm.format_american(side.american_odds)}"
            f" ({side.sportsbook}): maximum acceptable line {worst}"
        )
    for name, p in s.joint.items():
        print(f"  P({name.replace('_', ' ')}) = {p.value:.1%}")
    return 0


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
    summ = sub.add_parser(
        "summary", help="Health summary: storage, collection gaps, quotas, forward tests"
    )
    summ.add_argument("--days", type=int, default=7)
    summ.add_argument("--write", type=Path, help="Also write it to this file")
    fwd_freeze = sub.add_parser(
        "forward-freeze",
        help="Freeze a validated model (fitted on TRAIN) for forward testing; registers it",
    )
    fwd_freeze.add_argument(
        "--sport", type=Sport, choices=[Sport.CFB, Sport.NBA, Sport.NCAAB], required=True
    )
    fwd_freeze.add_argument("--feature-set", required=True, help="e.g. inseason (CFB), eff (NCAAB)")
    fwd_freeze.add_argument("--label", help="Name in the registry (default: the feature set)")
    fwd_freeze.add_argument(
        "--injury-substitution",
        action="store_true",
        help="NBA: replace who sits at tip (missing_diff) by the injury report's expected "
        "missing value at the snapshot",
    )
    fwd_snap = sub.add_parser(
        "forward-snapshot", help="One pass: snapshot forward-tested models for games due"
    )
    fwd_run = sub.add_parser("forward-run", help="Snapshot forward-tested models on a loop")
    fwd_run.add_argument("--loop-minutes", type=float, default=30)
    fwd_run.add_argument("--log", type=Path, help="Append output here (e.g. data/logs/forward.log)")
    for p_ in (fwd_snap, fwd_run):
        p_.add_argument(
            "--refresh-inputs",
            action="store_true",
            help="Before each model build: nflverse NFL games and play-by-play (~15 MB), "
            "CollegeFootballData efficiency (1 call)",
        )
    fwd_report = sub.add_parser("forward-report", help="Score forward snapshots on finished games")
    fwd_report.add_argument("--sport", type=Sport, choices=list(Sport))
    sub.add_parser(
        "injury-check",
        help="NBA: how often players listed on the injury report actually sat, by status",
    )
    repair = sub.add_parser(
        "repair-merged-games",
        help="Split games that wrongly joined two ESPN events (dry run unless --apply)",
    )
    repair.add_argument("--apply", action="store_true")
    repair.add_argument("--delay", type=float, default=0.25)
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
    for name, label in (
        ("backtest-nba", "NBA"),
        ("backtest-cfb", "college football"),
        ("backtest-ncaab", "men's college basketball"),
    ):
        espn_bt = sub.add_parser(name, help=f"Tune and backtest the {label} spread models")
        espn_bt.add_argument(
            "--final-test",
            action="store_true",
            help="Also score the sealed TEST seasons. Do this once, after the model is frozen.",
        )
    card = sub.add_parser("card", help="Show the daily card for a date (US Eastern)")
    card.add_argument("--date", type=date.fromisoformat, help="YYYY-MM-DD (default: today)")
    card.add_argument(
        "--why", action="store_true", help="Show every qualification check for each bet"
    )
    card.add_argument("--no-persist", action="store_true", help="Do not write prediction snapshots")
    bets = sub.add_parser("bets", help="Bet tracker")
    bets_sub = bets.add_subparsers(dest="bets_command", required=True)
    add = bets_sub.add_parser("add", help="Record a bet you placed")
    add.add_argument("--game-id", type=int, required=True, help="See `ttk card` or /api/games")
    add.add_argument(
        "--market",
        type=Market,
        choices=[Market.MONEYLINE, Market.SPREAD, Market.TOTAL],
        required=True,
    )
    add.add_argument("--selection", type=Selection, choices=list(Selection), required=True)
    add.add_argument("--line", type=float, help="Your line, e.g. -3.5 (spread) or 44.5 (total)")
    add.add_argument("--odds", type=float, required=True, help="American odds, e.g. -110")
    add.add_argument("--book", required=True, help="Sportsbook key, e.g. draftkings")
    add.add_argument("--stake", type=float, required=True)
    add.add_argument(
        "--placed-at",
        type=datetime.fromisoformat,
        help="ISO time with offset, e.g. 2026-09-27T11:05-04:00 (default: now)",
    )
    add.add_argument("--notes")
    bets_list = bets_sub.add_parser("list", help="List bets")
    bets_list.add_argument("--pending", action="store_true")
    bets_sub.add_parser("settle", help="Grade pending bets whose games are final")
    summary = bets_sub.add_parser("summary", help="Performance summary")
    summary.add_argument("--sport", type=Sport, choices=list(Sport))
    summary.add_argument(
        "--market", type=Market, choices=[Market.MONEYLINE, Market.SPREAD, Market.TOTAL]
    )
    simulate = sub.add_parser("simulate", help="Monte Carlo one game (NFL)")
    simulate.add_argument("--game-id", type=int, required=True)
    simulate.add_argument("--preset", choices=["quick", "detailed", "research"], default="quick")
    simulate.add_argument("--seed", type=int, help="Fix for a reproducible run")
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

    if args.command == "repair-espn-lines":
        return _repair_espn_lines(args, settings)

    if args.command in ("forward-freeze", "forward-snapshot", "forward-run", "forward-report"):
        return _forward(args, settings)

    if args.command == "summary":
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

    if args.command == "injury-check":
        return _injury_check(args, settings)

    if args.command == "repair-merged-games":
        return _repair_merged_games(args, settings)

    if args.command in ("import-cfbd", "import-cfbd-games"):
        from ttk.db.session import make_engine, make_session_factory
        from ttk.providers.cfbd import CfbdClient
        from ttk.services.cfbd_import import import_cfbd, import_cfbd_games

        if settings.cfbd_api_key is None:
            print(
                "Set TTK_CFBD_API_KEY in .env (free at collegefootballdata.com).", file=sys.stderr
            )
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

    if args.command == "import-team-boxes":
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

    if args.command == "import-boxscores":
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

    if args.command == "import-espn-history":
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

    if args.command == "backtest-nfl-elo":
        return _backtest_nfl_elo(args, settings.database_url)

    espn_backtests = {
        "backtest-nba": Sport.NBA,
        "backtest-cfb": Sport.CFB,
        "backtest-ncaab": Sport.NCAAB,
    }
    if args.command in espn_backtests:
        return _backtest_espn(args, settings.database_url, espn_backtests[args.command])

    if args.command == "card":
        return _card(args, settings)

    if args.command == "bets":
        return _bets(args, settings)

    if args.command == "simulate":
        return _simulate(args, settings)

    if args.command == "serve":
        import uvicorn

        uvicorn.run("ttk.api.app:create_app", factory=True, host="127.0.0.1", port=args.port)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
