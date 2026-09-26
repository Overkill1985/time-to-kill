"""Probability scoring and calibration. Every metric is reported with its sample size."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

EPS = 1e-12


@dataclass(frozen=True)
class Score:
    n: int
    brier: float
    log_loss: float


def score(probabilities: Sequence[float], outcomes: Sequence[int]) -> Score:
    if len(probabilities) != len(outcomes):
        raise ValueError("probabilities and outcomes differ in length")
    n = len(probabilities)
    if n == 0:
        return Score(0, math.nan, math.nan)
    brier = sum((p - y) ** 2 for p, y in zip(probabilities, outcomes, strict=True)) / n
    ll = (
        -sum(
            y * math.log(max(p, EPS)) + (1 - y) * math.log(max(1 - p, EPS))
            for p, y in zip(probabilities, outcomes, strict=True)
        )
        / n
    )
    return Score(n, brier, ll)


@dataclass(frozen=True)
class CalibrationBin:
    low: float
    high: float
    n: int
    mean_predicted: float
    observed_rate: float


def calibration_table(
    probabilities: Sequence[float], outcomes: Sequence[int], *, bins: int = 10
) -> list[CalibrationBin]:
    """Equal-width bins over [0, 1]; empty bins are omitted."""
    rows: list[CalibrationBin] = []
    for i in range(bins):
        low, high = i / bins, (i + 1) / bins
        members = [
            (p, y)
            for p, y in zip(probabilities, outcomes, strict=True)
            if low <= p < high or (i == bins - 1 and p == 1.0)
        ]
        if members:
            rows.append(
                CalibrationBin(
                    low,
                    high,
                    len(members),
                    sum(p for p, _ in members) / len(members),
                    sum(y for _, y in members) / len(members),
                )
            )
    return rows
