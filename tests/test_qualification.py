from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ttk import betting_math as bm
from ttk.domain import (
    BetClassification,
    DataQuality,
    ModelHealth,
    ModelStatus,
    Uncertainty,
)
from ttk.qualification import Opportunity, QualificationRules, qualify

NOW = datetime(2026, 9, 26, 17, 0, tzinfo=UTC)
RULES = QualificationRules()

BASE = Opportunity(
    model_probability=0.587,
    no_vig_probability=0.532,
    decimal_odds=bm.american_to_decimal(-105),
    odds_timestamp=NOW - timedelta(minutes=5),
    data_quality=DataQuality.GOOD,
    uncertainty=Uncertainty.LOW,
    model_status=ModelStatus.ACTIVE,
)


def classify(**changes: object) -> BetClassification:
    return qualify(replace(BASE, **changes), RULES, now=NOW).classification  # type: ignore[arg-type]


def test_qualified_when_everything_passes() -> None:
    result = qualify(BASE, RULES, now=NOW)
    assert result.classification is BetClassification.QUALIFIED
    assert result.edge == pytest.approx(0.055)
    assert result.reasons == ()


def test_spec_why_not_example_is_pass() -> None:
    # Philadelphia -3: model 57.8%, market 56.5%, edge +1.3, EV -0.7% -> PASS.
    result = qualify(
        replace(BASE, model_probability=0.578, no_vig_probability=0.565, decimal_odds=1.718),
        RULES,
        now=NOW,
    )
    assert result.classification is BetClassification.PASS
    checks = {c.name: c.passed for c in result.checks}
    assert checks["probability"] is True
    assert checks["edge"] is False
    assert checks["expected_value"] is False
    assert result.ev_per_unit == pytest.approx(-0.0069, abs=1e-4)


def test_high_probability_alone_does_not_qualify() -> None:
    # 72% at -400: model agrees with the market, EV negative.
    assert (
        classify(model_probability=0.72, no_vig_probability=0.78, decimal_odds=1.25)
        is BetClassification.PASS
    )


def test_lean_when_below_probability_threshold() -> None:
    # Positive edge and EV, but under 56%.
    assert (
        classify(model_probability=0.545, no_vig_probability=0.50, decimal_odds=2.0)
        is BetClassification.LEAN
    )


def test_lean_when_edge_too_small_but_ev_positive() -> None:
    assert (
        classify(model_probability=0.58, no_vig_probability=0.565, decimal_odds=1.80)
        is BetClassification.LEAN
    )


def test_paper_model_cannot_qualify() -> None:
    assert classify(model_status=ModelStatus.PAPER) is BetClassification.LEAN


def test_poor_data_is_lean_unusable_is_no_bet() -> None:
    assert classify(data_quality=DataQuality.POOR) is BetClassification.LEAN
    assert classify(data_quality=DataQuality.UNUSABLE) is BetClassification.NO_BET


@pytest.mark.parametrize(
    "changes",
    [
        {"odds_timestamp": NOW - timedelta(hours=2)},
        {"odds_timestamp": NOW + timedelta(minutes=1)},  # timestamp from the future
        {"uncertainty": Uncertainty.INSUFFICIENT_DATA},
        {"model_status": ModelStatus.RETIRED},
        {"model_health": ModelHealth.DEGRADED},
        {"model_health": ModelHealth.RETRAIN_REQUIRED},
    ],
)
def test_no_bet_conditions(changes: dict[str, object]) -> None:
    assert classify(**changes) is BetClassification.NO_BET


def test_thresholds_are_configurable() -> None:
    strict = replace(RULES, min_edge=0.06)
    assert qualify(BASE, strict, now=NOW).classification is BetClassification.LEAN


def test_push_probability_scales_ev() -> None:
    # model_probability is P(win | no push); a push chance returns the stake, so EV
    # shrinks by (1 - push) and edge (vs the no-vig market) is unchanged.
    plain = qualify(BASE, RULES, now=NOW)
    with_push = qualify(replace(BASE, push_probability=0.05), RULES, now=NOW)
    assert with_push.ev_per_unit == pytest.approx(plain.ev_per_unit * 0.95)
    assert with_push.edge == plain.edge
