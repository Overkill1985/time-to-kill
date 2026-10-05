"""Build the models under forward test (heavy: loads each sport's history).

- NFL: the validated market-anchored EPA + QB model (services/nfl_spread_predictor).
- ESPN-history sports: frozen artifacts (research/frozen.py) stored on their
  registry rows, in two variants each: the standalone feature model ("key") and
  the market-anchored one ("anchored").
- NBA "injury" models are the validated at-tip lineup model with one *live
  substitution*: who sits at tip (``missing_diff``, known only at tip-off) is
  replaced by the injury report's expected missing value at the snapshot,
  re-read from ``injury_reports`` on every snapshot pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import Game, InjuryReport, ModelVersion
from ttk.domain import GameStatus, Sport
from ttk.research.cfb_model import CFB
from ttk.research.espn_models import SportConfig, SportData, SportReport, load_sport
from ttk.research.frozen import FrozenSpreadModel, freeze, verify
from ttk.research.nba_injuries import InjuryTimeline, Report, SitRates, expected_missing
from ttk.research.nba_model import NBA
from ttk.research.ncaab_model import NCAAB
from ttk.services.daily_card import CardView
from ttk.services.forward_test import ForwardModel, ModelView, register
from ttk.services.frozen_simulator import FrozenSimulator
from ttk.services.nfl_spread_predictor import NflSpreadPredictor

CONFIGS: dict[Sport, SportConfig] = {Sport.CFB: CFB, Sport.NBA: NBA, Sport.NCAAB: NCAAB}
VARIANTS = ("key", "anchored")
FROZEN_VERSION = "1"
INJURY_SUBSTITUTION = {"missing_diff": "injury_report_expected_missing"}


def model_name(sport: Sport, label: str, variant: str) -> str:
    return f"{str(sport).lower()}-spread-{variant}-{label}"


def freeze_and_register(
    session: Session,
    sport: Sport,
    feature_set: str,
    *,
    label: str | None = None,
    live_substitution: dict[str, str] | None = None,
) -> tuple[list[ModelVersion], int, SportReport]:
    """Fit (TRAIN only), verify against the backtest, and register both variants
    as ``<sport>-spread-<variant>-<label>`` (label defaults to the feature set).
    Returns the rows, the number of validation games verified, and the report."""
    data = load_sport(session, CONFIGS[sport])
    artifact, report = freeze(data, feature_set)
    checked = verify(FrozenSpreadModel(artifact, data), report)
    if live_substitution:
        artifact["live_substitution"] = live_substitution
    scores = {c.name: c for c in report.validate_features}
    splits = CONFIGS[sport].splits
    rows = []
    for variant in VARIANTS:
        candidate = scores[
            f"{feature_set}_key_numbers" if variant == "key" else f"market_anchored_{feature_set}"
        ]
        rows.append(
            register(
                session,
                name=model_name(sport, label or feature_set, variant),
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
    schedules, results, odds, injuries and NBA box scores current on its own):

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


@dataclass
class InjuryState:
    """The injury report as of the latest snapshot pass, for one sport."""

    sport: Sport
    rates: SitRates
    rotations: dict[tuple[int, int], dict[str, tuple[float, float]]]
    timeline: InjuryTimeline = field(default_factory=InjuryTimeline)

    def refresh(self, session: Session) -> None:
        self.timeline = InjuryTimeline.build(
            [
                Report(r.player_source_identifier, r.status, r.cleared, r.observed_at, r.team_id)
                for r in session.scalars(
                    select(InjuryReport).where(
                        InjuryReport.sport == self.sport,
                        InjuryReport.provider == "espn",
                        InjuryReport.player_source_identifier.is_not(None),
                    )
                )
                if r.player_source_identifier
            ]
        )

    def missing(
        self, game: tuple[int, int, int], at: datetime, horizon: int
    ) -> tuple[float, float]:
        """(home, away) expected missing value; ``game`` = (id, home id, away id)."""
        game_id, home, away = game
        return tuple(  # type: ignore[return-value]
            expected_missing(
                self.rotations.get((game_id, team), {}),
                self.timeline,
                self.rates,
                team_id=team,
                at=at,
                horizon=horizon,
            )
            for team in (home, away)
        )


def _nfl_model(session: Session) -> ForwardModel | None:
    nfl = NflSpreadPredictor.build(session)
    if nfl is None:
        return None
    row = nfl.ensure_registered(session)

    def nfl_view(
        game_id: int, home_line: float, market: float, at: datetime, horizon: int
    ) -> ModelView | None:
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

    return ForwardModel(row, Sport.NFL, nfl_view, _latest_final(session, Sport.NFL))


def _frozen_model(
    row: ModelVersion,
    data: SportData,
    as_of: datetime,
    injuries: InjuryState | None,
    model: FrozenSpreadModel | None = None,
) -> ForwardModel:
    assert row.artifact is not None
    model = model or FrozenSpreadModel(row.artifact, data)
    anchored = row.artifact.get("variant") == "anchored"
    substitute = row.artifact.get("live_substitution") == INJURY_SUBSTITUTION
    teams = {g.game_id: (g.game_id, g.home_id, g.away_id) for g in data.games}
    if substitute and injuries is None:
        raise ValueError(f"{row.name} needs the injury report")

    def view(
        game_id: int, home_line: float, market: float, at: datetime, horizon: int
    ) -> ModelView | None:
        overrides: dict[str, float] = {}
        extra: dict[str, float] = {}
        if substitute:
            assert injuries is not None
            game = teams.get(game_id)
            if game is None:
                return None
            home, away = injuries.missing(game, at, horizon)
            overrides["missing_diff"] = home - away
            extra = {"expected_missing_home": home, "expected_missing_away": away}
        v = model.view(game_id, home_line, market, overrides=overrides)
        if v is None:
            return None
        p = v.home_cover_anchored if anchored else v.home_cover_key
        return ModelView(p, v.push, v.expected_margin, {**v.features, **extra})

    return ForwardModel(
        row,
        Sport(row.sport),
        view,
        as_of,
        refresh=injuries.refresh if substitute and injuries is not None else None,
    )


def build_forward_models(session: Session, sports: set[Sport] | None = None) -> list[ForwardModel]:
    models: list[ForwardModel] = []
    if sports is None or Sport.NFL in sports:
        nfl = _nfl_model(session)
        if nfl is not None:
            models.append(nfl)
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
        injuries = None
        if data.sit_rates is not None:
            injuries = InjuryState(sport, data.sit_rates, data.rotations)
            injuries.refresh(session)
        models.extend(_frozen_model(row, data, as_of, injuries) for row in rows)
    return models


CARD_MODEL_NAMES: dict[Sport, str] = {
    # One market-anchored model per sport on the daily card (as for the NFL).
    Sport.CFB: "cfb-spread-anchored-inseason",
    Sport.NBA: "nba-spread-anchored-injury",
    Sport.NCAAB: "ncaab-spread-anchored-eff",
}


@dataclass
class FrozenCardModel:
    """A frozen forward-tested model as a daily-card model (services/daily_card)."""

    model: ForwardModel
    season_games: dict[tuple[int, int], int]
    """(game_id, team_id) -> the team's finished games earlier that season."""
    simulator: FrozenSimulator | None = None
    """Monte Carlo for this sport (services/frozen_simulator)."""

    @property
    def version(self) -> ModelVersion:
        return self.model.version

    def refresh(self, session: Session) -> None:
        if self.model.refresh is not None:
            self.model.refresh(session)

    def view(self, game: Game, home_line: float, market: float, now: datetime) -> CardView | None:
        horizon = 1 if game.commence_time - now <= timedelta(hours=1) else 24
        v = self.model.view(game.id, home_line, market, now, horizon)
        if v is None or v.expected_margin is None:
            return None
        return CardView(
            v.home_cover,
            v.push,
            v.expected_margin,
            v.features,
            self.season_games.get((game.id, game.home_team_id), 0),
            self.season_games.get((game.id, game.away_team_id), 0),
        )


def _season_games(data: SportData) -> dict[tuple[int, int], int]:
    played: dict[tuple[int, int], int] = {}
    out: dict[tuple[int, int], int] = {}
    for g in sorted(data.games, key=lambda g: (g.commence_time, g.game_id)):
        for team in (g.home_id, g.away_id):
            out[(g.game_id, team)] = played.get((g.season, team), 0)
        if g.played:
            for team in (g.home_id, g.away_id):
                played[(g.season, team)] = played.get((g.season, team), 0) + 1
    return out


def build_card_models(
    session: Session, sports: set[Sport] | None = None
) -> dict[Sport, FrozenCardModel]:
    """The non-NFL card models, each with its simulator (heavy: loads each
    sport's history)."""
    out: dict[Sport, FrozenCardModel] = {}
    for sport, name in CARD_MODEL_NAMES.items():
        if sports is not None and sport not in sports:
            continue
        row = session.scalar(
            select(ModelVersion)
            .where(ModelVersion.name == name, ModelVersion.artifact.is_not(None))
            .order_by(ModelVersion.id.desc())
        )
        if row is None:
            continue
        data = load_sport(session, CONFIGS[sport])
        injuries = None
        if data.sit_rates is not None:
            injuries = InjuryState(sport, data.sit_rates, data.rotations)
            injuries.refresh(session)
        frozen = FrozenSpreadModel(row.artifact or {}, data)
        model = _frozen_model(row, data, _latest_final(session, sport), injuries, frozen)
        out[sport] = FrozenCardModel(model, _season_games(data), FrozenSimulator(model, frozen))
    return out
