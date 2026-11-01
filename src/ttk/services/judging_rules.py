"""Pre-registered judging rules for forward tests (docs/MODEL-GOVERNANCE.md).

A rule is written down before the games it is judged on, and judged once, on the
first ``min_decided`` decided records in kickoff order, so that neither the stopping
point nor the threshold can be chosen after seeing the results. Until then a rule
only reports progress. Passing a rule makes a model eligible for PAPER; it never
makes a record a recommendation.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from ttk import betting_math as bm
from ttk.services.forward_test import ScoredRow, scored_rows


@dataclass(frozen=True)
class Rule:
    model: str
    """ "name version", as in the forward tests."""
    registered: str
    """The day the rule was written down (Eastern), before the games it judges."""
    horizon_hours: int
    metric: Literal["log_loss_z", "roi_z"]
    min_decided: int
    """Judged once, on the first this many decided records (bets, for roi_z)."""
    threshold: float
    """log_loss_z: passes at a paired z <= threshold. roi_z: passes at an ROI z >=
    threshold."""
    min_edge: float = 0.0
    """roi_z: only records whose model-minus-market edge is at least this count."""
    note: str = ""


RULES: tuple[Rule, ...] = (
    Rule(
        "nfl-total-wind 1",
        "2026-11-01",
        horizon_hours=1,
        metric="log_loss_z",
        min_decided=60,
        threshold=-2.0,
        note="1 h snapshot (forecast wind closest to kickoff); 26 decided when registered",
    ),
    Rule(
        "ncaab-spread-anchored-eff 1",
        "2026-11-01",
        horizon_hours=1,
        metric="roi_z",
        min_decided=300,
        threshold=2.0,
        min_edge=0.04,
        note="the validation large-edge pattern (+10% at edge >= 4%) that lost on the test "
        "seasons; nothing decided when registered",
    ),
)


@dataclass
class RuleStatus:
    model: str
    registered: str
    rule: str
    decided: int
    needed: int
    value: float | None
    """The metric on the judged records (on all so far while waiting)."""
    state: Literal["waiting", "passed", "failed"]


def describe(rule: Rule) -> str:
    if rule.metric == "log_loss_z":
        return (
            f"{rule.horizon_hours} h log loss vs market, paired z <= {rule.threshold:g} "
            f"on the first {rule.min_decided} decided games"
        )
    return (
        f"{rule.horizon_hours} h bets at edge >= {rule.min_edge:.0%}, ROI z >= "
        f"{rule.threshold:g} on the first {rule.min_decided} decided bets"
    )


def _ll(p: float, y: int) -> float:
    p = min(max(p, 1e-9), 1 - 1e-9)
    return -math.log(p if y else 1 - p)


def _z(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    sd = statistics.stdev(values)
    return statistics.fmean(values) / (sd / math.sqrt(len(values))) if sd > 0 else None


def _log_loss_diffs(rows: Sequence[ScoredRow]) -> list[float]:
    out = []
    for r in rows:
        if r.cover_margin == 0:
            continue
        y = int(r.cover_margin > 0)
        out.append(_ll(r.model_home_cover, y) - _ll(r.market_home_cover, y))
    return out


def _returns(rows: Sequence[ScoredRow], min_edge: float) -> list[float]:
    out = []
    for r in rows:
        if r.side_odds is None or abs(r.edge) < min_edge or r.won is None:
            continue
        out.append(bm.american_to_decimal(r.side_odds) - 1.0 if r.won else -1.0)
    return out


def evaluate(rule: Rule, rows: Sequence[ScoredRow]) -> RuleStatus:
    """``rows``: the model's scored records, oldest kickoff first."""
    mine = [r for r in rows if r.model == rule.model and r.horizon_hours == rule.horizon_hours]
    mine.sort(key=lambda r: r.commence_time)
    values = _log_loss_diffs(mine) if rule.metric == "log_loss_z" else _returns(mine, rule.min_edge)
    judged = values[: rule.min_decided]
    z = _z(judged)
    status = RuleStatus(
        rule.model, rule.registered, describe(rule), len(values), rule.min_decided, z, "waiting"
    )
    if len(values) < rule.min_decided or z is None:
        return status
    passed = z <= rule.threshold if rule.metric == "log_loss_z" else z >= rule.threshold
    status.state = "passed" if passed else "failed"
    return status


def rule_statuses(session: Session, rules: Sequence[Rule] = RULES) -> list[RuleStatus]:
    rows = scored_rows(session)
    return [evaluate(rule, rows) for rule in rules]
