"""Build the models under forward test (heavy: loads each sport's history).

- NFL: the validated market-anchored EPA + QB model (services/nfl_spread_predictor).
- ESPN-history sports: frozen artifacts (research/frozen.py) stored on their
  registry rows, in two variants each: the standalone feature model ("key") and
  the market-anchored one ("anchored").
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import Game, ModelVersion
from ttk.domain import GameStatus, Sport
from ttk.research.cfb_model import CFB
from ttk.research.espn_models import SportConfig, SportReport, load_sport
from ttk.research.frozen import FrozenSpreadModel, freeze, verify
from ttk.research.nba_model import NBA
from ttk.research.ncaab_model import NCAAB
from ttk.services.forward_test import ForwardModel, ModelView, register
from ttk.services.nfl_spread_predictor import NflSpreadPredictor

CONFIGS: dict[Sport, SportConfig] = {Sport.CFB: CFB, Sport.NBA: NBA, Sport.NCAAB: NCAAB}
VARIANTS = ("key", "anchored")
FROZEN_VERSION = "1"


def model_name(sport: Sport, feature_set: str, variant: str) -> str:
    return f"{str(sport).lower()}-spread-{variant}-{feature_set}"


def freeze_and_register(
    session: Session, sport: Sport, feature_set: str
) -> tuple[list[ModelVersion], int, SportReport]:
    """Fit (TRAIN only), verify against the backtest, and register both variants.
    Returns the rows, the number of validation games verified, and the report."""
    data = load_sport(session, CONFIGS[sport])
    artifact, report = freeze(data, feature_set)
    checked = verify(FrozenSpreadModel(artifact, data), report)
    scores = {c.name: c for c in report.validate_features}
    splits = CONFIGS[sport].splits
    rows = []
    for variant in VARIANTS:
        label = (
            f"{feature_set}_key_numbers" if variant == "key" else f"market_anchored_{feature_set}"
        )
        candidate = scores[label]
        rows.append(
            register(
                session,
                name=model_name(sport, feature_set, variant),
                version=FROZEN_VERSION,
                sport=sport,
                algorithm=(
                    "linear margin + key numbers"
                    if variant == "key"
                    else "logistic(market no-vig, model disagreement)"
                ),
                features=list(artifact["margin"]["names"]),
                artifact={**artifact, "variant": variant},
                training_window=f"{splits.train[0]}-{splits.train[1]}",
                validation_window=f"{splits.validate[0]}-{splits.validate[1]}",
                log_loss=candidate.spread.log_loss,
                brier=candidate.spread.brier,
            )
        )
    return rows, checked, report


def football_season(now: datetime) -> int:
    """Football season year (the year it starts): before March, last year's."""
    return now.year if now.month >= 3 else now.year - 1


def refresh_inputs(
    session_factory: sessionmaker[Session], *, now: datetime, cfbd_api_key: str | None
) -> list[str]:
    """Bring forward-tested models' slow inputs up to date (the collector keeps
    schedules, results, odds and injuries current on its own):

    - NFL: nflverse games/starters and play-by-play efficiency for this season
      (two downloads, ~15 MB);
    - college football: CollegeFootballData per-game efficiency (1 API call).
    Returns one line per step for the log."""
    from ttk.providers.cfbd import CfbdClient
    from ttk.providers.nflverse import NflverseProvider
    from ttk.providers.nflverse_pbp import NflversePbpProvider
    from ttk.services.cfbd_import import import_cfbd_games
    from ttk.services.history_import import run_nfl_history_import
    from ttk.services.pbp_import import run_pbp_import

    season = football_season(now)
    out = []
    run = run_nfl_history_import(
        session_factory, NflverseProvider(), first_season=season, last_season=season
    )
    out.append(f"NFL {season} games: {run.status} {run.records_written}")
    for run in run_pbp_import(session_factory, NflversePbpProvider(), [season]):
        out.append(f"NFL {season} play-by-play: {run.status} {run.records_written} team-games")
    if cfbd_api_key:
        for run in import_cfbd_games(session_factory, CfbdClient(cfbd_api_key), (season, season)):
            out.append(f"CFB {season} efficiency: {run.status} {run.records_written} team-games")
    return out


def _latest_final(session: Session, sport: Sport) -> Any:
    return session.scalar(
        select(func.max(Game.commence_time)).where(
            Game.sport == sport, Game.status == GameStatus.FINAL
        )
    )


def build_forward_models(session: Session, sports: set[Sport] | None = None) -> list[ForwardModel]:
    models: list[ForwardModel] = []
    if sports is None or Sport.NFL in sports:
        nfl = NflSpreadPredictor.build(session)
        if nfl is not None:
            row = nfl.ensure_registered(session)

            def nfl_view(game_id: int, home_line: float, market: float) -> ModelView | None:
                assert nfl is not None
                v = nfl.spread(game_id, home_line, market)
                if v is None:
                    return None
                f = v.features
                return ModelView(
                    v.home_cover,
                    v.push,
                    v.expected_margin,
                    {
                        "elo_diff": v.elo_diff,
                        "epa_net_diff_pts": f.epa_net_diff_pts,
                        "qb_change_diff_pts": f.qb_change_diff_pts,
                    },
                )

            models.append(ForwardModel(row, Sport.NFL, nfl_view, _latest_final(session, Sport.NFL)))
    frozen = session.scalars(
        select(ModelVersion).where(ModelVersion.artifact.is_not(None)).order_by(ModelVersion.id)
    ).all()
    by_sport: dict[Sport, list[ModelVersion]] = {}
    for row in frozen:
        sport = Sport(row.sport)
        if sports is None or sport in sports:
            by_sport.setdefault(sport, []).append(row)
    for sport, rows in by_sport.items():
        data = load_sport(session, CONFIGS[sport])
        as_of = _latest_final(session, sport)
        for row in rows:
            assert row.artifact is not None
            frozen_model = FrozenSpreadModel(row.artifact, data)
            anchored = row.artifact.get("variant") == "anchored"

            def view(
                game_id: int,
                home_line: float,
                market: float,
                m: FrozenSpreadModel = frozen_model,
                use_anchor: bool = anchored,
            ) -> ModelView | None:
                v = m.view(game_id, home_line, market)
                if v is None:
                    return None
                p = v.home_cover_anchored if use_anchor else v.home_cover_key
                return ModelView(p, v.push, v.expected_margin, v.features)

            models.append(ForwardModel(row, sport, view, as_of))
    return models
