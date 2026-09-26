"""Qualification engine and Why-Not explanations.

Classification rules (docs/MODEL-GOVERNANCE.md):

- NO_BET: the prediction is unusable - stale odds, UNUSABLE data, insufficient
  data for an uncertainty estimate, a retired model, or a model whose drift
  health is DEGRADED / RETRAIN_REQUIRED.
- PASS: the model does not favor the wager at this price (edge <= 0 or EV <= 0).
- LEAN: the model favors the wager (positive edge and EV) but at least one
  qualification criterion fails.
- QUALIFIED: every criterion passes.

A high model probability alone never qualifies a bet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ttk import betting_math as bm
from ttk.domain import (
    BetClassification,
    DataQuality,
    ModelHealth,
    ModelStatus,
    Uncertainty,
)


@dataclass(frozen=True)
class QualificationRules:
    min_model_probability: float = 0.56
    min_edge: float = 0.02
    """Probability units: 0.02 = 2.0 percentage points over the no-vig market."""
    min_ev: float = 0.0
    """EV per unit must be strictly greater than this."""
    min_data_quality: DataQuality = DataQuality.ACCEPTABLE
    max_odds_age: timedelta = timedelta(minutes=30)
    qualifying_model_statuses: frozenset[ModelStatus] = frozenset({ModelStatus.ACTIVE})


@dataclass(frozen=True)
class Opportunity:
    model_probability: float
    no_vig_probability: float
    decimal_odds: float
    odds_timestamp: datetime
    data_quality: DataQuality
    uncertainty: Uncertainty
    model_status: ModelStatus
    model_health: ModelHealth = ModelHealth.HEALTHY
    push_probability: float = 0.0


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    actual: str
    required: str


@dataclass(frozen=True)
class QualificationResult:
    classification: BetClassification
    edge: float
    ev_per_unit: float
    checks: tuple[Check, ...]
    reasons: tuple[str, ...] = field(default=())
    """Human-readable reasons for any failed check, in check order."""


def _pct(p: float) -> str:
    return f"{p * 100:.1f}%"


def _pts(p: float) -> str:
    return f"{p * 100:+.1f} pts"


def qualify(opp: Opportunity, rules: QualificationRules, *, now: datetime) -> QualificationResult:
    edge = bm.edge(opp.model_probability, opp.no_vig_probability)
    ev = bm.expected_value(
        opp.model_probability, opp.decimal_odds, push_probability=opp.push_probability
    ).per_unit
    odds_age = now - opp.odds_timestamp

    checks = (
        Check(
            "odds_current",
            timedelta(0) <= odds_age <= rules.max_odds_age,
            f"{odds_age.total_seconds() / 60:.0f} min old",
            f"<= {rules.max_odds_age.total_seconds() / 60:.0f} min",
        ),
        Check(
            "data_usable",
            opp.data_quality is not DataQuality.UNUSABLE,
            opp.data_quality.value,
            "not UNUSABLE",
        ),
        Check(
            "uncertainty_estimable",
            opp.uncertainty is not Uncertainty.INSUFFICIENT_DATA,
            opp.uncertainty.value,
            "not INSUFFICIENT_DATA",
        ),
        Check(
            "model_healthy",
            opp.model_status is not ModelStatus.RETIRED
            and opp.model_health not in (ModelHealth.DEGRADED, ModelHealth.RETRAIN_REQUIRED),
            f"{opp.model_status.value} / {opp.model_health.value}",
            "not RETIRED, DEGRADED or RETRAIN_REQUIRED",
        ),
        Check(
            "probability",
            opp.model_probability >= rules.min_model_probability,
            _pct(opp.model_probability),
            f">= {_pct(rules.min_model_probability)}",
        ),
        Check("edge", edge >= rules.min_edge, _pts(edge), f">= {_pts(rules.min_edge)}"),
        Check("expected_value", ev > rules.min_ev, _pct(ev), f"> {_pct(rules.min_ev)}"),
        Check(
            "data_quality",
            opp.data_quality.meets(rules.min_data_quality),
            opp.data_quality.value,
            f">= {rules.min_data_quality.value}",
        ),
        Check(
            "model_validated",
            opp.model_status in rules.qualifying_model_statuses,
            opp.model_status.value,
            " or ".join(sorted(s.value for s in rules.qualifying_model_statuses)),
        ),
    )
    by_name = {c.name: c for c in checks}
    blocking = ("odds_current", "data_usable", "uncertainty_estimable", "model_healthy")

    if not all(by_name[n].passed for n in blocking):
        classification = BetClassification.NO_BET
    elif edge <= 0 or ev <= 0:
        classification = BetClassification.PASS
    elif all(c.passed for c in checks):
        classification = BetClassification.QUALIFIED
    else:
        classification = BetClassification.LEAN

    reasons = tuple(f"{c.name}: {c.actual} (required {c.required})" for c in checks if not c.passed)
    return QualificationResult(classification, edge, ev, checks, reasons)
