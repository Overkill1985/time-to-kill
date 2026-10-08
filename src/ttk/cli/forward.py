"""Forward tests: freeze, snapshot, run, report; the Performance Lab."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from ttk.cli._common import (
    Handler,
    _alerts,
    _weekly_summary,
)
from ttk.config import Settings
from ttk.domain import Sport


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
    from ttk.services.forward_test import (
        forward_counts,
        forward_report,
        score_finished,
        snapshot,
    )

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
    if args.command == "forward-freeze-totals":
        from ttk.services.forward_models import freeze_totals_and_register

        with factory() as session:
            rows, checked, result = freeze_totals_and_register(session, args.sport)
            session.commit()
        v = result.validate
        for row in rows:
            print(f"registered {row.name} {row.version} (DEVELOPMENT)")
        print(
            f"verified: the frozen model reproduces VALIDATE on {checked} games "
            f"(error {v.rmse:.2f} vs closing total {v.market_rmse:.2f})"
        )
        return 0
    if args.command == "forward-freeze-ml":
        from ttk.services.forward_models import freeze_moneyline

        with factory() as session:
            rows, ml_scores = freeze_moneyline(session, args.sport, label=args.label)
            session.commit()
        for row in rows:
            vs = ml_scores[row.artifact["moneyline_variant"] if row.artifact else ""]
            print(
                f"registered {row.name} {row.version} (DEVELOPMENT; validation {vs.games} games, "
                f"log loss {vs.log_loss:.4f} vs moneyline market {vs.market_log_loss:.4f}, "
                f"z {vs.z:+.2f})"
            )
        return 0
    if args.command == "forward-report":
        with factory() as session:
            score_finished(session)
            session.commit()
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
                    scored = score_finished(session)
                    session.commit()
                emit(
                    f"forward snapshots: {stats.written} written, skipped {dict(stats.skipped)}; "
                    f"{scored} finished scored"
                )
                if loop:
                    _weekly_summary(settings, factory, emit)
                    _alerts(settings, factory, emit)
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


def _lab(args: argparse.Namespace, settings: Settings) -> int:
    from ttk.db.session import make_engine, make_session_factory
    from ttk.services.performance_lab import lab_choices, performance_lab

    def num(x: float | None, fmt: str) -> str:
        return "n/a" if x is None else format(x, fmt)

    factory = make_session_factory(make_engine(settings.database_url))
    with factory() as session:
        choices = [
            c
            for c in lab_choices(session)
            if (args.model is None or c["model"] == args.model)
            and (args.horizon is None or c["horizon_hours"] == args.horizon)
        ]
        if not choices:
            print("No finished games with forward snapshots for that model/horizon yet.")
            return 0
        for c in choices:
            lab = performance_lab(session, c["model"], c["horizon_hours"])
            print(
                f"\n{lab['model']}, {lab['horizon_hours']} h before kickoff: {lab['games']} games"
            )
            print("  model P(side) >=   bets  record     ROI (+/- SE)      hit / break-even  CLV")
            for t in lab["thresholds"]:
                flag = "" if t["enough"] else "  (too few)"
                print(
                    f"  {t['min_probability']:>16.0%}  {t['bets']:>5}  "
                    f"{t['wins']}-{t['losses']}-{t['pushes']:<5}  "
                    f"{num(t['roi'], '+.1%'):>7} (+/- {num(t['roi_se'], '.1%')})  "
                    f"{num(t['hit_rate'], '.1%')} / {num(t['break_even'], '.1%')}  "
                    f"{num(t['avg_clv'], '+.2%')}{flag}"
                )
            print("  calibration (home cover, pushes out): bin  n  model predicted -> observed")
            for b in lab["calibration"]["model"]:
                if b["n"]:
                    print(
                        f"    {b['low']:.2f}-{b['high']:.2f}  {b['n']:>4}  "
                        f"{b['mean_predicted']:.3f} -> {b['observed']:.3f}"
                    )
            print("  week of     games  log loss diff (running)   CLV (running)")
            for w in lab["weeks"]:
                print(
                    f"    {w['week']}  {w['games']:>5}  {num(w['diff'], '+.4f')} "
                    f"({num(w['cumulative_diff'], '+.4f')})   {num(w['avg_clv'], '+.2%')} "
                    f"({num(w['cumulative_clv'], '+.2%')})"
                )
        print(f"\n{lab['note']}")
        return 0


def register(sub: Any) -> None:
    fwd_totals = sub.add_parser(
        "forward-freeze-totals", help="Backtest, freeze and register a basketball totals model"
    )
    fwd_totals.add_argument(
        "--sport", type=Sport, choices=[Sport.NCAAB, Sport.NBA], default=Sport.NCAAB
    )
    fwd_ml = sub.add_parser(
        "forward-freeze-ml",
        help="Register moneyline models built from a sport's frozen card spread model",
    )
    fwd_ml.add_argument(
        "--sport", type=Sport, choices=[Sport.CFB, Sport.NBA, Sport.NCAAB], required=True
    )
    fwd_ml.add_argument("--label", required=True, help="Registry label, e.g. eff (NCAAB)")
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
    lab = sub.add_parser(
        "lab", help="Performance Lab: thresholds, calibration and weekly drift (forward tests)"
    )
    lab.add_argument("--model", help='"name version", as `ttk forward-report` prints it')
    lab.add_argument("--horizon", type=int, choices=[24, 1])


HANDLERS: dict[str, Handler] = {
    "forward-freeze": _forward,
    "forward-freeze-ml": _forward,
    "forward-freeze-totals": _forward,
    "forward-snapshot": _forward,
    "forward-run": _forward,
    "forward-report": _forward,
    "lab": _lab,
}
