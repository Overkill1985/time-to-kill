import pytest

from ttk import betting_math as bm

approx = pytest.approx


class TestConversions:
    @pytest.mark.parametrize(
        ("american", "decimal"),
        [(-110, 1.9090909), (+150, 2.5), (-200, 1.5), (+100, 2.0), (-100, 2.0), (+1000, 11.0)],
    )
    def test_american_to_decimal(self, american: float, decimal: float) -> None:
        assert bm.american_to_decimal(american) == approx(decimal)

    @pytest.mark.parametrize("american", [-110, +150, -200, +100, -350, +1000, -105])
    def test_round_trip(self, american: float) -> None:
        back = bm.decimal_to_american(bm.american_to_decimal(american))
        # -100 and +100 are the same price; round trip normalizes to +100.
        assert back == approx(100 if american == -100 else american)

    @pytest.mark.parametrize("bad", [0, 50, -99.9, float("nan"), float("inf")])
    def test_rejects_invalid_american(self, bad: float) -> None:
        with pytest.raises(ValueError):
            bm.american_to_decimal(bad)

    @pytest.mark.parametrize("bad", [1.0, 0.5, -2.0, float("nan")])
    def test_rejects_invalid_decimal(self, bad: float) -> None:
        with pytest.raises(ValueError):
            bm.decimal_to_american(bad)

    def test_implied_probability(self) -> None:
        assert bm.american_implied_probability(-110) == approx(110 / 210)
        assert bm.american_implied_probability(+150) == approx(0.4)
        assert bm.decimal_implied_probability(2.5) == approx(0.4)
        assert bm.break_even_probability(1.9090909) == approx(0.5238095)

    def test_fair_odds(self) -> None:
        assert bm.fair_decimal_odds(0.5) == approx(2.0)
        assert bm.fair_american_odds(0.6) == approx(-150)
        assert bm.fair_american_odds(0.4) == approx(+150)
        with pytest.raises(ValueError):
            bm.fair_decimal_odds(0.0)

    def test_format_american(self) -> None:
        assert bm.format_american(149.6) == "+150"
        assert bm.format_american(-110.2) == "-110"


class TestNoVig:
    def test_symmetric_market(self) -> None:
        d = bm.american_to_decimal(-110)
        result = bm.no_vig_probabilities([d, d])
        assert result.probabilities == approx((0.5, 0.5))
        assert result.overround == approx(1 / 21)  # 2 x 110/210 - 1

    def test_asymmetric_market_sums_to_one(self) -> None:
        prices = [bm.american_to_decimal(-150), bm.american_to_decimal(+130)]
        result = bm.no_vig_probabilities(prices)
        assert sum(result.probabilities) == approx(1.0)
        # 0.6 / (0.6 + 0.434783) = 0.579832
        assert result.probabilities[0] == approx(0.579832, abs=1e-6)

    def test_three_way(self) -> None:
        result = bm.no_vig_probabilities([2.5, 3.2, 3.0])
        assert sum(result.probabilities) == approx(1.0)

    def test_needs_both_sides(self) -> None:
        with pytest.raises(ValueError):
            bm.no_vig_probabilities([1.9])


class TestExpectedValue:
    def test_spec_example(self) -> None:
        # Model 58.7% at -105.
        ev = bm.expected_value(0.587, bm.american_to_decimal(-105))
        assert ev.per_unit == approx(0.587 * (100 / 105) - 0.413)
        assert ev.percent == approx(ev.per_unit * 100)

    def test_break_even_is_zero(self) -> None:
        d = bm.american_to_decimal(-110)
        assert bm.expected_value(bm.break_even_probability(d), d).per_unit == approx(0.0, abs=1e-12)

    def test_push_returns_stake(self) -> None:
        # 50% win, 10% push, 40% loss at even money: EV = 0.5 - 0.4 = +0.1
        ev = bm.expected_value(0.5, 2.0, push_probability=0.1)
        assert ev.loss_probability == approx(0.4)
        assert ev.per_unit == approx(0.1)

    def test_rejects_probabilities_over_one(self) -> None:
        with pytest.raises(ValueError):
            bm.expected_value(0.95, 2.0, push_probability=0.1)

    def test_edge(self) -> None:
        assert bm.edge(0.587, 0.532) == approx(0.055)


class TestKelly:
    def test_textbook(self) -> None:
        # p=0.55 at even money: (1*0.55 - 0.45)/1 = 0.10
        assert bm.kelly_fraction(0.55, 2.0) == approx(0.10)

    def test_no_edge_is_zero(self) -> None:
        assert bm.kelly_fraction(0.45, 2.0) == 0.0

    def test_push_formula(self) -> None:
        # p=0.5, push=0.1, q=0.4, b=1: (0.5-0.4)/(1*0.9) = 0.1111
        assert bm.kelly_fraction(0.5, 2.0, push_probability=0.1) == approx(0.1 / 0.9)

    def test_fractional_and_cap(self) -> None:
        assert bm.kelly_stake(1000, 0.55, 2.0, kelly_multiplier=0.25) == approx(25.0)
        assert bm.kelly_stake(1000, 0.55, 2.0, kelly_multiplier=0.5, max_stake=40) == 40

    def test_rejects_bad_multiplier(self) -> None:
        with pytest.raises(ValueError):
            bm.kelly_stake(1000, 0.55, 2.0, kelly_multiplier=0)


class TestParlays:
    def test_parlay_odds(self) -> None:
        d = bm.american_to_decimal(-110)
        assert bm.parlay_decimal_odds([d, d]) == approx(3.6446281)
        # Two -110 legs is the familiar +264.
        assert bm.decimal_to_american(bm.parlay_decimal_odds([d, d])) == approx(264.46281)

    def test_probability_is_product_not_average(self) -> None:
        assert bm.independent_parlay_probability([0.6, 0.5, 0.7]) == approx(0.21)

    def test_empty_parlay_rejected(self) -> None:
        with pytest.raises(ValueError):
            bm.parlay_decimal_odds([])
        with pytest.raises(ValueError):
            bm.independent_parlay_probability([])

    def test_settlement_push_drops_leg(self) -> None:
        legs = [(1.9, "WIN"), (2.0, "PUSH"), (1.5, "WIN")]
        assert bm.parlay_settled_decimal_odds(legs) == approx(1.9 * 1.5)

    def test_settlement_loss(self) -> None:
        assert bm.parlay_settled_decimal_odds([(1.9, "WIN"), (2.0, "LOSS")]) is None

    def test_settlement_all_push_returns_stake(self) -> None:
        assert bm.parlay_settled_decimal_odds([(1.9, "PUSH"), (2.0, "VOID")]) == 1.0


class TestSettlementAndClv:
    @pytest.mark.parametrize(
        ("result", "expected"), [("WIN", 90.9090909), ("LOSS", -100), ("PUSH", 0), ("VOID", 0)]
    )
    def test_settle(self, result: str, expected: float) -> None:
        assert bm.settle_payout(100, bm.american_to_decimal(-110), result) == approx(expected)

    def test_settle_unknown(self) -> None:
        with pytest.raises(ValueError):
            bm.settle_payout(100, 2.0, "PENDING")

    def test_clv(self) -> None:
        # Bet at +110 (2.10); market closed at 52% no-vig -> 2.10 * 0.52 - 1 = +9.2%
        assert bm.closing_line_value(2.10, 0.52) == approx(0.092)
        assert bm.closing_line_value(1.80, 0.52) < 0
