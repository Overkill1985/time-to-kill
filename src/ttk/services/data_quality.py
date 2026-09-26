"""Data-quality and uncertainty classification for one game's prediction.

Rule-based and documented, not fitted: every class comes with the reasons that
produced it, so the card can explain itself. Thresholds are constants here.

Data quality (worst rule wins):
- UNUSABLE: no two-sided market, or the freshest book was last seen more than
  UNUSABLE_ODDS_AGE ago.
- POOR: odds older than the configured maximum age, fewer than MIN_BOOKS books,
  the game is not linked to the schedule authority, or either team has less
  than MIN_TEAM_GAMES games of EPA history.
- ACCEPTABLE: usable, but a starting QB is unknown.
- GOOD: starters known and at least GOOD_BOOKS books.
- EXCELLENT: GOOD plus at least EXCELLENT_BOOKS books and odds at most
  EXCELLENT_ODDS_AGE old.

Uncertainty (a heuristic flag, not a calibrated interval):
- INSUFFICIENT_DATA: no model margin (no rating or feature history).
- HIGH: thin team history, a starter unknown, or a starter the model has never
  seen throw (rated only by its prior).
- MODERATE: the model disagrees with the market line by more than
  MODERATE_DISAGREEMENT points - large disagreements are usually model error.
- LOW otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from ttk.domain import DataQuality, Uncertainty

UNUSABLE_ODDS_AGE = timedelta(hours=6)
EXCELLENT_ODDS_AGE = timedelta(minutes=15)
MIN_BOOKS = 3
GOOD_BOOKS = 5
EXCELLENT_BOOKS = 10
MIN_TEAM_GAMES = 2
MODERATE_DISAGREEMENT = 4.0


@dataclass(frozen=True)
class QualityInputs:
    odds_age: timedelta | None
    """Time since the least recently seen book used in the consensus."""
    books: int
    schedule_linked: bool
    home_team_games: int
    away_team_games: int
    home_starter_known: bool
    away_starter_known: bool
    home_starter_seen: bool
    """The starter has dropback history (not rated purely by the prior)."""
    away_starter_seen: bool
    disagreement_points: float | None
    """Model expected margin minus the market line, from the home side."""


@dataclass(frozen=True)
class Assessment:
    data_quality: DataQuality
    uncertainty: Uncertainty
    reasons: tuple[str, ...]


def assess(q: QualityInputs, *, max_odds_age: timedelta) -> Assessment:
    reasons: list[str] = []
    min_games = min(q.home_team_games, q.away_team_games)
    starters_known = q.home_starter_known and q.away_starter_known

    if q.odds_age is None or q.books == 0:
        quality = DataQuality.UNUSABLE
        reasons.append("no two-sided market")
    elif q.odds_age > UNUSABLE_ODDS_AGE:
        quality = DataQuality.UNUSABLE
        reasons.append(f"odds {q.odds_age.total_seconds() / 3600:.1f} h old")
    else:
        poor = []
        if q.odds_age > max_odds_age:
            poor.append(f"odds {q.odds_age.total_seconds() / 60:.0f} min old")
        if q.books < MIN_BOOKS:
            poor.append(f"only {q.books} books")
        if not q.schedule_linked:
            poor.append("not linked to the ESPN schedule")
        if min_games < MIN_TEAM_GAMES:
            poor.append(f"a team has {min_games} games of EPA history")
        if poor:
            quality = DataQuality.POOR
            reasons += poor
        elif not starters_known:
            quality = DataQuality.ACCEPTABLE
            reasons.append("a starting QB is not yet known")
        elif q.books >= EXCELLENT_BOOKS and q.odds_age <= EXCELLENT_ODDS_AGE:
            quality = DataQuality.EXCELLENT
        elif q.books >= GOOD_BOOKS:
            quality = DataQuality.GOOD
            reasons.append(
                f"{q.books} books, odds {q.odds_age.total_seconds() / 60:.0f} min old "
                f"(EXCELLENT needs {EXCELLENT_BOOKS}+ books and "
                f"<= {EXCELLENT_ODDS_AGE.total_seconds() / 60:.0f} min)"
            )
        else:
            quality = DataQuality.ACCEPTABLE
            reasons.append(f"{q.books} books")

    if q.disagreement_points is None:
        uncertainty = Uncertainty.INSUFFICIENT_DATA
        reasons.append("no model margin")
    elif (
        min_games < MIN_TEAM_GAMES
        or not starters_known
        or not (q.home_starter_seen and q.away_starter_seen)
    ):
        uncertainty = Uncertainty.HIGH
        if starters_known and not (q.home_starter_seen and q.away_starter_seen):
            reasons.append("a starter has no dropback history (prior only)")
    elif abs(q.disagreement_points) > MODERATE_DISAGREEMENT:
        uncertainty = Uncertainty.MODERATE
        reasons.append(f"model and market differ by {q.disagreement_points:+.1f} pts")
    else:
        uncertainty = Uncertainty.LOW
    return Assessment(quality, uncertainty, tuple(reasons))
