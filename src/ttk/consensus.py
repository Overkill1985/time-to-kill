"""Market consensus across sportsbooks for one two-sided market at one line.

The model is compared against a representative no-vig market estimate, not a
single book. Each book is de-vigged on its own (both sides at the same line),
then the per-book no-vig probabilities are combined by median, which is robust
to one stale or off-market book.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from ttk import betting_math as bm


@dataclass(frozen=True)
class TwoSidedQuote:
    """One book's prices for both sides of a market at the same line."""

    sportsbook: str
    side_decimal: float
    """Price of the side being evaluated."""
    other_side_decimal: float


@dataclass(frozen=True)
class BookPrice:
    sportsbook: str
    decimal_odds: float
    no_vig_probability: float


@dataclass(frozen=True)
class MarketConsensus:
    books_reporting: int
    consensus_no_vig_probability: float
    """Median of each book's own no-vig probability for the evaluated side."""
    consensus_decimal_odds: float
    """Median price offered on the evaluated side (includes vig)."""
    best: BookPrice
    """Highest price (best payout) on the evaluated side."""
    no_vig_range: tuple[float, float]
    """(min, max) of per-book no-vig probabilities: how much books disagree."""
    books: tuple[BookPrice, ...]


def market_consensus(quotes: Sequence[TwoSidedQuote]) -> MarketConsensus:
    if not quotes:
        raise ValueError("No quotes to build a consensus from")
    books = tuple(
        BookPrice(
            sportsbook=q.sportsbook,
            decimal_odds=q.side_decimal,
            no_vig_probability=bm.no_vig_probabilities(
                [q.side_decimal, q.other_side_decimal]
            ).probabilities[0],
        )
        for q in quotes
    )
    no_vig = [b.no_vig_probability for b in books]
    return MarketConsensus(
        books_reporting=len(books),
        consensus_no_vig_probability=statistics.median(no_vig),
        consensus_decimal_odds=statistics.median(b.decimal_odds for b in books),
        best=max(books, key=lambda b: b.decimal_odds),
        no_vig_range=(min(no_vig), max(no_vig)),
        books=books,
    )


@dataclass(frozen=True)
class PriceEv:
    sportsbook: str
    decimal_odds: float
    ev_per_unit: float


def ev_by_book(
    model_win_probability: float,
    consensus: MarketConsensus,
    *,
    push_probability: float = 0.0,
) -> list[PriceEv]:
    """Line shopping: EV at every available price, best first."""
    rows = [
        PriceEv(
            b.sportsbook,
            b.decimal_odds,
            bm.expected_value(
                model_win_probability, b.decimal_odds, push_probability=push_probability
            ).per_unit,
        )
        for b in consensus.books
    ]
    return sorted(rows, key=lambda r: r.ev_per_unit, reverse=True)
