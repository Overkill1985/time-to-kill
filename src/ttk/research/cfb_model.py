"""College football (FBS and FCS) spread research configuration; the pipeline is
research/espn_models.py.

Data: ``ttk import-espn-history --sport CFB`` (ESPN season year = the year a season
starts). Lines from 2013; the books' own openers from the 2024 bowls on.

Protocol (docs/MODEL-GOVERNANCE.md): burn-in 2013-2014, TRAIN 2015-2022,
VALIDATE 2023-2024, TEST 2025 sealed. 2026 is the live season.

Elo may regress each offseason toward the team's own recent level ('recent')
rather than one global mean: FCS teams are a weaker tier, and a global mean would
inflate them every year. Tuning chooses, on TRAIN only.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ttk.domain import Sport
from ttk.research.cfb_preseason import PRESEASON_FEATURES
from ttk.research.espn_models import SportConfig, SportData, load_sport
from ttk.research.nfl_elo import Splits

CFB_SPLITS = Splits(
    burn_in=(2013, 2014), train=(2015, 2022), validate=(2023, 2024), test=(2025, 2025)
)
CFB = SportConfig(
    sport=Sport.CFB,
    splits=CFB_SPLITS,
    home_field_grid=(40.0, 55.0, 70.0, 85.0, 100.0),
    # Widened 2026-09-27: the first TRAIN optimum sat on both edges (K=40 of 15-40;
    # 10 Elo per point of 10-35). College home margins are large (FCS teams travel).
    k_grid=(20.0, 30.0, 40.0, 50.0, 60.0, 80.0),
    home_field_per_point_grid=(4.0, 6.0, 8.0, 10.0, 12.0, 15.0),
    feature_sets={
        "rest": ("elo_diff", "rest_diff"),  # byes and short weeks
        # CollegeFootballData facts known before kickoff (research/cfb_preseason.py)
        "preseason": ("elo_diff", "rest_diff", *PRESEASON_FEATURES),
        # Team efficiency (CFBD PPA) from earlier games: raw, and opponent-adjusted
        "epa_raw": ("elo_diff", "rest_diff", "epa_raw_diff"),
        "epa": ("elo_diff", "rest_diff", "epa_diff"),
        "inseason": ("elo_diff", "rest_diff", *PRESEASON_FEATURES, "epa_diff"),
    },
    team_epa=True,
    rest_cap=14,
    regression_targets=("mean", "recent"),
)


def load_cfb(session: Session) -> SportData:
    return load_sport(session, CFB)
