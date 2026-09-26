import pytest

from ttk import betting_math as bm
from ttk.consensus import TwoSidedQuote, ev_by_book, market_consensus

d = bm.american_to_decimal


def test_consensus_spec_line_shopping_example() -> None:
    # Baltimore -2.5 at -105 / -110 / -115, each with the opposite side at the same vig shape.
    quotes = [
        TwoSidedQuote("book_a", d(-105), d(-115)),
        TwoSidedQuote("book_b", d(-110), d(-110)),
        TwoSidedQuote("book_c", d(-115), d(-105)),
    ]
    c = market_consensus(quotes)
    assert c.books_reporting == 3
    assert c.best.sportsbook == "book_a"
    assert c.consensus_no_vig_probability == pytest.approx(0.5)
    low, high = c.no_vig_range
    assert low < 0.5 < high

    evs = ev_by_book(0.587, c)
    assert [e.sportsbook for e in evs] == ["book_a", "book_b", "book_c"]
    assert evs[0].ev_per_unit == pytest.approx(bm.expected_value(0.587, d(-105)).per_unit)


def test_median_resists_one_stale_book() -> None:
    quotes = [
        TwoSidedQuote("a", d(-110), d(-110)),
        TwoSidedQuote("b", d(-112), d(-108)),
        TwoSidedQuote("stale", d(+150), d(-180)),
    ]
    c = market_consensus(quotes)
    assert 0.49 < c.consensus_no_vig_probability < 0.51


def test_empty_rejected() -> None:
    with pytest.raises(ValueError):
        market_consensus([])
