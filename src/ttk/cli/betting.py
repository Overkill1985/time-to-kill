"""The daily card, bets, bankroll and the simulator."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from typing import Any

from ttk import betting_math as bm
from ttk.cli._common import (
    Handler,
    _fmt_pct,
)
from ttk.config import Settings
from ttk.domain import BetClassification, Market, Selection, Sport


def _card(args: argparse.Namespace, settings: Settings) -> int:
    from zoneinfo import ZoneInfo

    from ttk.db.session import make_engine, make_session_factory
    from ttk.services.daily_card import CardFilter, build_card, filter_entries
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
            # Other sports' frozen models (slow): only the sports asked for.
            models=build_card_models(session, set(args.sport) if args.sport else None),
        )
    card_filter = CardFilter(
        sports=frozenset(args.sport) if args.sport else None,
        classifications=frozenset(args.only) if args.only else None,
        min_edge=args.min_edge / 100 if args.min_edge is not None else None,
        books=frozenset(b.lower() for b in args.book) if args.book else None,
    )
    entries = filter_entries(card.entries, card_filter)

    print(f"TIME-TO-KILL  {day:%A %B %d, %Y}".upper())
    print()
    summaries = [
        s
        for sport, s in card.by_sport.items()
        if card_filter.sports is None or sport in card_filter.sports
    ]
    modeled = sum(s.modeled for s in summaries)
    with_market = sum(s.with_market for s in summaries)
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
        if card_filter.sports is not None and sport not in card_filter.sports:
            continue
        print(
            f"  {sport:<6} games {s.games:>3}  with market {s.with_market:>3}  "
            f"modeled {s.modeled:>3}  qualified {s.qualified:>2}  lean {s.lean:>2}"
        )
    if predictor is None:
        print("\nNo NFL model inputs: run import-nfl-history and import-nfl-pbp.")
    if len(entries) != len(card.entries):
        print(f"\nShowing {len(entries)} of {len(card.entries)} entries (filtered).")
    for e in entries:
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
        if card_filter.sports is None or u.sport in card_filter.sports:
            print(f"\n[UNMODELED] {u.sport} {u.matchup}: {u.reason}")
    return 0


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
                        args.override,
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
            if bet.limit_override:
                print(f"  over a bankroll limit: {bet.limit_override}")
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


def _bankroll(args: argparse.Namespace, settings: Settings) -> int:
    from dataclasses import replace

    from ttk.db.session import make_engine, make_session_factory
    from ttk.services.bankroll import (
        EntryKind,
        add_entry,
        bankroll_state,
        current_policy,
        max_allowed,
        save_policy,
    )

    factory = make_session_factory(make_engine(settings.database_url))
    with factory() as session:
        try:
            if args.bankroll_command in ("deposit", "withdraw", "adjust"):
                kind = {
                    "deposit": EntryKind.DEPOSIT,
                    "withdraw": EntryKind.WITHDRAWAL,
                    "adjust": EntryKind.ADJUSTMENT,
                }[args.bankroll_command]
                add_entry(session, kind, args.amount, note=args.note)
            elif args.bankroll_command == "policy":
                changes = {
                    name: value
                    for name, value in (
                        ("kelly_multiplier", args.kelly),
                        ("max_stake_fraction", args.max_stake),
                        ("max_daily_fraction", args.max_daily),
                        ("max_open_fraction", args.max_open),
                        ("stop_drawdown_fraction", args.stop),
                    )
                    if value is not None
                }
                if not changes:
                    print("Nothing to change; see `ttk bankroll policy --help`", file=sys.stderr)
                    return 2
                save_policy(session, replace(current_policy(session), **changes), note=args.note)
        except ValueError as exc:
            print(f"Not saved: {exc}", file=sys.stderr)
            return 2
        session.commit()
        state = bankroll_state(session)
        p = state.policy
        if not state.configured:
            print("No bankroll yet: `ttk bankroll deposit AMOUNT` turns on the limits.")
        else:
            cap = max_allowed(state)
            print(
                f"Bankroll {state.balance:.2f} (net deposits {state.net_deposits:.2f}, "
                f"betting {state.realized_profit:+.2f}); peak {state.peak:.2f}, "
                f"drawdown {state.drawdown:.1%}"
            )
            print(
                f"Open {state.open_exposure:.2f} on {state.open_wagers} wager(s); "
                f"staked today {state.staked_today:.2f}; largest stake allowed now "
                f"{cap or 0:.2f}"
            )
        print(
            f"Policy{' (defaults)' if p.saved_at is None else ''}: {p.kelly_multiplier:g} Kelly; "
            f"max stake {p.max_stake_fraction:.1%}, per day {p.max_daily_fraction:.0%}, "
            f"open {p.max_open_fraction:.0%}; stop at {p.stop_drawdown_fraction:.0%} "
            "below peak"
        )
        return 0


def _simulate(args: argparse.Namespace, settings: Settings) -> int:
    from ttk.db.models import Game
    from ttk.db.session import make_engine, make_session_factory
    from ttk.domain import Sport
    from ttk.models.simulation import PRESETS
    from ttk.services.forward_models import build_card_models
    from ttk.services.nfl_spread_predictor import NflSpreadPredictor
    from ttk.services.simulation_service import SimulationError, run_simulation

    factory = make_session_factory(make_engine(settings.database_url))
    with factory() as session:
        game = session.get(Game, args.game_id)
        if game is None:
            print(f"Game {args.game_id} not found", file=sys.stderr)
            return 2
        sport = Sport(game.sport)
        if sport is Sport.NFL:
            simulator: Any = NflSpreadPredictor.build(session)
        else:
            card = build_card_models(session, {sport}).get(sport)
            simulator = card.simulator if card else None
        try:
            s = run_simulation(
                session,
                args.game_id,
                simulator,
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


def register(sub: Any) -> None:
    card = sub.add_parser("card", help="Show the daily card for a date (US Eastern)")
    card.add_argument("--date", type=date.fromisoformat, help="YYYY-MM-DD (default: today)")
    card.add_argument(
        "--why", action="store_true", help="Show every qualification check for each bet"
    )
    card.add_argument("--no-persist", action="store_true", help="Do not write prediction snapshots")
    card.add_argument(
        "--sport", type=Sport, choices=list(Sport), action="append", help="Repeatable"
    )
    card.add_argument(
        "--only",
        type=BetClassification,
        choices=list(BetClassification),
        action="append",
        help="Classification to show (repeatable), e.g. LEAN",
    )
    card.add_argument("--min-edge", type=float, help="Minimum edge in points, e.g. 2 = 2.0 points")
    card.add_argument("--book", action="append", help="Best-price book key (repeatable)")
    bankroll = sub.add_parser("bankroll", help="Bankroll balance and staking limits")
    bankroll_sub = bankroll.add_subparsers(dest="bankroll_command")
    for name, help_text in (
        ("deposit", "Add money to the bankroll"),
        ("withdraw", "Take money out of the bankroll"),
        ("adjust", "Correct the balance (signed amount)"),
    ):
        entry = bankroll_sub.add_parser(name, help=help_text)
        entry.add_argument("amount", type=float)
        entry.add_argument("--note")
    policy = bankroll_sub.add_parser("policy", help="Change staking limits (fractions)")
    policy.add_argument("--kelly", type=float, help="Kelly multiplier, e.g. 0.25")
    policy.add_argument("--max-stake", type=float, help="Per wager, e.g. 0.03 = 3%%")
    policy.add_argument("--max-daily", type=float, help="Staked per Eastern day, e.g. 0.10")
    policy.add_argument("--max-open", type=float, help="Pending stakes, e.g. 0.20")
    policy.add_argument("--stop", type=float, help="Stop below peak, e.g. 0.25")
    policy.add_argument("--note")
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
    add.add_argument(
        "--override", help="Reason to record the bet although it breaks a bankroll limit"
    )
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


HANDLERS: dict[str, Handler] = {
    "card": _card,
    "bets": _bets,
    "bankroll": _bankroll,
    "simulate": _simulate,
}
