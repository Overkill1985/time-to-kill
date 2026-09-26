"""Shared vocabulary. Stored in the database as these string values."""

from __future__ import annotations

from enum import StrEnum


class Sport(StrEnum):
    NFL = "NFL"
    NBA = "NBA"
    CFB = "CFB"
    NCAAB = "NCAAB"


class Market(StrEnum):
    MONEYLINE = "MONEYLINE"
    SPREAD = "SPREAD"
    TOTAL = "TOTAL"
    TEAM_TOTAL = "TEAM_TOTAL"


class Selection(StrEnum):
    HOME = "HOME"
    AWAY = "AWAY"
    DRAW = "DRAW"
    OVER = "OVER"
    UNDER = "UNDER"


class DataQuality(StrEnum):
    EXCELLENT = "EXCELLENT"
    GOOD = "GOOD"
    ACCEPTABLE = "ACCEPTABLE"
    POOR = "POOR"
    UNUSABLE = "UNUSABLE"

    @property
    def rank(self) -> int:
        """Higher is better. UNUSABLE = 0."""
        return _QUALITY_RANK[self]

    def meets(self, minimum: DataQuality) -> bool:
        return self.rank >= minimum.rank


_QUALITY_RANK = {
    DataQuality.UNUSABLE: 0,
    DataQuality.POOR: 1,
    DataQuality.ACCEPTABLE: 2,
    DataQuality.GOOD: 3,
    DataQuality.EXCELLENT: 4,
}


class Uncertainty(StrEnum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class ModelStatus(StrEnum):
    """Registry lifecycle (docs/MODEL-GOVERNANCE.md)."""

    DEVELOPMENT = "DEVELOPMENT"
    PAPER = "PAPER"
    ACTIVE = "ACTIVE"
    WATCH = "WATCH"
    RETIRED = "RETIRED"


class ModelHealth(StrEnum):
    """Drift monitoring status, separate from the lifecycle status."""

    HEALTHY = "HEALTHY"
    WATCH = "WATCH"
    DEGRADED = "DEGRADED"
    RETRAIN_REQUIRED = "RETRAIN_REQUIRED"


class BetClassification(StrEnum):
    QUALIFIED = "QUALIFIED"
    LEAN = "LEAN"
    PASS = "PASS"
    NO_BET = "NO_BET"


class BetResult(StrEnum):
    PENDING = "PENDING"
    WIN = "WIN"
    LOSS = "LOSS"
    PUSH = "PUSH"
    VOID = "VOID"


class CorrelationRisk(StrEnum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"
