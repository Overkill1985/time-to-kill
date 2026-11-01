from datetime import UTC, datetime, timedelta

from ttk.domain import Market, Selection
from ttk.services.forward_test import ScoredRow
from ttk.services.judging_rules import RULES, Rule, evaluate

T0 = datetime(2026, 11, 1, tzinfo=UTC)


def row(i: int, model_p: float, home_covers: bool, *, h: int = 1, odds: float = -110) -> ScoredRow:
    return ScoredRow(
        model="m 1",
        horizon_hours=h,
        game_id=i,
        sport="NFL",
        market=Market.TOTAL,
        commence_time=T0 + timedelta(days=i),
        home_line=0.0,
        model_home_cover=model_p,
        market_home_cover=0.5,
        cover_margin=1.0 if home_covers else -1.0,
        side=Selection.HOME if model_p >= 0.5 else Selection.AWAY,
        side_odds=odds,
        price_clv=None,
        points_vs_close=None,
    )


LL_RULE = Rule("m 1", "2026-11-01", 1, "log_loss_z", min_decided=40, threshold=-2.0)


def test_waits_until_enough_records_then_judges_once() -> None:
    rows = [row(i, 0.6, i % 4 != 0) for i in range(30)]  # model right 3 in 4
    status = evaluate(LL_RULE, rows)
    assert status.state == "waiting" and status.decided == 30 and status.needed == 40
    status = evaluate(LL_RULE, [row(i, 0.6, i % 4 != 0) for i in range(40)])
    assert status.state == "passed" and status.value is not None and status.value <= -2


def test_judged_on_the_first_records_only_so_later_results_cannot_rescue_it() -> None:
    first = [row(i, 0.6, i % 2 == 0) for i in range(40)]  # a coin flip: no edge
    later = [row(100 + i, 0.6, True) for i in range(200)]  # a hot streak afterwards
    status = evaluate(LL_RULE, first + later)
    assert status.state == "failed" and status.decided == 240


def test_other_horizons_and_models_are_ignored() -> None:
    rows = [row(i, 0.6, True, h=24) for i in range(50)]
    assert evaluate(LL_RULE, rows).decided == 0


def test_roi_rule_counts_only_bets_at_the_edge() -> None:
    rule = Rule("m 1", "2026-11-01", 1, "roi_z", min_decided=50, threshold=2.0, min_edge=0.04)
    small = [row(i, 0.52, True) for i in range(100)]  # edge 2%: not a bet
    big = [row(200 + i, 0.6, i % 10 != 0, odds=-110) for i in range(50)]  # 90% at -110
    status = evaluate(rule, small + big)
    assert status.decided == 50 and status.state == "passed"


def test_registered_rules_name_their_models() -> None:
    assert {r.model for r in RULES} == {"nfl-total-wind 1", "ncaab-spread-anchored-eff 1"}
