"""Forward tests: snapshot frozen models before kickoff, score them after.

Every model here is DEVELOPMENT: forward predictions are recorded and scored,
never qualified as bets. A snapshot is taken once per (model, game, horizon),
when a game is within ``horizon`` hours of kickoff (and, for the longer horizon,
not yet inside the shorter one). It records the main spread, the all-book
consensus no-vig probability and the best bettable prices *as we saw them*, so
the score is what a bettor at that moment could have done.

Scoring (``forward_report``) uses finished games only: log loss of the model and
of the market at the snapshot (paired), closing-line value against our own
closing prices at the same number, and results at the snapshot's best price.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ttk import betting_math as bm
from ttk.db.models import ForwardPrediction, Game, ModelVersion, utcnow
from ttk.domain import GameStatus, Market, Selection, Sport
from ttk.services.line_history import closing_line_value
from ttk.services.market import SideMarket, main_lines, side_markets

HORIZONS_HOURS = (24, 1)
MAX_ODDS_AGE = timedelta(hours=2)
EDGE_THRESHOLDS = (0.0, 0.02, 0.04, 0.06)


@dataclass(frozen=True)
class ModelView:
    home_cover: float
    push: float
    expected_margin: float | None
    features: dict[str, float]


@dataclass(frozen=True)
class ForwardModel:
    """One model under forward test: its registry row and how it prices a game."""

    version: ModelVersion
    sport: Sport
    view: Callable[[int, float, float], ModelView | None]
    """(game_id, home line, market no-vig P(home covers)) -> the model's view."""
    inputs_as_of: datetime
    """Newest finished game behind the model's state."""


def register(
    session: Session,
    *,
    name: str,
    version: str,
    sport: Sport,
    algorithm: str,
    features: list[str],
    artifact: dict[str, Any] | None,
    training_window: str,
    validation_window: str,
    log_loss: float | None,
    brier: float | None,
) -> ModelVersion:
    """The registry row for a forward-tested model, created once (DEVELOPMENT)."""
    row = session.scalar(
        select(ModelVersion).where(ModelVersion.name == name, ModelVersion.version == version)
    )
    if row is not None:
        if artifact is not None and row.artifact != artifact:
            raise ValueError(f"{name} {version} is frozen with a different artifact; bump it")
        return row
    row = ModelVersion(
        name=name,
        version=version,
        sport=sport,
        market="SPREAD",
        algorithm=algorithm,
        features=features,
        training_window=training_window,
        validation_window=validation_window,
        calibration_method="fitted on TRAIN",
        brier_score=brier,
        log_loss=log_loss,
        status="DEVELOPMENT",
        artifact=artifact,
    )
    session.add(row)
    session.flush()
    return row


@dataclass
class SnapshotStats:
    written: int = 0
    skipped: Counter[str] = field(default_factory=Counter)


def _main_spread(session: Session, game_id: int) -> tuple[SideMarket, SideMarket] | None:
    markets = side_markets(session, game_id)
    home = next(
        (
            m
            for m in main_lines(markets)
            if m.market is Market.SPREAD and m.selection is Selection.HOME
        ),
        None,
    )
    if home is None or home.line is None:
        return None
    away = next(
        (
            m
            for m in markets
            if m.market is Market.SPREAD and m.selection is Selection.AWAY and m.line == -home.line
        ),
        None,
    )
    return (home, away) if away is not None else None


def _best(sm: SideMarket, bettable: frozenset[str] | None) -> tuple[float | None, str | None]:
    prices = [b for b in sm.consensus.books if bettable is None or b.sportsbook in bettable]
    if not prices:
        return None, None
    best = max(prices, key=lambda b: b.decimal_odds)
    return bm.decimal_to_american(best.decimal_odds), best.sportsbook


def due_horizon(
    kickoff: datetime, now: datetime, horizons: Sequence[int] = HORIZONS_HOURS
) -> int | None:
    """The horizon a game is inside now: the smallest h with kickoff - now <= h."""
    lead = kickoff - now
    if lead <= timedelta(0):
        return None
    inside = [h for h in horizons if lead <= timedelta(hours=h)]
    return min(inside) if inside else None


def snapshot(
    session: Session,
    models: Sequence[ForwardModel],
    *,
    now: datetime | None = None,
    horizons: Sequence[int] = HORIZONS_HOURS,
    bettable_books: frozenset[str] | None = None,
) -> SnapshotStats:
    now = now or utcnow()
    stats = SnapshotStats()
    longest = max(horizons)
    for model in models:
        games = session.scalars(
            select(Game).where(
                Game.sport == model.sport,
                Game.commence_time > now,
                Game.commence_time <= now + timedelta(hours=longest),
            )
        ).all()
        for game in games:
            h = due_horizon(game.commence_time, now, horizons)
            if h is None:
                continue
            exists = session.scalar(
                select(ForwardPrediction.id).where(
                    ForwardPrediction.model_version_id == model.version.id,
                    ForwardPrediction.game_id == game.id,
                    ForwardPrediction.horizon_hours == h,
                )
            )
            if exists is not None:
                continue
            spread = _main_spread(session, game.id)
            if spread is None:
                stats.skipped["no two-sided spread"] += 1
                continue
            home, away = spread
            if now - min(home.oldest_observation, away.oldest_observation) > MAX_ODDS_AGE:
                stats.skipped["odds older than 2 hours"] += 1
                continue
            assert home.line is not None
            market = home.consensus.consensus_no_vig_probability
            view = model.view(game.id, home.line, market)
            if view is None:
                stats.skipped["no model inputs"] += 1
                continue
            home_odds, home_book = _best(home, bettable_books)
            away_odds, away_book = _best(away, bettable_books)
            session.add(
                ForwardPrediction(
                    model_version_id=model.version.id,
                    game_id=game.id,
                    horizon_hours=h,
                    snapshot_at=now,
                    home_line=home.line,
                    home_cover_probability=view.home_cover,
                    push_probability=view.push,
                    market_home_cover=market,
                    books=home.consensus.books_reporting,
                    best_home_odds=home_odds,
                    best_home_book=home_book,
                    best_away_odds=away_odds,
                    best_away_book=away_book,
                    expected_margin=view.expected_margin,
                    features=view.features,
                    # Everything used (odds, results, injuries) was observed by now.
                    inputs_as_of=now,
                )
            )
            stats.written += 1
    session.flush()
    return stats


# --------------------------------------------------------------------------- scoring


@dataclass
class Bets:
    min_edge: float
    bets: int = 0
    wins: int = 0
    losses: int = 0
    pushes: int = 0
    units: float = 0.0

    @property
    def roi(self) -> float | None:
        return self.units / self.bets if self.bets else None


@dataclass
class HorizonScore:
    model: str
    horizon_hours: int
    games: int = 0
    decided: int = 0
    model_log_loss: float | None = None
    market_log_loss: float | None = None
    paired_diff: float | None = None
    paired_se: float | None = None
    price_clv: list[float] = field(default_factory=list)
    points_vs_close: list[float] = field(default_factory=list)
    bets: list[Bets] = field(default_factory=list)

    @property
    def z(self) -> float | None:
        if self.paired_diff is None or not self.paired_se:
            return None
        return self.paired_diff / self.paired_se


def _ll(p: float, y: int) -> float:
    p = min(max(p, 1e-9), 1 - 1e-9)
    return -math.log(p if y else 1 - p)


def forward_report(session: Session, sport: Sport | None = None) -> list[HorizonScore]:
    query = (
        select(ForwardPrediction, Game, ModelVersion)
        .join(Game, Game.id == ForwardPrediction.game_id)
        .join(ModelVersion, ModelVersion.id == ForwardPrediction.model_version_id)
        .where(Game.status == GameStatus.FINAL)
        .order_by(ModelVersion.name, ForwardPrediction.horizon_hours, Game.commence_time)
    )
    if sport is not None:
        query = query.where(Game.sport == sport)
    groups: dict[tuple[str, int], list[tuple[ForwardPrediction, Game]]] = defaultdict(list)
    for fp, game, mv in session.execute(query).all():
        groups[(f"{mv.name} {mv.version}", fp.horizon_hours)].append((fp, game))
    out = []
    for (name, h), rows in groups.items():
        score = HorizonScore(name, h, games=len(rows), bets=[Bets(e) for e in EDGE_THRESHOLDS])
        diffs, model_ll, market_ll = [], [], []
        for fp, game in rows:
            assert game.home_score is not None and game.away_score is not None
            cover_margin = game.home_score - game.away_score + fp.home_line
            edge = fp.home_cover_probability - fp.market_home_cover
            side = Selection.HOME if edge >= 0 else Selection.AWAY
            odds = fp.best_home_odds if side is Selection.HOME else fp.best_away_odds
            if cover_margin != 0:
                y = int(cover_margin > 0)
                a, b = _ll(fp.home_cover_probability, y), _ll(fp.market_home_cover, y)
                model_ll.append(a)
                market_ll.append(b)
                diffs.append(a - b)
            if odds is not None:
                line = fp.home_line if side is Selection.HOME else -fp.home_line
                clv = closing_line_value(session, game.id, Market.SPREAD, side, line, odds)
                if clv.price_clv is not None:
                    score.price_clv.append(clv.price_clv)
                if clv.points_gained is not None:
                    score.points_vs_close.append(clv.points_gained)
                won = (cover_margin > 0) == (side is Selection.HOME)
                for bet in score.bets:
                    if abs(edge) < bet.min_edge:
                        continue
                    bet.bets += 1
                    if cover_margin == 0:
                        bet.pushes += 1
                    elif won:
                        bet.wins += 1
                        bet.units += bm.american_to_decimal(odds) - 1.0
                    else:
                        bet.losses += 1
                        bet.units -= 1.0
        score.decided = len(diffs)
        if diffs:
            n = len(diffs)
            mean = sum(diffs) / n
            score.model_log_loss = sum(model_ll) / n
            score.market_log_loss = sum(market_ll) / n
            score.paired_diff = mean
            if n > 1:
                var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
                score.paired_se = math.sqrt(var / n)
        out.append(score)
    return out


def forward_counts(session: Session) -> list[tuple[str, int, int]]:
    """(model, snapshots, of them on finished games)."""
    rows = session.execute(
        select(
            ModelVersion.name + " " + ModelVersion.version,
            func.count(ForwardPrediction.id),
            func.sum(func.iif(Game.status == GameStatus.FINAL, 1, 0)),
        )
        .join(ForwardPrediction, ForwardPrediction.model_version_id == ModelVersion.id)
        .join(Game, Game.id == ForwardPrediction.game_id)
        .group_by(ModelVersion.id)
    ).all()
    return [(str(a), int(b), int(c or 0)) for a, b, c in rows]
