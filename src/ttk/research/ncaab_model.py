"""Men's college basketball (Division I) spread research configuration; the
pipeline is research/espn_models.py.

Data: ``ttk import-espn-history --sport NCAAB`` (ESPN season year = the year a
season ends, 2025-26 = 2026). Lines from 2013; the books' own openers from 2023-24.

Protocol (docs/MODEL-GOVERNANCE.md): burn-in 2015-2016, TRAIN 2017-2022,
VALIDATE 2023-2024, TEST 2025-2026 sealed.

Rest: days since the last game, capped at 7, and back-to-back flags (conference
tournaments). Elo may regress toward each team's recent level: power conferences
and low-majors are different tiers. Tuning chooses, on TRAIN only.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ttk.domain import Sport
from ttk.research.espn_models import SportConfig, SportData, load_sport
from ttk.research.nfl_elo import Splits

NCAAB_SPLITS = Splits(
    burn_in=(2015, 2016), train=(2017, 2022), validate=(2023, 2024), test=(2025, 2026)
)
NCAAB = SportConfig(
    sport=Sport.NCAAB,
    splits=NCAAB_SPLITS,
    home_field_grid=(40.0, 60.0, 80.0, 100.0, 120.0),
    k_grid=(15.0, 20.0, 30.0, 40.0, 50.0),
    feature_sets={"rest": ("elo_diff", "rest_diff", "home_b2b", "away_b2b")},
    rest_cap=7,
    regression_targets=("mean", "recent"),
)


def load_ncaab(session: Session) -> SportData:
    return load_sport(session, NCAAB)
