"""Backtests and the one-time test-season scoring."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from ttk.cli._common import (
    Handler,
    _db_paths,
)
from ttk.config import Settings
from ttk.domain import Sport
from ttk.models.metrics import Score
from ttk.research.nfl_elo import BettingResult, SplitReport


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


def _backtest_totals(settings: Settings, sport: Sport) -> int:
    from ttk.db.session import make_engine, make_session_factory
    from ttk.research.espn_models import load_sport
    from ttk.research.totals import TotalsReport, backtest, load_team_games
    from ttk.services.forward_models import CONFIGS

    config = CONFIGS[sport]
    factory = make_session_factory(make_engine(settings.database_url))
    with factory() as session:
        data = load_sport(session, config)
        team_games = load_team_games(session, str(sport))
    result = backtest(
        data.games, team_games, data.closes, config.splits.train, config.splits.validate
    )
    for params, rmse in result.grid:
        print(
            f"half-life {params.half_life:>4g}  carryover {params.carryover}  "
            f"opponent-adjusted {params.opponent_adjust!s:<5}  TRAIN error {rmse:.3f}"
        )
    p = result.params
    print(
        f"\nchosen: half-life {p.half_life:g}, carryover {p.carryover}, "
        f"opponent-adjusted {p.opponent_adjust}; calibration {result.calibration.intercept:+.2f} "
        f"+ {result.calibration.slope:.3f} x predicted, sigma {result.calibration.sigma:.2f}"
    )

    def show(label: str, r: TotalsReport) -> None:
        (sd, sse), (ad, ase) = r.standalone_vs_market, r.anchored_vs_market
        print(
            f"{label}: {r.games} games, error {r.rmse:.2f} vs closing total {r.market_rmse:.2f}; "
            f"{r.priced} priced: standalone {sd:+.5f} (z {sd / sse if sse else 0:+.2f}), "
            f"anchored {ad:+.5f} (z {ad / ase if ase else 0:+.2f})"
        )
        for edge, (n, roi) in r.anchored_bets.items():
            print(f"    anchored edge >= {edge:.0%}: {n} bets, ROI {roi:+.1%}")

    show("TRAIN", result.train)
    show("VALIDATE", result.validate)
    print("Test seasons are not read. Log loss differences: negative beats the market.")
    return 0


def _score_test(args: argparse.Namespace, settings: Settings) -> int:
    """Score every frozen model of a sport on its sealed test seasons, once."""
    import json

    from sqlalchemy import select

    from ttk.db.models import ModelVersion, utcnow
    from ttk.db.session import make_engine, make_session_factory
    from ttk.research.espn_models import load_sport
    from ttk.research.frozen import FrozenSpreadModel, score_frozen
    from ttk.services.forward_models import CONFIGS

    _, data_dir = _db_paths(settings)
    out_dir = data_dir / "reports" / "test_scores"
    factory = make_session_factory(make_engine(settings.database_url))
    with factory() as session:
        rows = session.scalars(
            select(ModelVersion).where(
                ModelVersion.sport == args.sport, ModelVersion.artifact.is_not(None)
            )
        ).all()
        # Spread models only: moneyline and totals models were frozen after the test
        # seasons were used, and are judged by forward tests.
        rows = [r for r in rows if (r.artifact or {}).get("market", "SPREAD") == "SPREAD"]
        if not rows:
            print(f"No frozen {args.sport} models (see `ttk forward-freeze`).", file=sys.stderr)
            return 2
        done = [r for r in rows if (out_dir / f"{r.name}-{r.version}.json").exists()]
        if done:
            names = ", ".join(r.name for r in done)
            print(
                f"Already scored on the test seasons: {names}. A sealed season is scored once; "
                f"the reports are in {out_dir}.",
                file=sys.stderr,
            )
            return 1
        data = load_sport(session, CONFIGS[args.sport])
        for row in rows:
            assert row.artifact is not None
            window = tuple(row.artifact["splits"]["test"])
            result = score_frozen(FrozenSpreadModel(row.artifact, data), window)
            report = {
                "model": row.name,
                "version": row.version,
                "scored_at": utcnow().isoformat(),
                "note": "One-time score on sealed test seasons (docs/MODEL-GOVERNANCE.md). "
                "A live_substitution model is scored as frozen, with the at-tip feature.",
                **result,
            }
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{row.name}-{row.version}.json").write_text(
                json.dumps(report, indent=1, default=str), "utf-8"
            )
            z = result["z"]
            print(
                f"{row.name} {row.version} on {window[0]}-{window[1]}: "
                f"{result['spread_games']} spread games, log loss {result['log_loss']:.4f} "
                f"vs market {result['market_log_loss']:.4f} (z {z:+.1f}); margin RMSE "
                f"{result['margin_rmse']:.2f} vs close {result['market_rmse']:.2f}"
            )
            for b in result["betting"]:
                roi = "n/a" if b["roi"] is None else f"{b['roi']:+.1%}"
                print(
                    f"    edge >= {b['min_edge']:.0%}: {b['bets']} bets, "
                    f"{b['wins']}-{b['losses']}-{b['pushes']}, ROI {roi}"
                )
            if result["opener"]:
                o = result["opener"]
                clv = "n/a" if o["price_clv"] is None else f"{o['price_clv']:+.2%}"
                pts = "n/a" if o["points_vs_close"] is None else f"{o['points_vs_close']:+.2f}"
                print(
                    f"    opener ({o['games']} games): price CLV {clv} (n={o['price_clv_n']}), "
                    f"points vs close {pts} (n={o['points_n']})"
                )
    print(f"Reports written to {out_dir}")
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


def register(sub: Any) -> None:
    score_p = sub.add_parser(
        "score-test",
        help="Score a sport's frozen models on their sealed test seasons (once only)",
    )
    score_p.add_argument(
        "--sport", type=Sport, choices=[Sport.CFB, Sport.NBA, Sport.NCAAB], required=True
    )
    sub.add_parser(
        "injury-check",
        help="NBA: how often players listed on the injury report actually sat, by status",
    )
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
    bt_totals = sub.add_parser(
        "backtest-totals", help="Tune and backtest a basketball totals model (pace and efficiency)"
    )
    bt_totals.add_argument(
        "--sport", type=Sport, choices=[Sport.NCAAB, Sport.NBA], default=Sport.NCAAB
    )


HANDLERS: dict[str, Handler] = {
    "backtest-nfl-elo": lambda a, s: _backtest_nfl_elo(a, s.database_url),
    "backtest-nba": lambda a, s: _backtest_espn(a, s.database_url, Sport.NBA),
    "backtest-cfb": lambda a, s: _backtest_espn(a, s.database_url, Sport.CFB),
    "backtest-ncaab": lambda a, s: _backtest_espn(a, s.database_url, Sport.NCAAB),
    "backtest-totals": lambda a, s: _backtest_totals(s, a.sport),
    "score-test": _score_test,
    "injury-check": _injury_check,
}
