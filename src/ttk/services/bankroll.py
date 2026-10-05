"""Bankroll: balance, staking limits and Kelly stake guidance.

The balance is rebuilt from history, never stored: net deposits and withdrawals
(append-only ``bankroll_entries``) plus the realized profit of every settled
single bet and parlay. Limits come from the newest ``bankroll_policies`` row
(defaults until one is saved) and are fractions of the balance:

- max stake per wager;
- max staked per Eastern calendar day;
- max open exposure (pending stakes);
- a stop when the balance has fallen too far from its peak (deposits and
  withdrawals move the peak with the balance, so only betting results count).

A wager over a limit is refused unless the user gives a reason, which is kept
on the wager with the limits it broke. With no bankroll entries at all, nothing
is checked: the tracker works as before.

Kelly guidance is only a recommendation for a QUALIFIED opportunity
(docs/MODEL-GOVERNANCE.md); for anything else the recommended stake is 0 and
the reason says why. It is never above the tightest limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ttk import betting_math as bm
from ttk.db.models import BankrollEntry, BankrollPolicy, Bet, Parlay, utcnow
from ttk.domain import BetResult

EASTERN = ZoneInfo("America/New_York")


class EntryKind(StrEnum):
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    ADJUSTMENT = "ADJUSTMENT"


class LimitError(ValueError):
    """A wager over a bankroll limit without an override reason. The message is
    safe to show the user."""


@dataclass(frozen=True)
class Policy:
    kelly_multiplier: float = 0.25
    max_stake_fraction: float = 0.03
    max_daily_fraction: float = 0.10
    max_open_fraction: float = 0.20
    stop_drawdown_fraction: float = 0.25
    saved_at: datetime | None = None
    """None: the defaults, no policy saved yet."""

    def validate(self) -> None:
        if not 0 < self.kelly_multiplier <= 1:
            raise ValueError("Kelly multiplier must be in (0, 1]")
        for name in ("max_stake_fraction", "max_daily_fraction", "max_open_fraction"):
            if not 0 < getattr(self, name) <= 1:
                raise ValueError(f"{name} must be in (0, 1]")
        if not 0 < self.stop_drawdown_fraction < 1:
            raise ValueError("stop_drawdown_fraction must be in (0, 1)")


@dataclass(frozen=True)
class BankrollState:
    at: datetime
    configured: bool
    """False when no deposit was ever recorded: no limits apply."""
    balance: float
    net_deposits: float
    realized_profit: float
    peak: float
    drawdown: float
    """Fraction below the peak balance (<= 0)."""
    open_exposure: float
    open_wagers: int
    staked_today: float
    available: float
    """Balance minus open exposure."""
    policy: Policy


@dataclass(frozen=True)
class Breach:
    limit: str
    message: str


@dataclass(frozen=True)
class StakeGuidance:
    full_kelly_fraction: float
    kelly_stake: float
    """Fractional Kelly on the balance, before limits."""
    max_allowed: float | None
    """The most any wager may stake now under every limit (None: no bankroll)."""
    recommended: float
    reason: str


def current_policy(session: Session) -> Policy:
    row = session.scalar(
        select(BankrollPolicy).order_by(BankrollPolicy.created_at.desc(), BankrollPolicy.id.desc())
    )
    if row is None:
        return Policy()
    return Policy(
        row.kelly_multiplier,
        row.max_stake_fraction,
        row.max_daily_fraction,
        row.max_open_fraction,
        row.stop_drawdown_fraction,
        row.created_at,
    )


def save_policy(session: Session, policy: Policy, *, note: str | None = None) -> BankrollPolicy:
    policy.validate()
    row = BankrollPolicy(
        kelly_multiplier=policy.kelly_multiplier,
        max_stake_fraction=policy.max_stake_fraction,
        max_daily_fraction=policy.max_daily_fraction,
        max_open_fraction=policy.max_open_fraction,
        stop_drawdown_fraction=policy.stop_drawdown_fraction,
        note=note,
    )
    session.add(row)
    session.flush()
    return row


def add_entry(
    session: Session,
    kind: EntryKind,
    amount: float,
    *,
    at: datetime | None = None,
    note: str | None = None,
) -> BankrollEntry:
    """``amount`` is positive for deposits and withdrawals; an adjustment is signed."""
    if kind is EntryKind.ADJUSTMENT:
        if amount == 0:
            raise ValueError("An adjustment must be non-zero")
        signed = amount
    else:
        if amount <= 0:
            raise ValueError("Amount must be positive")
        signed = amount if kind is EntryKind.DEPOSIT else -amount
    entry = BankrollEntry(at=at or utcnow(), kind=kind, amount=signed, note=note)
    session.add(entry)
    session.flush()
    return entry


def _day_bounds(at: datetime) -> tuple[datetime, datetime]:
    day = at.astimezone(EASTERN).date()
    start = datetime.combine(day, time(), EASTERN)
    return start, start + timedelta(days=1)


def bankroll_state(session: Session, *, at: datetime | None = None) -> BankrollState:
    """The bankroll as of ``at`` (default now): only entries made and wagers
    settled by then count, and a wager is open if placed by then and not yet
    settled at that moment."""
    at = at or utcnow()
    policy = current_policy(session)
    entries = session.scalars(select(BankrollEntry).where(BankrollEntry.at <= at)).all()
    singles = session.scalars(select(Bet).where(Bet.parlay_id.is_(None))).all()
    parlays = session.scalars(select(Parlay)).all()
    wagers: list[tuple[datetime | None, datetime | None, float, float | None, str]] = [
        (b.placed_at, b.settled_at, b.stake or 0.0, b.profit_loss, b.result) for b in singles
    ] + [(p.placed_at, p.settled_at, p.stake or 0.0, p.profit_loss, p.result) for p in parlays]

    events: list[tuple[datetime, float, bool]] = [(e.at, e.amount, True) for e in entries]
    open_exposure, open_wagers, staked_today = 0.0, 0, 0.0
    day_start, day_end = _day_bounds(at)
    for placed, settled, stake, profit, result in wagers:
        if settled is not None and settled <= at:
            if profit is not None:
                events.append((settled, profit, False))
        elif placed is not None and placed <= at and result != BetResult.VOID:
            open_exposure += stake
            open_wagers += 1
        if (
            placed is not None
            and day_start <= placed < day_end
            and placed <= at
            and result != BetResult.VOID
        ):
            staked_today += stake

    balance = peak = net = profit_total = 0.0
    for _, amount, is_flow in sorted(events, key=lambda e: e[0]):
        balance += amount
        if is_flow:
            net += amount
            peak += amount
        else:
            profit_total += amount
        peak = max(peak, balance)
    return BankrollState(
        at=at,
        configured=bool(entries),
        balance=balance,
        net_deposits=net,
        realized_profit=profit_total,
        peak=peak,
        drawdown=balance / peak - 1 if peak > 0 else 0.0,
        open_exposure=open_exposure,
        open_wagers=open_wagers,
        staked_today=staked_today,
        available=balance - open_exposure,
        policy=policy,
    )


def max_allowed(state: BankrollState) -> float | None:
    """The largest stake no limit would refuse right now (None: no limits apply)."""
    if not state.configured:
        return None
    p, b = state.policy, state.balance
    if b <= 0 or state.drawdown <= -p.stop_drawdown_fraction:
        return 0.0
    return max(
        0.0,
        min(
            p.max_stake_fraction * b,
            p.max_daily_fraction * b - state.staked_today,
            p.max_open_fraction * b - state.open_exposure,
            state.available,
        ),
    )


def check_limits(state: BankrollState, stake: float) -> list[Breach]:
    if not state.configured:
        return []
    p, b = state.policy, state.balance
    if b <= 0:
        return [Breach("balance", f"the bankroll is {b:.2f}")]
    breaches = []
    if state.drawdown <= -p.stop_drawdown_fraction:
        breaches.append(
            Breach(
                "stop",
                f"the bankroll is {-state.drawdown:.1%} below its peak "
                f"(stop at {p.stop_drawdown_fraction:.0%})",
            )
        )
    if stake > p.max_stake_fraction * b + 1e-9:
        breaches.append(
            Breach(
                "max_stake",
                f"stake {stake:.2f} is over {p.max_stake_fraction:.1%} of the bankroll "
                f"({p.max_stake_fraction * b:.2f})",
            )
        )
    if state.staked_today + stake > p.max_daily_fraction * b + 1e-9:
        breaches.append(
            Breach(
                "daily",
                f"today's stakes would be {state.staked_today + stake:.2f}, over "
                f"{p.max_daily_fraction:.0%} of the bankroll ({p.max_daily_fraction * b:.2f})",
            )
        )
    if state.open_exposure + stake > p.max_open_fraction * b + 1e-9:
        breaches.append(
            Breach(
                "open",
                f"open stakes would be {state.open_exposure + stake:.2f}, over "
                f"{p.max_open_fraction:.0%} of the bankroll ({p.max_open_fraction * b:.2f})",
            )
        )
    if stake > state.available + 1e-9:
        breaches.append(
            Breach(
                "available",
                f"stake {stake:.2f} is more than the uncommitted bankroll ({state.available:.2f})",
            )
        )
    return breaches


def enforce_limits(
    session: Session, stake: float, at: datetime, override: str | None
) -> tuple[float | None, str | None]:
    """For recording a wager: (bankroll at the bet, override record). Raises
    LimitError if a limit is broken and no reason was given."""
    state = bankroll_state(session, at=at)
    breaches = check_limits(state, stake)
    bankroll_at_bet = state.balance if state.configured else None
    if not breaches:
        return bankroll_at_bet, None
    broken = "; ".join(b.message for b in breaches)
    reason = (override or "").strip()
    if not reason:
        raise LimitError(f"Over bankroll limits: {broken}. Give an override reason to record it")
    return bankroll_at_bet, f"{broken}. Reason: {reason}"


def stake_guidance(
    state: BankrollState,
    win_probability: float,
    decimal_odds: float,
    *,
    push_probability: float = 0.0,
    qualified: bool,
) -> StakeGuidance:
    """``win_probability`` is P(win | no push); the push share is applied here,
    as in qualification."""
    p_win = win_probability * (1 - push_probability)
    full = bm.kelly_fraction(p_win, decimal_odds, push_probability=push_probability)
    kelly = state.balance * full * state.policy.kelly_multiplier if state.balance > 0 else 0.0
    cap = max_allowed(state)
    if cap is None:
        reason, recommended = "No bankroll set up: record a deposit first", 0.0
    elif not qualified:
        reason, recommended = "Not QUALIFIED: no stake is recommended", 0.0
    elif full <= 0:
        reason, recommended = "No positive edge at this price", 0.0
    elif cap <= 0:
        reason, recommended = "A bankroll limit leaves no room for a stake", 0.0
    else:
        recommended = min(kelly, cap)
        reason = (
            f"{state.policy.kelly_multiplier:g} Kelly"
            if recommended == kelly
            else f"{state.policy.kelly_multiplier:g} Kelly, capped by a limit"
        )
    return StakeGuidance(full, kelly, cap, recommended, reason)
