"""Forward test of the NFL prop model (research/prop_model): record its chance for
every prop main line in each props pull before kickoff, and score it once the
players' stats are in.

A record per (model version, pull, player, prop): the main line (most books on both
sides), the model's P(over) and mean, the market's median no-vig P(over) (two or more
books quoting both sides) and the best prices. Pulls are taken 24 h and 1 h before
kickoff, so a prop is recorded twice; the 1 h line is the nearest thing to a close,
and the report says whether the line moved toward the model in between.

Scoring: the player's stat once his game is final and its stats are imported. A
player with no stat line is left out (inactive, or played without one), as is a
push. Log loss of the model and of the market, paired; a z is shown once
``MIN_FOR_Z`` props are decided. DEVELOPMENT: never a recommendation.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ttk.db.models import (
    Game,
    ModelVersion,
    NflPlayerGameStat,
    PropPrediction,
    PropPull,
    PropQuote,
    Sportsbook,
    Team,
    utcnow,
)
from ttk.domain import GameStatus, Sport
from ttk.research import prop_model as pm
from ttk.research.moneyline import no_vig
from ttk.services import player_props as pp
from ttk.services.forward_test import register

MODEL_NAME = "nfl-prop-usage"
MODEL_VERSION = "0.1.0"
MIN_BOOKS = 2
MIN_FOR_Z = 100


def freeze_and_register(session: Session) -> tuple[ModelVersion, pm.PropBacktest]:
    result = pm.backtest(pm.load_player_games(session))
    row = register(
        session,
        name=MODEL_NAME,
        version=MODEL_VERSION,
        sport=Sport.NFL,
        algorithm="weighted recent usage; nbinom / gamma",
        features=list(pm.STATS),
        artifact=pm.freeze(result),
        training_window=f"{pm.TRAIN[0]}-{pm.TRAIN[1]}",
        validation_window=f"{pm.VALIDATE[0]}-{pm.VALIDATE[1]}",
        log_loss=None,
        brier=None,
    )
    return row, result


def prop_versions(session: Session) -> list[ModelVersion]:
    return [
        row
        for row in session.scalars(
            select(ModelVersion).where(ModelVersion.artifact.is_not(None)).order_by(ModelVersion.id)
        )
        if (row.artifact or {}).get("kind") == "nfl_prop_usage"
    ]


def _due(session: Session, version: ModelVersion, now: datetime) -> list[tuple[PropPull, Game]]:
    taken = select(PropPrediction.pull_id).where(PropPrediction.model_version_id == version.id)
    return list(
        session.execute(
            select(PropPull, Game)
            .join(Game, Game.id == PropPull.game_id)
            .where(
                Game.sport == Sport.NFL,
                Game.commence_time > now,
                PropPull.id.not_in(taken),
            )
            .order_by(PropPull.pulled_at)
        ).tuples()
    )


def snapshot_pulls(
    session: Session,
    *,
    versions: Sequence[ModelVersion] | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Record every registered prop model on every pull of a game that hasn't started
    and has no records from it yet. Returns log lines."""
    now = now or utcnow()
    lines: list[str] = []
    for version in versions if versions is not None else prop_versions(session):
        due = _due(session, version, now)
        if not due:
            continue
        games = pm.load_player_games(session)
        model = pm.FrozenPropModel(version.artifact or {}, games)
        teams = {t.id: t for t in session.scalars(select(Team).where(Team.sport == Sport.NFL))}
        history = pp._history(session, Sport.NFL, list(teams), now + timedelta(days=1))
        books = dict(session.execute(select(Sportsbook.id, Sportsbook.key)).all())
        for pull, game in due:
            written = _snapshot(session, version, model, history, books, pull, game)
            lines.append(
                f"prop model {version.name} {version.version}: game {game.id} "
                f"{pull.horizon_hours} h: {written} props recorded"
            )
        session.flush()
    return lines


def _snapshot(
    session: Session,
    version: ModelVersion,
    model: pm.FrozenPropModel,
    history: pp._History,
    books: dict[int, str],
    pull: PropPull,
    game: Game,
) -> int:
    quotes = [
        (q, books[q.sportsbook_id])
        for q in session.scalars(select(PropQuote).where(PropQuote.pull_id == pull.id))
    ]
    written = 0
    seen: set[tuple[str, str]] = set()
    for (_, side, market), prop in pp._props(quotes).items():
        if market not in model.dists:
            continue
        line = pp._main_line(prop.points)
        sides = prop.points[line]
        no_vigs = [
            p
            for b in sides.values()
            if len(b) == 2 and (p := no_vig(b["Over"], b["Under"])) is not None
        ]
        if len(no_vigs) < MIN_BOOKS:
            continue
        team_id = game.home_team_id if side == "HOME" else game.away_team_id
        player_id = pp._match(history, team_id, prop.name)
        if player_id is None or (player_id, market) in seen:
            continue
        view = model.view(player_id, market, line, game.commence_time, game.season or 0)
        if view is None:
            continue
        seen.add((player_id, market))
        over, under = pp._best(sides, "Over"), pp._best(sides, "Under")
        session.add(
            PropPrediction(
                model_version_id=version.id,
                game_id=game.id,
                pull_id=pull.id,
                horizon_hours=pull.horizon_hours,
                player=prop.name,
                player_id=player_id,
                market=market,
                line=line,
                p_over=view.p_over,
                mean=view.mean,
                games=view.games,
                market_over=statistics.median(no_vigs),
                books=len(no_vigs),
                best_over=over.american if over else None,
                best_over_book=over.book if over else None,
                best_under=under.american if under else None,
                best_under_book=under.book if under else None,
            )
        )
        written += 1
    return written


@dataclass(frozen=True)
class ScoredProp:
    prediction: PropPrediction
    actual: float
    over: bool


def scored(session: Session, version_id: int) -> list[ScoredProp]:
    rows = session.execute(
        select(PropPrediction, Game)
        .join(Game, Game.id == PropPrediction.game_id)
        .where(PropPrediction.model_version_id == version_id, Game.status == GameStatus.FINAL)
    ).all()
    if not rows:
        return []
    game_ids = {g.id for _, g in rows}
    stats = {
        (r.game_id, r.player_id): r
        for r in session.scalars(
            select(NflPlayerGameStat).where(NflPlayerGameStat.game_id.in_(game_ids))
        )
    }
    out = []
    for p, _ in rows:
        stat = stats.get((p.game_id, p.player_id))
        fn = pm.STATS.get(p.market)
        if stat is None or fn is None:
            continue  # stats not in yet, or no stat line (inactive / unrecorded)
        actual = float(fn(stat))
        if actual == p.line:
            continue
        out.append(ScoredProp(p, actual, actual > p.line))
    return out


def _ll(p: float, over: bool) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return -math.log(p if over else 1 - p)


@dataclass
class PropScore:
    label: str
    decided: int
    model_log_loss: float | None
    market_log_loss: float | None
    paired_diff: float | None
    """Model minus market log loss (negative = the model is better)."""
    z: float | None
    """Shown from MIN_FOR_Z decided props."""


@dataclass
class PropReport:
    model: str
    recorded: int
    decided: int
    scores: list[PropScore] = field(default_factory=list)
    moved_toward: int = 0
    moved_away: int = 0
    """Props recorded at 24 h and 1 h whose line moved toward / away from the model's
    lean at 24 h (a model that knows something sees lines move its way)."""


def _score(label: str, rows: Sequence[ScoredProp]) -> PropScore:
    if not rows:
        return PropScore(label, 0, None, None, None, None)
    d = [_ll(r.prediction.p_over, r.over) - _ll(r.prediction.market_over, r.over) for r in rows]
    mean = statistics.fmean(d)
    z = None
    if len(d) >= MIN_FOR_Z:
        sd = statistics.stdev(d)
        z = mean / (sd / math.sqrt(len(d))) if sd > 0 else None
    return PropScore(
        label,
        len(rows),
        statistics.fmean(_ll(r.prediction.p_over, r.over) for r in rows),
        statistics.fmean(_ll(r.prediction.market_over, r.over) for r in rows),
        mean,
        z,
    )


def prop_report(session: Session, version: ModelVersion) -> PropReport:
    recorded = (
        session.scalar(select(func.count()).where(PropPrediction.model_version_id == version.id))
        or 0
    )
    rows = scored(session, version.id)
    report = PropReport(f"{version.name} {version.version}", int(recorded), len(rows))
    report.scores.append(_score("all", rows))
    for h in (24, 1):
        report.scores.append(_score(f"{h} h", [r for r in rows if r.prediction.horizon_hours == h]))
    for market in sorted({r.prediction.market for r in rows}):
        report.scores.append(_score(market, [r for r in rows if r.prediction.market == market]))
    # Line movement, 24 h -> 1 h, against the model's lean at 24 h.
    by_prop: dict[tuple[int, str, str], dict[int, PropPrediction]] = {}
    for p in session.scalars(
        select(PropPrediction).where(PropPrediction.model_version_id == version.id)
    ):
        by_prop.setdefault((p.game_id, p.player_id, p.market), {})[p.horizon_hours] = p
    for snaps in by_prop.values():
        early, late = snaps.get(24), snaps.get(1)
        if early is None or late is None or late.line == early.line:
            continue
        lean = early.p_over - early.market_over
        if lean == 0:
            continue
        toward = (late.line > early.line) == (lean > 0)
        report.moved_toward += toward
        report.moved_away += not toward
    return report


def snapshot_due(session_factory: sessionmaker[Session]) -> list[str]:
    """For the collector: record prop models on new pulls, in their own transaction."""
    with session_factory() as session:
        lines = snapshot_pulls(session)
        session.commit()
    return lines
