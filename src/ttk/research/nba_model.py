"""NBA spread research configuration (the pipeline is research/espn_models.py).

Protocol (docs/MODEL-GOVERNANCE.md): ESPN season years (2025-26 = 2026).
Burn-in 2018, TRAIN 2019-2022, VALIDATE 2023-2024, TEST 2025-2026 sealed.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ttk.domain import Sport
from ttk.research.espn_models import SportConfig, SportData, SportReport, load_sport, sport_backtest
from ttk.research.nfl_elo import Splits

NBA_SPLITS = Splits(
    burn_in=(2018, 2018), train=(2019, 2022), validate=(2023, 2024), test=(2025, 2026)
)
REST_CAP = 4
REST_FEATURES = ("elo_diff", "rest_diff", "home_b2b", "away_b2b")
NBA = SportConfig(
    sport=Sport.NBA,
    splits=NBA_SPLITS,
    home_field_grid=(40.0, 60.0, 80.0, 100.0, 120.0),
    k_grid=(5.0, 7.5, 10.0, 15.0, 20.0, 25.0),  # 10 was the TRAIN optimum at the old edge
    feature_sets={
        "rest": REST_FEATURES,
        # Known at tip-off only: scored against the close, never used at the opener.
        "lineup_tip": (*REST_FEATURES, "missing_diff"),
        # Known before the opener (the team's previous game).
        "lineup_prev": (*REST_FEATURES, "missing_prev_diff"),
    },
    rest_cap=REST_CAP,
    at_tip_only=frozenset({"lineup_tip"}),
    injuries=True,
)


def load_nba(session: Session) -> SportData:
    return load_sport(session, NBA)


def nba_backtest(data: SportData, *, include_test: bool = False) -> SportReport:
    return sport_backtest(data, include_test=include_test)
