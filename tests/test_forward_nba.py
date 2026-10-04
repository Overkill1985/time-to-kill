from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import InjuryReport, ModelVersion, Team
from ttk.domain import Sport
from ttk.models.elo import EloGame
from ttk.research.espn_models import SportData
from ttk.research.nba_injuries import STATUS_PRIORS, SitRates
from ttk.research.nba_model import NBA
from ttk.services.forward_models import INJURY_SUBSTITUTION, InjuryState, _frozen_model

TIP = datetime(2026, 10, 21, 23, 30, tzinfo=UTC)


def artifact() -> dict[str, object]:
    return {
        "artifact_version": 1,
        "sport": "NBA",
        "feature_set": "lineup_tip",
        "variant": "key",
        "live_substitution": INJURY_SUBSTITUTION,
        "elo": {
            "k": 10.0,
            "home_field": 0.0,
            "season_regression": 0.5,
            "margin_of_victory": True,
            "initial": 1500.0,
            "regression_target": "mean",
            "home_field_mode": "constant",
            "home_field_per_point": None,
        },
        "margin": {
            "names": ["elo_diff", "missing_diff"],
            "intercept": 0.0,
            "coefs": {"elo_diff": 0.0, "missing_diff": -0.2},
            "sigma": 12.0,
            "key_weights": {},
            "n_train": 100,
        },
        "anchored": {
            "intercept": 0.0,
            "market_coef": 1.0,
            "disagreement_coef": 0.0,
            "n_train": 100,
        },
    }


def test_injury_substitution_uses_the_report_at_the_snapshot(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        home = Team(sport=Sport.NBA, name="Home", espn_id="1")
        away = Team(sport=Sport.NBA, name="Away", espn_id="2")
        session.add_all([home, away])
        session.flush()
        game = EloGame(1, 2027, TIP, home.id, away.id, False, None, None)
        data = SportData(
            NBA,
            [game],
            {},
            {},
            {1: (2.0, 2.0, False, False)},
            rotations={(1, home.id): {"star": (1.0, 20.0)}, (1, away.id): {}},
        )
        row = ModelVersion(
            name="nba-spread-key-injury",
            version="1",
            sport=Sport.NBA,
            market="SPREAD",
            algorithm="test",
            artifact=artifact(),
        )
        session.add(row)
        session.flush()
        state = InjuryState(Sport.NBA, SitRates(), data.rotations)
        model = _frozen_model(row, data, TIP - timedelta(days=1), state)
        assert model.refresh is not None

        model.refresh(session)  # nobody listed yet
        before = model.view(1, -3.0, 0.5, TIP - timedelta(hours=24), 24)
        assert before is not None and before.expected_margin == pytest.approx(0.0)

        session.add(
            InjuryReport(
                sport=Sport.NBA,
                team_id=home.id,
                player_name="Star",
                player_source_identifier="star",
                status="Out",
                provider="espn",
                observed_at=TIP - timedelta(hours=30),
            )
        )
        session.flush()
        model.refresh(session)  # each snapshot pass re-reads the report
        after = model.view(1, -3.0, 0.5, TIP - timedelta(hours=24), 24)
        sat, played = STATUS_PRIORS["Out"]
        q = sat / (sat + played)
        assert after is not None
        assert after.features["expected_missing_home"] == pytest.approx(q * 20.0)
        assert after.features["missing_diff"] == pytest.approx(q * 20.0)
        assert after.expected_margin == pytest.approx(-0.2 * q * 20.0)
        # A report observed after the snapshot time is not used.
        early = model.view(1, -3.0, 0.5, TIP - timedelta(hours=40), 24)
        assert early is not None and early.expected_margin == pytest.approx(0.0)
