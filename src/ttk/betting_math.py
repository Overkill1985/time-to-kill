"""The single home for betting mathematics.

Every odds conversion, probability, EV, Kelly and parlay formula in the app lives
here. UI code, services and models import from this module; none of them
re-derive these formulas.

Conventions:
- Probabilities are floats in [0, 1].
- Decimal odds include the stake (2.0 = even money).
- American odds are +100 or greater, or -100 or less. -100 and +100 are both even money.
- Nothing here rounds; formatting for display is the caller's job (see
  ``format_american``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

# --------------------------------------------------------------------------- conversions


def _check_american(american: float) -> None:
    if not math.isfinite(american) or -100 < american < 100:
        raise ValueError(f"American odds must be <= -100 or >= +100, got {american}")


def _check_decimal(decimal_odds: float) -> None:
    if not math.isfinite(decimal_odds) or decimal_odds <= 1.0:
        raise ValueError(f"Decimal odds must be > 1.0, got {decimal_odds}")


def _check_probability(p: float, *, allow_zero: bool = False, allow_one: bool = False) -> None:
    low_ok = p >= 0.0 if allow_zero else p > 0.0
    high_ok = p <= 1.0 if allow_one else p < 1.0
    if not (math.isfinite(p) and low_ok and high_ok):
        raise ValueError(f"Probability out of range: {p}")


def american_to_decimal(american: float) -> float:
    _check_american(american)
    if american > 0:
        return 1.0 + american / 100.0
    return 1.0 + 100.0 / -american


def decimal_to_american(decimal_odds: float) -> float:
    """Unrounded American odds. Even money (2.0) is returned as +100."""
    _check_decimal(decimal_odds)
    if decimal_odds >= 2.0:
        return (decimal_odds - 1.0) * 100.0
    return -100.0 / (decimal_odds - 1.0)


def decimal_implied_probability(decimal_odds: float) -> float:
    """Probability implied by a single price. Includes the book's vig."""
    _check_decimal(decimal_odds)
    return 1.0 / decimal_odds


def american_implied_probability(american: float) -> float:
    """Probability implied by a single American price. Includes the book's vig."""
    return decimal_implied_probability(american_to_decimal(american))


def break_even_probability(decimal_odds: float) -> float:
    """Win probability at which a bet at this price has zero EV (no pushes)."""
    return decimal_implied_probability(decimal_odds)


def fair_decimal_odds(probability: float) -> float:
    """Zero-margin decimal odds for a probability: 1 / p."""
    _check_probability(probability)
    return 1.0 / probability


def fair_american_odds(probability: float) -> float:
    return decimal_to_american(fair_decimal_odds(probability))


def format_american(american: float) -> str:
    """Display form: '+150', '-110'. Rounds to the nearest whole number."""
    rounded = round(american)
    return f"+{rounded}" if rounded > 0 else str(rounded)


# --------------------------------------------------------------------------- no-vig


@dataclass(frozen=True)
class NoVigResult:
    probabilities: tuple[float, ...]
    overround: float
    """Sum of implied probabilities minus 1 (the book's margin), e.g. 0.0476 at -110/-110."""


def no_vig_probabilities(decimal_prices: Sequence[float]) -> NoVigResult:
    """Remove the margin from a complete market (every outcome priced) by
    proportional normalization: each implied probability divided by their sum.

    The prices must cover every mutually exclusive outcome of one market at one
    book (both sides of a spread at the same line, all moneyline outcomes).
    """
    if len(decimal_prices) < 2:
        raise ValueError("A no-vig calculation needs every side of the market (>= 2 prices)")
    implied = [decimal_implied_probability(d) for d in decimal_prices]
    total = sum(implied)
    return NoVigResult(
        probabilities=tuple(p / total for p in implied),
        overround=total - 1.0,
    )


# --------------------------------------------------------------------------- edge and EV


def edge(model_probability: float, market_probability: float) -> float:
    """Model probability minus the no-vig market probability, in probability units
    (0.055 = +5.5 percentage points)."""
    return model_probability - market_probability


@dataclass(frozen=True)
class ExpectedValue:
    per_unit: float
    """Expected profit per 1 unit staked. Equal to EV% / 100."""
    win_probability: float
    push_probability: float
    loss_probability: float

    @property
    def percent(self) -> float:
        return self.per_unit * 100.0


def expected_value(
    win_probability: float, decimal_odds: float, *, push_probability: float = 0.0
) -> ExpectedValue:
    """EV = P(win) x net_profit - P(loss) x stake, per 1 unit staked.

    A push returns the stake, so it contributes zero and is excluded from P(loss).
    """
    _check_decimal(decimal_odds)
    _check_probability(win_probability, allow_zero=True, allow_one=True)
    _check_probability(push_probability, allow_zero=True, allow_one=True)
    loss = 1.0 - win_probability - push_probability
    if loss < -1e-12:
        raise ValueError("win_probability + push_probability exceeds 1")
    loss = max(loss, 0.0)
    per_unit = win_probability * (decimal_odds - 1.0) - loss
    return ExpectedValue(per_unit, win_probability, push_probability, loss)


# --------------------------------------------------------------------------- Kelly


def kelly_fraction(
    win_probability: float, decimal_odds: float, *, push_probability: float = 0.0
) -> float:
    """Full-Kelly fraction of bankroll. 0 when the bet has no positive edge.

    With pushes, maximizing p*ln(1+bf) + q*ln(1-f) gives f = (bp - q) / (b(p + q)),
    which reduces to the textbook (bp - q) / b when there are no pushes.
    """
    ev = expected_value(win_probability, decimal_odds, push_probability=push_probability)
    b = decimal_odds - 1.0
    p, q = ev.win_probability, ev.loss_probability
    if p + q == 0.0:
        return 0.0
    f = (b * p - q) / (b * (p + q))
    return max(f, 0.0)


def kelly_stake(
    bankroll: float,
    win_probability: float,
    decimal_odds: float,
    *,
    kelly_multiplier: float = 0.25,
    max_stake: float | None = None,
    push_probability: float = 0.0,
) -> float:
    """Stake in currency for fractional Kelly (0.25 = quarter Kelly), capped at max_stake."""
    if bankroll < 0 or not 0 < kelly_multiplier <= 1:
        raise ValueError("bankroll must be >= 0 and kelly_multiplier in (0, 1]")
    f = kelly_fraction(win_probability, decimal_odds, push_probability=push_probability)
    stake = bankroll * f * kelly_multiplier
    return min(stake, max_stake) if max_stake is not None else stake


# --------------------------------------------------------------------------- parlays


def parlay_decimal_odds(leg_decimal_odds: Sequence[float]) -> float:
    """Sportsbook parlay price: the product of each leg's decimal odds."""
    if not leg_decimal_odds:
        raise ValueError("A parlay needs at least one leg")
    result = 1.0
    for d in leg_decimal_odds:
        _check_decimal(d)
        result *= d
    return result


def independent_parlay_probability(leg_probabilities: Sequence[float]) -> float:
    """P(all legs win) = P1 x P2 x ... x Pn. Valid ONLY for independent legs;
    same-game legs are generally correlated and need a joint estimate."""
    if not leg_probabilities:
        raise ValueError("A parlay needs at least one leg")
    result = 1.0
    for p in leg_probabilities:
        _check_probability(p, allow_zero=True, allow_one=True)
        result *= p
    return result


def settle_payout(stake: float, decimal_odds: float, result: str) -> float:
    """Profit/loss in currency for a settled straight bet.
    result: WIN, LOSS, PUSH or VOID."""
    _check_decimal(decimal_odds)
    match result:
        case "WIN":
            return stake * (decimal_odds - 1.0)
        case "LOSS":
            return -stake
        case "PUSH" | "VOID":
            return 0.0
    raise ValueError(f"Unknown result {result!r}")


def parlay_settled_decimal_odds(legs: Sequence[tuple[float, str]]) -> float | None:
    """Effective decimal odds of a settled parlay given (decimal_odds, result) per leg.

    Common sportsbook rule: a pushed or voided leg drops out and the parlay is
    repriced on the remaining legs. Returns None if any leg lost (the parlay lost),
    1.0 if every leg pushed/voided (stake returned).
    """
    result = 1.0
    for decimal_odds, leg_result in legs:
        if leg_result == "LOSS":
            return None
        if leg_result == "WIN":
            _check_decimal(decimal_odds)
            result *= decimal_odds
        elif leg_result not in ("PUSH", "VOID"):
            raise ValueError(f"Unknown leg result {leg_result!r}")
    return result


# --------------------------------------------------------------------------- CLV


def closing_line_value(bet_decimal_odds: float, closing_no_vig_probability: float) -> float:
    """CLV as expected return of the bet price against the closing no-vig probability:
    bet_decimal x p_close - 1. Positive means the bet beat the closing market."""
    _check_decimal(bet_decimal_odds)
    _check_probability(closing_no_vig_probability)
    return bet_decimal_odds * closing_no_vig_probability - 1.0
